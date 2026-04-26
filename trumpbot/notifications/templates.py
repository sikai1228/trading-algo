"""Single source of truth for every Telegram message string.

Phase 3 Part 2.

THE INVARIANT: every byte of text the user sees in Telegram lives in
this file. Code that needs to send a message does NOT construct strings
inline; it references a template by name and passes a data dict, and
:func:`render_template` renders it. The grep-test in CI enforces this:
no Telegram-message text exists outside this module.

Why so strict: the user iterates on copy after deployment. Wording and
formatting will change. Each template must be:

- **Identifiable by name** — unique key in :data:`TEMPLATE_CATALOG`.
- **Editable in one location** — this file.
- **Findable in seconds** — grep for the template name returns one place
  in the catalog plus the call sites.
- **Versionable** — a wording change touches the catalog, not the code
  that uses it.

Templates use Python's :meth:`str.format` with named fields. The set
of available fields per template is documented inline above the
template entry. Calling :func:`render_template` with extra fields is
fine (they're ignored by ``.format``); calling with a missing field
raises :class:`KeyError`, surfacing the bug at the boundary.

Adding a template: pick a category (``heartbeat`` / ``digest`` /
``trade_outcome`` / ``trade_proposal`` / ``alert_critical`` /
``alert_warning`` / ``alert_info`` / ``command_reply``), set audibility
(``True`` only for ``alert_critical``; everything else silent), write
the format string. Add a unit test rendering it with a sample data
dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Category = Literal[
    "heartbeat",
    "digest",
    "trade_outcome",
    "trade_proposal",
    "alert_critical",
    "alert_warning",
    "alert_info",
    "command_reply",
]


@dataclass(frozen=True)
class MessageTemplate:
    """One row in :data:`TEMPLATE_CATALOG`.

    ``audible`` controls Telegram's notification setting on send: True
    -> normal audible push, False -> ``disable_notification=True`` so
    the message arrives silently. Per spec: only ``alert_critical_*``
    templates are audible.
    """

    category: Category
    audible: bool
    format: str

    def render(self, data: dict[str, Any]) -> str:
        """Render this template's format string with ``data``.

        Raises :class:`KeyError` if a required field is missing. Extra
        fields are ignored.
        """
        return self.format.format(**data)


@dataclass(frozen=True)
class RenderedMessage:
    """Output of :func:`render_template`. The audibility flag travels
    with the rendered text so the caller can pass it straight to
    Telegram's ``send_message(disable_notification=...)``."""

    template_name: str
    category: Category
    audible: bool
    text: str


def render_template(name: str, data: dict[str, Any]) -> RenderedMessage:
    """Resolve ``name`` against :data:`TEMPLATE_CATALOG` and render with
    ``data``. Raises :class:`KeyError` if the template name is unknown
    or if any required field is missing from ``data``."""
    if name not in TEMPLATE_CATALOG:
        raise KeyError(f"unknown template: {name!r}")
    tpl = TEMPLATE_CATALOG[name]
    return RenderedMessage(
        template_name=name,
        category=tpl.category,
        audible=tpl.audible,
        text=tpl.render(data),
    )


# ---------------------------------------------------------------------------
# Heartbeat + digest
# ---------------------------------------------------------------------------

_HEARTBEAT_PERIODIC = MessageTemplate(
    category="heartbeat",
    audible=False,
    # fields: time_et, open_count, today_pnl, llm_today, llm_cap,
    #         sources_active, sources_total
    format=(
        "✓ {time_et} | open: {open_count} | today: {today_pnl} | "
        "LLM: {llm_today}/{llm_cap} | sources: {sources_active}/{sources_total}"
    ),
)

_DAILY_DIGEST = MessageTemplate(
    category="digest",
    audible=False,
    # fields: date, closed_count, wins, losses, win_rate, pnl_yesterday,
    #         open_count, unrealized_pnl, pnl_week, pnl_month,
    #         sources_active, sources_total, sources_note,
    #         critical_count, llm_mtd, llm_cap, llm_pct
    format=(
        "📊 Daily Digest -- {date}\n\n"
        "Yesterday:\n"
        "  Trades closed: {closed_count} ({wins}W / {losses}L, {win_rate} win rate)\n"
        "  Realized P&L: {pnl_yesterday}\n\n"
        "Currently open:\n"
        "  Positions: {open_count}\n"
        "  Unrealized P&L: {unrealized_pnl}\n\n"
        "This week: {pnl_week}\n"
        "This month: {pnl_month}\n\n"
        "System health:\n"
        "  Sources active: {sources_active}/{sources_total}{sources_note}\n"
        "  Critical errors last 24h: {critical_count}\n"
        "  LLM spend MTD: {llm_mtd} / {llm_cap} ({llm_pct})"
    ),
)

