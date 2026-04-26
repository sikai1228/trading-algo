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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from trumpbot.approval.gate import ApprovalGate
from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    get_last_closed_trade_for_ticker,
    get_open_trade_for_ticker,
    get_system_state,
    insert_system_event,
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

# Phase 4 Part 2.11 — auto-approval Telegram confirmation hook. The
# decision_loop calls this AFTER the executor finishes for any entry
# whose approval was sourced='auto_approval'. Daemon wires it to
# ``send_text``; tests pass None to skip notifications.
AutoNotifyFn = Callable[[str, dict[str, object]], Awaitable[None]]


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
    auto_notify: AutoNotifyFn | None = None,
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
                auto_notify=auto_notify,
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
    auto_notify: AutoNotifyFn | None = None,
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
        await _approve_and_submit(decision, gate, executor, db, auto_notify=auto_notify)


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
    the halt guard above can early-return without an awkward
    indentation wrap."""
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

    Phase 4 Part 2.8 — joins ``llm_classifications`` so the snapshot
    builder sees ``parsed_interaction_occurred`` directly. The decision
    engine still reads ``confidence`` and ``interaction_occurred`` off
    the snapshot; that's what gates a trade.
    """
    conn = db.connect()
    try:
        return list(
            conn.execute(
                """
                SELECT m.*,
                       c.parsed_interaction_occurred AS parsed_interaction_occurred,
                       c.parsed_subject AS parsed_subject,
                       c.parsed_confidence AS parsed_confidence,
                       c.parsed_key_quote AS parsed_key_quote,
                       n.source AS news_source,
                       n.headline AS news_headline,
                       n.url_canonical AS news_url_canonical,
                       n.url AS news_url,
                       n.raw_published_ts AS news_published_ts
                FROM news_market_matches m
                LEFT JOIN trades t ON t.triggering_match_id = m.id
                LEFT JOIN llm_classifications c ON c.id = m.llm_classification_id
                LEFT JOIN news_events n ON n.id = m.news_event_id
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
    """Project a ``news_market_matches`` row (joined with
    ``llm_classifications``) into a :class:`MatchSnapshot` for the
    decision engine.

    Phase 4 Part 2.9 cleanup — the defensive try/except suppressing
    missing ``classifier_type`` and ``parsed_interaction_occurred``
    columns is gone. Migration 011 added both, and
    ``_fetch_unevaluated_matches`` always JOINs ``llm_classifications``
    so they're always present in the row.

    Logic:

    - ``classifier_type == 'llm_cascade'``: read
      ``parsed_interaction_occurred`` (0/1). True iff truthy.
    - Anything else (``keyword_only`` — cap-hit, LLM disabled, or
      pre-classification backlog): ``False``. Keyword-only matches
      must never fire trades.
    """
    classifier_type = match_row["classifier_type"]
    parsed_interaction_raw = match_row["parsed_interaction_occurred"]

    interaction_occurred = classifier_type == "llm_cascade" and bool(parsed_interaction_raw)

    # Phase 4 Part 2.11 — pull article-context fields off the joined
    # news_events + llm_classifications rows. Defensive ``or ""`` so
    # NULL columns (legacy backlog rows) don't blow up Pydantic's
    # required-string validators downstream.
    article_url = (
        _safe_row_get(match_row, "news_url_canonical") or _safe_row_get(match_row, "news_url") or ""
    )
    article_headline = _safe_row_get(match_row, "news_headline") or ""
    article_key_quote = _safe_row_get(match_row, "parsed_key_quote") or ""
    article_published_ts = _safe_row_get(match_row, "news_published_ts")
    source_name = _safe_row_get(match_row, "news_source") or "unknown"

    return MatchSnapshot(
        match_id=match_row["id"],
        ticker=match_row["ticker"],
        confidence=match_row["confidence"],
        interaction_occurred=interaction_occurred,
        source_name=source_name,
        is_kalshi_approved=True,  # filter happens at ingestion
        market_open_ts=market_row["open_ts"],
        market_close_ts=market_row["close_ts"],
        article_published_ts=article_published_ts,
        classified_at_ts=datetime.now(UTC).isoformat(),
        article_url=article_url,
        article_headline=article_headline,
        article_key_quote=article_key_quote,
    )


def _safe_row_get(row, key: str):  # type: ignore[no-untyped-def]
    """Return ``row[key]`` if present, else ``None``. Tolerates legacy
    backlog rows that pre-date the Phase 4 Part 2.11 query columns."""
    with contextlib.suppress(IndexError, KeyError):
        return row[key]
    return None


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
    auto_notify: AutoNotifyFn | None = None,
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

    # Phase 4 Part 2.11 — auto-approval confirmation. Only fire for
    # entry intents whose approval source was 'auto_approval'. The
    # human path already shows the operator everything via the
    # original approval message.
    if auto_notify is None or approval.decision_source != "auto_approval":
        return
    with contextlib.suppress(Exception):
        await _send_auto_confirmation(decision, result, auto_notify)


async def _send_auto_confirmation(  # type: ignore[no-untyped-def]
    decision: RiskApprovedOrder,
    result,
    auto_notify: AutoNotifyFn,
) -> None:
    """Render the appropriate ``trade_filled_auto`` /
    ``trade_killed_auto`` template and dispatch via the notifier.
    Best-effort; failures are swallowed (an auto-approval message
    being lost should never block the next cycle)."""
    from trumpbot.notifications.trade_render import (
        article_link_markdown,
        compute_potential_loss_cents,
        compute_settlement_pnl,
        dollars,
        dollars_signed,
        format_et_short,
        humanize_age_since,
        now_et_long,
        percent_from_bps,
        render_key_quote,
    )

    intent = decision.intent
    ticker = getattr(intent, "ticker", "?")
    if result.status == "filled" and result.trade_id > 0:
        qty = int(result.fill_quantity or 0)
        avg = int(result.fill_price_cents or 0)
        cost_cents = qty * avg
        entry_fees = int(getattr(intent, "estimated_fees_cents", 0) or 0)
        slippage = int(getattr(intent, "slippage_cents", 0) or 0)
        best_ask = max(0, avg - slippage)
        settlement, _exit_fees, profit, roi_bps = compute_settlement_pnl(
            quantity=qty, cost_basis_cents=cost_cents, entry_fees_cents=entry_fees
        )
        loss = compute_potential_loss_cents(
            quantity=qty,
            cost_basis_cents=cost_cents,
            entry_fees_cents=entry_fees,
            entry_price_cents=avg,
            yes_bid_levels=None,
        )
        published_ts = getattr(intent, "triggering_published_ts", "") or ""
        age = humanize_age_since(published_ts) if published_ts else "unknown"
        article_age_note = (
            f" (published {age} ago)" if published_ts and age not in {"unknown", "just now"} else ""
        )
        await auto_notify(
            "trade_filled_auto",
            {
                "trade_id": result.trade_id,
                "timestamp_et": now_et_long(),
                "signal_to_trade_age": age if published_ts else "unknown",
                "market_title": getattr(intent, "triggering_headline", "") or f"(market {ticker})",
                "ticker": ticker,
                "subject_full_name": getattr(intent, "triggering_source", "") or "—",
                "actual_fill_price": avg,
                "filled_quantity": qty,
                "actual_cost": dollars(cost_cents),
                "actual_fees": dollars(entry_fees),
                "actual_slippage": slippage,
                "best_ask_at_send": best_ask,
                "total_spent": dollars(cost_cents + entry_fees),
                "settlement_value": dollars(settlement),
                "potential_profit": dollars_signed(profit),
                "potential_roi": percent_from_bps(roi_bps),
                "potential_loss": dollars(loss),
                "source": getattr(intent, "triggering_source", "") or "(unknown)",
                "published_time_et": (format_et_short(published_ts) if published_ts else "unknown"),
                "article_age_note": article_age_note,
                "headline": getattr(intent, "triggering_headline", "") or "(no headline)",
                "key_quote": render_key_quote(getattr(intent, "triggering_key_quote", "")),
                "article_url": article_link_markdown(getattr(intent, "triggering_article_url", "")),
            },
        )
        return

    # Anything else = killed. ExecutionResult.notes carries the kind.
    kind = "killed"
    notes = result.notes or ""
    if "FOK killed" in notes:
        kind = "killed_book_moved"
    elif "no ask available" in notes or "no_fill" in notes:
        kind = "killed_no_fill"
    elif result.status == "rejected":
        kind = result.notes.split(":")[0] if ":" in (result.notes or "") else "rejected"
    await auto_notify(
        "trade_killed_auto",
        {
            "intent_id_short": intent.intent_id.split("-")[0],
            "timestamp_et": now_et_long(),
            "market_title": getattr(intent, "triggering_headline", "") or f"(market {ticker})",
            "ticker": ticker,
            "kill_reason": notes or "no reason recorded",
            "kill_kind": kind,
            "target_quantity": getattr(intent, "target_quantity", "?"),
            "target_avg_fill": getattr(intent, "target_avg_fill_price_cents", 0),
            "source": getattr(intent, "triggering_source", "") or "(unknown)",
            "article_url": article_link_markdown(getattr(intent, "triggering_article_url", "")),
        },
    )


__all__ = [
    "OrderbookFn",
    "decision_loop",
    "position_marking_loop",
    "reentry_loop",
    "stop_loss_loop",
]
