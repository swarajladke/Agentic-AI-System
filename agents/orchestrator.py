"""Orchestrator agent for decomposition and DAG dispatch."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from agents.base_agent import BaseAgent
from api.sse import format_sse
from config import Settings
from pipeline.batch_dispatcher import BatchItem
from pipeline.dag_resolver import DagResolver
from pipeline.queue import RedisStreamClient
from utils.logger import get_logger
from utils.retry import async_retry


class OrchestratorAgent(BaseAgent):
    """Plans a user task into step DAG and dispatches steps when ready."""

    def __init__(
        self,
        *,
        queue_client: RedisStreamClient,
        settings: Settings,
        resolver: DagResolver,
    ) -> None:
        super().__init__(
            queue_client=queue_client,
            settings=settings,
            handled_agent="orchestrator",
            consumer_group="group:orchestrator",
            name="orchestrator",
        )
        self.resolver = resolver
        self.logger = get_logger("agents.orchestrator")

    async def process(self, task: dict[str, Any]) -> dict[str, Any]:
        """Support BaseAgent contract; used when orchestrator is invoked as a worker."""
        task_id = task.get("task_id")
        instruction = task.get("instruction", task.get("user_task", ""))
        if not task_id or not instruction:
            raise ValueError("Orchestrator task requires task_id and instruction")
        await self.create_task(task_id=task_id, user_task=instruction)
        return {"task_id": task_id, "status": "planned"}

    async def handle_batch(self, batch: list[BatchItem]) -> None:
        """Dispatch a full batch to planning pipeline."""
        for item in batch:
            await self.create_task(task_id=item.task_id, user_task=item.user_task)

    async def create_task(self, *, task_id: str, user_task: str) -> None:
        """Plan and dispatch initial steps for a user task."""
        user_stream = self.settings.user_stream_name(task_id)
        await self.queue.publish_user_event(
            stream=user_stream,
            sse_payload=format_sse(
                {
                    "type": "status",
                    "task_id": task_id,
                    "step": "orchestrator",
                    "message": "Planning task with orchestrator.",
                }
            ),
        )

        try:
            steps = await self.decompose_task(user_task)
            await self.resolver.register_task(task_id=task_id, user_task=user_task, steps=steps)
            ready_steps = await self.resolver.get_ready_steps(task_id)
            for step in ready_steps:
                await self.dispatch_step(task_id, step)
            await self.queue.publish_user_event(
                stream=user_stream,
                sse_payload=format_sse(
                    {
                        "type": "status",
                        "task_id": task_id,
                        "step": "orchestrator",
                        "message": f"Plan ready with {len(steps)} steps.",
                    }
                ),
            )
        except Exception as exc:
            await self.queue.publish_user_event(
                stream=user_stream,
                sse_payload=format_sse(
                    {
                        "type": "task_failed",
                        "task_id": task_id,
                        "step": "orchestrator",
                        "status": "failed",
                        "reason": str(exc),
                    }
                ),
                terminal=True,
            )
            raise

    async def dispatch_step(self, task_id: str, step: dict[str, Any]) -> None:
        """Publish a step onto stream:tasks once dependencies are satisfied."""
        step_id = str(step["id"])
        dependency_context = await self.resolver.get_dependency_context(task_id=task_id, step_id=step_id)
        user_task = await self.resolver.get_user_task(task_id)

        await self.queue.add(
            self.settings.task_stream,
            {
                "task_id": task_id,
                "step_id": step_id,
                "agent": step["agent"],
                "instruction": step["instruction"],
                "depends_on": step.get("depends_on", []),
                "context": dependency_context,
                "user_task": user_task,
                "enqueued_at": datetime.now(UTC).isoformat(),
            },
        )
        await self.resolver.mark_dispatched(task_id=task_id, step_id=step_id)

        await self.queue.publish_user_event(
            stream=self.settings.user_stream_name(task_id),
            sse_payload=format_sse(
                {
                    "type": "step_dispatched",
                    "task_id": task_id,
                    "step_id": step_id,
                    "agent": step["agent"],
                }
            ),
        )

    async def decompose_task(self, user_task: str) -> list[dict[str, Any]]:
        """Generate execution plan from user query."""
        if not self.settings.anthropic_api_key:
            return self._fallback_steps(user_task)

        prompt = (
            "Decompose the user task into minimal executable JSON steps.\n"
            "Output strict JSON only using this schema:\n"
            '{"steps":[{"id":"1","agent":"retriever","instruction":"...","depends_on":[]}]}\n'
            "Allowed agents: retriever, analyzer, writer.\n"
            "Use dependency links. Ensure writer is terminal.\n\n"
            f"User task: {user_task}"
        )
        text = await self._call_claude(prompt)
        plan = self._extract_plan(text)
        return plan or self._fallback_steps(user_task)

    @async_retry(max_retries=3, base_delay_seconds=1.0)
    async def _call_claude(self, prompt: str) -> str:
        """Call Claude API for planning."""
        headers = {
            "x-api-key": self.settings.anthropic_api_key or "",
            "anthropic-version": self.settings.anthropic_version,
            "content-type": "application/json",
        }
        body = {
            "model": self.settings.anthropic_model,
            "max_tokens": 900,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=self.settings.api_timeout_seconds) as client:
            response = await client.post(self.settings.anthropic_api_url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
            content = payload.get("content", [])
            text_blocks = [part.get("text", "") for part in content if isinstance(part, dict)]
            return "\n".join(text_blocks).strip()

    def _extract_plan(self, raw_text: str) -> list[dict[str, Any]]:
        """Extract JSON plan from model output."""
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            obj = json.loads(raw_text[start : end + 1])
        except json.JSONDecodeError:
            return []
        steps = obj.get("steps", [])
        validated: list[dict[str, Any]] = []
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            agent = str(step.get("agent", "")).strip().lower()
            if agent not in {"retriever", "analyzer", "writer"}:
                continue
            validated.append(
                {
                    "id": str(step.get("id", idx)),
                    "agent": agent,
                    "instruction": str(step.get("instruction", "")).strip() or f"Execute step {idx}",
                    "depends_on": [str(dep) for dep in step.get("depends_on", [])],
                }
            )
        return validated

    @staticmethod
    def _fallback_steps(user_task: str) -> list[dict[str, Any]]:
        """Return deterministic fallback decomposition when API is unavailable."""
        return [
            {
                "id": "1",
                "agent": "retriever",
                "instruction": f"Collect high-quality evidence and references for: {user_task}",
                "depends_on": [],
            },
            {
                "id": "2",
                "agent": "analyzer",
                "instruction": "Analyze retrieved evidence and produce structured insights with risks and trends.",
                "depends_on": ["1"],
            },
            {
                "id": "3",
                "agent": "writer",
                "instruction": "Write the final response tailored to the original request using analysis context.",
                "depends_on": ["2"],
            },
        ]
