"""Tests for orchestrator planning and dispatch behavior."""

from __future__ import annotations

from typing import Any

import pytest

from agents.orchestrator import OrchestratorAgent
from config import Settings
from pipeline.dag_resolver import DagResolver


class FakeQueueClient:
    """Minimal queue stub for orchestrator unit tests."""

    def __init__(self) -> None:
        self.add_calls: list[tuple[str, dict[str, Any]]] = []
        self.user_events: list[tuple[str, str, bool]] = []

    async def add(self, stream: str, fields: dict[str, Any]) -> str:
        self.add_calls.append((stream, fields))
        return "1-0"

    async def publish_user_event(self, *, stream: str, sse_payload: str, terminal: bool = False) -> str:
        self.user_events.append((stream, sse_payload, terminal))
        return "1-1"


@pytest.mark.asyncio
async def test_orchestrator_fallback_plan_is_valid() -> None:
    queue = FakeQueueClient()
    resolver = DagResolver()
    settings = Settings(anthropic_api_key=None)
    orchestrator = OrchestratorAgent(queue_client=queue, settings=settings, resolver=resolver)

    steps = await orchestrator.decompose_task("Test complex task")
    assert len(steps) == 3
    assert [step["agent"] for step in steps] == ["retriever", "analyzer", "writer"]
    assert steps[1]["depends_on"] == ["1"]
    assert steps[2]["depends_on"] == ["2"]


@pytest.mark.asyncio
async def test_orchestrator_dispatches_only_ready_steps_initially() -> None:
    queue = FakeQueueClient()
    resolver = DagResolver()
    settings = Settings(anthropic_api_key=None)
    orchestrator = OrchestratorAgent(queue_client=queue, settings=settings, resolver=resolver)

    await orchestrator.create_task(task_id="task-123", user_task="Research market and write memo")

    task_messages = [call for call in queue.add_calls if call[0] == settings.task_stream]
    assert len(task_messages) == 1
    _, message = task_messages[0]
    assert message["task_id"] == "task-123"
    assert message["step_id"] == "1"
    assert message["agent"] == "retriever"
