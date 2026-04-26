"""Replay historical news_market_matches + price_snapshots through the
SAME DecisionEngine + RiskManager that runs in production.

Critical invariant: this module imports those classes directly. There
is no parallel "backtest engine"; the only place a strategy rule lives
is in :class:`DecisionEngine` and :class:`RiskManager`. If a rule
changes, the backtest result changes accordingly.

Phase 3 Part 1 added the **walk-aware, fee-aware** path:

- The backtester now goes through the production walker
  (:func:`walk_orderbook_for_buy`) and the production fee model
  (:func:`calculate_entry_fee_cents`). Fees are deducted from the
  realized P&L on every trade.
- ``markets.volume`` flows through to the engine so cap_two engages
  on thin markets exactly as it does in production.

**Caveat (documented):** ``price_snapshots`` only stores top-of-book
bid/ask, so the backtester synthesizes a single-level book at the
historical ask. That means the walker always fills entirely at top of
book and slippage is zero. This *under*-states slippage relative to
real Kalshi books (which are typically 3-5 levels deep). Fees alone
still produce the documented v2-vs-v1 delta. A future Phase will add
an ``orderbook_snapshots`` table and let the backtester replay
realistic depth.

Phase 4 will add live execution. Backtests run through the same
engine + risk + walker code so historical replays remain meaningful.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trumpbot.decision.engine import (
    BankrollState,
    DecisionConfig,
    DecisionEngine,
    MarketState,
    MatchSnapshot,
    Position,
)
from trumpbot.execution.fees import (
    calculate_exit_fee_cents,
)
from trumpbot.risk.manager import RiskConfig, RiskManager, RiskState
from trumpbot.types.intents import RiskRejection, StopLossIntent


@dataclass
class BacktestTrade:
    ticker: str
    entered_at: str
    entry_price_cents: int
    quantity: int
    exit_at: str | None = None
    exit_price_cents: int | None = None
    realized_pnl_usd_cents: int = 0
    triggering_match_id: int = 0
    triggering_subject: str | None = None
    """Subject key the match resolved to (e.g. 'putin'). Powers
    by_subject_breakdown."""

    triggering_source: str | None = None
    """Name of the news source (e.g. 'reuters', 'ap_via_gnews').
    Powers by_source_breakdown."""

    # Phase 3 Part 1 — fee + slippage audit per trade.
    entry_fees_cents: int = 0
    exit_fees_cents: int = 0
    slippage_cents: int = 0
    cap_binding: str = "unknown"


@dataclass
class BacktestResult:
    start_ts: str
    end_ts: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_realized_pnl_usd_cents: int = 0
    total_unrealized_pnl_usd_cents: int = 0
    average_entry_price_cents: int = 0
    average_exit_price_cents: int = 0
    average_hold_time_hours: float = 0.0
    sharpe_ratio: float = 0.0
    """Annualized Sharpe of the daily P&L series, rf=0. Returns 0.0
    when there is no variance (degenerate case)."""

    max_drawdown_usd_cents: int = 0
    """Worst peak-to-trough decline in the running equity curve, in
    USDCents. Always non-negative."""

    risk_rejections: int = 0
    """Number of intents the risk manager rejected during the run."""

    # ---- Phase 3 Part 1 — fee/slippage audit (v2 metrics) ----
    schema_version: int = 2
    """Bumped from 1 -> 2 in Phase 3 Part 1 when the backtester became
    walk-aware and fee-aware. Old v1 backtest results are no longer
    comparable to v2 — re-run them."""

    total_entry_fees_cents: int = 0
    total_exit_fees_cents: int = 0
    total_slippage_cents: int = 0
    """Sum of (avg fill - best ask) x qty across all entries.
    Always 0 in v2 because we synthesize a single-level book from
    historical top-of-book snapshots; will be > 0 once the future
    ``orderbook_snapshots`` table is available."""

    ideal_realized_pnl_usd_cents: int = 0
    """What ``total_realized_pnl_usd_cents`` would be with zero fees
    and zero slippage — i.e. the v1 (Phase 2) metric. Subtract this
    from the v2 realized P&L to see the dollar cost of slippage + fees
    on the strategy's edge."""
    # ----------------------------------------------------------

    trade_log: list[BacktestTrade] = field(default_factory=list)
    by_subject: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_source: dict[str, dict[str, Any]] = field(default_factory=dict)


