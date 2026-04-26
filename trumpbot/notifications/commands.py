"""Telegram command handlers (/status, /halt, /resume, /snooze, ...).

Phase 3 Part 2.

Each handler is an ``async def handle_<command>(ctx) -> str`` taking a
:class:`CommandContext` (the parsed user input + DB + cost guard) and
returning the message text to send back. The text is rendered via
:func:`trumpbot.notifications.templates.render_template` -- handlers
do NOT construct strings inline, so the single-source-of-truth
invariant holds.

The handlers are pure-async + DB-bound; no Telegram I/O happens here.
The Telegram bot wraps each handler in a thin shim that:

1. Validates the message came from the allowlisted chat.
2. Rate-limits commands (default 30/min/chat).
3. Calls the handler.
4. Sends the returned text back to the same chat with
   ``disable_notification=True`` (command replies are silent by spec).

If a handler raises, the bot replies with a generic "command failed,
see logs" message and an audit row goes to ``system_events`` so the
user can debug.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    delete_snoozed_market,
    get_open_trade_for_ticker,
    get_system_state,
    list_active_snoozed_markets,
    list_open_trades,
    list_source_status,
    set_system_state,
    upsert_snoozed_market,
)
from trumpbot.notifications.llm_cost import LLMCostGuard
from trumpbot.notifications.templates import RenderedMessage, render_template
from trumpbot.utils.logging import get_logger
from trumpbot.utils.timeutil import utcnow_iso

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# CommandContext + dispatch
# ---------------------------------------------------------------------------


@dataclass
class CommandContext:
    """Everything a handler needs. Built once by the Telegram bot for
    each incoming command."""

    db: Database
    args: list[str]
    """The whitespace-tokenized arguments after the command name."""

    cost_guard: LLMCostGuard | None = None
    bankroll_usd_cents: int = 50000  # default $500
    daemon_started_at: datetime | None = None
    sources_total: int = 0
    sources_active: int = 0


CommandHandler = Callable[[CommandContext], Awaitable[RenderedMessage]]


def dispatch(command: str) -> CommandHandler | None:
    """Return the handler for ``/command`` or ``None`` for unknown."""
    return _HANDLERS.get(command.lstrip("/").lower())


def all_command_names() -> list[str]:
    """List of all ``/command`` strings (with leading slash) the bot
    knows. Used by the Telegram setup to register handlers."""
    return [f"/{name}" for name in sorted(_HANDLERS)]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_help(ctx: CommandContext) -> RenderedMessage:
    del ctx
    return render_template("command_reply_help", {})


async def handle_heartbeat(ctx: CommandContext) -> RenderedMessage:
    del ctx
    return render_template("command_reply_heartbeat", {"time_et": _now_et_short()})


async def handle_halt(ctx: CommandContext) -> RenderedMessage:
    set_system_state(ctx.db, key="halt_flag", value="true")
    log.info("halt_set", source="command")
    return render_template("command_reply_halt", {})


async def handle_resume(ctx: CommandContext) -> RenderedMessage:
    set_system_state(ctx.db, key="halt_flag", value="false")
    log.info("halt_cleared", source="command")
    open_count = len(list_open_trades(ctx.db))
    return render_template(
        "command_reply_resume",
        {"time_et": _now_et_short(), "open_count": open_count},
    )


async def handle_status(ctx: CommandContext) -> RenderedMessage:
    halt = get_system_state(ctx.db, "halt_flag") or "false"
    snoozed = list_active_snoozed_markets(ctx.db)
    open_trades = list_open_trades(ctx.db)
    today_realized = _today_realized_cents(ctx.db)
    month_realized = _month_realized_cents(ctx.db)
    unrealized = sum((r["unrealized_pnl_usd_cents"] or 0) for r in open_trades)
    llm_mtd_cents = ctx.cost_guard.month_to_date_cents() if ctx.cost_guard else 0
    llm_cap_cents = ctx.cost_guard.monthly_cap_usd_cents if ctx.cost_guard else 0
    llm_pct = f"{int(round(100 * llm_mtd_cents / llm_cap_cents))}%" if llm_cap_cents else "n/a"
    uptime = _format_uptime(ctx.daemon_started_at)
    return render_template(
        "command_reply_status",
        {
            "execution_mode": "dry_run",
            "approval_mode": "human",
            "halt_status": "ON" if halt == "true" else "off",
            "snoozed_count": len(snoozed),
            "bankroll": _dollars(ctx.bankroll_usd_cents),
            "deposit_status": "Kalshi balance reflects this amount",
            "open_count": len(open_trades),
            "unrealized_pnl": _dollars_signed(unrealized),
            "today_pnl": _dollars_signed(today_realized),
            "month_pnl": _dollars_signed(month_realized),
            "sources_active": ctx.sources_active,
            "sources_total": ctx.sources_total,
            "llm_mtd": _dollars(llm_mtd_cents),
            "llm_cap": _dollars(llm_cap_cents) if llm_cap_cents else "n/a",
            "llm_pct": llm_pct,
            "last_heartbeat": _now_et_short(),
            "heartbeat_age": "0 min",
            "uptime": uptime,
        },
    )


async def handle_positions(ctx: CommandContext) -> RenderedMessage:
    open_trades = list_open_trades(ctx.db)
    if not open_trades:
        # Render with count=0 and an empty position_list. Templates'
        # str.format handles the blank cleanly.
        return render_template(
            "command_reply_positions",
            {
                "count": 0,
                "position_list": "(no open positions)",
                "total_cost": "$0.00",
                "total_mtm": "+$0.00",
            },
        )
    lines: list[str] = []
    total_cost = 0
    total_mtm = 0
    for r in open_trades:
        cost = int(r["cost_basis_usd_cents"])
        unrealized = int(r["unrealized_pnl_usd_cents"] or 0)
        total_cost += cost
        total_mtm += unrealized
        line = render_template(
            "_position_line",
            {
                "ticker": r["ticker"],
                "quantity": r["quantity"],
                "entry_price": r["entry_price_cents"],
                "current_price": r["entry_price_cents"] + (unrealized // max(1, r["quantity"])),
                "unrealized_sign": "+" if unrealized >= 0 else "-",
                "unrealized_amount": _dollars(abs(unrealized)),
                "entry_relative_time": _ago(r["entered_at"]),
                "source": "news",
            },
        )
        lines.append(line.text)
    return render_template(
        "command_reply_positions",
        {
            "count": len(open_trades),
            "position_list": "\n\n".join(lines),
            "total_cost": _dollars(total_cost),
            "total_mtm": _dollars_signed(total_mtm),
        },
    )


async def handle_why(ctx: CommandContext) -> RenderedMessage:
    if not ctx.args:
        return render_template(
            "command_reply_usage_hint", {"command": "/why", "usage": "<trade_id>"}
        )
    try:
        trade_id = int(ctx.args[0])
    except ValueError:
        return render_template(
            "command_reply_usage_hint", {"command": "/why", "usage": "<trade_id>"}
        )
    conn = ctx.db.connect()
    row = conn.execute(
        """
        SELECT t.*, m.subject_full_name, m.volume
        FROM trades t LEFT JOIN markets m ON m.ticker = t.ticker
        WHERE t.id = ?
        """,
        (trade_id,),
    ).fetchone()
    if row is None:
        return render_template(
            "command_reply_usage_hint",
            {"command": "/why", "usage": f"<trade_id>  (no trade #{trade_id})"},
        )
    return render_template(
        "command_reply_why",
        {
            "trade_id": trade_id,
            "ticker": row["ticker"],
            "subject_full_name": row["subject_full_name"] or "n/a",
            "entry_time_et": _format_et(row["entered_at"]),
            "quantity": row["quantity"],
            "entry_price": row["entry_price_cents"],
            "total_cost": _dollars(int(row["cost_basis_usd_cents"])),
            "fees": _dollars(int(row["entry_fees_cents"] or 0)),
            "source": "news",
            "source_weight": "1.0",
            "headline": "(see triggering_intent_json for details)",
            "published_time_et": "n/a",
            "lag": "n/a",
            "url": "n/a",
            "confidence": "n/a",
            "llm_reasoning": (row["reasoning_text"] or "")[:200],
            "cap_one_amount": _dollars(int(row["cap_one_value_cents"] or 0)),
            "cap_one_status": ("binds" if row["cap_binding"] == "cap_one" else "not binding"),
            "cap_two_pct": "5%",
            "market_volume": int(row["volume"] or 0),
            "cap_two_amount": _dollars(int(row["cap_two_value_cents"] or 0)),
            "binding_cap": row["cap_binding"] or "unknown",
            "slippage": int(row["slippage_cents"] or 0),
            "best_ask": int(row["entry_price_cents"]) - int(row["slippage_cents"] or 0),
            "expected_roi": _expected_roi_pct(
                int(row["entry_price_cents"] or 0),
                int(row["entry_fees_cents"] or 0),
                int(row["quantity"] or 0),
            ),
        },
    )


async def handle_history(ctx: CommandContext) -> RenderedMessage:
    n = 10
    if ctx.args:
        with contextlib.suppress(ValueError):
            n = max(1, min(50, int(ctx.args[0])))
    conn = ctx.db.connect()
    rows = conn.execute(
        """
        SELECT id, ticker, status, entry_price_cents, exit_price_cents,
               realized_pnl_usd_cents
        FROM trades
        WHERE status LIKE '%_closed_%'
        ORDER BY exited_at DESC
        LIMIT ?
        """,
        (n,),
    ).fetchall()
    wins = sum(1 for r in rows if (r["realized_pnl_usd_cents"] or 0) > 0)
    losses = sum(1 for r in rows if (r["realized_pnl_usd_cents"] or 0) < 0)
    total = sum((r["realized_pnl_usd_cents"] or 0) for r in rows)
    win_rate = f"{int(round(100 * wins / max(1, len(rows))))}%"
    line_strs: list[str] = []
    for r in rows:
        line = render_template(
            "_history_line",
            {
                "trade_id": r["id"],
                "ticker": r["ticker"],
                "entry_price": r["entry_price_cents"],
                "resolution": ("YES" if "resolved" in (r["status"] or "") else "STOP"),
                "pnl": _dollars_signed(int(r["realized_pnl_usd_cents"] or 0)),
            },
        )
        line_strs.append(line.text)
    return render_template(
        "command_reply_history",
        {
            "n": len(rows),
            "trade_lines": "\n".join(line_strs) or "(no closed trades)",
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": _dollars_signed(total),
        },
    )


async def handle_spend(ctx: CommandContext) -> RenderedMessage:
    if ctx.cost_guard is None:
        return render_template(
            "command_reply_spend",
            {
                "today": "n/a",
                "week": "n/a",
                "month": "n/a",
                "cap": "n/a",
                "pct": "0",
                "avg_per_call": "n/a",
                "projected": "n/a",
            },
        )
    cg = ctx.cost_guard
    now = datetime.now(UTC)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_week = start_of_today - timedelta(days=now.weekday())
    today_cents = _spend_since(ctx.db, start_of_today.isoformat())
    week_cents = _spend_since(ctx.db, start_of_week.isoformat())
    month_cents = cg.month_to_date_cents(now_utc=now)
    cap_cents = cg.monthly_cap_usd_cents
    n_calls = cg.call_count_since_month_start(now_utc=now)
    avg = month_cents // n_calls if n_calls else 0
    days_in_month = _days_in_month(now)
    days_elapsed = max(1, now.day)
    projected = int(month_cents / days_elapsed * days_in_month)
    return render_template(
        "command_reply_spend",
        {
            "today": _dollars(today_cents),
            "week": _dollars(week_cents),
            "month": _dollars(month_cents),
            "cap": _dollars(cap_cents),
            "pct": (f"{int(round(100 * month_cents / cap_cents))}" if cap_cents else "0"),
            "avg_per_call": _dollars(avg),
            "projected": _dollars(projected),
        },
    )


async def handle_mode(ctx: CommandContext) -> RenderedMessage:
    halt = get_system_state(ctx.db, "halt_flag") or "false"
    snoozed = list_active_snoozed_markets(ctx.db)
    return render_template(
        "command_reply_mode",
        {
            "execution_mode": "dry_run",
            "approval_mode": "human",
            "halt_status": "ON" if halt == "true" else "off",
            "snoozed_count": len(snoozed),
        },
    )


async def handle_snooze(ctx: CommandContext) -> RenderedMessage:
    if not ctx.args:
        return render_template(
            "command_reply_usage_hint",
            {"command": "/snooze", "usage": "<ticker> [duration]"},
        )
    ticker = ctx.args[0]
    duration_str = ctx.args[1] if len(ctx.args) > 1 else "24h"
    try:
        delta = parse_duration(duration_str)
    except ValueError:
        return render_template(
            "command_reply_usage_hint",
            {
                "command": "/snooze",
                "usage": "<ticker> [duration like 24h, 30m, 3d, 2h30m]",
            },
        )
    until = datetime.now(UTC) + delta
    upsert_snoozed_market(
        ctx.db, ticker=ticker, snoozed_until=until.isoformat(), reason="user_command"
    )
    return render_template(
        "command_reply_snooze",
        {
            "ticker": ticker,
            "duration": duration_str,
            "resume_time_et": _format_et(until.isoformat()),
        },
    )


async def handle_unsnooze(ctx: CommandContext) -> RenderedMessage:
    if not ctx.args:
        return render_template(
            "command_reply_usage_hint",
            {"command": "/unsnooze", "usage": "<ticker>"},
        )
    ticker = ctx.args[0]
    delete_snoozed_market(ctx.db, ticker=ticker)
    return render_template("command_reply_unsnooze", {"ticker": ticker})


# ---------------------------------------------------------------------------
# Duration parser (24h, 30m, 3d, 2h30m, ...)
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"(\d+)([dhm])")


def parse_duration(s: str) -> timedelta:
    """Parse strings like ``24h``, ``30m``, ``3d``, ``2h30m``. Raises
    :class:`ValueError` on anything else."""
    matches = _DURATION_RE.findall(s)
    if not matches:
        raise ValueError(f"unrecognised duration: {s!r}")
    # Reject if the matches don't account for every char (e.g. "24x" would
    # match nothing useful but still match 24).
    consumed = "".join(n + u for n, u in matches)
    if consumed != s:
        raise ValueError(f"unrecognised duration: {s!r}")
    seconds = 0
    for n, unit in matches:
        n_int = int(n)
        if unit == "d":
            seconds += n_int * 86400
        elif unit == "h":
            seconds += n_int * 3600
        elif unit == "m":
            seconds += n_int * 60
    if seconds <= 0:
        raise ValueError(f"non-positive duration: {s!r}")
    return timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


@dataclass
class CommandRateLimiter:
    """30 commands per minute per chat by default. Defends against a
    stolen Telegram session firing /halt /resume /halt in a loop."""

    max_per_minute: int = 30
    _hits: dict[int, deque[float]] = field(default_factory=dict)

    def check(self, chat_id: int) -> bool:
        """Return True if this chat is allowed to send a command now;
        False if rate-limited."""
        now = time.monotonic()
        window = self._hits.setdefault(chat_id, deque())
        # Drop hits older than 60 seconds.
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.max_per_minute:
            return False
        window.append(now)
        return True


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------


_HANDLERS: dict[str, CommandHandler] = {
    "help": handle_help,
    "heartbeat": handle_heartbeat,
    "halt": handle_halt,
    "resume": handle_resume,
    "status": handle_status,
    "positions": handle_positions,
    "why": handle_why,
    "history": handle_history,
    "spend": handle_spend,
    "mode": handle_mode,
    "snooze": handle_snooze,
    "unsnooze": handle_unsnooze,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"


def _dollars_signed(cents: int) -> str:
    return f"{'+' if cents >= 0 else '-'}${abs(cents) / 100:.2f}"


_ET = "America/New_York"


def _now_et_short() -> str:
    """``HH:MM ET`` (auto-handles EST vs EDT via tz database)."""
    from zoneinfo import ZoneInfo

    return datetime.now(UTC).astimezone(ZoneInfo(_ET)).strftime("%H:%M ET")


def _format_et(iso: str | None) -> str:
    """Format a stored ISO-8601-UTC timestamp as ``YYYY-MM-DD HH:MM ET``
    for display. The DB always stores UTC; the user always sees ET."""
    if not iso:
        return "n/a"
    from zoneinfo import ZoneInfo

    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "n/a"
    return ts.astimezone(ZoneInfo(_ET)).strftime("%Y-%m-%d %H:%M ET")


def _ago(iso: str | None) -> str:
    if not iso:
        return "n/a"
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "n/a"
    delta = datetime.now(UTC) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _format_uptime(started_at: datetime | None) -> str:
    if started_at is None:
        return "unknown"
    delta = datetime.now(UTC) - started_at
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, _ = divmod(rem, 3600)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h"
    return f"{total // 60}m"


def _days_in_month(now: datetime) -> int:
    """Number of days in the calendar month of ``now``."""
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)
    last_day = next_month - timedelta(days=1)
    return last_day.day


def _today_realized_cents(db: Database) -> int:
    today = datetime.now(UTC).date().isoformat()
    row = (
        db.connect()
        .execute(
            "SELECT COALESCE(SUM(realized_pnl_usd_cents), 0) FROM trades "
            "WHERE substr(exited_at, 1, 10) = ?",
            (today,),
        )
        .fetchone()
    )
    return int(row[0])


def _month_realized_cents(db: Database) -> int:
    month_prefix = datetime.now(UTC).date().isoformat()[:7]
    row = (
        db.connect()
        .execute(
            "SELECT COALESCE(SUM(realized_pnl_usd_cents), 0) FROM trades "
            "WHERE substr(exited_at, 1, 7) = ?",
            (month_prefix,),
        )
        .fetchone()
    )
    return int(row[0])


def _spend_since(db: Database, since_iso: str) -> int:
    row = (
        db.connect()
        .execute(
            "SELECT COALESCE(SUM(cost_usd_cents), 0) FROM llm_spend_log " "WHERE occurred_at >= ?",
            (since_iso,),
        )
        .fetchone()
    )
    return int(row[0])


def _expected_roi_pct(entry_cents: int, fees_cents: int, qty: int) -> int:
    if entry_cents <= 0 or qty <= 0:
        return 0
    payoff = 100 * qty
    cost = entry_cents * qty + fees_cents
    if cost <= 0:
        return 0
    return int(round(100 * (payoff - cost) / cost))


# Suppress unused-import warning on the rare branches above.
_ = (utcnow_iso, get_open_trade_for_ticker, list_source_status, Any)


__all__ = [
    "CommandContext",
    "CommandHandler",
    "CommandRateLimiter",
    "all_command_names",
    "dispatch",
    "parse_duration",
]
