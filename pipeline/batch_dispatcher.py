"""Manual batching for inbound user tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable
from uuid import uuid4

from utils.logger import get_logger


@dataclass(slots=True)
class BatchItem:
    """Represents a single user task entering the batching window."""

    task_id: str
    user_task: str
    submitted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


class BatchDispatcher:
    """Collect items for up to N seconds or until max batch size is reached."""

    def __init__(
        self,
        *,
        batch_size: int,
        batch_window_seconds: float,
        dispatch_callback: Callable[[list[BatchItem]], Awaitable[None]],
    ) -> None:
        self.batch_size = batch_size
        self.batch_window_seconds = batch_window_seconds
        self.dispatch_callback = dispatch_callback
        self.logger = get_logger("pipeline.batch_dispatcher")
        self._queue: asyncio.Queue[BatchItem] = asyncio.Queue()
        self._running = False
        self._loop_task: asyncio.Task[None] | None = None
        self.last_dispatch_at: str | None = None
        self.last_batch_size: int = 0

    async def start(self) -> None:
        """Start background dispatch loop."""
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._dispatch_loop(), name="batch-dispatch-loop")

    async def stop(self) -> None:
        """Stop background dispatch loop."""
        self._running = False
        if self._loop_task is None:
            return
        self._loop_task.cancel()
        try:
            await self._loop_task
        except asyncio.CancelledError:
            pass
        self._loop_task = None

    async def submit(self, user_task: str, metadata: dict[str, Any] | None = None) -> str:
        """Queue a user task and return assigned task id."""
        task_id = str(uuid4())
        await self._queue.put(BatchItem(task_id=task_id, user_task=user_task, metadata=metadata or {}))
        return task_id

    def health(self) -> dict[str, Any]:
        """Return dispatcher health status."""
        return {
            "running": self._running,
            "queue_depth": self._queue.qsize(),
            "last_batch_size": self.last_batch_size,
            "last_dispatch_at": self.last_dispatch_at,
        }

    async def _dispatch_loop(self) -> None:
        while self._running:
            first = await self._queue.get()
            batch = [first]

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.batch_window_seconds
            while len(batch) < self.batch_size:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                    batch.append(item)
                except TimeoutError:
                    break

            started_at = datetime.now(UTC)
            try:
                await self.dispatch_callback(batch)
            except Exception as exc:
                self.logger.error(
                    "Batch dispatch failed",
                    extra={
                        "extra_fields": {
                            "batch_size": len(batch),
                            "reason": str(exc),
                        }
                    },
                )
            finally:
                for _ in batch:
                    self._queue.task_done()

            self.last_batch_size = len(batch)
            self.last_dispatch_at = started_at.isoformat()
            self.logger.info(
                "Dispatched batch",
                extra={
                    "extra_fields": {
                        "batch_size": len(batch),
                        "dispatch_time": self.last_dispatch_at,
                    }
                },
            )