# ---------------------------------------------------------------------------
# Trade proposal (sent when ApprovalGate asks the user to approve)
# ---------------------------------------------------------------------------

# Shared body for buy proposals — entry and re-entry differ only in the
# header and the optional prior-trade context.
_PROPOSAL_BODY = (
    "Position sizing:\n"
    "  Cap one (hard): {cap_one_dollars}\n"
    "  Cap two (5% of volume): {cap_two_dollars}\n"
    "  Binding: {cap_binding}\n\n"
    "Order book walk for {effective_cap_dollars}:\n"
    "  Filled: {quantity} contracts at avg {avg_fill}c\n"
    "  Slippage: {slippage}c from best ask\n"
    "  Fee estimate: {fees_dollars}\n"
    "  Total cost: {total_cost_dollars}\n\n"
    "Action: BUY YES @ avg ~{avg_fill}c (FOK, target {quantity} contracts)\n\n"
    "Reasoning:\n{reasoning_text}\n\n"
    "If book moves unfavorably between approval and execution,\n"
    "order will be killed (no trade).\n"
    "[APPROVE] [REJECT]"
)

_TRADE_PROPOSAL_ENTRY = MessageTemplate(
    category="trade_proposal",
    audible=False,
    # fields: ticker, match_id, confidence, weight, plus the proposal
    # body fields documented on _PROPOSAL_BODY.
    format=(
        "💰 TRADE PROPOSAL\n"
        "Ticker: {ticker}\n"
        "Triggered by match #{match_id}\n"
        "Confidence: {confidence}  Confirmation weight: {weight}\n\n"
        "Approve within 3:00 to execute.\n\n" + _PROPOSAL_BODY
    ),
)

_TRADE_PROPOSAL_REENTRY = MessageTemplate(
    category="trade_proposal",
    audible=False,
    # fields: ticker, match_id, confidence, prior_trade_id,
    #         prior_trade_outcome, prior_realized_dollars, plus body fields.
    format=(
        "🔄 RE-ENTRY OPPORTUNITY\n"
        "Ticker: {ticker}\n"
        "Prior trade #{prior_trade_id} closed via {prior_trade_outcome}\n"
        "Prior realized P&L: {prior_realized_dollars}\n\n"
        "Fresh signal: match #{match_id} (confidence {confidence})\n\n"
        "No timeout -- respond when ready.\n\n" + _PROPOSAL_BODY
    ),
)

_TRADE_PROPOSAL_STOP_LOSS = MessageTemplate(
    category="trade_proposal",
    audible=False,
    # fields: ticker, trade_id, entry_price, current_bid, drop, quantity,
    #         cost_basis_dollars, current_value_dollars, unrealized_dollars,
    #         reasoning_text
    format=(
        "⚠️ STOP-LOSS TRIGGER\n"
        "Ticker: {ticker}\n"
        "Trade #{trade_id}\n"
        "Entry: {entry_price}c   Current bid: {current_bid}c   "
        "Drop: {drop}c\n\n"
        "Position: {quantity} contracts\n"
        "Cost basis: {cost_basis_dollars}\n"
        "Current value: {current_value_dollars}\n"
        "Unrealized P&L: {unrealized_dollars}\n\n"
        "Reasoning:\n{reasoning_text}\n\n"
        "No timeout -- respond when ready.\n"
        "[APPROVE -- exit at market] [REJECT -- hold]"
    ),
)

# ---------------------------------------------------------------------------
# Trade outcomes
# ---------------------------------------------------------------------------

