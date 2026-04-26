"""Startup reconciliation between local trades and Kalshi state.

Phase 4 Part 1.

When the daemon restarts, three classes of drift can exist between
``trades`` rows and Kalshi's actual state:

1. **Pending without ack** — a trade row sits at ``status='pending'``
   because the previous run died between submission and ack. We look
   the order up by ``client_order_id``: if Kalshi has it, promote to
   ``live`` (or one of the killed / error statuses); if Kalshi doesn't
   have it, mark the row ``killed_no_fill`` and surface the drift.

2. **Live without position** — a trade row at ``status='live'`` but
   Kalshi reports we hold zero contracts on that ticker. Most likely
   the position was already settled in our absence (the daemon was
   down across the resolution boundary); also possible we manually
   closed via the Kalshi web UI. Tag ``reconcile_orphaned`` and
   require operator decision via ``/reconcile_resolve``.

3. **Position without trade** — Kalshi reports a position our DB
   doesn't know about. Could be a pre-existing manual trade, or a
   recovered idempotent submission whose UUID we lost. Insert a
   placeholder ``live_imported`` row tagged with the Kalshi order id.

Reconciliation is **gating**: the daemon refuses to start the
decision / stop-loss / re-entry loops until reconciliation has run
once successfully. If Kalshi is unreachable on startup, the daemon
logs the failure and continues to retry — but no orders go out until
we've cleared the drift check.

The ``/reconcile_resolve`` Telegram command lets the operator
acknowledge orphan rows so they stop generating alerts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    TradeInsertRow,
    get_trade_by_client_order_id,
    get_trade_by_kalshi_order_id,
    insert_system_event,
    list_open_live_trades,
    list_pending_trades,
    update_trade_status_by_client_order_id,
)
from trumpbot.kalshi.client import KalshiClient
from trumpbot.kalshi.schemas import KalshiPosition
from trumpbot.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ReconciliationDrift:
    """Per-row drift report. ``severity`` drives the alert color."""

    kind: str
    """One of: pending_recovered, pending_lost, live_orphaned,
    position_unknown."""

    ticker: str
    detail: str
    severity: str = "warning"


@dataclass(frozen=True)
class ReconciliationReport:
    """Aggregate result of a single reconciliation pass."""

    pending_count: int
    live_count: int
    kalshi_position_count: int
    drifts: list[ReconciliationDrift]
    succeeded: bool

    @property
    def has_drift(self) -> bool:
        return bool(self.drifts)


async def reconcile_once(
    *,
    db: Database,
    kalshi: KalshiClient,
) -> ReconciliationReport:
    """Run one reconciliation pass. Caller decides what to do with
    drift (alert via Telegram, halt the bot, etc.). Returns a
    :class:`ReconciliationReport` whose ``succeeded`` is True iff we
    successfully reached Kalshi for both /orders and /positions."""
    drifts: list[ReconciliationDrift] = []
    pending_rows = list_pending_trades(db)
    live_rows = list_open_live_trades(db)

    # ----- 1. Recover pending rows ----------------------------------
    for row in pending_rows:
        coid = row["client_order_id"]
        if not coid:
            # Should not happen — every pending row was minted with a
            # client_order_id by the executor. Tag it for review.
            drifts.append(
                ReconciliationDrift(
                    kind="pending_lost",
                    ticker=row["ticker"],
                    detail=f"trade {row['id']} pending without client_order_id",
                    severity="error",
                )
            )
            continue
        try:
            order = await kalshi.get_order_by_client_id(coid)
        except Exception as exc:
            log.error("reconcile_get_order_failed", coid=coid, error=repr(exc))
            return ReconciliationReport(
                pending_count=len(pending_rows),
                live_count=len(live_rows),
                kalshi_position_count=0,
                drifts=[],
                succeeded=False,
            )
        if order is None:
            update_trade_status_by_client_order_id(
                db,
                client_order_id=coid,
                new_status="killed_no_fill",
            )
            drifts.append(
                ReconciliationDrift(
                    kind="pending_lost",
                    ticker=row["ticker"],
                    detail=(
                        f"trade {row['id']} pending; Kalshi has no order with "
                        f"client_order_id={coid}; tagged killed_no_fill"
                    ),
                )
            )
            continue
        # Kalshi knows about it — promote based on actual status.
        filled = (
            (order.filled_count or 0)
            if order.filled_count is not None
            else (order.count or 0) - (order.remaining_count or 0)
        )
        if filled > 0 and order.status in {"executed", "filled"}:
            actual_avg = order.avg_fill_price or row["entry_price_cents"]
            update_trade_status_by_client_order_id(
                db,
                client_order_id=coid,
                new_status="live",
                kalshi_order_id=order.order_id,
                entry_price_cents=actual_avg,
                quantity=filled,
                cost_basis_usd_cents=actual_avg * filled,
                actual_avg_fill_price_cents=actual_avg,
            )
            drifts.append(
                ReconciliationDrift(
                    kind="pending_recovered",
                    ticker=row["ticker"],
                    detail=(
                        f"trade {row['id']} promoted pending→live "
                        f"({filled} contracts at {actual_avg}c)"
                    ),
                    severity="info",
                )
            )
        else:
            update_trade_status_by_client_order_id(
                db,
                client_order_id=coid,
                new_status="killed_no_fill",
                kalshi_order_id=order.order_id,
            )
            drifts.append(
                ReconciliationDrift(
                    kind="pending_lost",
                    ticker=row["ticker"],
                    detail=(
                        f"trade {row['id']} pending; Kalshi reported "
                        f"status={order.status} filled={filled}; "
                        "tagged killed_no_fill"
                    ),
                )
            )

    # ----- 2. Cross-check live rows against Kalshi positions --------
    try:
        positions = await kalshi.get_positions()
    except Exception as exc:
        log.error("reconcile_get_positions_failed", error=repr(exc))
        return ReconciliationReport(
            pending_count=len(pending_rows),
            live_count=len(live_rows),
            kalshi_position_count=0,
            drifts=drifts,
            succeeded=False,
        )

    pos_by_ticker: dict[str, KalshiPosition] = {p.ticker: p for p in positions}
    for row in live_rows:
        kpos = pos_by_ticker.get(row["ticker"])
        if kpos is None or kpos.position == 0:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE trades SET status = 'reconcile_orphaned' WHERE id = ?",
                    (row["id"],),
                )
            drifts.append(
                ReconciliationDrift(
                    kind="live_orphaned",
                    ticker=row["ticker"],
                    detail=(
                        f"trade {row['id']} marked live but Kalshi reports "
                        f"position=0; tagged reconcile_orphaned. Use "
                        f"/reconcile_resolve {row['id']} to acknowledge."
                    ),
                    severity="error",
                )
            )

    # ----- 3. Find Kalshi positions we don't know about -------------
    known_tickers = {row["ticker"] for row in live_rows}
    for kpos in positions:
        if kpos.position == 0 or kpos.ticker in known_tickers:
            continue
        # Skip if a previously-imported row already exists.
        if get_trade_by_kalshi_order_id(db, kpos.ticker) is not None:
            continue
        _insert_imported_position(db, kpos)
        drifts.append(
            ReconciliationDrift(
                kind="position_unknown",
                ticker=kpos.ticker,
                detail=(
                    f"Kalshi reports position={kpos.position} on {kpos.ticker} "
                    "but no local trade row; inserted as live_imported. Use "
                    f"/reconcile_resolve <trade_id> to acknowledge."
                ),
                severity="warning",
            )
        )

    # ----- 4. Audit row -------------------------------------------------
    insert_system_event(
        db,
        event_type="reconciliation_pass",
        severity="info" if not drifts else "warning",
        component="reconcile",
        message=(
            f"reconciliation: {len(pending_rows)} pending, {len(live_rows)} live, "
            f"{len(positions)} kalshi positions, {len(drifts)} drifts"
        ),
        detail={
            "pending_count": len(pending_rows),
            "live_count": len(live_rows),
            "kalshi_position_count": len(positions),
            "drifts": [{"kind": d.kind, "ticker": d.ticker, "detail": d.detail} for d in drifts],
        },
    )

    return ReconciliationReport(
        pending_count=len(pending_rows),
        live_count=len(live_rows),
        kalshi_position_count=len(positions),
        drifts=drifts,
        succeeded=True,
    )


def _insert_imported_position(db: Database, kpos: KalshiPosition) -> None:
    """Create a placeholder ``live_imported`` row for a Kalshi position
    we have no local record of.

    Since the trades table has FKs on ``triggering_match_id`` and
    ``risk_decision_id`` (both NOT NULL), we have to either disable
    FK checking around the insert OR create real placeholder ancillary
    rows. SQLite refuses ``PRAGMA foreign_keys=OFF`` inside an open
    transaction (silent no-op), so we go via auto-commit on the raw
    connection: turn FKs off, insert, turn back on.
    """
    intent_blob = json.dumps(
        {
            "synthetic": True,
            "reason": "reconciliation_imported",
            "kalshi_position": kpos.model_dump(),
        }
    )
    conn = db.connect()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(
            """
            INSERT INTO trades (
                ticker, side, action, status,
                entry_price_cents, quantity, cost_basis_usd_cents,
                triggering_match_id, triggering_intent_json,
                risk_decision_id, approval_id, is_reentry, prior_trade_id,
                reasoning_text, entered_at
            ) VALUES (
                ?, 'yes', 'buy', 'live_imported',
                0, ?, 0,
                0, ?,
                0, NULL, 0, NULL,
                'reconciliation: imported from Kalshi position', ?
            )
            """,
            (
                kpos.ticker,
                abs(kpos.position),
                intent_blob,
                _utcnow_iso(),
            ),
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _utcnow_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "ReconciliationDrift",
    "ReconciliationReport",
    "TradeInsertRow",
    "get_trade_by_client_order_id",
    "reconcile_once",
]
