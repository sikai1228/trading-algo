"""Token-bucket rate limiter, applied as an async-method decorator.

Cap usage at a configurable fraction of the documented tier limit
(default 80 %) even when the API would allow more — this keeps headroom
for bursts and protects against accidental floods.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


class TokenBucket:
    """Classic token bucket: ``capacity`` tokens, refilled at ``rate`` per second."""

    def __init__(self, *, rate_per_sec: float, capacity: float) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.rate_per_sec = rate_per_sec
        self.capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Block (asyncio-style) until ``tokens`` are available, then consume them."""
        if tokens > self.capacity:
            raise ValueError(f"requested {tokens} tokens exceeds bucket capacity {self.capacity}")
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_sec)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate_per_sec
            await asyncio.sleep(wait)


def rate_limited(
    bucket_attr: str = "_rate_limiter",
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorate an async method so it waits on ``self.<bucket_attr>`` before running.

    Usage::

        class KalshiClient:
            def __init__(...):
                self._rate_limiter = TokenBucket(...)

            @rate_limited()
            async def get_market(self, ticker): ...
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> T:
            bucket: TokenBucket = getattr(self, bucket_attr)
            await bucket.acquire()
            return await func(self, *args, **kwargs)

        return wrapper

    return decorator
