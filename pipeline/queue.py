"""Async Redis Streams helper client."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import ResponseError

from utils.logger import get_logger


@dataclass(slots=True)
class StreamMessage:
    """A normalized Redis stream message."""

    message_id: str
    fields: dict[str, str]


class RedisStreamClient:
    """Convenience wrapper for Redis Streams with async operations."""

    def __init__(self, redis_url: str) -> None:
        self.redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        self.logger = get_logger("pipeline.queue")

    @staticmethod
    def _serialize_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        return json.dumps(value, ensure_ascii=True)

    @staticmethod
    def _deserialize_fields(raw_fields: dict[str, Any]) -> dict[str, str]:
        return {str(k): str(v) for k, v in raw_fields.items()}

    async def ping(self) -> bool:
        """Return True if Redis is reachable."""
        return bool(await self.redis.ping())

    async def close(self) -> None:
        """Close the Redis connection."""
        if hasattr(self.redis, "aclose"):
            await self.redis.aclose()
            return
        await self.redis.close()

    async def ensure_group(self, stream: str, group: str) -> None:
        """Create a stream consumer group if it does not already exist."""
        try:
            await self.redis.xgroup_create(name=stream, groupname=group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def add(self, stream: str, fields: dict[str, Any]) -> str:
        """Append a message to a stream."""
        payload = {k: self._serialize_value(v) for k, v in fields.items()}
        return await self.redis.xadd(stream, payload)

    async def ack(self, stream: str, group: str, message_id: str) -> int:
        """Acknowledge a consumed stream message."""
        return int(await self.redis.xack(stream, group, message_id))

    async def read_group(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int = 3000,
        latest_id: str = ">",
    ) -> list[StreamMessage]:
        """Read from a stream using a consumer group."""
        raw = await self.redis.xreadgroup(
            groupname=group,
            consumername=consumer,
            streams={stream: latest_id},
            count=count,
            block=block_ms,
        )
        return self._normalize_read_response(raw)

    async def read(
        self,
        *,
        stream: str,
        last_id: str,
        count: int = 20,
        block_ms: int = 3000,
    ) -> list[StreamMessage]:
        """Read raw stream messages without consumer group semantics."""
        raw = await self.redis.xread(streams={stream: last_id}, count=count, block=block_ms)
        return self._normalize_read_response(raw)

    async def autoclaim(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,
        min_idle_ms: int,
        start_id: str = "0-0",
        count: int = 10,
    ) -> list[StreamMessage]:
        """Claim stale pending messages for a different consumer."""
        claimed = await self.redis.xautoclaim(
            name=stream,
            groupname=group,
            consumername=consumer,
            min_idle_time=min_idle_ms,
            start_id=start_id,
            count=count,
        )

        # redis-py may return (next_id, messages) or (next_id, messages, deleted_ids)
        messages = claimed[1] if isinstance(claimed, (list, tuple)) and len(claimed) >= 2 else []
        normalized: list[StreamMessage] = []
        for message_id, raw_fields in messages:
            normalized.append(
                StreamMessage(
                    message_id=str(message_id),
                    fields=self._deserialize_fields(raw_fields),
                )
            )
        return normalized

    async def add_to_dlq(
        self,
        *,
        dlq_stream: str,
        source_stream: str,
        message_id: str,
        reason: str,
        payload: dict[str, Any],
    ) -> str:
        """Move a failed message to dead-letter stream."""
        return await self.add(
            dlq_stream,
            {
                "source_stream": source_stream,
                "source_message_id": message_id,
                "reason": reason,
                "payload": payload,
                "moved_at": datetime.now(UTC).isoformat(),
            },
        )

    async def publish_user_event(self, *, stream: str, sse_payload: str, terminal: bool = False) -> str:
        """Publish a preformatted SSE payload into a user output stream."""
        return await self.add(
            stream,
            {
                "sse": sse_payload,
                "terminal": str(terminal).lower(),
                "created_at": datetime.now(UTC).isoformat(),
            },
        )

    def _normalize_read_response(self, raw: list[Any]) -> list[StreamMessage]:
        messages: list[StreamMessage] = []
        for _stream_name, entries in raw:
            for message_id, raw_fields in entries:
                messages.append(
                    StreamMessage(
                        message_id=str(message_id),
                        fields=self._deserialize_fields(raw_fields),
                    )
                )
        return messages