_TRADE_SETTLED_YES = MessageTemplate(
    category="trade_outcome",
    audible=False,
    # fields: ticker, subject_full_name, quantity, entry_price,
    #         pnl_dollars, roi, series, remaining_in_series
    format=(
        "🎯 Settled: {ticker}\n"
        "{subject_full_name}\n\n"
        "Resolution: YES at $1.00\n"
        "Your position: {quantity} contracts @ entry {entry_price}c avg\n"
        "Realized: +${pnl_dollars} (+{roi}% ROI after fees/slippage)\n\n"
        "Open positions in {series} series: {remaining_in_series} remaining"
    ),
)

_TRADE_SETTLED_NO = MessageTemplate(
    category="trade_outcome",
    audible=False,
    # fields: ticker, subject_full_name, quantity, entry_price,
    #         stop_status, loss_dollars, resolution_date
    format=(
        "🎯 Settled: {ticker}\n"
        "{subject_full_name}\n\n"
        "Resolution: NO at $0\n"
        "Your position: {quantity} contracts @ entry {entry_price}c avg "
        "({stop_status})\n"
        "Realized: -${loss_dollars}\n\n"
        "Reason: market resolved NO (no qualifying meeting before {resolution_date})"
    ),
)

_TRADE_STOPPED_OUT = MessageTemplate(
    category="trade_outcome",
    audible=False,
    # fields: ticker, subject_full_name, exit_price, quantity,
    #         entry_price, loss_dollars
    format=(
        "🎯 Stopped Out: {ticker}\n"
        "{subject_full_name}\n\n"
        "Approved exit at {exit_price}c avg\n"
        "Position: {quantity} contracts @ entry {entry_price}c avg\n"
        "Realized: -${loss_dollars}\n\n"
        "This was a stop-loss approval. Trade closed before resolution."
    ),
)

# ---------------------------------------------------------------------------
# Critical alerts (audible)
# ---------------------------------------------------------------------------

_ALERT_CRITICAL_LLM_CAP = MessageTemplate(
    category="alert_critical",
    audible=True,
    # fields: spend, cap, time_et
    format=(
        "🚨 CRITICAL: LLM monthly cost cap exceeded\n\n"
        "Spend: {spend} / {cap}\n"
        "Action taken: LLM cascade halted, falling back to keyword-only matching\n"
        "Time: {time_et}\n\n"
        "LLM matching is now degraded. Stage 1 keyword filter still active.\n"
        "Some real signals will be missed.\n\n"
        "Resume next month, or raise cap and restart with /resume_llm"
    ),
)

_ALERT_CRITICAL_KALSHI_DISCONNECT = MessageTemplate(
    category="alert_critical",
    audible=True,
    # fields: duration, last_success_et, attempts, last_error
    format=(
        "🚨 CRITICAL: Kalshi feed disconnected for {duration}\n\n"
        "Last successful message: {last_success_et}\n"
        "Reconnection attempts: {attempts} (all failed with {last_error})\n"
        "Live price snapshots paused\n"
        "Decision logic suspended (no current orderbook data)\n\n"
        "Bot will keep retrying. Check Kalshi status page if extended."
    ),
)

_ALERT_CRITICAL_ANTHROPIC_AUTH = MessageTemplate(
    category="alert_critical",
    audible=True,
    # fields: (none — fixed text)
    format=(
        "🚨 CRITICAL: Anthropic API key invalid (401)\n\n"
        "LLM cascade disabled. All articles falling back to keyword-only.\n\n"
        "Required action: regenerate ANTHROPIC_API_KEY in console, update\n"
        "secrets.env, restart daemon. Until fixed, matcher quality is\n"
        "significantly degraded."
    ),
)

_ALERT_CRITICAL_DAEMON_CRASH = MessageTemplate(
    category="alert_critical",
    audible=True,
    # fields: crash_time_et, restart_time_et, gap, exception_summary,
    #         active_positions, crash_filename
    format=(
        "🚨 CRITICAL: Daemon crashed and restarted by systemd\n\n"
        "Crash time: {crash_time_et}\n"
        "Restart time: {restart_time_et} ({gap}s gap)\n"
        "Crash reason: {exception_summary}\n"
        "Active positions during crash: {active_positions} (all preserved in DB)\n\n"
        "Full traceback in logs. Crash sequence saved as\n"
        "data/crashes/{crash_filename} for review."
    ),
)

