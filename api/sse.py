"""Server-Sent Events helpers."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from pipeline.queue import RedisStreamClient


def format_sse(payload: dict[str, Any]) -> str:
    """Format payload as SSE `data:` line."""
    return f"data: {json.dumps(payload, ensure_ascii=True)}\n\n"


async def user_stream_generator(
    *,
    queue_client: RedisStreamClient,
    stream_name: str,
    block_ms: int,
    from_start: bool = True,
) -> AsyncIterator[str]:
    """Yield SSE lines from a user output stream."""
    last_id = "0-0" if from_start else "$"

    while True:
        messages = await queue_client.read(
            stream=stream_name,
            last_id=last_id,
            block_ms=block_ms,
            count=25,
        )
        if not messages:
            yield ": keep-alive\n\n"
            continue
        for message in messages:
            last_id = message.message_id
            sse = message.fields.get("sse")
            if not sse:
                sse = format_sse({"raw": message.fields})
            yield sse
            if message.fields.get("terminal", "false").lower() == "true":
                return
