"""Dependency resolution for task execution DAGs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class StepNode:
    """Represents one execution step in a task DAG."""

    step_id: str
    agent: str
    instruction: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    output: dict[str, Any] | None = None
    error: str | None = None


@dataclass(slots=True)
class TaskDag:
    """Task-level DAG state."""

    task_id: str
    user_task: str
    steps: dict[str, StepNode]


class DagResolver:
    """In-memory DAG tracker supporting parallel and diamond dependencies."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskDag] = {}
        self._lock = __import__("asyncio").Lock()

    async def register_task(self, *, task_id: str, user_task: str, steps: list[dict[str, Any]]) -> None:
        """Register a new task DAG."""
        parsed_steps: dict[str, StepNode] = {}
        for step in steps:
            step_id = str(step["id"])
            parsed_steps[step_id] = StepNode(
                step_id=step_id,
                agent=str(step["agent"]).strip().lower(),
                instruction=str(step["instruction"]).strip(),
                depends_on=[str(dep) for dep in step.get("depends_on", [])],
            )
        async with self._lock:
            self._tasks[task_id] = TaskDag(task_id=task_id, user_task=user_task, steps=parsed_steps)

    async def get_ready_steps(self, task_id: str) -> list[dict[str, Any]]:
        """Return all steps whose dependencies are satisfied and not yet dispatched."""
        async with self._lock:
            task = self._tasks[task_id]
            ready: list[dict[str, Any]] = []
            for node in task.steps.values():
                if node.status != "pending":
                    continue
                if all(task.steps[dep].status == "completed" for dep in node.depends_on):
                    ready.append(self._node_to_dict(node))
            return ready

    async def mark_dispatched(self, *, task_id: str, step_id: str) -> None:
        """Mark step as dispatched to an agent."""
        async with self._lock:
            node = self._tasks[task_id].steps[step_id]
            node.status = "dispatched"

    async def mark_step_completed(
        self,
        *,
        task_id: str,
        step_id: str,
        output: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Mark step completed and return newly unlocked steps."""
        async with self._lock:
            task = self._tasks[task_id]
            node = task.steps[step_id]
            node.status = "completed"
            node.output = output

            ready: list[dict[str, Any]] = []
            for candidate in task.steps.values():
                if candidate.status != "pending":
                    continue
                if all(task.steps[dep].status == "completed" for dep in candidate.depends_on):
                    ready.append(self._node_to_dict(candidate))
            return ready

    async def mark_step_failed(self, *, task_id: str, step_id: str, reason: str) -> None:
        """Mark a step failed."""
        async with self._lock:
            node = self._tasks[task_id].steps[step_id]
            node.status = "failed"
            node.error = reason

    async def get_dependency_context(self, *, task_id: str, step_id: str) -> dict[str, Any]:
        """Return outputs from all dependency steps."""
        async with self._lock:
            task = self._tasks[task_id]
            node = task.steps[step_id]
            return {
                dep_id: (task.steps[dep_id].output or {})
                for dep_id in node.depends_on
                if dep_id in task.steps
            }

    async def is_task_complete(self, task_id: str) -> bool:
        """Return True when all steps are completed."""
        async with self._lock:
            task = self._tasks[task_id]
            return all(step.status == "completed" for step in task.steps.values())

    async def has_failures(self, task_id: str) -> bool:
        """Return True if any step failed."""
        async with self._lock:
            task = self._tasks[task_id]
            return any(step.status == "failed" for step in task.steps.values())

    async def get_user_task(self, task_id: str) -> str:
        """Return original user task."""
        async with self._lock:
            return self._tasks[task_id].user_task

    async def snapshot(self) -> dict[str, Any]:
        """Return a health/debug snapshot."""
        async with self._lock:
            payload: dict[str, Any] = {}
            for task_id, task in self._tasks.items():
                payload[task_id] = {
                    step.step_id: {
                        "agent": step.agent,
                        "status": step.status,
                        "depends_on": step.depends_on,
                    }
                    for step in task.steps.values()
                }
            return payload

    @staticmethod
    def _node_to_dict(node: StepNode) -> dict[str, Any]:
        return {
            "id": node.step_id,
            "agent": node.agent,
            "instruction": node.instruction,
            "depends_on": list(node.depends_on),
        }
