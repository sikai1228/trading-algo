"""Replay historical news_market_matches + price_snapshots through the
SAME DecisionEngine + RiskManager that runs in production.

Critical invariant: this module imports those classes directly. There
is no parallel "backtest engine"; the only place a strategy rule lives
is in :class:`DecisionEngine`. If the rule changes, the backtest result
changes accordingly.

Phase 2 backtester is intentionally minimal:
- Auto-approves every risk-approved order (no human in backtest)
- Uses the closest price_snapshot to the match's classified_at_ts
- Closes positions on stop-loss trigger or market resolution (YES @ 100¢, NO @ 0¢)
- Reports total trades, win/loss/win-rate, realized + unrealized P&L

Slippage modeling, fees, partial fills, and P&L attribution are all
Phase 3+ (out of scope per CLAUDE.md).
"""

from __future__ import annotations

import json
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
from trumpbot.types.intents import StopLossIntent


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
    triggering_source: str | None = None


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
    ) -> None:
        self._db_path = Path(db_path)
        self._cfg = config or DecisionConfig()
        self._engine = DecisionEngine(self._cfg)
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
                market_state = MarketState(
                    ticker=ticker, yes_bid_cents=quote[0], yes_ask_cents=quote[1]
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
                    live_trading_started_at=None,
                )
                intent = self._engine.evaluate_news_match(snap, market_state, None, bankroll)
                if intent is None:
                    continue
                # Auto-approve in backtest.
                trade = BacktestTrade(
                    ticker=ticker,
                    entered_at=row["created_at"],
                    entry_price_cents=intent.target_price_cents,
                    quantity=intent.target_quantity,
                    triggering_match_id=intent.triggering_match_id,
                    triggering_source=row["matched_subject"],
                )
                open_positions[ticker] = trade
                next_trade_id += 1

            # Close any remaining open positions at market resolution if known.
            for ticker, trade in list(open_positions.items()):
                resolution = self._market_resolution(conn, ticker)
                if resolution is not None:
                    payoff = 100 if resolution == "settled_yes" else 0
                    realized = (payoff - trade.entry_price_cents) * trade.quantity
                    trade.exit_price_cents = payoff
                    trade.exit_at = end_ts
                    trade.realized_pnl_usd_cents = realized
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
        return conn.execute(
            """
            SELECT m.*
            FROM news_market_matches m
            WHERE m.confidence >= ?
              AND m.created_at >= ?
              AND m.created_at <= ?
            ORDER BY m.created_at ASC
            """,
            (self._cfg.llm_confidence_threshold, start_ts, end_ts),
        )

    def _market(self, conn: sqlite3.Connection, ticker: str):  # type: ignore[no-untyped-def]
        return conn.execute(
            "SELECT ticker, open_ts, close_ts, status FROM markets WHERE ticker = ?",
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
        trade = positions.pop(ticker)
        trade.exit_price_cents = stop.current_bid_cents
        trade.exit_at = when_ts
        trade.realized_pnl_usd_cents = (
            stop.current_bid_cents - trade.entry_price_cents
        ) * trade.quantity
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
        # By-subject and by-source breakdowns
        by_subject: dict[str, dict[str, int]] = defaultdict(
            lambda: {"trades": 0, "realized_pnl_usd_cents": 0}
        )
        by_source: dict[str, dict[str, int]] = defaultdict(
            lambda: {"trades": 0, "realized_pnl_usd_cents": 0}
        )
        for t in result.trade_log:
            subject = t.triggering_source or "unknown"
            by_subject[subject]["trades"] += 1
            by_subject[subject]["realized_pnl_usd_cents"] += t.realized_pnl_usd_cents
        result.by_subject = dict(by_subject)
        result.by_source = dict(by_source)

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
