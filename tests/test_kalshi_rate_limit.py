"""Tests for the token-bucket rate limiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from trumpbot.kalshi.rate_limit import TokenBucket, rate_limited


class TestTokenBucket:
    async def test_acquire_consumes_tokens(self) -> None:
        b = TokenBucket(rate_per_sec=100, capacity=5)
        for _ in range(5):
            await b.acquire()  # all five immediate
        # next acquire should block briefly
        start = time.monotonic()
        await b.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.005  # at 100/s a token refills in ~10ms

    async def test_capacity_validation(self) -> None:
        b = TokenBucket(rate_per_sec=10, capacity=2)
        with pytest.raises(ValueError):
            await b.acquire(tokens=5)

    def test_zero_rate_rejected(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate_per_sec=0, capacity=1)


class TestDecorator:
    async def test_rate_limited_decorator(self) -> None:
        class Box:
            def __init__(self) -> None:
                self._rate_limiter = TokenBucket(rate_per_sec=200, capacity=1)
                self.calls = 0

            @rate_limited()
            async def hit(self) -> int:
                self.calls += 1
                return self.calls

        box = Box()
        results = await asyncio.gather(box.hit(), box.hit(), box.hit())
        assert sorted(results) == [1, 2, 3]