_ALERT_CRITICAL_CONTRACT_CHANGED = MessageTemplate(
    category="alert_critical",
    audible=True,
    # fields: series, time_et, old_hash, new_hash, diff_excerpt,
    #         old_version, new_version
    format=(
        "🚨 CRITICAL: Kalshi resolution rules changed for {series}\n\n"
        "Detected at: {time_et}\n"
        "Hash changed from {old_hash}... to {new_hash}...\n\n"
        "Diff (first 500 chars):\n{diff_excerpt}\n\n"
        "Action: prompt_version auto-bumped from {old_version} to {new_version}.\n"
        "Cache invalidated. Future articles re-classified under new rules.\n\n"
        "Review the changes manually and confirm the LLM prompt still\n"
        "correctly applies them. If rules changed materially, you may\n"
        "need to update the LLM prompt template."
    ),
)

# ---------------------------------------------------------------------------
# Warning alerts (silent)
# ---------------------------------------------------------------------------

_ALERT_WARNING_SOURCE_DOWN = MessageTemplate(
    category="alert_warning",
    audible=False,
    # fields: source_name, last_success_et, duration_min, attempt_summary,
    #         active_count, total_count
    format=(
        "⚠️ Source down: {source_name}\n"
        "Last successful poll: {last_success_et} ({duration_min} min ago)\n"
        "Last 4 poll attempts: {attempt_summary}\n\n"
        "Bot continuing with {active_count}/{total_count} active sources.\n"
        "{source_name} re-tries continue with exponential backoff."
    ),
)

_ALERT_WARNING_DB_SLOW = MessageTemplate(
    category="alert_warning",
    audible=False,
    # fields: query_duration, threshold
    format=(
        "⚠️ Database performance degraded performance degraded\n\n"
        "Heartbeat query took {query_duration} (threshold {threshold})\n"
        "This may indicate index issue or disk pressure.\n\n"
        "Run `python -m scripts.db_diagnose` for detail."
    ),
)

_ALERT_WARNING_RISK_REJECTION = MessageTemplate(
    category="alert_warning",
    audible=False,
    # fields: ticker, source, time_et, confidence, rejection_reason,
    #         rejection_detail
    format=(
        "⚠️ Trade rejected: {ticker}\n\n"
        "Trigger: {source} report at {time_et}, confidence {confidence}\n"
        "Rejected because: {rejection_reason}\n"
        "{rejection_detail}\n\n"
        "The signal was real. You couldn't take it because of risk caps.\n"
        "Consider whether to /halt for new trades or close an existing\n"
        "position."
    ),
)

# ---------------------------------------------------------------------------
# Info alerts (silent, no notification)
# ---------------------------------------------------------------------------

_ALERT_INFO_MARKET_DISCOVERED = MessageTemplate(
    category="alert_info",
    audible=False,
    # fields: event_ticker, market_count, new_subjects_summary,
    #         removed_subjects_summary, snapshot_path
    format=(
        "📅 New month: {event_ticker} just opened\n\n"
        "Discovered {market_count} markets. {new_subjects_summary}\n\n"
        "{removed_subjects_summary}\n\n"
        "Snapshot saved to data/markets/{snapshot_path}"
    ),
)

_ALERT_INFO_SUBJECT_ENRICHED = MessageTemplate(
    category="alert_info",
    audible=False,
    # fields: subject_full_name, ticker, original_aliases,
    #         added_aliases_bulleted, total_count
    format=(
        "✨ Aliases enriched for {subject_full_name} ({ticker})\n\n"
        "Auto-extracted: {original_aliases}\n"
        "LLM-enriched (added):\n{added_aliases_bulleted}\n\n"
        "Total aliases: {total_count}. Matcher will now catch articles\n"
        "using these forms."
    ),
)

_ALERT_INFO_LLM_SPEND_UPDATE = MessageTemplate(
    category="alert_info",
    audible=False,
    # fields: spend, cap, pct, projected, remaining
    format=(
        "LLM spend update: {spend} / {cap} ({pct}%)\n\n"
        "At current pace, projected month-end spend: {projected}\n"
        "Buffer: {remaining} remaining\n\n"
        "If you want to proactively pause LLM, /halt_llm."
    ),
)

_ALERT_WARNING_EVENT_RESOLUTION_RULES_MISSING = MessageTemplate(
    category="alert_warning",
    audible=False,
    # fields: event_ticker
    format=(
        "⚠️ {event_ticker} returned markets with no resolution rules. "
        "Skipping snapshot write; manual review required."
    ),
)

