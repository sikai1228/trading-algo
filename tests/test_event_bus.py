"""Tests for the in-process event bus."""

from __future__ import annotations

from collections.abc import Awaitable

from trumpbot.events.bus import Event, EventBus


class TestEventBus:
    async def test_subscribe_specific(self) -> None:
        bus = EventBus()
        seen: list[Event] = []

        async def cb(event: Event) -> None:
            seen.append(event)

        bus.subscribe("foo", cb)
        await bus.publish(Event(type="foo", payload={"x": 1}))
        await bus.publish(Event(type="bar", payload={"y": 2}))
        assert len(seen) == 1
        assert seen[0].payload == {"x": 1}

    async def test_subscribe_wildcard(self) -> None:
        bus = EventBus()
        seen: list[str] = []

        async def cb(event: Event) -> None:
            seen.append(event.type)

        bus.subscribe("*", cb)
        await bus.publish(Event(type="a", payload={}))
        await bus.publish(Event(type="b", payload={}))
        assert seen == ["a", "b"]

    async def test_one_failing_subscriber_does_not_block_others(self) -> None:
        bus = EventBus()
        ok_calls = 0

        async def boom(event: Event) -> None:
            raise RuntimeError("boom")

        async def ok(event: Event) -> None:
            nonlocal ok_calls
            ok_calls += 1

        bus.subscribe("e", boom)
        bus.subscribe("e", ok)
        await bus.publish(Event(type="e", payload={}))
        assert ok_calls == 1

    async def test_no_subscribers_no_op(self) -> None:
        bus = EventBus()
        # should not raise
        await bus.publish(Event(type="x", payload={}))

    async def test_returned_awaitable_runs(self) -> None:
        bus = EventBus()
        seen: list[Awaitable[None]] = []

        async def cb(event: Event) -> None:
            seen.append(event)  # type: ignore[arg-type]

        bus.subscribe("e", cb)
        await bus.publish(Event(type="e", payload={"k": "v"}))
        assert len(seen) == 1