class Backtester:
    """Single-pass replay over the database's historical rows."""

    def __init__(
        self,
        *,
        db_path: Path | str,
        config: DecisionConfig | None = None,
        starting_bankroll_usd: float = 500.0,
        risk_config: RiskConfig | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._cfg = config or DecisionConfig()
        # Mirror the engine config into a RiskConfig with the same caps
        # so production and backtest share the same numbers.
        self._risk_cfg = risk_config or RiskConfig(
            enabled=True,
            max_buy_price_cents=self._cfg.max_buy_price_cents,
            position_size_hard_cap_cents=self._cfg.position_size_hard_cap_cents,
            halted=False,
        )
        self._engine = DecisionEngine(self._cfg)
        # db=None -> RiskManager runs the in-memory check chain but does
        # NOT persist risk_decisions; backtests must not pollute the
        # production audit table.
        self._risk = RiskManager(db=None, config=self._risk_cfg)
        self._bankroll_cents = int(round(starting_bankroll_usd * 100))

    def run(self, *, start_ts: str, end_ts: str) -> BacktestResult:
        result = BacktestResult(start_ts=start_ts, end_ts=end_ts)
        open_positions: dict[str, BacktestTrade] = {}
        next_trade_id = 1
        with self._read() as conn:
            matches = list(self._fetch_matches(conn, start_ts, end_ts))
            for row in matches:
                ticker = row["ticker"]
                market_row = self._market(conn, ticker)
                snap = self._snapshot(row, market_row)
                quote = self._closest_quote(conn, ticker, row["created_at"])
                if quote is None:
                    continue
                # Phase 3: thread market.volume into the engine for
                # cap_two computation. Default 0 if missing.
                volume = (market_row["volume"] if market_row else None) or 0
                market_state = MarketState(
                    ticker=ticker,
                    yes_bid_cents=quote[0],
                    yes_ask_cents=quote[1],
                    total_volume_traded_contracts=int(volume),
                )
                if ticker in open_positions:
                    # Check stop-loss first.
                    pos_trade = open_positions[ticker]
                    pos = Position(
                        trade_id=0,
                        ticker=ticker,
                        entry_price_cents=pos_trade.entry_price_cents,
                        quantity=pos_trade.quantity,
                        cost_basis_usd_cents=pos_trade.entry_price_cents * pos_trade.quantity,
                        triggering_match_id=pos_trade.triggering_match_id,
                    )
                    stop = self._engine.evaluate_stop_loss(pos, market_state)
                    if stop is not None:
                        self._close_at_bid(open_positions, ticker, stop, row["created_at"], result)
                    continue

                bankroll = BankrollState(
                    bankroll_usd_cents=self._bankroll_cents,
                    open_position_cost_usd_cents=sum(
                        t.entry_price_cents * t.quantity for t in open_positions.values()
                    ),
                )
                # Phase 3 walk-aware path: synthesize a single-level
                # book at the historical ask. (See module docstring for
                # the slippage caveat.)
                yes_ask_levels: list[tuple[int, int]] = (
                    [(quote[1], 10_000)] if quote[1] is not None else []
                )
                intent = self._engine.evaluate_news_match(
                    snap,
                    market_state,
                    None,
                    bankroll,
                    yes_ask_levels=yes_ask_levels,
                )
                if intent is None:
                    continue
                # Run the production RiskManager.
                risk_state = RiskState(
                    bankroll=bankroll,
                    open_position_tickers=frozenset(open_positions.keys()),
                )
                approved = self._risk.evaluate(intent, risk_state)
                if isinstance(approved, RiskRejection):
                    result.risk_rejections += 1
                    continue
                actual_qty = approved.adjusted_quantity or intent.target_quantity
                # The engine's walk already computed the avg fill price
                # and entry fees. Persist them so v2 metrics work.
                entry_price = intent.target_avg_fill_price_cents or (quote[1] or 0)
                trade = BacktestTrade(
                    ticker=ticker,
                    entered_at=row["created_at"],
                    entry_price_cents=entry_price,
                    quantity=actual_qty,
                    triggering_match_id=intent.triggering_match_id,
                    triggering_subject=row["matched_subject"],
                    triggering_source=_row_source_name(row),
                    entry_fees_cents=intent.estimated_fees_cents,
                    slippage_cents=intent.slippage_cents,
                    cap_binding=intent.cap_binding,
                )
                open_positions[ticker] = trade
                next_trade_id += 1

            # Close any remaining open positions at market resolution if known.
            # Phase 3: net the exit fees off realized P&L for v2 metrics.
            for ticker, trade in list(open_positions.items()):
                resolution = self._market_resolution(conn, ticker)
                if resolution is not None:
                    payoff = 100 if resolution == "settled_yes" else 0
                    gross = (payoff - trade.entry_price_cents) * trade.quantity
                    exit_fees = calculate_exit_fee_cents(payoff, trade.quantity)
                    trade.exit_price_cents = payoff
                    trade.exit_at = end_ts
                    # Realized P&L = gross - entry fees - exit fees.
                    trade.exit_fees_cents = exit_fees
                    trade.realized_pnl_usd_cents = gross - trade.entry_fees_cents - exit_fees
                    result.trade_log.append(trade)
                    open_positions.pop(ticker)
                else:
                    # Still open at end of window. Mark to last known bid.
                    quote = self._closest_quote(conn, ticker, end_ts)
                    if quote and quote[0]:
                        result.total_unrealized_pnl_usd_cents += (
                            quote[0] - trade.entry_price_cents
                        ) * trade.quantity
                    result.trade_log.append(trade)

        self._aggregate(result)
        return result

    # -- DB plumbing ---------------------------------------------------

    def _read(self) -> sqlite3.Connection:
        uri = f"file:{self._db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _fetch_matches(
        self, conn: sqlite3.Connection, start_ts: str, end_ts: str
    ) -> Iterable[sqlite3.Row]:
        # JOIN to news_events so by_source_breakdown can be populated.
        return conn.execute(
            """
            SELECT m.*, e.source AS source_name, e.is_kalshi_approved AS is_kalshi_approved
            FROM news_market_matches m
            LEFT JOIN news_events e ON e.id = m.news_event_id
            WHERE m.confidence >= ?
              AND m.created_at >= ?
              AND m.created_at <= ?
            ORDER BY m.created_at ASC
            """,
            (self._cfg.llm_confidence_threshold, start_ts, end_ts),
        )

    def _market(self, conn: sqlite3.Connection, ticker: str):  # type: ignore[no-untyped-def]
        return conn.execute(
            "SELECT ticker, open_ts, close_ts, status, volume FROM markets WHERE ticker = ?",
            (ticker,),
        ).fetchone()

    def _market_resolution(self, conn: sqlite3.Connection, ticker: str) -> str | None:
        row = conn.execute("SELECT status FROM markets WHERE ticker = ?", (ticker,)).fetchone()
        if row is None:
            return None
        if row["status"] in {"settled_yes", "finalized_yes"}:
            return "settled_yes"
        if row["status"] in {"settled_no", "finalized_no"}:
            return "settled_no"
        return None

    def _closest_quote(
        self, conn: sqlite3.Connection, ticker: str, when_ts: str
    ) -> tuple[int | None, int | None] | None:
        row = conn.execute(
            """
            SELECT yes_bid_cents, yes_ask_cents
            FROM price_snapshots
            WHERE ticker = ? AND ts <= ?
            ORDER BY ts DESC LIMIT 1
            """,
            (ticker, when_ts),
        ).fetchone()
        if row is None:
            return None
        return (row["yes_bid_cents"], row["yes_ask_cents"])

    def _snapshot(self, match_row, market_row) -> MatchSnapshot:  # type: ignore[no-untyped-def]
        # Backtest treats every replayed match as LLM-confirmed; in
        # production the daemon is the gatekeeper for this flag.
        # Use classified_at_ts as the effective article timestamp so
        # the engine's window check has something concrete to compare.
        return MatchSnapshot(
            match_id=match_row["id"],
            ticker=match_row["ticker"],
            confidence=match_row["confidence"],
            interaction_occurred=True,
            source_name="backtest",
            source_weight=1.0,
            is_kalshi_approved=True,
            market_open_ts=market_row["open_ts"] if market_row else None,
            market_close_ts=market_row["close_ts"] if market_row else None,
            article_published_ts=match_row["created_at"],
            classified_at_ts=match_row["created_at"],
        )

    # -- aggregation --------------------------------------------------

    def _close_at_bid(
        self,
        positions: dict[str, BacktestTrade],
        ticker: str,
        stop: StopLossIntent,
        when_ts: str,
        result: BacktestResult,
    ) -> None:
        """Close at the current bid. Phase 3 nets exit fees off the
        realized P&L."""
        trade = positions.pop(ticker)
        gross = (stop.current_bid_cents - trade.entry_price_cents) * trade.quantity
        exit_fees = calculate_exit_fee_cents(stop.current_bid_cents, trade.quantity)
        trade.exit_price_cents = stop.current_bid_cents
        trade.exit_at = when_ts
        trade.exit_fees_cents = exit_fees
        # Realized = gross - entry fees - exit fees.
        trade.realized_pnl_usd_cents = gross - trade.entry_fees_cents - exit_fees
        result.trade_log.append(trade)

    def _aggregate(self, result: BacktestResult) -> None:
        result.total_trades = len(result.trade_log)
        wins = [t for t in result.trade_log if t.realized_pnl_usd_cents > 0]
        losses = [t for t in result.trade_log if t.realized_pnl_usd_cents < 0]
        result.winning_trades = len(wins)
        result.losing_trades = len(losses)
        if result.total_trades > 0:
            result.win_rate = result.winning_trades / result.total_trades
            result.total_realized_pnl_usd_cents = sum(
                t.realized_pnl_usd_cents for t in result.trade_log
            )
            result.average_entry_price_cents = int(
                round(sum(t.entry_price_cents for t in result.trade_log) / result.total_trades)
            )
            exited = [t for t in result.trade_log if t.exit_price_cents is not None]
            if exited:
                result.average_exit_price_cents = int(
                    round(sum(t.exit_price_cents or 0 for t in exited) / len(exited))
                )
                hold_hours: list[float] = []
                for t in exited:
                    if t.exit_at and t.entered_at:
                        try:
                            entered = datetime.fromisoformat(t.entered_at.replace("Z", "+00:00"))
                            exited_ts = datetime.fromisoformat(t.exit_at.replace("Z", "+00:00"))
                            hold_hours.append((exited_ts - entered).total_seconds() / 3600.0)
                        except ValueError:
                            continue
                if hold_hours:
                    result.average_hold_time_hours = sum(hold_hours) / len(hold_hours)

        # ---- by-subject + by-source breakdowns ----
        by_subject: dict[str, dict[str, int]] = defaultdict(
            lambda: {"trades": 0, "realized_pnl_usd_cents": 0}
        )
        by_source: dict[str, dict[str, int]] = defaultdict(
            lambda: {"trades": 0, "realized_pnl_usd_cents": 0}
        )
        for t in result.trade_log:
            subject_key = t.triggering_subject or "unknown"
            by_subject[subject_key]["trades"] += 1
            by_subject[subject_key]["realized_pnl_usd_cents"] += t.realized_pnl_usd_cents
            source_key = t.triggering_source or "unknown"
            by_source[source_key]["trades"] += 1
            by_source[source_key]["realized_pnl_usd_cents"] += t.realized_pnl_usd_cents
        result.by_subject = dict(by_subject)
        result.by_source = dict(by_source)

        # ---- Sharpe + max drawdown over daily P&L ----
        result.sharpe_ratio = _annualized_sharpe(result.trade_log)
        result.max_drawdown_usd_cents = _max_drawdown_usd_cents(result.trade_log)

        # ---- Phase 3 v2 metrics: fee + slippage totals ----
        result.total_entry_fees_cents = sum(t.entry_fees_cents for t in result.trade_log)
        result.total_exit_fees_cents = sum(t.exit_fees_cents for t in result.trade_log)
        result.total_slippage_cents = sum(t.slippage_cents for t in result.trade_log)
        # Ideal P&L = realized + entry fees + exit fees + slippage cost.
        # Slippage cost per trade ≈ slippage_cents x quantity (the
        # extra dollars we paid above best ask).
        slippage_cost = sum(t.slippage_cents * t.quantity for t in result.trade_log)
        result.ideal_realized_pnl_usd_cents = (
            result.total_realized_pnl_usd_cents
            + result.total_entry_fees_cents
            + result.total_exit_fees_cents
            + slippage_cost
        )

    # -- output --------------------------------------------------------

    def write_csv(self, result: BacktestResult, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "ticker,entered_at,entry_price_cents,quantity,exit_at,"
            "exit_price_cents,realized_pnl_usd_cents,triggering_match_id\n"
            + "\n".join(
                ",".join(
                    str(v) if v is not None else ""
                    for v in (
                        t.ticker,
                        t.entered_at,
                        t.entry_price_cents,
                        t.quantity,
                        t.exit_at,
                        t.exit_price_cents,
                        t.realized_pnl_usd_cents,
                        t.triggering_match_id,
                    )
                )
                for t in result.trade_log
            )
        )
        return path

    @staticmethod
    def summary(result: BacktestResult) -> str:
        return json.dumps(asdict(result), indent=2, default=str)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_source_name(row: sqlite3.Row) -> str | None:
    """Safely read the JOIN-ed news_events.source column off a Row.

    The column may be absent in pathological fixtures (older snapshots
    of the schema, malformed replays). Default to None — the
    aggregator buckets None as 'unknown'.
    """
    try:
        value = row["source_name"]
    except (IndexError, KeyError):
        return None
    return str(value) if value is not None else None