_ALERT_WARNING_MARKET_RESOLUTION_RULES_MISSING = MessageTemplate(
    category="alert_warning",
    audible=False,
    # fields: ticker, full_name
    format=("⚠️ Market {ticker} ({full_name}) returned with no resolution " "rules. Not inserted."),
)

_ALERT_CRITICAL_RESOLUTION_RULES_CHANGED_MIDEVENT = MessageTemplate(
    category="alert_critical",
    audible=True,
    # fields: ticker
    format=(
        "🚨 CRITICAL: {ticker} title or resolution_rules changed "
        "mid-event. Frozen original retained; manual review required."
    ),
)

_ALERT_INFO_SOURCE_RECOVERED = MessageTemplate(
    category="alert_info",
    audible=False,
    # fields: source_name, time_et, outage_duration
    format=(
        "{source_name} back online: first successful poll {time_et}\n"
        "Source caught up after {outage_duration} outage."
    ),
)

# ---------------------------------------------------------------------------
# Command replies
# ---------------------------------------------------------------------------

_COMMAND_REPLY_STATUS = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: execution_mode, approval_mode, halt_status, snoozed_count,
    #         bankroll, deposit_status, open_count, unrealized_pnl,
    #         today_pnl, month_pnl, sources_active, sources_total,
    #         llm_mtd, llm_cap, llm_pct, last_heartbeat, heartbeat_age,
    #         uptime
    format=(
        "🤖 Bot Status\n\n"
        "Mode: {execution_mode} | approval: {approval_mode}\n"
        "Halt: {halt_status} | snoozed: {snoozed_count}\n\n"
        "Bankroll: {bankroll} ({deposit_status})\n"
        "Open positions: {open_count} (unrealized: {unrealized_pnl})\n"
        "Today: {today_pnl} realized\n"
        "This month: {month_pnl} realized\n\n"
        "System: {sources_active}/{sources_total} sources active\n"
        "LLM spend MTD: {llm_mtd} / {llm_cap} ({llm_pct})\n\n"
        "Last heartbeat: {last_heartbeat} ({heartbeat_age} ago)\n"
        "Daemon uptime: {uptime}"
    ),
)

_COMMAND_REPLY_POSITIONS = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: count, position_list (pre-rendered string), total_cost,
    #         total_mtm
    format=(
        "📋 Open Positions ({count})\n\n"
        "{position_list}\n\n"
        "Total cost basis: {total_cost}\n"
        "Total mark-to-market: {total_mtm} unrealized"
    ),
)

# Used by command_reply_positions to format each line; the caller joins
# them with newlines. This is a sub-template, not a top-level message.
_POSITION_LINE = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: ticker, quantity, entry_price, current_price,
    #         unrealized_sign, unrealized_amount, entry_relative_time, source
    format=(
        "{ticker}\n"
        "  {quantity} contracts @ entry {entry_price}c | current "
        "{current_price}c | unrealized {unrealized_sign}{unrealized_amount}\n"
        "  Entry: {entry_relative_time} ({source} trigger)"
    ),
)

_COMMAND_REPLY_WHY = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: trade_id, ticker, subject_full_name, entry_time_et,
    #         quantity, entry_price, total_cost, fees, source,
    #         source_weight, headline, published_time_et, lag, url,
    #         confidence, llm_reasoning, cap_one_amount, cap_one_status,
    #         cap_two_pct, market_volume, cap_two_amount, binding_cap,
    #         slippage, best_ask, expected_roi
    format=(
        "🔍 Trade #{trade_id} reasoning\n\n"
        "Market: {ticker} ({subject_full_name})\n"
        "Entered: {entry_time_et}\n"
        "Quantity: {quantity} contracts @ {entry_price}c avg\n"
        "Total cost: {total_cost} + {fees} fees\n\n"
        "Triggering article:\n"
        "  Source: {source} (weight {source_weight})\n"
        '  Headline: "{headline}"\n'
        "  Published: {published_time_et} ({lag} before trade)\n"
        "  URL: {url}\n\n"
        "LLM classification: confidence {confidence}\n"
        'Reasoning: "{llm_reasoning}"\n\n'
        "Position sizing:\n"
        "  Cap one ({cap_one_amount}): {cap_one_status}\n"
        "  Cap two ({cap_two_pct} of {market_volume} volume): {cap_two_amount}\n"
        "  Binding: {binding_cap}\n\n"
        "Slippage: {slippage}c from best ask ({best_ask}c)\n"
        "Total expected ROI: {expected_roi}% on YES resolution"
    ),
)

