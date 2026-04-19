"""Base class for specialized stream-processing agents."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from config import Settings
from pipeline.queue import RedisStreamClient, StreamMessage
from utils.logger import get_logger


class BaseAgent(ABC):
    """Shared Redis Streams worker lifecycle for all task agents."""

    def __init__(
        self,
        *,
        queue_client: RedisStreamClient,
        settings: Settings,
        handled_agent: str,
        consumer_group: str,
        name: str,
    ) -> None:
        self.queue = queue_client
        self.settings = settings
        self.handled_agent = handled_agent
        self.consumer_group = consumer_group
        self.consumer_name = f"{name}-{uuid4().hex[:8]}"
        self.name = name
        self.running = False
        self.last_processed_at: str | None = None
        self.logger = get_logger(f"agents.{name}")

    async def start(self) -> None:
        """Start consuming task stream messages."""
        self.running = True
        await self.queue.ensure_group(self.settings.task_stream, self.consumer_group)

        while self.running:
            claimed = await self.queue.autoclaim(
                stream=self.settings.task_stream,
                group=self.consumer_group,
                consumer=self.consumer_name,
                min_idle_ms=self.settings.claim_idle_ms,
                count=10,
            )
            new_messages = await self.queue.read_group(
                stream=self.settings.task_stream,
                group=self.consumer_group,
                consumer=self.consumer_name,
                count=10,
                block_ms=self.settings.stream_block_ms,
            )
            messages = [*claimed, *new_messages]
            if not messages:
                continue
            for message in messages:
                await self._handle_stream_message(message)

    async def stop(self) -> None:
        """Stop message loop."""
        self.running = False

    def health(self) -> dict[str, Any]:
        """Return lightweight health metadata."""
        return {
            "running": self.running,
            "consumer_group": self.consumer_group,
            "consumer_name": self.consumer_name,
            "handled_agent": self.handled_agent,
            "last_processed_at": self.last_processed_at,
        }

    @abstractmethod
    async def process(self, task: dict[str, Any]) -> dict[str, Any]:
        """Process an individual task payload."""

    async def _handle_stream_message(self, message: StreamMessage) -> None:
        payload = message.fields
        target_agent = payload.get("agent", "").strip().lower()
        task_id = payload.get("task_id", "")
        step_id = payload.get("step_id", "")

        if target_agent != self.handled_agent:
            await self.queue.ack(self.settings.task_stream, self.consumer_group, message.message_id)
            return

        self.last_processed_at = datetime.now(UTC).isoformat()
        try:
            output = await self.process(payload)
            await self.queue.add(
                self.settings.result_stream,
                {
                    "task_id": task_id,
                    "step_id": step_id,
                    "agent": self.handled_agent,
                    "status": "success",
                    "output": json.dumps(output, ensure_ascii=True),
                    "processed_at": self.last_processed_at,
                },
            )
        except Exception as exc:
            reason = str(exc)
            await self.queue.add(
                self.settings.result_stream,
                {
                    "task_id": task_id,
                    "step_id": step_id,
                    "agent": self.handled_agent,
                    "status": "failed",
                    "reason": reason,
                    "processed_at": datetime.now(UTC).isoformat(),
                },
            )
            await self.queue.add_to_dlq(
                dlq_stream=self.settings.dlq_stream,
                source_stream=self.settings.task_stream,
                message_id=message.message_id,
                reason=reason,
                payload=payload,
            )
            self.logger.error(
                "Agent processing failed",
                extra={
                    "extra_fields": {
                        "task_id": task_id,
                        "step_id": step_id,
                        "reason": reason,
                    }
                },
            )
        finally:
            await self.queue.ack(self.settings.task_stream, self.consumer_group, message.message_id)
