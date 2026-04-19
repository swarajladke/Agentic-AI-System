"""Async retry utilities."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


def async_retry(
    *,
    max_retries: int = 3,
    base_delay_seconds: float = 1.0,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_final_failure: Callable[[BaseException], Awaitable[None]] | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Retry an async function with exponential backoff.

    Backoff schedule is `base_delay_seconds * (2 ** attempt_index)`.
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_error: BaseException | None = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except retry_exceptions as exc:  # type: ignore[misc]
                    last_error = exc
                    if attempt == max_retries - 1:
                        if on_final_failure is not None:
                            await on_final_failure(exc)
                        raise
                    delay = base_delay_seconds * (2**attempt)
                    await asyncio.sleep(delay)
            assert last_error is not None
            raise last_error

        return wrapper

    return decorator