_COMMAND_REPLY_HISTORY = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: n, trade_lines (pre-rendered string), wins, losses,
    #         win_rate, total_pnl
    format=(
        "📜 Last {n} closed trades\n\n"
        "{trade_lines}\n\n"
        "{wins}W, {losses}L ({win_rate} win rate)\n"
        "Total: {total_pnl}"
    ),
)

# Sub-template for individual trade lines in /history.
_HISTORY_LINE = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: trade_id, ticker, entry_price, resolution, pnl
    format=("#{trade_id} {ticker} | YES @ {entry_price}c -> settled " "{resolution} | {pnl}"),
)

_COMMAND_REPLY_HALT = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: (none)
    format=(
        "🛑 Trading HALTED\n\n"
        "No new trade proposals will fire.\n"
        "Open positions remain. Stop-loss approvals continue working.\n\n"
        "To resume: /resume\n\n"
        "Reason logged. System events updated."
    ),
)

_COMMAND_REPLY_RESUME = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: time_et, open_count
    format=(
        "✅ Trading RESUMED\n\n"
        "Active again at {time_et}.\n"
        "LLM cascade resumed. Decision engine processing news matches.\n\n"
        "Open positions: {open_count} still active."
    ),
)

_COMMAND_REPLY_SNOOZE = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: ticker, duration, resume_time_et
    format=(
        "🔕 Snoozed: {ticker}\n"
        "Duration: {duration}\n"
        "Resumes: {resume_time_et}\n\n"
        "The bot will not propose new trades on this market until then.\n"
        "Existing position remains. To unsnooze early: /unsnooze {ticker}"
    ),
)

_COMMAND_REPLY_UNSNOOZE = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: ticker
    format=("✅ Unsnoozed: {ticker}\n" "Trading proposals resumed for this market."),
)

_COMMAND_REPLY_HEARTBEAT = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: time_et
    format="✓ {time_et} | I'm alive",
)

_COMMAND_REPLY_SPEND = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: today, week, month, cap, pct, avg_per_call, projected
    format=(
        "💸 LLM Spend\n\n"
        "Today: {today}\n"
        "This week: {week}\n"
        "This month: {month} / {cap} ({pct}%)\n\n"
        "Average per LLM call: {avg_per_call}\n"
        "Projected month-end at current pace: {projected}"
    ),
)

_COMMAND_REPLY_MODE = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: execution_mode, approval_mode, halt_status, snoozed_count
    format=(
        "Current mode:\n"
        "  Execution: {execution_mode}\n"
        "  Approval: {approval_mode}\n"
        "  Halt: {halt_status}\n"
        "  Snoozed: {snoozed_count}\n\n"
        "To change execution mode: requires config edit + restart\n"
        "(safety: not changeable from chat)"
    ),
)

_COMMAND_REPLY_HELP = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: (none)
    format=(
        "Commands:\n"
        "  /status                   bot state, P&L, sources, LLM spend\n"
        "  /positions                open trades + mark-to-market\n"
        "  /why <trade_id>           reasoning for a specific trade\n"
        "  /history [N]              last N closed trades (default 10)\n"
        "  /spend                    LLM spend (today / week / month)\n"
        "  /mode                     current execution + approval mode\n"
        "  /halt                     pause new trade proposals\n"
        "  /resume                   resume new trade proposals\n"
        "  /snooze <ticker> [dur]    snooze one market (e.g. /snooze X 24h)\n"
        "  /unsnooze <ticker>        unsnooze one market\n"
        "  /heartbeat                quick liveness check\n"
        "  /help                     this list"
    ),
)

_COMMAND_REPLY_UNKNOWN = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: command
    format=("Unknown command: {command}\n" "Send /help for the full list."),
)

_COMMAND_REPLY_USAGE_HINT = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: command, usage
    format="Usage: {command} {usage}",
)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

