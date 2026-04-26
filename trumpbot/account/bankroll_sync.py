"""Bankroll syncing — periodically refresh the local bankroll cache
from Kalshi's reported account balance.

Phase 4 Part 1.

The decision engine needs an accurate bankroll to size positions. In
dry-run mode we use ``cfg.bankroll.starting_amount_usd``; once live
trading is on, that number is meaningless because the real bankroll
moves with every fill, settlement, fee, and (eventually) deposit /
withdrawal. The sync loop reads ``GET /portfolio/balance`` every 5
minutes and stores the result in the ``system_state`` key/value bag
under ``bankroll_usd_cents``.

Consumers (the decision loops) call :func:`get_synced_bankroll_cents`
which returns the cached value, falling back to the configured
starting amount if the sync hasn't run yet.

Idempotent: if Kalshi is unreachable, the loop logs and waits for the
next tick. The cache holds the LAST KNOWN good value rather than
zeroing out — better to size off a slightly-stale balance than to
freeze trading.
"""

from __future__ import annotations

import asyncio
import contextlib

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    get_system_state,
    insert_system_event,
    set_system_state,
)
from trumpbot.kalshi.client import KalshiClient
from trumpbot.utils.logging import get_logger

log = get_logger(__name__)

BANKROLL_STATE_KEY = "bankroll_usd_cents"
BANKROLL_LAST_SYNC_KEY = "bankroll_last_synced_at"


def get_synced_bankroll_cents(
    db: Database,
    *,
    fallback_starting_amount_usd: float,
) -> int:
    """Read the cached bankroll. Returns the configured starting
    amount (converted to cents) when the sync hasn't run yet."""
    raw = get_system_state(db, BANKROLL_STATE_KEY)
    if raw is None:
        return int(round(fallback_starting_amount_usd * 100))
    try:
        return int(raw)
    except ValueError:
        log.error("bankroll_state_corrupt", raw=raw)
        return int(round(fallback_starting_amount_usd * 100))


async def sync_bankroll_once(db: Database, kalshi: KalshiClient) -> int | None:
    """One-shot sync. Returns the new bankroll in cents on success,
    ``None`` on failure. Used by the daemon loop and by
    ``scripts/pre_live_checklist.py`` to validate connectivity."""
    try:
        balance = await kalshi.get_balance()
    except Exception as exc:
        log.warning("bankroll_sync_failed", error=repr(exc))
        return None
    cents = int(balance.balance)
    set_system_state(db, key=BANKROLL_STATE_KEY, value=str(cents))
    from datetime import UTC, datetime

    set_system_state(db, key=BANKROLL_LAST_SYNC_KEY, value=datetime.now(UTC).isoformat())
    log.info("bankroll_synced", cents=cents)
    return cents


async def bankroll_sync_loop(
    *,
    db: Database,
    kalshi: KalshiClient,
    poll_interval_sec: int,
    stop_event: asyncio.Event,
) -> None:
    """Long-running task. Runs ``sync_bankroll_once`` every
    ``poll_interval_sec`` (typically 300s) until ``stop_event`` fires.
    """
    component = "bankroll_sync_loop"
    log.info(f"{component}_started", poll_interval_sec=poll_interval_sec)
    # Sync immediately on startup so the engine's first sizing decision
    # uses fresh data.
    await sync_bankroll_once(db, kalshi)
    while not stop_event.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_sec)
        if stop_event.is_set():
            break
        try:
            cents = await sync_bankroll_once(db, kalshi)
            if cents is None:
                insert_system_event(
                    db,
                    event_type="bankroll_sync_failed",
                    severity="warning",
                    component=component,
                    message="bankroll sync returned None; using last cached value",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"{component}_error", error=repr(exc))
            insert_system_event(
                db,
                event_type="bankroll_sync_error",
                severity="error",
                component=component,
                message=str(exc),
            )
    log.info(f"{component}_stopped")


__all__ = [
    "BANKROLL_LAST_SYNC_KEY",
    "BANKROLL_STATE_KEY",
    "bankroll_sync_loop",
    "get_synced_bankroll_cents",
    "sync_bankroll_once",
]
