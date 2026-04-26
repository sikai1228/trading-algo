"""Tiny in-process pub/sub event bus.

Every significant action (news detected, market discovered, snapshot
written, system event) is published as a typed event. The database
writer is one subscriber; future subscribers (Telegram notifier,
WebSocket push for the observability backend) can attach without any
change to publishers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from trumpbot.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Event:
    """A typed event with arbitrary payload."""

    type: str
    payload: dict[str, Any]


Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    """Async fan-out bus. Subscribers run concurrently per event."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber]] = {}
        self._wildcards: list[Subscriber] = []

    def subscribe(self, event_type: str, callback: Subscriber) -> None:
        """Subscribe to a specific event type. Use ``"*"`` for all events."""
        if event_type == "*":
            self._wildcards.append(callback)
        else:
            self._subscribers.setdefault(event_type, []).append(callback)

    async def publish(self, event: Event) -> None:
        """Dispatch an event to every matching subscriber concurrently.

        Subscriber failures are logged and isolated; one failing
        subscriber never blocks others.
        """
        callbacks = list(self._subscribers.get(event.type, ())) + list(self._wildcards)
        if not callbacks:
            return
        results = await asyncio.gather(
            *(_safe_invoke(cb, event) for cb in callbacks),
            return_exceptions=True,
        )
        for cb, result in zip(callbacks, results, strict=False):
            if isinstance(result, BaseException):
                log.error(
                    "event_subscriber_failed",
                    event_type=event.type,
                    subscriber=getattr(cb, "__qualname__", repr(cb)),
                    error=repr(result),
                )


async def _safe_invoke(callback: Subscriber, event: Event) -> None:
    await callback(event)
