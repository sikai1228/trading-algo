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

Adding a template: pick a category (``digest`` / ``trade_outcome``
/ ``trade_proposal`` / ``alert_critical`` / ``alert_warning`` /
``alert_info`` / ``command_reply``), set audibility (``True`` only
for ``alert_critical``; everything else silent), write the format
string. Add a unit test rendering it with a sample data dict.

(Phase 4 Part 2.10 removed the ``heartbeat`` category along with
the periodic-heartbeat templates.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Category = Literal[
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
# Daily digest
# ---------------------------------------------------------------------------
# Phase 4 Part 2.10 — _HEARTBEAT_PERIODIC and the ``heartbeat``
# category were REMOVED. The morning daily digest is the regular
# status notification now.

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
# Trade proposals (Phase 4 Part 2.11 — standardized info categories)
# ---------------------------------------------------------------------------
#
# Per the Deliverable 7 spec, both human approval requests and
# auto-approval confirmations show the same six categories:
#
#   1. When (timestamp ET)
#   2. Market (ticker, subject, full title)
#   3. Entry contract count and price
#   4. Potential P&L (settlement, profit, loss)
#   5. Reasoning (key quote from the article)
#   6. Article link
#
# Human-side templates show "potential" numbers and ask for approval.
# Auto-approval templates show "actual" numbers and inform after
# execution.

# Shared body block (entry + re-entry).
# Required fields: timestamp_et, market_title, ticker, subject_full_name,
#                  avg_fill_price, target_quantity, total_cost,
#                  total_fees, slippage, best_ask, total_commitment,
#                  settlement_value, potential_profit, potential_roi,
#                  potential_loss, source, published_time_et,
#                  article_age_note, headline, key_quote, article_url,
#                  reasoning_text.
_PROPOSAL_BODY_V2 = (
    "⏱ {timestamp_et}\n"
    "📍 {market_title}\n"
    "     Ticker: {ticker}\n"
    "     Subject: {subject_full_name}\n\n"
    "💵 If approved:\n"
    "     Buy YES @ ~{avg_fill_price}c (FOK)\n"
    "     {target_quantity} contracts x ~{avg_fill_price}c = {total_cost}\n"
    "     Plus fees: {total_fees}\n"
    "     Plus slippage: {slippage}c from best ask ({best_ask}c)\n"
    "     Total commitment: {total_commitment}\n\n"
    "📈 If resolves YES:\n"
    "     Settlement: {settlement_value}\n"
    "     Profit: {potential_profit} ({potential_roi})\n\n"
    "📉 If stops out:\n"
    "     Approx loss: -{potential_loss}\n\n"
    "📰 Reasoning:\n"
    "     Source: {source} at {published_time_et}{article_age_note}\n"
    '     Headline: "{headline}"\n'
    '     Key quote: "{key_quote}"\n'
    "     Article: {article_url}\n\n"
    "Cap analysis: cap_one={cap_one_dollars}, "
    "cap_two={cap_two_dollars} "
    "({cap_two_contracts} of {available_contracts} contracts), "
    "binding={cap_binding}.\n\n"
    "Engine reasoning:\n{reasoning_text}\n\n"
    "If book moves unfavorably between approval and execution,\n"
    "order will be killed (no trade).\n"
)

_TRADE_PROPOSAL_ENTRY = MessageTemplate(
    category="trade_proposal",
    audible=False,
    # fields: intent_id_short + every field on _PROPOSAL_BODY_V2.
    format=(
        "💰 TRADE PROPOSAL [#{intent_id_short}]\n\n"
        + _PROPOSAL_BODY_V2
        + "\n⏰ Approve within 3:00 to execute.\n"
        + "[APPROVE] [REJECT] [DETAILS]"
    ),
)

_TRADE_PROPOSAL_REENTRY = MessageTemplate(
    category="trade_proposal",
    audible=False,
    # fields: intent_id_short, prior_trade_id, prior_trade_outcome,
    #         prior_realized_dollars, prior_closed_age, plus every
    #         field on _PROPOSAL_BODY_V2.
    format=(
        "🔄 RE-ENTRY OPPORTUNITY [#{intent_id_short}]\n\n"
        "Prior trade #{prior_trade_id} closed via {prior_trade_outcome}\n"
        "({prior_closed_age} ago).\n"
        "Prior realized P&L: {prior_realized_dollars}\n\n" + _PROPOSAL_BODY_V2 + "\n"
        "[APPROVE] [REJECT]"
    ),
)

# Stop-loss now also follows the standardized layout. Required fields:
# ticker, trade_id, market_title, subject_full_name, timestamp_et,
# entry_price, quantity, cost_basis_dollars, current_bid, drop,
# current_value_dollars, unrealized_dollars, time_held, news_context,
# reasoning_text.
_TRADE_PROPOSAL_STOP_LOSS = MessageTemplate(
    category="trade_proposal",
    audible=False,
    format=(
        "⚠️ STOP-LOSS TRIGGER [trade #{trade_id}]\n\n"
        "⏱ {timestamp_et}\n"
        "📍 {market_title}\n"
        "     Ticker: {ticker}\n"
        "     Subject: {subject_full_name}\n\n"
        "💵 Original entry:\n"
        "     {quantity} contracts @ {entry_price}c\n"
        "     Cost basis: {cost_basis_dollars}\n"
        "     Held for: {time_held}\n\n"
        "📉 Current state:\n"
        "     YES bid: {current_bid}c (drop: {drop}c from entry)\n"
        "     Hold value: {current_value_dollars}\n"
        "     Unrealized P&L: {unrealized_dollars}\n\n"
        "📰 Recent news for {ticker}:\n{news_context}\n\n"
        "Engine reasoning:\n{reasoning_text}\n\n"
        "No timeout -- respond when ready.\n"
        "[EXIT NOW -- market exit] [HOLD -- ride it out]"
    ),
)


# ---------------------------------------------------------------------------
# Auto-approval confirmations (Phase 4 Part 2.11)
# ---------------------------------------------------------------------------

_TRADE_FILLED_AUTO = MessageTemplate(
    category="trade_outcome",
    audible=False,
    # fields: trade_id, timestamp_et, market_title, ticker,
    # subject_full_name, actual_fill_price, filled_quantity,
    # actual_cost, actual_fees, actual_slippage, best_ask_at_send,
    # total_spent, settlement_value, potential_profit, potential_roi,
    # potential_loss, source, published_time_et, article_age_note,
    # signal_to_trade_age, headline, key_quote, article_url.
    format=(
        "✅ AUTO-APPROVED TRADE EXECUTED [trade #{trade_id}]\n\n"
        "⏱ {timestamp_et}  (placed {signal_to_trade_age} after signal)\n"
        "📍 {market_title}\n"
        "     Ticker: {ticker}\n"
        "     Subject: {subject_full_name}\n\n"
        "💵 Filled:\n"
        "     Bought YES @ {actual_fill_price}c avg\n"
        "     {filled_quantity} contracts x {actual_fill_price}c = {actual_cost}\n"
        "     Fees: {actual_fees}\n"
        "     Slippage: {actual_slippage}c from best ask "
        "({best_ask_at_send}c)\n"
        "     Total spent: {total_spent}\n\n"
        "📈 If resolves YES:\n"
        "     Settlement: {settlement_value}\n"
        "     Profit: {potential_profit} ({potential_roi})\n\n"
        "📉 If stops out:\n"
        "     Approx loss: -{potential_loss}\n\n"
        "📰 Reasoning:\n"
        "     Source: {source} at {published_time_et}{article_age_note}\n"
        '     Headline: "{headline}"\n'
        '     Key quote: "{key_quote}"\n'
        "     Article: {article_url}\n\n"
        "⚠️ This trade was placed automatically (auto-approval mode)."
    ),
)

_TRADE_KILLED_AUTO = MessageTemplate(
    category="trade_outcome",
    audible=False,
    # fields: intent_id_short, timestamp_et, market_title, ticker,
    # kill_reason, kill_kind, target_quantity, target_avg_fill,
    # source, article_url.
    format=(
        "⚠️ AUTO-APPROVED TRADE KILLED [#{intent_id_short}]\n\n"
        "⏱ {timestamp_et}\n"
        "📍 {market_title} ({ticker})\n\n"
        "Reason: {kill_reason}\n"
        "(kind: {kill_kind})\n\n"
        "Original target: {target_quantity} contracts at "
        "~{target_avg_fill}c\n"
        "No trade was placed.\n\n"
        "Source: {source}\n"
        "Article: {article_url}"
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

_ALERT_CRITICAL_AUTO_APPROVAL_ENABLED = MessageTemplate(
    category="alert_critical",
    audible=True,
    # fields: time_et
    # Phase 4 Part 2.11 — fired once on daemon startup whenever
    # ``cfg.approval.mode == "auto"``. Makes accidental auto-mode
    # highly visible.
    format=(
        "🚨 AUTO-APPROVAL MODE ACTIVE\n\n"
        "Daemon started with approval.mode=auto.\n\n"
        "Entry trades will fire WITHOUT manual approval.\n"
        "Stop-loss and re-entry approvals are still required.\n\n"
        "Started at: {time_et}\n\n"
        "To disable: set approval.mode=human in config.yaml and restart."
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

_ALERT_CRITICAL_CONTRACT_RULES_CHANGED = MessageTemplate(
    category="alert_critical",
    audible=True,
    # fields: old_hash, new_hash
    # Phase 4 Part 2.8 — emitted by the LLM classifier when the
    # ``data/contracts/kxtrumpmeet_rules.txt`` SHA-256 hash drifts
    # mid-process. Stripped-down version of
    # ``alert_critical_contract_changed`` since the classifier path
    # doesn't have a diff or prompt-version bump to surface.
    format=(
        "🚨 CRITICAL: contract rules file changed mid-run.\n\n"
        "Old hash: {old_hash}...\n"
        "New hash: {new_hash}...\n\n"
        "The classifier loaded the new content; future articles use\n"
        "the new rules. Re-run scripts/snapshot_contract.py if Kalshi\n"
        "updated the contract; otherwise investigate the file edit."
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

# PR #33 — fired when a source returns 200 / 304 normally but the
# parsed feed's newest item is older than the rotation_paused
# threshold (default 12 h). Distinct from `alert_warning_source_down`
# (which fires on absent successful polls); rotation_paused fires on
# present-but-stale feed contents. Deduped per source per 24 h via
# the dispatcher's window_seconds_override.
_ALERT_WARNING_SOURCE_ROTATION_PAUSED = MessageTemplate(
    category="alert_warning",
    audible=False,
    # fields: source_name, newest_item_et, duration_ago,
    #         active_count, total_count
    format=(
        "⚠️ Source rotation paused: {source_name}\n\n"
        "Last article in feed: {newest_item_et} ({duration_ago} ago)\n"
        "Feed is returning 200 OK but no new content.\n\n"
        "Bot continuing with {active_count}/{total_count} active sources.\n"
        "This source may have been deprioritized by the publisher;\n"
        "monitor for resumption."
    ),
)

_ALERT_WARNING_DB_SLOW = MessageTemplate(
    category="alert_warning",
    audible=False,
    # fields: query_duration, threshold
    format=(
        "⚠️ Database performance degraded\n\n"
        "Diagnostic query took {query_duration} (threshold {threshold})\n"
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
# Pre-live fix #2 — bankroll sync auto-halt + auto-resume
# ---------------------------------------------------------------------------

_ALERT_CRITICAL_BANKROLL_SYNC_FAILED = MessageTemplate(
    category="alert_critical",
    audible=True,
    # fields: failure_count, first_failure_time, last_error,
    #         last_success_time, age
    format=(
        "🚨 CRITICAL: Kalshi balance sync failing\n\n"
        "Failed {failure_count} consecutive times since {first_failure_time}.\n"
        "Last error: {last_error}\n"
        "Last successful sync: {last_success_time} ({age} ago)\n\n"
        "Trading halted to prevent decisions on stale balance.\n"
        "Will auto-resume when sync succeeds.\n\n"
        "If outage continues, check Kalshi status page."
    ),
)

_ALERT_INFO_BANKROLL_SYNC_RECOVERED = MessageTemplate(
    category="alert_info",
    audible=False,
    # fields: time_et, balance
    format=(
        "✅ Kalshi balance sync recovered\n\n"
        "Successful sync at {time_et}.\n"
        "Reported balance: {balance}\n\n"
        "Auto-halt cleared (was set by bankroll_sync_loop)."
    ),
)

# ---------------------------------------------------------------------------
# Command replies
# ---------------------------------------------------------------------------

_COMMAND_REPLY_STATUS = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: execution_mode, approval_mode, halt_status,
    #         bankroll, deposit_status, open_count, unrealized_pnl,
    #         today_pnl, month_pnl, sources_active, sources_total,
    #         llm_mtd, llm_cap, llm_pct, uptime
    # Phase 4 Part 2.10 — last_heartbeat / heartbeat_age dropped
    # along with the heartbeat loop. ``uptime`` is the relevant
    # liveness indicator now.
    format=(
        "🤖 Bot Status\n\n"
        "Mode: {execution_mode} | approval: {approval_mode}\n"
        "Halt: {halt_status}\n\n"
        "Bankroll: {bankroll} ({deposit_status})\n"
        "Open positions: {open_count} (unrealized: {unrealized_pnl})\n"
        "Today: {today_pnl} realized\n"
        "This month: {month_pnl} realized\n\n"
        "System: {sources_active}/{sources_total} sources active\n"
        "LLM spend MTD: {llm_mtd} / {llm_cap} ({llm_pct})\n\n"
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
    #         headline, published_time_et, lag, url,
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
        "  Source: {source}\n"
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

# Phase 4 Part 2.9 — _COMMAND_REPLY_SNOOZE / _COMMAND_REPLY_UNSNOOZE
# were REMOVED with the /snooze and /unsnooze commands. /halt + /resume
# are the global override; per-ticker snooze is gone.

# Phase 4 Part 2.10 — _COMMAND_REPLY_HEARTBEAT was REMOVED with the
# /heartbeat command. /status answers the on-demand "is it alive?"
# question with richer information.

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
    # fields: execution_mode, approval_mode, halt_status
    format=(
        "Current mode:\n"
        "  Execution: {execution_mode}\n"
        "  Approval: {approval_mode}\n"
        "  Halt: {halt_status}\n\n"
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
        "  /status                       bot state, P&L, sources, LLM spend\n"
        "  /positions                    open trades + mark-to-market\n"
        "  /why <trade_id>               reasoning for a specific trade\n"
        "  /history [N]                  last N closed trades (default 10)\n"
        "  /spend                        LLM spend (today / week / month)\n"
        "  /mode                         current execution + approval mode\n"
        "  /halt                         pause new trade proposals\n"
        "  /resume                       resume new trade proposals\n"
        "  /shadow_report [Nd]           auto-approve simulation (default 7d)\n"
        "  /reconcile_resolve <trade_id> acknowledge a reconcile drift row\n"
        "  /tax_summary [year]           year-to-date realized gains / losses\n"
        "  /tax_export [year] [format]   filing-ready CSV / JSON / Form 8949\n"
        "  /tax_reconcile [year]         Kalshi 1099-B reconciliation report\n"
        "  /help                         this list"
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
# Phase 4 — live trading lifecycle notifications
# ---------------------------------------------------------------------------

_TRADE_FILLED_LIVE = MessageTemplate(
    category="trade_outcome",
    audible=False,
    # fields: ticker, quantity, fill_price, total_cost_dollars, fees_dollars,
    #         kalshi_order_id, slippage, target_avg
    format=(
        "✅ Live fill: {ticker}\n"
        "{quantity} contracts @ avg {fill_price}c\n"
        "Total cost: {total_cost_dollars} (+{fees_dollars} fees)\n\n"
        "Slippage from target: {slippage}c (target avg {target_avg}c)\n"
        "Kalshi order: {kalshi_order_id}"
    ),
)

_TRADE_KILLED_BOOK_MOVED_LIVE = MessageTemplate(
    category="trade_outcome",
    audible=False,
    # fields: ticker, target_avg, target_qty, new_avg, new_qty
    format=(
        "🛑 Live order killed (book moved): {ticker}\n\n"
        "Target avg fill: {target_avg}c for {target_qty} contracts\n"
        "Re-walk produced: {new_avg}c for {new_qty} contracts\n\n"
        "Order was NOT submitted. No position opened.\n"
        "Re-trigger requires fresh news signal."
    ),
)

_TRADE_KILLED_NO_FILL_LIVE = MessageTemplate(
    category="trade_outcome",
    audible=False,
    # fields: ticker, target_qty, filled_qty
    format=(
        "🛑 Live order killed (no fill): {ticker}\n\n"
        "FOK rejected by Kalshi. Filled {filled_qty} of {target_qty} "
        "contracts (FOK requires all-or-nothing).\n\n"
        "Order canceled. No position opened.\n"
        "Common cause: order book emptied between approval and submission."
    ),
)

_TRADE_ERROR_VALIDATION = MessageTemplate(
    category="alert_warning",
    audible=False,
    # fields: ticker, detail, client_order_id
    format=(
        "⚠️ Live order rejected by Kalshi (validation): {ticker}\n\n"
        "Detail: {detail}\n"
        "Client order id: {client_order_id}\n\n"
        "This indicates a code bug -- the request was malformed.\n"
        "Trade row tagged error_validation. Investigate logs."
    ),
)

_TRADE_ERROR_TRANSIENT = MessageTemplate(
    category="alert_warning",
    audible=False,
    # fields: ticker, detail, client_order_id
    format=(
        "⚠️ Live order may have failed (transient): {ticker}\n\n"
        "Network or 5xx error: {detail}\n"
        "Client order id: {client_order_id}\n\n"
        "We don't know if the order landed. Trade row tagged\n"
        "error_transient. On next restart, reconciliation will look\n"
        "this up by client_order_id and recover the real state."
    ),
)

_RECONCILIATION_FAILED = MessageTemplate(
    category="alert_critical",
    audible=True,
    # fields: detail
    format=(
        "🚨 CRITICAL: startup reconciliation failed\n\n"
        "Couldn't reach Kalshi for order/position lookup: {detail}\n\n"
        "Trading loops are GATED until reconciliation succeeds. The\n"
        "daemon will keep retrying. No new orders will go out until\n"
        "this clears."
    ),
)

_RECONCILIATION_DRIFT = MessageTemplate(
    category="alert_warning",
    audible=False,
    # fields: drift_summary
    format=(
        "⚠️ Reconciliation drift detected\n\n"
        "{drift_summary}\n\n"
        "Use /reconcile_resolve <trade_id> to acknowledge each.\n"
        "See system_events table for the full audit trail."
    ),
)

_RECONCILIATION_OK = MessageTemplate(
    category="alert_info",
    audible=False,
    # fields: pending_count, live_count, kalshi_position_count
    format=(
        "✅ Reconciliation clean\n\n"
        "Pending: {pending_count} | Live: {live_count} | "
        "Kalshi positions: {kalshi_position_count}\n"
        "No drift detected. Trading loops starting."
    ),
)

_MODE_SWITCHED_LIVE = MessageTemplate(
    category="alert_critical",
    audible=True,
    # fields: bankroll, time_et
    format=(
        "🟢 LIVE TRADING ENABLED\n\n"
        "Execution mode is now: live\n"
        "Bankroll synced: {bankroll}\n"
        "Time: {time_et}\n\n"
        "Real money is now at risk. Every trade still requires your\n"
        "approval in Telegram. Use /halt to pause new proposals."
    ),
)

_MODE_SWITCHED_DRY_RUN = MessageTemplate(
    category="alert_info",
    audible=False,
    # fields: time_et
    format=(
        "🔵 Dry-run mode active\n\n"
        "Execution mode is now: dry_run\n"
        "Time: {time_et}\n\n"
        "All trades are simulated; no real money is at risk."
    ),
)

_COMMAND_REPLY_SHADOW_REPORT = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: window, total_proposals, approved_count, rejected_count,
    #         expired_count, avg_decision_lag, avg_price_movement,
    #         hypothetical_pnl_diff
    format=(
        "🕯️ Shadow Auto-Approval Report ({window})\n\n"
        "Total proposals: {total_proposals}\n"
        "  Approved by you: {approved_count}\n"
        "  Rejected by you: {rejected_count}\n"
        "  Expired (timeout): {expired_count}\n\n"
        "Average decision lag: {avg_decision_lag}\n"
        "Average price movement during lag: {avg_price_movement}\n\n"
        "Hypothetical P&L difference if auto-approved: {hypothetical_pnl_diff}\n"
        "(positive == auto-approve would have done better)\n\n"
        "Note: auto-approve is HARDCODED OFF in v1. This is data only."
    ),
)

_COMMAND_REPLY_RECONCILE_RESOLVE = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: trade_id, action_taken
    format=(
        "✅ Reconciliation resolved for trade #{trade_id}\n\n"
        "Action: {action_taken}\n\n"
        "Drift acknowledged and recorded in system_events."
    ),
)

_COMMAND_REPLY_RECONCILE_RESOLVE_NOT_FOUND = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: trade_id
    format=(
        "Trade #{trade_id} not found, or not in a reconcile-pending state.\n"
        "Try /positions to list active rows."
    ),
)


# ---------------------------------------------------------------------------
# Phase 4 Part 2.1 — tax tracking + exports + monthly digest
# ---------------------------------------------------------------------------

_COMMAND_REPLY_TAX_SUMMARY = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: year, total_trades, closed_trades, open_trades, wins,
    #         losses, win_rate, total_gain, total_loss, net_pnl,
    #         largest_gain, largest_gain_market, largest_loss,
    #         largest_loss_market, total_fees, total_slippage,
    #         avg_holding_days
    format=(
        "📊 Tax Summary -- {year}\n\n"
        "Total trades: {total_trades}\n"
        "Closed: {closed_trades} | Open: {open_trades}\n\n"
        "Wins: {wins} | Losses: {losses}\n"
        "Win rate: {win_rate}%\n\n"
        "Total realized gain: {total_gain}\n"
        "Total realized loss: {total_loss}\n"
        "Net P&L: {net_pnl}\n\n"
        "Largest single gain: ${largest_gain} ({largest_gain_market})\n"
        "Largest single loss: ${largest_loss} ({largest_loss_market})\n\n"
        "Total fees paid: {total_fees}\n"
        "Total slippage: {total_slippage}\n\n"
        "Average holding period: {avg_holding_days} days\n\n"
        "All trades are short-term capital gains (held under 1 year).\n"
        "Use /tax_export to generate filing-ready CSV."
    ),
)

_COMMAND_REPLY_TAX_EXPORT = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: year, format, count, net_pnl, file_path, use_case_description
    format=(
        "💾 Tax Export -- {year}\n\n"
        "Format: {format}\n"
        "Trades exported: {count}\n"
        "Total P&L: {net_pnl}\n\n"
        "File saved: {file_path}\n\n"
        "This file is suitable for {use_case_description}.\n"
        "Year-end totals match the /tax_summary output."
    ),
)

_COMMAND_REPLY_TAX_RECONCILE = MessageTemplate(
    category="command_reply",
    audible=False,
    # fields: year, total_proceeds, total_cost, net_pnl, line_count,
    #         file_path
    format=(
        "🔍 Kalshi 1099-B Reconciliation -- {year}\n\n"
        "Total proceeds:  {total_proceeds}\n"
        "Total cost basis: {total_cost}\n"
        "Net P&L:          {net_pnl}\n\n"
        "Per-trade detail: {line_count} lines\n"
        "File saved: {file_path}\n\n"
        "Compare these totals against Kalshi's 1099-B when issued.\n"
        "Use /tax_export 8949 for IRS Form 8949 layout."
    ),
)

_MONTHLY_TAX_DIGEST = MessageTemplate(
    category="digest",
    audible=False,
    # fields: month_name, year, count, wins, losses, win_rate, pnl,
    #         fees, slippage, largest_gain, largest_gain_ticker,
    #         largest_loss, largest_loss_ticker, avg_holding_days,
    #         month, ytd_pnl
    format=(
        "📊 Monthly Tax Digest -- {month_name} {year}\n\n"
        "Trades closed: {count}\n"
        "Wins: {wins} | Losses: {losses} | Win rate: {win_rate}%\n\n"
        "Realized P&L: {pnl}\n"
        "Total fees paid: {fees}\n"
        "Total slippage: {slippage}\n\n"
        "Largest gain: ${largest_gain} on {largest_gain_ticker}\n"
        "Largest loss: ${largest_loss} on {largest_loss_ticker}\n\n"
        "Average holding period: {avg_holding_days} days\n\n"
        "CSV export saved to data/exports/monthly/{year}-{month}.csv\n\n"
        "Year-to-date P&L: {ytd_pnl}"
    ),
)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

TEMPLATE_CATALOG: dict[str, MessageTemplate] = {
    # Daily digest
    "daily_digest": _DAILY_DIGEST,
    # Trade proposals (the approval-flow messages)
    "trade_proposal_entry": _TRADE_PROPOSAL_ENTRY,
    "trade_proposal_reentry": _TRADE_PROPOSAL_REENTRY,
    "trade_proposal_stop_loss": _TRADE_PROPOSAL_STOP_LOSS,
    # Trade outcomes (post-settlement / post-stop / auto-approval)
    "trade_settled_yes": _TRADE_SETTLED_YES,
    "trade_settled_no": _TRADE_SETTLED_NO,
    "trade_stopped_out": _TRADE_STOPPED_OUT,
    "trade_filled_auto": _TRADE_FILLED_AUTO,
    "trade_killed_auto": _TRADE_KILLED_AUTO,
    # Critical alerts (audible)
    "alert_critical_llm_cap": _ALERT_CRITICAL_LLM_CAP,
    "alert_critical_kalshi_disconnect": _ALERT_CRITICAL_KALSHI_DISCONNECT,
    "alert_critical_anthropic_auth": _ALERT_CRITICAL_ANTHROPIC_AUTH,
    "alert_critical_auto_approval_enabled": _ALERT_CRITICAL_AUTO_APPROVAL_ENABLED,
    "alert_critical_daemon_crash": _ALERT_CRITICAL_DAEMON_CRASH,
    "alert_critical_contract_changed": _ALERT_CRITICAL_CONTRACT_CHANGED,
    "alert_critical_contract_rules_changed": _ALERT_CRITICAL_CONTRACT_RULES_CHANGED,
    # Warning alerts (silent)
    "alert_warning_source_down": _ALERT_WARNING_SOURCE_DOWN,
    "alert_warning_source_rotation_paused": _ALERT_WARNING_SOURCE_ROTATION_PAUSED,
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
    # Phase 4 Part 2.2 (pre-live fix #2) — bankroll sync auto-halt
    "alert_critical_bankroll_sync_failed": _ALERT_CRITICAL_BANKROLL_SYNC_FAILED,
    "alert_info_bankroll_sync_recovered": _ALERT_INFO_BANKROLL_SYNC_RECOVERED,
    # Command replies
    "command_reply_status": _COMMAND_REPLY_STATUS,
    "command_reply_positions": _COMMAND_REPLY_POSITIONS,
    "command_reply_why": _COMMAND_REPLY_WHY,
    "command_reply_history": _COMMAND_REPLY_HISTORY,
    "command_reply_halt": _COMMAND_REPLY_HALT,
    "command_reply_resume": _COMMAND_REPLY_RESUME,
    "command_reply_spend": _COMMAND_REPLY_SPEND,
    "command_reply_mode": _COMMAND_REPLY_MODE,
    "command_reply_help": _COMMAND_REPLY_HELP,
    "command_reply_unknown": _COMMAND_REPLY_UNKNOWN,
    "command_reply_usage_hint": _COMMAND_REPLY_USAGE_HINT,
    # Phase 4 — live trading lifecycle
    "trade_filled_live": _TRADE_FILLED_LIVE,
    "trade_killed_book_moved_live": _TRADE_KILLED_BOOK_MOVED_LIVE,
    "trade_killed_no_fill_live": _TRADE_KILLED_NO_FILL_LIVE,
    "trade_error_validation": _TRADE_ERROR_VALIDATION,
    "trade_error_transient": _TRADE_ERROR_TRANSIENT,
    "reconciliation_failed": _RECONCILIATION_FAILED,
    "reconciliation_drift": _RECONCILIATION_DRIFT,
    "reconciliation_ok": _RECONCILIATION_OK,
    "mode_switched_live": _MODE_SWITCHED_LIVE,
    "mode_switched_dry_run": _MODE_SWITCHED_DRY_RUN,
    "command_reply_shadow_report": _COMMAND_REPLY_SHADOW_REPORT,
    "command_reply_reconcile_resolve": _COMMAND_REPLY_RECONCILE_RESOLVE,
    "command_reply_reconcile_resolve_not_found": _COMMAND_REPLY_RECONCILE_RESOLVE_NOT_FOUND,
    # Phase 4 Part 2.1 — tax tracking
    "command_reply_tax_summary": _COMMAND_REPLY_TAX_SUMMARY,
    "command_reply_tax_export": _COMMAND_REPLY_TAX_EXPORT,
    "command_reply_tax_reconcile": _COMMAND_REPLY_TAX_RECONCILE,
    "monthly_tax_digest": _MONTHLY_TAX_DIGEST,
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
