"""Writer agent implementation with Claude streaming."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from agents.base_agent import BaseAgent
from api.sse import format_sse
from config import Settings
from pipeline.queue import RedisStreamClient
from utils.retry import async_retry


class WriterAgent(BaseAgent):
    """Streams final answer token-by-token back to user output stream."""

    def __init__(self, *, queue_client: RedisStreamClient, settings: Settings) -> None:
        super().__init__(
            queue_client=queue_client,
            settings=settings,
            handled_agent="writer",
            consumer_group="group:writer",
            name="writer",
        )

    async def process(self, task: dict[str, Any]) -> dict[str, Any]:
        """Generate final output from analysis context and stream partial tokens."""
        task_id = task.get("task_id", "")
        step_id = task.get("step_id", "")
        user_task = task.get("user_task", "")
        context = self._parse_json(task.get("context", "{}"))

        try:
            if self.settings.anthropic_api_key:
                final_text = await self._stream_claude(task_id=task_id, step_id=step_id, user_task=user_task, context=context)
            else:
                final_text = await self._simulate_writer_stream(task_id=task_id, user_task=user_task)
        except Exception as exc:
            await self.queue.publish_user_event(
                stream=self.settings.user_stream_name(task_id),
                sse_payload=format_sse(
                    {
                        "type": "task_failed",
                        "task_id": task_id,
                        "step": "writer",
                        "status": "failed",
                        "reason": str(exc),
                    }
                ),
                terminal=True,
            )
            raise

        return {
            "final_output": final_text,
            "word_count": len(final_text.split()),
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @async_retry(max_retries=3, base_delay_seconds=1.0)
    async def _stream_claude(
        self,
        *,
        task_id: str,
        step_id: str,
        user_task: str,
        context: dict[str, Any],
    ) -> str:
        """Call Claude with SSE streaming and relay text chunks into user stream."""
        prompt = (
            "You are the writer agent. Produce polished output for the user request.\n"
            "Use the analysis context faithfully and avoid hallucinations.\n\n"
            f"User task:\n{user_task}\n\n"
            f"Analysis context:\n{json.dumps(context, ensure_ascii=True)}"
        )
        headers = {
            "x-api-key": self.settings.anthropic_api_key or "",
            "anthropic-version": self.settings.anthropic_version,
            "content-type": "application/json",
        }
        body = {
            "model": self.settings.anthropic_model,
            "max_tokens": 1800,
            "temperature": 0.3,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }

        full_text_parts: list[str] = []
        async with httpx.AsyncClient(timeout=self.settings.api_timeout_seconds) as client:
            async with client.stream("POST", self.settings.anthropic_api_url, headers=headers, json=body) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw_data = line[len("data:") :].strip()
                    if raw_data == "[DONE]":
                        break
                    try:
                        event = json.loads(raw_data)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "content_block_delta":
                        continue
                    delta = event.get("delta", {})
                    if delta.get("type") != "text_delta":
                        continue
                    token = str(delta.get("text", ""))
                    if not token:
                        continue
                    full_text_parts.append(token)
                    await self._publish_token(task_id=task_id, token=token, step="writer")

        final_text = "".join(full_text_parts).strip()
        if not final_text:
            raise RuntimeError("Claude streaming returned empty output")
        await self._publish_token(task_id=task_id, token="", step="writer", done=True, step_id=step_id)
        return final_text

    async def _simulate_writer_stream(self, *, task_id: str, user_task: str) -> str:
        """Fallback local streaming used when Anthropic API key is missing."""
        if "quantum computing" in user_task.lower():
            text = (
                "Quantum computing has shifted from headline qubit races to engineering discipline. "
                "Across 2025 and early 2026, leading teams have emphasized error mitigation, calibration stability, and hybrid orchestration "
                "so pilots can deliver repeatable results rather than one-off demonstrations. Enterprises are increasingly evaluating quantum tools "
                "in targeted domains such as optimization, molecular simulation, and risk modeling where classical bottlenecks are already well understood.\n\n"
                "A second major trend is commercialization maturity. Vendors now package more integrated workflows, including managed runtime environments "
                "and domain libraries, which reduces adoption friction for non-research teams. At the same time, buyers are becoming more selective: "
                "they prioritize proof of value, benchmark transparency, and interoperability over raw hardware claims. This has improved decision quality "
                "but also lengthened enterprise procurement cycles.\n\n"
                "Key takeaways for leadership are clear: invest in a hybrid roadmap, not a replacement narrative; align pilots to measurable business outcomes; "
                "and build internal talent early to avoid dependency risk. Organizations that combine pragmatic experimentation with capability building "
                "are best positioned to capture upside as quantum systems and software continue to mature."
            )
        else:
            text = (
                "This is a simulated writer output because no Anthropic API key is configured. "
                "The system still demonstrates end-to-end orchestration, dependency resolution, retries, and real-time streaming behavior."
            )

        chunks = text.split(" ")
        emitted: list[str] = []
        for chunk in chunks:
            token = f"{chunk} "
            emitted.append(token)
            await self._publish_token(task_id=task_id, token=token, step="writer")
            await asyncio.sleep(0.01)
        await self._publish_token(task_id=task_id, token="", step="writer", done=True)
        return "".join(emitted).strip()

    async def _publish_token(
        self,
        *,
        task_id: str,
        token: str,
        step: str,
        done: bool = False,
        step_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"token": token, "task_id": task_id, "step": step}
        if step_id is not None:
            payload["step_id"] = step_id
        if done:
            payload["done"] = True
        await self.queue.publish_user_event(
            stream=self.settings.user_stream_name(task_id),
            sse_payload=format_sse(payload),
            terminal=False,
        )

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
