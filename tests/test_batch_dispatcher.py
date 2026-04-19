"""Tests for manual batch dispatching."""

from __future__ import annotations

import asyncio

import pytest

from pipeline.batch_dispatcher import BatchDispatcher


@pytest.mark.asyncio
async def test_batch_dispatcher_splits_by_batch_size() -> None:
    batches: list[int] = []

    async def callback(items):  # type: ignore[no-untyped-def]
        batches.append(len(items))

    dispatcher = BatchDispatcher(batch_size=5, batch_window_seconds=0.2, dispatch_callback=callback)
    await dispatcher.start()

    try:
        for idx in range(7):
            await dispatcher.submit(f"task-{idx}")
        await asyncio.sleep(0.5)
    finally:
        await dispatcher.stop()

    assert batches[0] == 5
    assert batches[1] == 2


@pytest.mark.asyncio
async def test_batch_dispatcher_flushes_on_time_window() -> None:
    batches: list[int] = []

    async def callback(items):  # type: ignore[no-untyped-def]
        batches.append(len(items))

    dispatcher = BatchDispatcher(batch_size=5, batch_window_seconds=0.1, dispatch_callback=callback)
    await dispatcher.start()

    try:
        await dispatcher.submit("single-task")
        await asyncio.sleep(0.25)
    finally:
        await dispatcher.stop()

    assert batches == [1]