# ---------------------------------------------------------------------------
# Performance metrics
# ---------------------------------------------------------------------------


def _annualized_sharpe(trades: list[BacktestTrade]) -> float:
    """Annualized Sharpe of the per-day realized P&L series, rf=0.

    Returns 0.0 if there is no variance (fewer than 2 distinct days, or
    every day has the same P&L). 252 trading-days/year is the
    convention; we use 365 here because political-prediction markets
    don't observe market closures.
    """
    daily = _daily_pnl_series(trades)
    if len(daily) < 2:
        return 0.0
    mean = sum(daily) / len(daily)
    variance = sum((d - mean) ** 2 for d in daily) / (len(daily) - 1)
    if variance <= 0:
        return 0.0
    std = math.sqrt(variance)
    return (mean / std) * math.sqrt(365.0)


def _max_drawdown_usd_cents(trades: list[BacktestTrade]) -> int:
    """Worst peak-to-trough decline of the running equity curve.

    Equity at time t = cumulative realized P&L through t. We walk the
    trade log in close-time order, track the running max, and return the
    largest drop below it. Always >= 0.
    """
    closed = [t for t in trades if t.exit_at is not None]
    if not closed:
        return 0
    closed.sort(key=lambda t: t.exit_at or "")
    running = 0
    peak = 0
    worst = 0
    for t in closed:
        running += t.realized_pnl_usd_cents
        if running > peak:
            peak = running
        drawdown = peak - running
        if drawdown > worst:
            worst = drawdown
    return worst


def _daily_pnl_series(trades: list[BacktestTrade]) -> list[int]:
    """Sum realized P&L per UTC day for closed trades. Days with no
    closed trade contribute 0 only if they fall between days that did
    (so the series doesn't have artificial gaps that would skew variance)."""
    closed = [t for t in trades if t.exit_at and t.exit_price_cents is not None]
    if not closed:
        return []
    by_day: dict[str, int] = defaultdict(int)
    for t in closed:
        try:
            day = datetime.fromisoformat((t.exit_at or "").replace("Z", "+00:00")).date()
        except ValueError:
            continue
        by_day[day.isoformat()] += t.realized_pnl_usd_cents
    return [by_day[d] for d in sorted(by_day.keys())]
