"""Bankroll syncing — periodically refresh the local bankroll cache
from Kalshi's reported account balance.

Phase 4 Part 1, with pre-live fix #2 (Phase 4 Part 2.2) added the
auto-halt + auto-resume behavior described below.

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

**Auto-halt** (pre-live fix #2): after three consecutive sync failures,
the loop sets ``system_state.halt_flag = 'true'`` AND records the
halt source as ``bankroll_sync`` in ``system_state.halt_reason``.
A critical Telegram alert fires once. On the next successful sync,
if and only if the halt was set by THIS mechanism (halt_reason ==
``bankroll_sync``), the flag is cleared and an info alert fires.

This protects against trading off a stale balance during a
prolonged Kalshi outage. The user's manual ``/halt`` is never
overridden by the auto-resume logic — only the bot's own auto-halt
is auto-cleared.
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

# ---------------------------------------------------------------------
# Pre-live fix #2 — auto-halt on consecutive sync failures.
# ---------------------------------------------------------------------

# system_state keys for halt source + counters. Kept distinct from
# the existing ``halt_flag`` key so the user's /halt and the bot's
# auto-halt don't fight each other on resume.
HALT_REASON_KEY = "halt_reason"
HALT_REASON_BANKROLL_SYNC = "bankroll_sync"

# After this many consecutive failures, the loop auto-halts.
SYNC_FAILURE_HALT_THRESHOLD = 3


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


class SyncOutcome:
    """Result tag for a single :func:`sync_bankroll_once` call.

    Use as ``SyncOutcome.SUCCESS`` / ``FAILURE`` rather than parsing
    the return value of ``sync_bankroll_once`` so the loop's
    consecutive-failure logic doesn't have to guess at what ``None``
    means.
    """

    SUCCESS = "success"
    FAILURE = "failure"


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


# ---------------------------------------------------------------------
# Telegram-send callable plumbed in by the daemon. Same shape as the
# ``SendTextFn`` in scheduled.py so the loop can fire the
# alert_critical_bankroll_sync_failed template directly.
# ---------------------------------------------------------------------

from collections.abc import Awaitable, Callable  # noqa: E402

SendTextFn = Callable[[str, bool], Awaitable[None]]


async def bankroll_sync_loop(
    *,
    db: Database,
    kalshi: KalshiClient,
    poll_interval_sec: int,
    stop_event: asyncio.Event,
    send_text: SendTextFn | None = None,
) -> None:
    """Long-running task. Runs ``sync_bankroll_once`` every
    ``poll_interval_sec`` (typically 300s) until ``stop_event`` fires.

    Pre-live fix #2:

    - Tracks consecutive failures across iterations.
    - After ``SYNC_FAILURE_HALT_THRESHOLD`` (3) consecutive failures,
      sets ``halt_flag='true'`` + ``halt_reason='bankroll_sync'`` and
      sends the ``alert_critical_bankroll_sync_failed`` template (if
      a Telegram callable is plumbed in).
    - On the next successful sync, if and only if ``halt_reason ==
      'bankroll_sync'``, clears the halt and sends an info alert. If
      the halt was set by the user's ``/halt`` (no halt_reason or
      a different one), the auto-resume logic is a no-op.
    """
    from datetime import UTC, datetime

    from trumpbot.notifications.templates import render_template

    component = "bankroll_sync_loop"
    log.info(f"{component}_started", poll_interval_sec=poll_interval_sec)

    # Sync immediately on startup so the engine's first sizing decision
    # uses fresh data.
    consecutive_failures = 0
    first_failure_at: datetime | None = None
    last_error: str = ""
    halted_by_us = False

    initial = await sync_bankroll_once(db, kalshi)
    if initial is None:
        consecutive_failures = 1
        first_failure_at = datetime.now(UTC)

    while not stop_event.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_sec)
        if stop_event.is_set():
            break

        try:
            cents = await sync_bankroll_once(db, kalshi)
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
            cents = None
            last_error = repr(exc)

        if cents is None:
            consecutive_failures += 1
            if first_failure_at is None:
                first_failure_at = datetime.now(UTC)
            insert_system_event(
                db,
                event_type="bankroll_sync_failed",
                severity=(
                    "warning" if consecutive_failures < SYNC_FAILURE_HALT_THRESHOLD else "error"
                ),
                component=component,
                message=(
                    f"bankroll sync failed; consecutive_failures="
                    f"{consecutive_failures}/{SYNC_FAILURE_HALT_THRESHOLD}; "
                    "using last cached value"
                ),
                detail={"consecutive_failures": consecutive_failures},
            )
            # Auto-halt at threshold (only fires once — re-firing on
            # subsequent failures would spam the user).
            if consecutive_failures >= SYNC_FAILURE_HALT_THRESHOLD and not halted_by_us:
                _set_halt_by_bankroll_sync(db)
                halted_by_us = True
                if send_text is not None:
                    last_success = get_system_state(db, BANKROLL_LAST_SYNC_KEY) or "never"
                    age = _format_age(last_success)
                    rendered = render_template(
                        "alert_critical_bankroll_sync_failed",
                        {
                            "failure_count": consecutive_failures,
                            "first_failure_time": (
                                first_failure_at.isoformat() if first_failure_at else "unknown"
                            ),
                            "last_error": last_error or "(no error captured)",
                            "last_success_time": last_success,
                            "age": age,
                        },
                    )
                    with contextlib.suppress(Exception):
                        await send_text(rendered.text, not rendered.audible)
            continue

        # Success path — reset counters; clear our auto-halt if and
        # only if WE set it.
        if consecutive_failures > 0:
            log.info(
                f"{component}_recovered",
                after_failures=consecutive_failures,
            )
        consecutive_failures = 0
        first_failure_at = None
        last_error = ""
        if halted_by_us:
            cleared = _clear_halt_if_ours(db)
            halted_by_us = False
            if cleared and send_text is not None:
                rendered = render_template(
                    "alert_info_bankroll_sync_recovered",
                    {
                        "time_et": datetime.now(UTC).isoformat(),
                        "balance": f"${cents / 100:.2f}",
                    },
                )
                with contextlib.suppress(Exception):
                    await send_text(rendered.text, not rendered.audible)

    log.info(f"{component}_stopped")


def _set_halt_by_bankroll_sync(db: Database) -> None:
    """Set ``halt_flag='true'`` AND record the source so the
    auto-resume logic can later distinguish from a user-issued /halt."""
    set_system_state(db, key="halt_flag", value="true")
    set_system_state(db, key=HALT_REASON_KEY, value=HALT_REASON_BANKROLL_SYNC)
    insert_system_event(
        db,
        event_type="halted_by_bankroll_sync",
        severity="error",
        component="bankroll_sync_loop",
        message=(
            "auto-halt: bankroll sync failed "
            f"{SYNC_FAILURE_HALT_THRESHOLD}+ consecutive times; "
            "trading halted to prevent decisions on stale balance"
        ),
    )


def _clear_halt_if_ours(db: Database) -> bool:
    """Clear ``halt_flag`` and ``halt_reason`` IFF we were the source.
    Returns True when we cleared (so the caller can fire the info
    alert), False when we left the user's manual halt alone."""
    reason = get_system_state(db, HALT_REASON_KEY)
    if reason != HALT_REASON_BANKROLL_SYNC:
        return False
    set_system_state(db, key="halt_flag", value="false")
    set_system_state(db, key=HALT_REASON_KEY, value="")
    insert_system_event(
        db,
        event_type="halt_cleared_by_bankroll_sync",
        severity="info",
        component="bankroll_sync_loop",
        message="bankroll sync recovered; auto-halt cleared",
    )
    return True


def _format_age(iso: str) -> str:
    """Pretty-format an ISO timestamp's age relative to now."""
    if iso in {"never", ""}:
        return "n/a"
    from datetime import UTC, datetime

    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "n/a"
    secs = int((datetime.now(UTC) - ts).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


__all__ = [
    "BANKROLL_LAST_SYNC_KEY",
    "BANKROLL_STATE_KEY",
    "HALT_REASON_BANKROLL_SYNC",
    "HALT_REASON_KEY",
    "SYNC_FAILURE_HALT_THRESHOLD",
    "SendTextFn",
    "SyncOutcome",
    "bankroll_sync_loop",
    "get_synced_bankroll_cents",
    "sync_bankroll_once",
]
