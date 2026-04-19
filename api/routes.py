"""FastAPI route handlers."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.sse import user_stream_generator

router = APIRouter()


class TaskRequest(BaseModel):
    """Incoming task request body."""

    task: str = Field(..., min_length=3, description="Natural language task")
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/task")
async def submit_task(request: Request, body: TaskRequest) -> dict[str, str]:
    """Queue a task for batched orchestrator dispatch."""
    services = request.app.state.services
    task_id = await services.batch_dispatcher.submit(body.task, metadata=body.metadata)
    return {
        "task_id": task_id,
        "status": "queued",
        "stream_url": f"/stream/{task_id}",
    }


@router.get("/stream/{task_id}")
async def stream_task_output(request: Request, task_id: str) -> StreamingResponse:
    """Stream task output in real time using SSE."""
    services = request.app.state.services
    generator = user_stream_generator(
        queue_client=services.queue_client,
        stream_name=services.settings.user_stream_name(task_id),
        block_ms=services.settings.stream_block_ms,
    )
    return StreamingResponse(generator, media_type="text/event-stream")


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Return system health across all consumers."""
    services = request.app.state.services
    redis_ok = await services.queue_client.ping()
    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": redis_ok,
        "dispatcher": services.batch_dispatcher.health(),
        "orchestrator": services.orchestrator.health(),
        "aggregator": services.aggregator.health(),
        "retriever": services.retriever.health(),
        "analyzer": services.analyzer.health(),
        "writer": services.writer.health(),
    }
