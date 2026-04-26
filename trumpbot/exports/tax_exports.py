"""Tax tracking exports for Phase 4 Part 2.1.

Backed entirely by the per-trade tax fields populated at lifecycle
time (Phase 4 Part 2.1, migration 008). Nothing here recomputes from
raw lifecycle rows — exporters read the stable
``acquisition_cost_cents`` / ``disposal_proceeds_cents`` /
``realized_gain_loss_cents`` / ``acquired_date`` / ``disposed_date``
columns and present them in the requested format.

Money rules carried over from CLAUDE.md:

- Storage stays in integer cents (USDCents).
- Conversion to dollar strings happens ONLY at the export boundary
  via :func:`_dollars_str` — the helper formats with two decimal
  places and never touches floating-point.

The IRS Form 8949 layout follows the official "Sales and Other
Dispositions of Capital Assets" column spec as of TY 2024. We emit
the column names the form lists; the operator's accountant can map
them to the form's specific boxes (1099-B reconciliation imports
care about column NAMES, not box numbers).
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from trumpbot.db.connection import Database
from trumpbot.utils.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _dollars_str(cents: int | None) -> str:
    """Render an integer-cents amount as a ``$NN.NN`` dollar string.

    Uses :class:`decimal.Decimal` so the rounding is deterministic and
    free of float drift: a -$17.49 stored as ``-1749`` always renders
    as ``-$17.49`` and never ``-$17.490000000001``.
    """
    if cents is None:
        return "$0.00"
    sign = "-" if cents < 0 else ""
    amount = Decimal(abs(int(cents))) / Decimal(100)
    return f"{sign}${amount:.2f}"


def _bare_dollars(cents: int | None) -> str:
    """Like :func:`_dollars_str` but without the leading ``$`` — used
    in CSV columns where the dollar sign would confuse downstream tools."""
    if cents is None:
        return "0.00"
    sign = "-" if cents < 0 else ""
    amount = Decimal(abs(int(cents))) / Decimal(100)
    return f"{sign}{amount:.2f}"


def _market_description(row: sqlite3.Row, market_title: str | None) -> str:
    """Best-effort human-readable market description for the export.

    Pulls the ticker + the cached market title (if joined). Form 8949
    cares about a "Description of property"; we use ``ticker`` plus an
    inline description so the line is self-descriptive in the absence
    of the markets table at filing time.
    """
    if market_title:
        return f"{row['ticker']} — {market_title}"
    return str(row["ticker"])


def _resolution_outcome(status: str) -> str:
    """Translate a terminal trade status into a plain-English outcome."""
    return {
        "dry_run_closed_resolved": "settled YES (dry-run)",
        "dry_run_closed_resolved_yes": "settled YES (dry-run)",
        "dry_run_closed_resolved_no": "settled NO (dry-run)",
        "dry_run_closed_stop": "stop-loss exit (dry-run)",
        "live_closed_resolved": "settled YES",
        "live_closed_resolved_yes": "settled YES",
        "live_closed_resolved_no": "settled NO",
        "live_closed_stop": "stop-loss exit",
    }.get(status, status)


# ---------------------------------------------------------------------
# Aggregate result models
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class YearlySummary:
    """Aggregated stats for one tax year. Field names mirror the
    template variables in ``command_reply_tax_summary``."""

    year: int
    total_trades: int
    closed_trades: int
    open_trades: int
    wins: int
    losses: int
    win_rate_pct: int
    total_gain_cents: int
    total_loss_cents: int
    net_pnl_cents: int
    largest_gain_cents: int
    largest_gain_market: str
    largest_loss_cents: int
    largest_loss_market: str
    total_fees_cents: int
    total_slippage_cents: int
    avg_holding_days: int
    by_market: dict[str, int]
    """Net P&L by ticker, in USDCents."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "total_trades": self.total_trades,
            "closed_trades": self.closed_trades,
            "open_trades": self.open_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": self.win_rate_pct,
            "total_gain_cents": self.total_gain_cents,
            "total_loss_cents": self.total_loss_cents,
            "net_pnl_cents": self.net_pnl_cents,
            "largest_gain_cents": self.largest_gain_cents,
            "largest_gain_market": self.largest_gain_market,
            "largest_loss_cents": self.largest_loss_cents,
            "largest_loss_market": self.largest_loss_market,
            "total_fees_cents": self.total_fees_cents,
            "total_slippage_cents": self.total_slippage_cents,
            "avg_holding_days": self.avg_holding_days,
            "by_market": dict(self.by_market),
        }


