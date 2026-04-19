"""Tests for async retry decorator."""

from __future__ import annotations

import pytest

from utils.retry import async_retry


@pytest.mark.asyncio
async def test_async_retry_succeeds_after_retries() -> None:
    attempts = {"count": 0}

    @async_retry(max_retries=3, base_delay_seconds=0.001)
    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("temporary")
        return "ok"

    result = await flaky()
    assert result == "ok"
    assert attempts["count"] == 3


@pytest.mark.asyncio
async def test_async_retry_raises_after_max_retries() -> None:
    attempts = {"count": 0}

    @async_retry(max_retries=3, base_delay_seconds=0.001)
    async def always_fail() -> None:
        attempts["count"] += 1
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await always_fail()
    assert attempts["count"] == 3
