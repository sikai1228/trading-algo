"""Scheduled-message daemon loops.

Phase 3 Part 2 introduced these. Phase 4 Part 2.10 removed
``heartbeat_loop`` and its supporting helpers; the morning daily
digest is the regular status notification now.

Three asyncio coroutines the daemon supervises alongside the
existing data-collection + decision tasks:

- :func:`daily_digest_loop` -- once per day at 8:00 AM ET. Renders
  ``daily_digest`` from yesterday's outcomes + month-to-date.
- :func:`settlement_notification_loop` -- every 5 min. Detects markets
  that resolved since the last cycle for tickers we hold positions in,
  closes them via the executor, sends ``trade_settled_yes`` /
  ``trade_settled_no``.
- :func:`source_health_loop` -- every 5 min. Walks ``source_status``;
  fires ``alert_warning_source_down`` when a source has been down >30
  min, ``alert_info_source_recovered`` on recovery.

Each loop is short and stateless. They read state from the DB, call
the alert dispatcher (or send_text directly), and sleep until the
next tick. SIGTERM cancels via ``stop_event``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    get_source_status,
    get_system_state,
    list_open_trades,
    list_source_status,
    upsert_source_status,
)
from trumpbot.notifications.alerts import AlertDispatcher
from trumpbot.notifications.llm_cost import LLMCostGuard
from trumpbot.notifications.templates import render_template
from trumpbot.utils.logging import get_logger

log = get_logger(__name__)


# Telegram-send callable: (text, silent) -> awaitable.
SendTextFn = Callable[[str, bool], Awaitable[None]]


# Phase 4 Part 2.10 — ``heartbeat_loop`` + ``_build_heartbeat_data``
# + ``_seconds_until_next_aligned_tick`` were REMOVED. The morning
# daily digest is the regular status notification; /status answers
# the on-demand "is it alive?" question; the healthcheck endpoint
# (`/healthz` on port 9090) is the machine-readable liveness probe.


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------


async def daily_digest_loop(
    *,
    db: Database,
    send_text: SendTextFn,
    cost_guard: LLMCostGuard | None,
    digest_hour_utc: int,
    stop_event: asyncio.Event,
) -> None:
    """Send the ``daily_digest`` template once per day at
    ``digest_hour_utc``. The user's spec said 8:00 AM ET; we accept
    a UTC hour so the daemon is timezone-independent (12 UTC == 8
    AM ET in standard time, 13 UTC == 9 AM ET adjustment is left to
    the operator).
    """
    component = "daily_digest_loop"
    log.info(f"{component}_started", hour_utc=digest_hour_utc)
    while not stop_event.is_set():
        try:
            sleep_for = _seconds_until_next_hour(digest_hour_utc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
                break  # stop_event set during sleep
            except TimeoutError:
                pass  # time to fire
            data = _build_digest_data(db, cost_guard)
            rendered = render_template("daily_digest", data)
            await send_text(rendered.text, True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover -- defensive
            log.error(f"{component}_error", error=repr(exc))
            # Avoid hot-looping on a recurring failure.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=300)
    log.info(f"{component}_stopped")


def _seconds_until_next_hour(hour_utc: int, *, now: datetime | None = None) -> float:
    n = now or datetime.now(UTC)
    target = n.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= n:
        target = target + timedelta(days=1)
    return (target - n).total_seconds()


def _build_digest_data(db: Database, cost_guard: LLMCostGuard | None) -> dict[str, Any]:
    yesterday: date = (datetime.now(UTC) - timedelta(days=1)).date()
    yest_rows = (
        db.connect()
        .execute(
            "SELECT realized_pnl_usd_cents FROM trades " "WHERE substr(exited_at, 1, 10) = ?",
            (yesterday.isoformat(),),
        )
        .fetchall()
    )
    closed = len(yest_rows)
    wins = sum(1 for r in yest_rows if (r[0] or 0) > 0)
    losses = sum(1 for r in yest_rows if (r[0] or 0) < 0)
    pnl_yesterday = sum((r[0] or 0) for r in yest_rows)
    win_rate = f"{int(round(100 * wins / max(1, closed)))}%" if closed else "n/a"

    open_trades = list_open_trades(db)
    unrealized = sum((r["unrealized_pnl_usd_cents"] or 0) for r in open_trades)

    # Week / month totals, exit_at-based.
    today = datetime.now(UTC).date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    month_prefix = today.isoformat()[:7]
    pnl_week = (
        db.connect()
        .execute(
            "SELECT COALESCE(SUM(realized_pnl_usd_cents), 0) FROM trades " "WHERE exited_at >= ?",
            (week_start,),
        )
        .fetchone()[0]
    )
    pnl_month = (
        db.connect()
        .execute(
            "SELECT COALESCE(SUM(realized_pnl_usd_cents), 0) FROM trades "
            "WHERE substr(exited_at, 1, 7) = ?",
            (month_prefix,),
        )
        .fetchone()[0]
    )
    sources = list_source_status(db)
    s_total = len(sources)
    s_active = sum(1 for r in sources if r.current_status == "active")
    sources_note = "" if s_active == s_total else f"  ({s_total - s_active} down)"
    crit_count = (
        db.connect()
        .execute(
            "SELECT COUNT(*) FROM system_events " "WHERE severity = 'critical' AND created_at >= ?",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
        )
        .fetchone()[0]
    )
    if cost_guard is None:
        llm_mtd = "n/a"
        llm_cap = "n/a"
        llm_pct = "n/a"
    else:
        m = cost_guard.month_to_date_cents()
        llm_mtd = f"${m / 100:.2f}"
        llm_cap = f"${cost_guard.monthly_cap_usd_cents / 100:.2f}"
        cap = cost_guard.monthly_cap_usd_cents
        llm_pct = f"{int(round(100 * m / cap))}%" if cap else "n/a"
    return {
        "date": today.isoformat(),
        "closed_count": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "pnl_yesterday": _signed(int(pnl_yesterday)),
        "open_count": len(open_trades),
        "unrealized_pnl": _signed(int(unrealized)),
        "pnl_week": _signed(int(pnl_week)),
        "pnl_month": _signed(int(pnl_month)),
        "sources_active": s_active,
        "sources_total": s_total,
        "sources_note": sources_note,
        "critical_count": int(crit_count),
        "llm_mtd": llm_mtd,
        "llm_cap": llm_cap,
        "llm_pct": llm_pct,
    }


# ---------------------------------------------------------------------------
# Settlement notifications
# ---------------------------------------------------------------------------


async def settlement_notification_loop(
    *,
    db: Database,
    send_text: SendTextFn,
    interval_seconds: int,
    stop_event: asyncio.Event,
) -> None:
    """Watch for markets that resolved while we held a position. For
    each: render ``trade_settled_yes`` or ``trade_settled_no``, send,
    then close the trade row via :func:`close_trade`.

    The check is done by joining ``trades`` (still-open) against
    ``markets`` (status = settled_yes / settled_no). The notifier does
    NOT compute realized P&L itself -- that lives in the executor's
    :meth:`close_resolved`. It just discovers the transition and asks
    the daemon to drive closure.
    """
    component = "settlement_notification_loop"
    log.info(f"{component}_started", interval_sec=interval_seconds)
    while not stop_event.is_set():
        try:
            await _process_settlements(db, send_text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover -- defensive
            log.error(f"{component}_error", error=repr(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
    log.info(f"{component}_stopped")


async def _process_settlements(db: Database, send_text: SendTextFn) -> None:
    """Find open positions whose market has settled. For each, send
    the appropriate template. Closing the trade row is the executor's
    job (called from the daemon wiring via the WS feed's
    ``market_status_changed`` event); this loop is the user-facing
    notifier and the safety net in case the WS event was missed."""
    rows = (
        db.connect()
        .execute(
            """
            SELECT t.id, t.ticker, t.entry_price_cents, t.quantity,
                   t.entry_fees_cents, t.cost_basis_usd_cents,
                   m.status, m.subject_full_name
            FROM trades t
            JOIN markets m ON m.ticker = t.ticker
            WHERE t.status IN ('dry_run', 'live')
              AND m.status IN ('settled_yes', 'settled_no')
            """
        )
        .fetchall()
    )
    for r in rows:
        if r["status"] == "settled_yes":
            payoff_cents = 100 * r["quantity"]
            entry_fees = int(r["entry_fees_cents"] or 0)
            cost = int(r["cost_basis_usd_cents"]) + entry_fees
            gross_pnl = payoff_cents - cost
            roi = int(round(100 * gross_pnl / max(1, cost)))
            data = {
                "ticker": r["ticker"],
                "subject_full_name": r["subject_full_name"] or "n/a",
                "quantity": r["quantity"],
                "entry_price": r["entry_price_cents"],
                "pnl_dollars": f"{abs(gross_pnl) / 100:.2f}",
                "roi": roi,
                "series": str(r["ticker"]).split("-", 1)[0],
                "remaining_in_series": "n/a",
            }
            rendered = render_template("trade_settled_yes", data)
        else:
            entry_fees = int(r["entry_fees_cents"] or 0)
            cost = int(r["cost_basis_usd_cents"]) + entry_fees
            data = {
                "ticker": r["ticker"],
                "subject_full_name": r["subject_full_name"] or "n/a",
                "quantity": r["quantity"],
                "entry_price": r["entry_price_cents"],
                "stop_status": "no stop fired",
                "loss_dollars": f"{cost / 100:.2f}",
                "resolution_date": "(market close)",
            }
            rendered = render_template("trade_settled_no", data)
        await send_text(rendered.text, True)


# ---------------------------------------------------------------------------
# Source health
# ---------------------------------------------------------------------------


async def source_health_loop(
    *,
    db: Database,
    dispatcher: AlertDispatcher,
    interval_seconds: int,
    down_threshold_minutes: int,
    stop_event: asyncio.Event,
    rotation_paused_threshold_hours: int = 12,
) -> None:
    """Walk ``source_status`` every ``interval_seconds``. For each
    source whose ``last_successful_poll`` is older than
    ``down_threshold_minutes``, fire ``alert_warning_source_down``
    (deduped per source per hour). When a previously-down source
    transitions to ``current_status='active'`` (a successful poll
    happened), fire ``alert_info_source_recovered`` once.

    PR #33 — additionally checks ``newest_feed_item_ts`` for each
    source. When the newest article in the parsed feed is older than
    ``rotation_paused_threshold_hours`` (default 12 h), the source is
    classified as ``rotation_paused`` and
    ``alert_warning_source_rotation_paused`` is sent (deduped per
    source per 24 h). The audit's ``fox_politics`` (7 h stale) and
    ``dod_news`` (52 h stale) cases motivate this signal.
    """
    component = "source_health_loop"
    log.info(f"{component}_started", interval_sec=interval_seconds)
    while not stop_event.is_set():
        try:
            await _check_source_health(
                db, dispatcher, down_threshold_minutes, rotation_paused_threshold_hours
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover -- defensive
            log.error(f"{component}_error", error=repr(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
    log.info(f"{component}_stopped")


# PR #33 — dedup window for rotation_paused alerts. Per-source per
# 24 h: the spec calls for "max 1 alert per source per 24h" so a feed
# that's been paused for a week doesn't spam Telegram.
ROTATION_PAUSED_DEDUP_WINDOW_SECONDS: int = 24 * 3600


async def _check_source_health(
    db: Database,
    dispatcher: AlertDispatcher,
    threshold_minutes: int,
    rotation_paused_threshold_hours: int = 12,
) -> None:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
    rows = list_source_status(db)
    threshold = timedelta(minutes=threshold_minutes)
    rotation_threshold = timedelta(hours=rotation_paused_threshold_hours)
    now = datetime.now(UTC)
    total = len(rows)
    active = sum(1 for r in rows if r.current_status == "active")
    for r in rows:
        last_poll = r.last_successful_poll
        if last_poll is None:
            continue
        try:
            last_dt = datetime.fromisoformat(last_poll.replace("Z", "+00:00"))
        except ValueError:
            continue
        gap = now - last_dt
        if r.current_status == "down" and gap < threshold:
            # Recovered (its last_successful_poll is recent again).
            outage = _humanize_duration(gap)
            await dispatcher.send(
                template_name="alert_info_source_recovered",
                data={
                    "source_name": r.source_name,
                    "time_et": now.astimezone(_ET).strftime("%H:%M ET"),
                    "outage_duration": outage,
                },
            )
            upsert_source_status(db, source_name=r.source_name, current_status="active")
            continue
        if r.current_status != "down" and gap >= threshold:
            # Just crossed the down threshold.
            await dispatcher.send(
                template_name="alert_warning_source_down",
                data={
                    "source_name": r.source_name,
                    "last_success_et": last_dt.astimezone(_ET).strftime("%Y-%m-%d %H:%M ET"),
                    "duration_min": int(gap.total_seconds() // 60),
                    "attempt_summary": (f"failed for {int(gap.total_seconds() // 60)} min"),
                    "active_count": active,
                    "total_count": total,
                },
                dedup_key=f"src_down:{r.source_name}",
            )
            upsert_source_status(db, source_name=r.source_name, current_status="down")
            continue

        # PR #33 — rotation_paused check. Only meaningful for sources
        # that ARE successfully polling (gap < threshold) but whose
        # feed contents themselves are stale.
        if r.current_status == "down":
            continue  # don't double-alert; source_down already covers it
        newest = r.newest_feed_item_ts
        if newest is None:
            continue  # need at least one successful fetch with parseable dates
        try:
            newest_dt = datetime.fromisoformat(newest.replace("Z", "+00:00"))
        except ValueError:
            continue
        feed_gap = now - newest_dt
        if feed_gap < rotation_threshold:
            # Feed is fresh — if we previously marked rotation_paused,
            # transition back to active. Don't fire a recovery alert
            # for rotation_paused (silent recovery is fine; the active
            # state will be reflected in /status).
            if r.current_status == "rotation_paused":
                upsert_source_status(db, source_name=r.source_name, current_status="active")
            continue
        # Feed has been stale > rotation_threshold. Fire the alert
        # (deduped per source per 24 h) and mark rotation_paused.
        await dispatcher.send(
            template_name="alert_warning_source_rotation_paused",
            data={
                "source_name": r.source_name,
                "newest_item_et": newest_dt.astimezone(_ET).strftime("%Y-%m-%d %H:%M ET"),
                "duration_ago": _humanize_duration(feed_gap),
                "active_count": active,
                "total_count": total,
            },
            dedup_key=f"src_rotation_paused:{r.source_name}",
            window_seconds_override=ROTATION_PAUSED_DEDUP_WINDOW_SECONDS,
        )
        upsert_source_status(db, source_name=r.source_name, current_status="rotation_paused")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signed(cents: int) -> str:
    return f"{'+' if cents >= 0 else '-'}${abs(cents) / 100:.2f}"


def _humanize_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60} min"
    return f"{secs // 3600}h"


# Used to satisfy unused-import lints on infrequent paths.
_ = (get_source_status, get_system_state)


# ---------------------------------------------------------------------------
# Phase 4 Part 2.1 — monthly tax digest
# ---------------------------------------------------------------------------


_ET_TZ = "America/New_York"


def _seconds_until_next_monthly_tick(
    *,
    fire_day: int,
    fire_time_et: str,
    now: datetime | None = None,
) -> float:
    """Compute the seconds-from-now until the next monthly digest tick.

    ``fire_day`` is the calendar day of the month (1..28 to be safe
    across all months). ``fire_time_et`` is ``HH:MM`` local Eastern
    Time. We compute everything in ET and convert to UTC for the
    sleep duration, so EST/EDT transitions don't drift the firing
    time.
    """
    from zoneinfo import ZoneInfo

    et = ZoneInfo(_ET_TZ)
    now_et = (now or datetime.now(UTC)).astimezone(et)
    hh, mm = (int(x) for x in fire_time_et.split(":"))

    def _candidate(year: int, month: int) -> datetime:
        # Clamp fire_day to month's actual length; e.g. day=31 in Feb
        # rolls back to the 28th/29th. We never overshoot to next month.
        import calendar

        last_day = calendar.monthrange(year, month)[1]
        day = min(fire_day, last_day)
        return datetime(year, month, day, hh, mm, tzinfo=et)

    candidate = _candidate(now_et.year, now_et.month)
    if candidate <= now_et:
        # Roll to next month.
        next_month = now_et.month + 1
        next_year = now_et.year
        if next_month > 12:
            next_month = 1
            next_year += 1
        candidate = _candidate(next_year, next_month)
    return (candidate - now_et).total_seconds()


def _previous_month_bounds(*, now: datetime | None = None) -> tuple[date, date, str, int, int]:
    """Return ``(month_start, month_end_inclusive, month_name, year, month_index)``
    for the calendar month before ``now``.

    Used so the digest fired on the 1st of N covers month N-1 in full.
    """
    n = (now or datetime.now(UTC)).date()
    # First day of current month → step back one day → that's last
    # day of previous month.
    first_of_current = n.replace(day=1)
    prev_end = first_of_current - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    month_name = prev_start.strftime("%B")
    return prev_start, prev_end, month_name, prev_start.year, prev_start.month


async def monthly_tax_digest_loop(
    *,
    db: Database,
    send_text: SendTextFn,
    exports_dir: Path | None,
    fire_day: int = 1,
    fire_time_et: str = "09:00",
    stop_event: asyncio.Event,
) -> None:
    """Send the ``monthly_tax_digest`` template once per month at
    ``fire_day`` of the month, ``fire_time_et`` Eastern. Also writes
    ``data/exports/monthly/YYYY-MM.csv`` with the previous month's
    full trade log so the operator has the per-trade detail right
    next to the digest.

    Idempotent: if the daemon restarts between sleep and fire, the
    next iteration computes the same target time and sleeps again.
    Worst case (daemon down across the firing instant) the digest
    silently skips that month — committed CSVs in
    ``data/exports/monthly/`` cover the audit trail regardless.
    """
    from pathlib import Path as _Path

    from trumpbot.exports.tax_exports import (
        TaxExporter,
        _bare_dollars,
        _dollars_str,
        write_export,
    )

    component = "monthly_tax_digest_loop"
    log.info(
        f"{component}_started",
        fire_day=fire_day,
        fire_time_et=fire_time_et,
    )
    while not stop_event.is_set():
        try:
            sleep_for = _seconds_until_next_monthly_tick(
                fire_day=fire_day, fire_time_et=fire_time_et
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
                break
            except TimeoutError:
                pass

            (
                _prev_start,
                prev_end,
                month_name,
                prev_year,
                prev_month,
            ) = _previous_month_bounds()

            # Pull aggregate stats from the previous month's tax_year +
            # disposed_date filter. Reuse TaxExporter where possible
            # for shape consistency with /tax_summary.
            exporter = TaxExporter(db)
            month_rows = list(
                db.connect().execute(
                    """
                    SELECT t.*, m.title AS market_title
                      FROM trades t
                      LEFT JOIN markets m ON m.ticker = t.ticker
                     WHERE t.tax_year = ?
                       AND substr(t.disposed_date, 6, 2) = ?
                     ORDER BY t.disposed_date
                    """,
                    (prev_year, f"{prev_month:02d}"),
                )
            )
            count = len(month_rows)
            wins = sum(1 for r in month_rows if int(r["realized_gain_loss_cents"] or 0) > 0)
            losses = count - wins
            win_rate = int(round(100 * wins / count)) if count else 0
            pnl_cents = sum(int(r["realized_gain_loss_cents"] or 0) for r in month_rows)
            fees = sum(
                int(r["entry_fees_cents"] or 0) + int(r["exit_fees_cents"] or 0) for r in month_rows
            )
            slip = sum(int(r["slippage_cents"] or 0) for r in month_rows)
            largest_gain = 0
            largest_gain_t = "-"
            largest_loss = 0
            largest_loss_t = "-"
            for r in month_rows:
                gl = int(r["realized_gain_loss_cents"] or 0)
                if gl > largest_gain:
                    largest_gain = gl
                    largest_gain_t = r["ticker"]
                if gl < -largest_loss:
                    largest_loss = -gl
                    largest_loss_t = r["ticker"]
            holding = [r["holding_period_days"] for r in month_rows if r["holding_period_days"]]
            avg_holding = int(round(sum(holding) / len(holding))) if holding else 0
            ytd_summary = exporter.export_yearly_summary(prev_year)

            # Save monthly CSV — use the same columns as /tax_export csv
            # but scoped to the month. Easiest: re-run the trade log
            # builder filtered to month-of-disposal.
            month_str = f"{prev_year}-{prev_month:02d}"
            base_dir = exports_dir or _Path("data/exports")
            csv_path = base_dir / "monthly" / f"{month_str}.csv"
            # The /tax_export csv path covers a full year. For the
            # monthly file, write a trimmed variant — same columns,
            # filtered to disposed_date in the month.
            month_csv = _build_monthly_csv(month_rows)
            write_export(csv_path, month_csv)

            data = {
                "month_name": month_name,
                "year": prev_year,
                "count": count,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "pnl": _dollars_str(pnl_cents),
                "fees": _dollars_str(fees),
                "slippage": _dollars_str(slip),
                "largest_gain": _bare_dollars(largest_gain),
                "largest_gain_ticker": largest_gain_t,
                "largest_loss": _bare_dollars(largest_loss),
                "largest_loss_ticker": largest_loss_t,
                "avg_holding_days": avg_holding,
                "month": f"{prev_month:02d}",
                "ytd_pnl": _dollars_str(ytd_summary.net_pnl_cents),
            }
            rendered = render_template("monthly_tax_digest", data)
            await send_text(rendered.text, True)
            log.info(f"{component}_fired", month=month_str, csv=str(csv_path))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover -- defensive
            log.error(f"{component}_error", error=repr(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=3600)
    log.info(f"{component}_stopped")


def _build_monthly_csv(rows: list[Any]) -> str:
    """Same CSV columns as TaxExporter._trade_log_csv, filtered to the
    rows the caller already pre-fetched (one calendar month)."""
    import csv as _csv
    import io as _io

    from trumpbot.exports.tax_exports import (
        _bare_dollars,
        _market_description,
        _resolution_outcome,
    )

    buf = _io.StringIO()
    w = _csv.writer(buf, quoting=_csv.QUOTE_MINIMAL, lineterminator="\n")
    w.writerow(
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
        w.writerow(
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


__all__ = [
    "SendTextFn",
    "daily_digest_loop",
    "monthly_tax_digest_loop",
    "settlement_notification_loop",
    "source_health_loop",
]
