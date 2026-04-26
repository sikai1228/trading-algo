"""Live-mode settlement detector.

Phase 4 Part 1.

Polls ``GET /portfolio/settlements`` every 5 minutes and closes out
any open ``live`` trades whose markets have resolved. The detector
keys off Kalshi's ``settled_time`` and ``market_result``, not our
local market-status table — Kalshi is the source of truth for actual
settlement.

For each settled ticker with an open live trade:

- ``market_result == 'yes'`` → close at 100c → ``live_closed_resolved_yes``
- ``market_result == 'no'``  → close at   0c → ``live_closed_resolved_no``
- ``market_result == 'void'`` → close at entry price → ``live_closed_resolved_no``
                                 (treated as a no-loss exit; rare)

Also dispatches a notification through the alert dispatcher so the
user sees the settlement immediately. Idempotent — already-closed
trades are skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    close_trade,
    get_open_trade_for_ticker,
    insert_system_event,
)
from trumpbot.kalshi.client import KalshiClient
from trumpbot.utils.logging import get_logger

log = get_logger(__name__)


# Settlement notifier callable. Accepts kw-only args; returns Awaitable.
# Defined as a plain Callable (not a Protocol class) so the daemon's
# nested async def can pass type-check without us subclassing.
SettlementNotifier = Callable[..., Awaitable[None]]


def _utcnow_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


async def detect_and_close_settlements(
    *,
    db: Database,
    kalshi: KalshiClient,
    notifier: SettlementNotifier | None = None,
) -> int:
    """Single-pass settlement detector. Returns the number of trades
    closed in this pass. Caller wraps in a loop."""
    try:
        settlements = await kalshi.get_settlements()
    except Exception as exc:
        log.warning("settlement_poll_failed", error=repr(exc))
        return 0

    closed = 0
    for s in settlements:
        row = get_open_trade_for_ticker(db, s.ticker)
        if row is None:
            continue
        if row["status"] != "live":
            continue
        result = (s.market_result or "").lower()
        if result == "yes":
            payoff = 100
            new_status = "live_closed_resolved_yes"
        elif result == "no":
            payoff = 0
            new_status = "live_closed_resolved_no"
        elif result == "void":
            payoff = row["entry_price_cents"]
            new_status = "live_closed_resolved_no"
        else:
            log.warning(
                "settlement_unknown_result",
                ticker=s.ticker,
                market_result=s.market_result,
            )
            continue
        proceeds = payoff * row["quantity"]
        realized = proceeds - row["cost_basis_usd_cents"]
        close_trade(
            db,
            trade_id=row["id"],
            new_status=new_status,
            exit_price_cents=payoff,
            realized_pnl_usd_cents=realized,
            exited_at=_utcnow_iso(),
            exit_fees_cents=0,  # Kalshi fee is 0 at p=0/p=100 settlements
        )
        insert_system_event(
            db,
            event_type="market_settled",
            severity="info",
            component="settlement_detector",
            message=(
                f"market {s.ticker} settled {result}; trade {row['id']} closed "
                f"({row['quantity']} contracts x {payoff}c = "
                f"realized {realized}c)"
            ),
            detail={
                "ticker": s.ticker,
                "trade_id": row["id"],
                "market_result": result,
                "payoff_cents": payoff,
                "realized_pnl_cents": realized,
            },
        )
        if notifier is not None:
            with contextlib.suppress(Exception):
                await notifier(
                    ticker=s.ticker,
                    result=result,
                    realized_pnl_cents=realized,
                    quantity=row["quantity"],
                    payoff_cents=payoff,
                )
        closed += 1
    return closed


async def settlement_loop(
    *,
    db: Database,
    kalshi: KalshiClient,
    poll_interval_sec: int,
    stop_event: asyncio.Event,
    notifier: SettlementNotifier | None = None,
) -> None:
    """Long-running detector. Runs every ``poll_interval_sec``."""
    component = "settlement_loop"
    log.info(f"{component}_started", poll_interval_sec=poll_interval_sec)
    while not stop_event.is_set():
        try:
            closed = await detect_and_close_settlements(db=db, kalshi=kalshi, notifier=notifier)
            if closed:
                log.info("settlement_loop_closed", closed_count=closed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"{component}_error", error=repr(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_sec)
    log.info(f"{component}_stopped")


__all__ = ["SettlementNotifier", "detect_and_close_settlements", "settlement_loop"]