TEMPLATE_CATALOG: dict[str, MessageTemplate] = {
    # Heartbeat + digest
    "heartbeat_periodic": _HEARTBEAT_PERIODIC,
    "daily_digest": _DAILY_DIGEST,
    # Trade proposals (the approval-flow messages)
    "trade_proposal_entry": _TRADE_PROPOSAL_ENTRY,
    "trade_proposal_reentry": _TRADE_PROPOSAL_REENTRY,
    "trade_proposal_stop_loss": _TRADE_PROPOSAL_STOP_LOSS,
    # Trade outcomes (post-settlement / post-stop)
    "trade_settled_yes": _TRADE_SETTLED_YES,
    "trade_settled_no": _TRADE_SETTLED_NO,
    "trade_stopped_out": _TRADE_STOPPED_OUT,
    # Critical alerts (audible)
    "alert_critical_llm_cap": _ALERT_CRITICAL_LLM_CAP,
    "alert_critical_kalshi_disconnect": _ALERT_CRITICAL_KALSHI_DISCONNECT,
    "alert_critical_anthropic_auth": _ALERT_CRITICAL_ANTHROPIC_AUTH,
    "alert_critical_daemon_crash": _ALERT_CRITICAL_DAEMON_CRASH,
    "alert_critical_contract_changed": _ALERT_CRITICAL_CONTRACT_CHANGED,
    # Warning alerts (silent)
    "alert_warning_source_down": _ALERT_WARNING_SOURCE_DOWN,
    "alert_warning_db_slow": _ALERT_WARNING_DB_SLOW,
    "alert_warning_risk_rejection": _ALERT_WARNING_RISK_REJECTION,
    "alert_warning_event_resolution_rules_missing": _ALERT_WARNING_EVENT_RESOLUTION_RULES_MISSING,
    "alert_warning_market_resolution_rules_missing": _ALERT_WARNING_MARKET_RESOLUTION_RULES_MISSING,
    "alert_critical_resolution_rules_changed_midevent": (
        _ALERT_CRITICAL_RESOLUTION_RULES_CHANGED_MIDEVENT
    ),
    # Info alerts (silent)
    "alert_info_market_discovered": _ALERT_INFO_MARKET_DISCOVERED,
    "alert_info_subject_enriched": _ALERT_INFO_SUBJECT_ENRICHED,
    "alert_info_llm_spend_update": _ALERT_INFO_LLM_SPEND_UPDATE,
    "alert_info_source_recovered": _ALERT_INFO_SOURCE_RECOVERED,
    # Command replies
    "command_reply_status": _COMMAND_REPLY_STATUS,
    "command_reply_positions": _COMMAND_REPLY_POSITIONS,
    "command_reply_why": _COMMAND_REPLY_WHY,
    "command_reply_history": _COMMAND_REPLY_HISTORY,
    "command_reply_halt": _COMMAND_REPLY_HALT,
    "command_reply_resume": _COMMAND_REPLY_RESUME,
    "command_reply_snooze": _COMMAND_REPLY_SNOOZE,
    "command_reply_unsnooze": _COMMAND_REPLY_UNSNOOZE,
    "command_reply_heartbeat": _COMMAND_REPLY_HEARTBEAT,
    "command_reply_spend": _COMMAND_REPLY_SPEND,
    "command_reply_mode": _COMMAND_REPLY_MODE,
    "command_reply_help": _COMMAND_REPLY_HELP,
    "command_reply_unknown": _COMMAND_REPLY_UNKNOWN,
    "command_reply_usage_hint": _COMMAND_REPLY_USAGE_HINT,
    # Sub-templates exposed for callers that need to render rows /
    # lines that get joined into a parent template.
    "_position_line": _POSITION_LINE,
    "_history_line": _HISTORY_LINE,
}


# ---------------------------------------------------------------------------
# UI literals -- button labels rendered as inline-keyboard buttons.
# Kept here (not as MessageTemplate entries) so the single-source-of-
# truth invariant covers the entire user-facing surface, not just text
# messages. Edit here, not in telegram_bot.py.
# ---------------------------------------------------------------------------

BUTTON_APPROVE_LABEL = "✅ Approve"
BUTTON_REJECT_LABEL = "❌ Reject"


__all__ = [
    "BUTTON_APPROVE_LABEL",
    "BUTTON_REJECT_LABEL",
    "TEMPLATE_CATALOG",
    "Category",
    "MessageTemplate",
    "RenderedMessage",
    "render_template",
]
