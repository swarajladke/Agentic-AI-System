"""Aggregates step results and unlocks dependent DAG steps."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable
from uuid import uuid4

from api.sse import format_sse
from config import Settings
from pipeline.dag_resolver import DagResolver
from pipeline.queue import RedisStreamClient, StreamMessage
from utils.logger import get_logger


class ResultAggregator:
    """Consumes agent step results and dispatches newly ready steps."""

    def __init__(
        self,
        *,
        queue_client: RedisStreamClient,
        resolver: DagResolver,
        settings: Settings,
        dispatch_step_callback: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.queue = queue_client
        self.resolver = resolver
        self.settings = settings
        self.dispatch_step_callback = dispatch_step_callback
        self.consumer_group = "group:aggregator"
        self.consumer_name = f"aggregator-{uuid4().hex[:8]}"
        self.logger = get_logger("pipeline.result_aggregator")
        self.running = False
        self.last_processed_at: str | None = None

    async def start(self) -> None:
        """Start result processing loop."""
        self.running = True
        await self.queue.ensure_group(self.settings.result_stream, self.consumer_group)
        while self.running:
            messages = await self.queue.autoclaim(
                stream=self.settings.result_stream,
                group=self.consumer_group,
                consumer=self.consumer_name,
                min_idle_ms=self.settings.claim_idle_ms,
                count=10,
            )
            if not messages:
                messages = await self.queue.read_group(
                    stream=self.settings.result_stream,
                    group=self.consumer_group,
                    consumer=self.consumer_name,
                    count=10,
                    block_ms=self.settings.stream_block_ms,
                )
            if not messages:
                continue
            for message in messages:
                await self._handle_message(message)

    async def stop(self) -> None:
        """Stop loop."""
        self.running = False

    def health(self) -> dict[str, Any]:
        """Return consumer health details."""
        return {
            "running": self.running,
            "consumer_group": self.consumer_group,
            "consumer_name": self.consumer_name,
            "last_processed_at": self.last_processed_at,
        }

    async def _handle_message(self, message: StreamMessage) -> None:
        fields = message.fields
        task_id = fields.get("task_id", "")
        step_id = fields.get("step_id", "")
        status = fields.get("status", "failed")
        agent = fields.get("agent", "unknown")
        user_stream = self.settings.user_stream_name(task_id)
        self.last_processed_at = datetime.now(UTC).isoformat()

        if status == "success":
            output_raw = fields.get("output", "{}")
            try:
                output = json.loads(output_raw)
            except json.JSONDecodeError:
                output = {"raw_output": output_raw}

            next_steps = await self.resolver.mark_step_completed(
                task_id=task_id,
                step_id=step_id,
                output=output,
            )
            await self.queue.publish_user_event(
                stream=user_stream,
                sse_payload=format_sse(
                    {
                        "type": "step_completed",
                        "task_id": task_id,
                        "step_id": step_id,
                        "agent": agent,
                        "status": "success",
                    }
                ),
            )

            for step in next_steps:
                await self.dispatch_step_callback(task_id, step)

            if await self.resolver.is_task_complete(task_id):
                await self.queue.publish_user_event(
                    stream=user_stream,
                    sse_payload=format_sse(
                        {
                            "type": "task_completed",
                            "task_id": task_id,
                            "status": "completed",
                        }
                    ),
                    terminal=True,
                )
        else:
            reason = fields.get("reason", "Unknown agent failure")
            await self.resolver.mark_step_failed(task_id=task_id, step_id=step_id, reason=reason)
            await self.queue.publish_user_event(
                stream=user_stream,
                sse_payload=format_sse(
                    {
                        "type": "task_failed",
                        "task_id": task_id,
                        "step_id": step_id,
                        "status": "failed",
                        "reason": reason,
                    }
                ),
                terminal=True,
            )

        await self.queue.ack(self.settings.result_stream, self.consumer_group, message.message_id)

        self.logger.info(
            "Processed result message",
            extra={
                "extra_fields": {
                    "task_id": task_id,
                    "step_id": step_id,
                    "status": status,
                }
            },
        )