# ---------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------


class TaxExporter:
    """Generates filing-ready exports from the tax fields on closed
    trades. All methods are pure functions of the database — no I/O
    side effects, no Telegram, no scheduling. Caller decides what to
    do with the returned strings / dicts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- aggregates ---------------------------------------------------

    def export_yearly_summary(self, year: int) -> YearlySummary:
        """Aggregate stats for ``year``. Uses the ``tax_year`` index.

        Note: ``tax_year`` is the year of disposal, NOT the year of
        entry. A trade entered Dec 30, 2025 and closed Jan 5, 2026
        belongs to tax year 2026. This mirrors how the IRS treats
        disposal date as the gain-recognition trigger.
        """
        conn = self._db.connect()
        # Closed rows for the year.
        rows = list(
            conn.execute(
                """
                SELECT t.*, m.title AS market_title
                  FROM trades t
                  LEFT JOIN markets m ON m.ticker = t.ticker
                 WHERE t.tax_year = ?
                 ORDER BY t.disposed_date ASC, t.id ASC
                """,
                (year,),
            )
        )
        closed_trades = len(rows)
        # Open trades for the year (entered, not yet disposed).
        open_count_row = conn.execute(
            """
            SELECT COUNT(*) AS c
              FROM trades
             WHERE substr(entered_at, 1, 4) = ?
               AND tax_year IS NULL
            """,
            (str(year),),
        ).fetchone()
        open_trades = int(open_count_row["c"])

        wins = 0
        losses = 0
        total_gain = 0
        total_loss = 0
        largest_gain = 0
        largest_gain_market = ""
        largest_loss = 0  # stored as positive magnitude
        largest_loss_market = ""
        total_fees = 0
        total_slippage = 0
        holding_total = 0
        holding_count = 0
        by_market: dict[str, int] = {}

        for r in rows:
            gl = int(r["realized_gain_loss_cents"] or 0)
            if gl > 0:
                wins += 1
                total_gain += gl
                if gl > largest_gain:
                    largest_gain = gl
                    largest_gain_market = _market_description(r, r["market_title"])
            else:
                losses += 1
                total_loss += abs(gl)
                if abs(gl) > largest_loss:
                    largest_loss = abs(gl)
                    largest_loss_market = _market_description(r, r["market_title"])
            entry_fees = int(r["entry_fees_cents"] or 0)
            exit_fees = int(r["exit_fees_cents"] or 0)
            total_fees += entry_fees + exit_fees
            total_slippage += int(r["slippage_cents"] or 0)
            hp = r["holding_period_days"]
            if hp is not None:
                holding_total += int(hp)
                holding_count += 1
            by_market[r["ticker"]] = by_market.get(r["ticker"], 0) + gl

        net_pnl = total_gain - total_loss
        win_rate_pct = int(round(100 * wins / closed_trades)) if closed_trades else 0
        avg_holding = int(round(holding_total / holding_count)) if holding_count else 0

        return YearlySummary(
            year=year,
            total_trades=closed_trades + open_trades,
            closed_trades=closed_trades,
            open_trades=open_trades,
            wins=wins,
            losses=losses,
            win_rate_pct=win_rate_pct,
            total_gain_cents=total_gain,
            total_loss_cents=total_loss,
            net_pnl_cents=net_pnl,
            largest_gain_cents=largest_gain,
            largest_gain_market=largest_gain_market,
            largest_loss_cents=largest_loss,
            largest_loss_market=largest_loss_market,
            total_fees_cents=total_fees,
            total_slippage_cents=total_slippage,
            avg_holding_days=avg_holding,
            by_market=by_market,
        )

    # -- per-trade exports --------------------------------------------

    def export_trade_log(self, year: int, format: Literal["csv", "json"]) -> str:
        """Per-trade export for the year.

        CSV columns (locked — exporters / spreadsheets depend on the
        order):

        ``trade_id, ticker, market_description, acquired_date,
        disposed_date, holding_period_days, quantity,
        acquisition_cost_usd, disposal_proceeds_usd,
        realized_gain_loss_usd, status, resolution_outcome, notes``
        """
        if format == "csv":
            return self._trade_log_csv(year)
        if format == "json":
            return self._trade_log_json(year)
        raise ValueError(f"unsupported export format: {format!r}")

    def _trade_log_rows(self, year: int) -> list[sqlite3.Row]:
        conn = self._db.connect()
        return list(
            conn.execute(
                """
                SELECT t.*, m.title AS market_title
                  FROM trades t
                  LEFT JOIN markets m ON m.ticker = t.ticker
                 WHERE t.tax_year = ?
                 ORDER BY t.acquired_date ASC, t.id ASC
                """,
                (year,),
            )
        )

    def _trade_log_csv(self, year: int) -> str:
        rows = self._trade_log_rows(year)
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(
            [
                "trade_id",
                "ticker",
                "market_description",
                "acquired_date",
                "disposed_date",
                "holding_period_days",
                "quantity",
                "acquisition_cost_usd",
                "disposal_proceeds_usd",
                "realized_gain_loss_usd",
                "status",
                "resolution_outcome",
                "notes",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r["id"],
                    r["ticker"],
                    _market_description(r, r["market_title"]),
                    r["acquired_date"] or "",
                    r["disposed_date"] or "",
                    r["holding_period_days"] if r["holding_period_days"] is not None else "",
                    r["quantity"],
                    _bare_dollars(r["acquisition_cost_cents"]),
                    _bare_dollars(r["disposal_proceeds_cents"]),
                    _bare_dollars(r["realized_gain_loss_cents"]),
                    r["status"],
                    _resolution_outcome(r["status"]),
                    (r["reasoning_text"] or "").replace("\n", " ")[:400],
                ]
            )
        return buf.getvalue()

    def _trade_log_json(self, year: int) -> str:
        summary = self.export_yearly_summary(year)
        rows = self._trade_log_rows(year)
        payload: dict[str, Any] = {
            "year": year,
            "generated_at": datetime.now(UTC).isoformat(),
            "summary": summary.as_dict(),
            "trades": [
                {
                    "trade_id": int(r["id"]),
                    "ticker": r["ticker"],
                    "market_description": _market_description(r, r["market_title"]),
                    "acquired_date": r["acquired_date"],
                    "disposed_date": r["disposed_date"],
                    "holding_period_days": r["holding_period_days"],
                    "quantity": int(r["quantity"]),
                    "acquisition_cost_cents": int(r["acquisition_cost_cents"] or 0),
                    "disposal_proceeds_cents": int(r["disposal_proceeds_cents"] or 0),
                    "realized_gain_loss_cents": int(r["realized_gain_loss_cents"] or 0),
                    "status": r["status"],
                    "resolution_outcome": _resolution_outcome(r["status"]),
                    "entry_fees_cents": int(r["entry_fees_cents"] or 0),
                    "exit_fees_cents": int(r["exit_fees_cents"] or 0),
                    "slippage_cents": int(r["slippage_cents"] or 0),
                }
                for r in rows
            ],
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    def export_form_8949_format(self, year: int) -> str:
        """IRS Form 8949 — Sales and Other Dispositions of Capital
        Assets. Column names match the official form spec; the
        operator (or their accountant) maps to the actual form boxes.

        We do NOT compute "Adjustment" or "Code" — those are
        situation-specific (wash sales, basis-not-reported-to-IRS,
        etc.) and out of scope per CLAUDE.md. Both columns are blank.
        """
        rows = self._trade_log_rows(year)
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(
            [
                "Description of property",
                "Date acquired",
                "Date sold or disposed of",
                "Proceeds (sales price)",
                "Cost or other basis",
                "Adjustment, if any",
                "Code, if any",
                "Gain or (loss)",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    _market_description(r, r["market_title"]),
                    r["acquired_date"] or "",
                    r["disposed_date"] or "",
                    _bare_dollars(r["disposal_proceeds_cents"]),
                    _bare_dollars(r["acquisition_cost_cents"]),
                    "",  # Adjustment — left for the operator's accountant
                    "",  # Code — left for the operator's accountant
                    _bare_dollars(r["realized_gain_loss_cents"]),
                ]
            )
        return buf.getvalue()

    def export_kalshi_reconciliation(self, year: int) -> dict[str, Any]:
        """Per-trade detail in the shape Kalshi's 1099-B uses, suitable
        for line-item comparison once Kalshi issues the form."""
        rows = self._trade_log_rows(year)
        total_proceeds = 0
        total_cost = 0
        net_pnl = 0
        line_items = []
        for r in rows:
            proceeds = int(r["disposal_proceeds_cents"] or 0)
            cost = int(r["acquisition_cost_cents"] or 0)
            gl = int(r["realized_gain_loss_cents"] or 0)
            total_proceeds += proceeds
            total_cost += cost
            net_pnl += gl
            line_items.append(
                {
                    "trade_id": int(r["id"]),
                    "ticker": r["ticker"],
                    "kalshi_order_id": r["kalshi_order_id"],
                    "client_order_id": r["client_order_id"],
                    "acquired_date": r["acquired_date"],
                    "disposed_date": r["disposed_date"],
                    "quantity": int(r["quantity"]),
                    "proceeds_cents": proceeds,
                    "cost_basis_cents": cost,
                    "gain_loss_cents": gl,
                    "status": r["status"],
                }
            )
        return {
            "year": year,
            "generated_at": datetime.now(UTC).isoformat(),
            "totals": {
                "proceeds_cents": total_proceeds,
                "cost_basis_cents": total_cost,
                "net_pnl_cents": net_pnl,
            },
            "line_items": line_items,
        }


# ---------------------------------------------------------------------
# File-system convenience
# ---------------------------------------------------------------------


def write_export(path: Path, content: str) -> Path:
    """Write export ``content`` to ``path`` (creating parent dirs).

    Returns the path so callers can echo it in Telegram messages.
    Encoded as UTF-8 (per the export-format spec)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def file_size_bytes(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


__all__ = [
    "TaxExporter",
    "YearlySummary",
    "file_size_bytes",
    "write_export",
]


# Re-export for callers that want to use the helpers directly.
def export_yearly_summary_dict(db: Database, year: int) -> dict[str, Any]:
    """Convenience wrapper for the Telegram command path."""
    return TaxExporter(db).export_yearly_summary(year).as_dict()


def export_dollar_helpers() -> tuple[Any, Any]:
    """Expose the dollar formatters for the command layer (which has
    its own ``_dollars`` helper but should use ours for tax-specific
    formatting consistency)."""
    return _dollars_str, _bare_dollars


def render_summary_template_data(summary: YearlySummary) -> dict[str, Any]:
    """Convert a :class:`YearlySummary` into the dict shape the
    ``command_reply_tax_summary`` template expects. Centralized so
    the formatting rule (signed dollars, ``%`` suffix, etc.) lives
    next to the data it formats."""
    return {
        "year": summary.year,
        "total_trades": summary.total_trades,
        "closed_trades": summary.closed_trades,
        "open_trades": summary.open_trades,
        "wins": summary.wins,
        "losses": summary.losses,
        "win_rate": summary.win_rate_pct,
        "total_gain": _dollars_str(summary.total_gain_cents),
        "total_loss": _dollars_str(summary.total_loss_cents),
        "net_pnl": _dollars_str(summary.net_pnl_cents),
        "largest_gain": _bare_dollars(summary.largest_gain_cents),
        "largest_gain_market": summary.largest_gain_market or "—",
        "largest_loss": _bare_dollars(summary.largest_loss_cents),
        "largest_loss_market": summary.largest_loss_market or "—",
        "total_fees": _dollars_str(summary.total_fees_cents),
        "total_slippage": _dollars_str(summary.total_slippage_cents),
        "avg_holding_days": summary.avg_holding_days,
    }


# Re-export the ticker iter for callers that want a fast count.
def closed_trade_tickers_for_year(db: Database, year: int) -> Iterable[str]:
    conn = db.connect()
    return [
        str(r["ticker"])
        for r in conn.execute("SELECT DISTINCT ticker FROM trades WHERE tax_year = ?", (year,))
    ]
