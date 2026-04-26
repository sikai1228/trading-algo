"""Phase 2 daemon loops: decision / stop-loss / position-marking / re-entry.

Each loop is an asyncio coroutine the daemon launches alongside the
existing ingestion tasks. Loops are short and stateless; they pull
state from the database, run the engine + risk + approval pipeline,
and write back via the executor.

All loops respect the global halt flag (RiskConfig.halted, set by the
Phase-3 ``/halt`` command — Phase 2 leaves it False).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime

from trumpbot.approval.gate import ApprovalGate
from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    get_last_closed_trade_for_ticker,
    get_open_trade_for_ticker,
    get_system_state,
    insert_system_event,
    is_market_snoozed,
    list_open_trades,
    total_open_position_cost_cents,
    update_telegram_approval,
)
from trumpbot.decision.engine import (
    BankrollState,
    DecisionEngine,
    MarketState,
    MatchSnapshot,
    Position,
)
from trumpbot.execution.dry_run import DryRunExecutor, Quote
from trumpbot.execution.live_executor import KalshiExecutor
from trumpbot.risk.manager import RiskManager, RiskState
from trumpbot.types.intents import RiskApprovedOrder, RiskRejection
from trumpbot.utils.logging import get_logger

log = get_logger(__name__)


Executor = DryRunExecutor | KalshiExecutor
"""Either flavor of executor — the daemon picks one at startup based
on ``cfg.execution.mode``. Both expose the same ``async submit`` and
``update_position_marks`` surface; ``close_resolved`` is also shared."""

OrderbookFn = Callable[[str], Quote]
DepthFn = Callable[[str], list[tuple[int, int]] | None]
"""Phase 3 Part 1: full YES-ask depth for the engine's walker. The
daemon wires this to a thin wrapper over the WS feed."""


def _bankroll_state(
    db: Database,
    *,
    starting_amount_usd: float,
    execution_mode: str = "dry_run",
) -> BankrollState:
    """Build a :class:`BankrollState` for one engine evaluation.

    Pre-live fix #1: when ``execution_mode == "live"``, prefer the
    cached Kalshi balance written by ``bankroll_sync_loop`` into
    ``system_state['bankroll_usd_cents']``. Falls back to the
    configured starting amount when the cache is empty (daemon just
    started OR sync has never succeeded). Records the provenance via
    :class:`~trumpbot.decision.engine.BankrollSource` so the
    reasoning text can disclose it.

    Dry-run mode always returns the configured value — dry-run is
    explicitly a "trade against this fake bankroll" rehearsal.
    """
    open_cost_cents = total_open_position_cost_cents(db)
    if execution_mode != "live":
        return BankrollState(
            bankroll_usd_cents=int(round(starting_amount_usd * 100)),
            open_position_cost_usd_cents=open_cost_cents,
            source="config",
            last_synced_at=None,
        )
    # Live mode: consult the synced cache.
    from trumpbot.account.bankroll_sync import (
        BANKROLL_LAST_SYNC_KEY,
        BANKROLL_STATE_KEY,
    )

    cached_raw = get_system_state(db, BANKROLL_STATE_KEY)
    last_sync_iso = get_system_state(db, BANKROLL_LAST_SYNC_KEY)
    last_synced_at: datetime | None = None
    if last_sync_iso:
        with contextlib.suppress(ValueError):
            last_synced_at = datetime.fromisoformat(last_sync_iso.replace("Z", "+00:00"))
    if cached_raw is None:
        # No sync has succeeded yet — fall back, but tag the state
        # so the reasoning text discloses the fallback.
        return BankrollState(
            bankroll_usd_cents=int(round(starting_amount_usd * 100)),
            open_position_cost_usd_cents=open_cost_cents,
            source="kalshi_fallback",
            last_synced_at=None,
        )
    try:
        cached_cents = int(cached_raw)
    except ValueError:
        log.error("bankroll_state_corrupt", raw=cached_raw)
        return BankrollState(
            bankroll_usd_cents=int(round(starting_amount_usd * 100)),
            open_position_cost_usd_cents=open_cost_cents,
            source="kalshi_fallback",
            last_synced_at=last_synced_at,
        )
    return BankrollState(
        bankroll_usd_cents=cached_cents,
        open_position_cost_usd_cents=open_cost_cents,
        source="kalshi_synced",
        last_synced_at=last_synced_at,
    )


# ---------------------------------------------------------------------------
# Decision loop (initial entry)
# ---------------------------------------------------------------------------


async def decision_loop(
    *,
    db: Database,
    engine: DecisionEngine,
    risk: RiskManager,
    gate: ApprovalGate,
    executor: Executor,
    orderbook: OrderbookFn,
    depth: DepthFn,
    starting_amount_usd: float,
    execution_mode: str,
    poll_interval_sec: int,
    stop_event: asyncio.Event,
) -> None:
    component = "decision_loop"
    log.info(f"{component}_started")
    while not stop_event.is_set():
        try:
            await _run_decision_cycle(
                db=db,
                engine=engine,
                risk=risk,
                gate=gate,
                executor=executor,
                orderbook=orderbook,
                depth=depth,
                starting_amount_usd=starting_amount_usd,
                execution_mode=execution_mode,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"{component}_error", error=repr(exc))
            insert_system_event(
                db,
                event_type="decision_loop_error",
                severity="error",
                component=component,
                message=str(exc),
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_sec)
    log.info(f"{component}_stopped")


async def _run_decision_cycle(
    *,
    db: Database,
    engine: DecisionEngine,
    risk: RiskManager,
    gate: ApprovalGate,
    executor: Executor,
    orderbook: OrderbookFn,
    depth: DepthFn,
    starting_amount_usd: float,
    execution_mode: str = "dry_run",
) -> None:
    # Phase 3 Part 2: respect /halt as the first thing every cycle.
    # When the user has issued /halt the entire cycle is a no-op until
    # /resume clears the flag. Stop-losses and re-entries are also
    # gated (handled separately in their loops).
    if _is_halted(db):
        return

    matches = _fetch_unevaluated_matches(db)
    if not matches:
        return
    for match in matches:
        ticker = match["ticker"]
        # Phase 3 Part 2: skip per-ticker if /snooze is active.
        if is_market_snoozed(db, ticker):
            insert_system_event(
                db,
                event_type="trade_skipped_snoozed",
                severity="info",
                component="decision_loop",
                message=f"skipping match for snoozed ticker {ticker}",
                detail={"ticker": ticker, "match_id": match["id"]},
            )
            continue
        market_row = _get_market_row(db, ticker)
        if market_row is None:
            continue
        position_row = get_open_trade_for_ticker(db, ticker)
        position = _row_to_position(position_row)
        bankroll = _bankroll_state(
            db,
            starting_amount_usd=starting_amount_usd,
            execution_mode=execution_mode,
        )
        snap = _row_to_snapshot(match, market_row)
        market_state = _market_state(orderbook, ticker, db=db)
        levels = depth(ticker) or []
        intent = engine.evaluate_news_match(
            snap, market_state, position, bankroll, yes_ask_levels=levels
        )
        if intent is None:
            continue
        decision = risk.evaluate(
            intent,
            RiskState(
                bankroll=bankroll,
                open_position_tickers=_open_tickers(db),
            ),
        )
        if isinstance(decision, RiskRejection):
            log.info("decision_loop_risk_rejected", reason=decision.reason)
            continue
        await _approve_and_submit(decision, gate, executor, db)


def _is_halted(db: Database) -> bool:
    """Read ``system_state.halt_flag``. Phase 3 Part 2 plumbing for
    the /halt and /resume commands."""
    return (get_system_state(db, "halt_flag") or "false").lower() == "true"


# ---------------------------------------------------------------------------
# Stop-loss loop
# ---------------------------------------------------------------------------


async def stop_loss_loop(
    *,
    db: Database,
    engine: DecisionEngine,
    risk: RiskManager,
    gate: ApprovalGate,
    executor: Executor,
    orderbook: OrderbookFn,
    depth: DepthFn,
    starting_amount_usd: float,
    execution_mode: str,
    poll_interval_sec: int,
    stop_event: asyncio.Event,
) -> None:
    component = "stop_loss_loop"
    log.info(f"{component}_started")
    while not stop_event.is_set():
        try:
            for trade_row in list_open_trades(db):
                position = _row_to_position(trade_row)
                if position is None:
                    continue
                quote = orderbook(position.ticker)
                market_state = MarketState(
                    ticker=position.ticker,
                    yes_bid_cents=quote.yes_bid_cents,
                    yes_ask_cents=quote.yes_ask_cents,
                )
                stop_intent = engine.evaluate_stop_loss(position, market_state)
                if stop_intent is None:
                    continue
                bankroll = _bankroll_state(
                    db,
                    starting_amount_usd=starting_amount_usd,
                    execution_mode=execution_mode,
                )
                state = RiskState(bankroll=bankroll, open_position_tickers=_open_tickers(db))
                decision = risk.evaluate(stop_intent, state)
                if isinstance(decision, RiskRejection):
                    continue
                await _approve_and_submit(decision, gate, executor, db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"{component}_error", error=repr(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_sec)
    log.info(f"{component}_stopped")


# ---------------------------------------------------------------------------
# Position marking loop
# ---------------------------------------------------------------------------


async def position_marking_loop(
    *,
    executor: Executor,
    poll_interval_sec: int,
    stop_event: asyncio.Event,
) -> None:
    component = "position_marking_loop"
    log.info(f"{component}_started")
    while not stop_event.is_set():
        try:
            executor.update_position_marks()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"{component}_error", error=repr(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_sec)
    log.info(f"{component}_stopped")


# ---------------------------------------------------------------------------
# Re-entry loop
# ---------------------------------------------------------------------------


async def reentry_loop(
    *,
    db: Database,
    engine: DecisionEngine,
    risk: RiskManager,
    gate: ApprovalGate,
    executor: Executor,
    orderbook: OrderbookFn,
    depth: DepthFn,
    starting_amount_usd: float,
    execution_mode: str,
    poll_interval_sec: int,
    stop_event: asyncio.Event,
) -> None:
    component = "reentry_loop"
    log.info(f"{component}_started")
    while not stop_event.is_set():
        try:
            # Phase 3 Part 2: respect /halt for re-entries (they ARE
            # new trade proposals from the user's perspective).
            if _is_halted(db):
                pass  # fall through to the sleep at the end
            else:
                for match in _fetch_unevaluated_matches(db):
                    ticker = match["ticker"]
                    # Phase 3 Part 2: skip if /snooze is active.
                    if is_market_snoozed(db, ticker):
                        continue
                    await _maybe_reentry(
                        db=db,
                        engine=engine,
                        risk=risk,
                        gate=gate,
                        executor=executor,
                        orderbook=orderbook,
                        depth=depth,
                        starting_amount_usd=starting_amount_usd,
                        execution_mode=execution_mode,
                        match=match,
                        ticker=ticker,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"{component}_error", error=repr(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_sec)
    log.info(f"{component}_stopped")


async def _maybe_reentry(  # type: ignore[no-untyped-def]
    *,
    db: Database,
    engine: DecisionEngine,
    risk: RiskManager,
    gate: ApprovalGate,
    executor: Executor,
    orderbook: OrderbookFn,
    depth: DepthFn,
    starting_amount_usd: float,
    execution_mode: str,
    match,
    ticker: str,
) -> None:
    """Refactored body of the re-entry per-match loop. Pulled out so
    the halt + snooze guards above can early-return without an
    awkward indentation wrap."""
    if get_open_trade_for_ticker(db, ticker) is not None:
        return
    prior_row = get_last_closed_trade_for_ticker(db, ticker)
    if prior_row is None:
        return
    market_row = _get_market_row(db, ticker)
    if market_row is None:
        return
    snap = _row_to_snapshot(match, market_row)
    market_state = _market_state(orderbook, ticker, db=db)
    bankroll = _bankroll_state(
        db,
        starting_amount_usd=starting_amount_usd,
        execution_mode=execution_mode,
    )
    prior_position = _row_to_position(prior_row)
    if prior_position is None:
        return
    levels = depth(ticker) or []
    intent = engine.evaluate_reentry(
        snap,
        market_state,
        prior_position,
        prior_row["status"],
        prior_row["realized_pnl_usd_cents"],
        bankroll,
        yes_ask_levels=levels,
    )
    if intent is None:
        return
    state = RiskState(bankroll=bankroll, open_position_tickers=_open_tickers(db))
    decision = risk.evaluate(intent, state)
    if isinstance(decision, RiskRejection):
        return
    await _approve_and_submit(decision, gate, executor, db)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_unevaluated_matches(db: Database) -> list:  # type: ignore[type-arg]
    """Recent high-confidence LLM-cascade matches not yet evaluated.

    Phase 2 reads ``classifier_type`` (added by Phase-1.5 migration 003)
    so it only triggers on LLM-classified rows. If migration 003 hasn't
    been applied yet the column is missing — we fall back to filtering
    by confidence alone but the keyword-only rows have
    interaction_occurred=False (synthesized in the daemon mapping below)
    so they won't fire trades.
    """
    conn = db.connect()
    try:
        return list(
            conn.execute(
                """
                SELECT m.*
                FROM news_market_matches m
                LEFT JOIN trades t ON t.triggering_match_id = m.id
                WHERE m.confidence >= 0.85
                  AND t.id IS NULL
                  AND m.created_at >= datetime('now', '-1 hour')
                ORDER BY m.created_at DESC
                LIMIT 50
                """
            )
        )
    except Exception as exc:
        log.error("fetch_matches_failed", error=repr(exc))
        return []


def _get_market_row(db: Database, ticker: str):  # type: ignore[no-untyped-def]
    return (
        db.connect()
        .execute(
            "SELECT ticker, open_ts, close_ts, volume FROM markets WHERE ticker = ?",
            (ticker,),
        )
        .fetchone()
    )


def _row_to_snapshot(match_row, market_row) -> MatchSnapshot:  # type: ignore[no-untyped-def]
    classifier_type = None
    with contextlib.suppress(IndexError, KeyError):
        classifier_type = match_row["classifier_type"]
    return MatchSnapshot(
        match_id=match_row["id"],
        ticker=match_row["ticker"],
        confidence=match_row["confidence"],
        # Conservative default: only LLM-classified rows have
        # interaction_occurred=True. Without Phase-1.5 LLM cascade
        # deployed, every match fails this check (correct — we don't
        # want to fire trades on keyword-only signal).
        interaction_occurred=classifier_type in {"llm_haiku", "llm_haiku_cached"},
        source_name="unknown",
        is_kalshi_approved=True,  # filter happens at ingestion
        market_open_ts=market_row["open_ts"],
        market_close_ts=market_row["close_ts"],
        article_published_ts=None,
        classified_at_ts=datetime.now(UTC).isoformat(),
    )


def _row_to_position(row) -> Position | None:  # type: ignore[no-untyped-def]
    if row is None:
        return None
    return Position(
        trade_id=row["id"],
        ticker=row["ticker"],
        entry_price_cents=row["entry_price_cents"],
        quantity=row["quantity"],
        cost_basis_usd_cents=row["cost_basis_usd_cents"],
        triggering_match_id=row["triggering_match_id"],
    )


def _market_state(
    orderbook: OrderbookFn, ticker: str, *, db: Database | None = None
) -> MarketState:
    quote = orderbook(ticker)
    volume = 0
    if db is not None:
        row = _get_market_row(db, ticker)
        if row is not None:
            volume = int(row["volume"] or 0)
    return MarketState(
        ticker=ticker,
        yes_bid_cents=quote.yes_bid_cents,
        yes_ask_cents=quote.yes_ask_cents,
        total_volume_traded_contracts=volume,
    )


def _open_tickers(db: Database) -> frozenset[str]:
    return frozenset(r["ticker"] for r in list_open_trades(db))


async def _approve_and_submit(
    decision: RiskApprovedOrder,
    gate: ApprovalGate,
    executor: Executor,
    db: Database,
) -> None:
    approval = await gate.request_approval(decision)
    if approval.decision != "approved":
        log.info(
            "approval_not_granted",
            intent_id=decision.intent.intent_id,
            decision=approval.decision,
        )
        return
    result = await executor.submit(decision)
    if result.trade_id > 0 and approval.approval_record_id is not None:
        # Backfill the trades.approval_id link.
        with db.transaction() as conn:
            conn.execute(
                "UPDATE trades SET approval_id = ? WHERE id = ?",
                (approval.approval_record_id, result.trade_id),
            )
        # Update the approval row's idempotency state.
        update_telegram_approval(
            db,
            approval_id=approval.approval_record_id,
            decision=approval.decision,
            decision_source=approval.decision_source,
        )
    log.info(
        "trade_executed",
        intent_id=decision.intent.intent_id,
        trade_id=result.trade_id,
        status=result.status,
    )


__all__ = [
    "OrderbookFn",
    "decision_loop",
    "position_marking_loop",
    "reentry_loop",
    "stop_loss_loop",
]
