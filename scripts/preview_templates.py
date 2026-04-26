"""Render every Telegram message template with synthetic data.

Useful for reviewing copy without running the full daemon. Run:

    uv run python -m scripts.preview_templates [name_substring]

With no argument, renders every template. With a substring, renders
only templates whose name contains it (e.g. ``preview_templates
alert`` for just the alert-class messages).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

# Allow running as `python scripts/preview_templates.py` from repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trumpbot.notifications.templates import (  # noqa: E402
    TEMPLATE_CATALOG,
    render_template,
)

_FIELD_RE = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


# A small library of plausible sample values, indexed by field name. New
# fields fall back to the placeholder ``<field_name>`` so the preview
# still renders. Add to this dict as you add templates.
_SAMPLES: dict[str, Any] = {
    # Daily digest + shared
    "time_et": "14:23 ET",
    "open_count": 3,
    "today_pnl": "+$23.40",
    "llm_today": "$0.84",
    "llm_cap": "$10.00",
    "sources_active": 8,
    "sources_total": 8,
    "date": "2026-04-25",
    "closed_count": 4,
    "wins": 3,
    "losses": 1,
    "win_rate": "75%",
    "pnl_yesterday": "+$45.20",
    "unrealized_pnl": "+$8.10",
    "pnl_week": "+$120.30",
    "pnl_month": "+$340.00",
    "sources_note": "",
    "critical_count": 0,
    "llm_mtd": "$2.45",
    "llm_pct": "24.5%",
    # Trade proposal body
    "ticker": "KXTRUMPMEET-26APR-XJIN",
    "match_id": 1287,
    "confidence": 0.93,
    "cap_one_dollars": "$20.00",
    "cap_two_dollars": "$425.00",
    "cap_binding": "cap_one",
    "effective_cap_dollars": "$20.00",
    "quantity": 11,
    "avg_fill": 67,
    "slippage": 2,
    "fees_dollars": "$0.18",
    "total_cost_dollars": "$7.73",
    "reasoning_text": (
        "Source reuters_via_gnews classified an article matching "
        "KXTRUMPMEET-26APR-XJIN at confidence 0.93..."
    ),
    "prior_trade_id": 42,
    "prior_trade_outcome": "dry_run_closed_stop",
    "prior_realized_dollars": "-$1.20",
    # Trade outcomes
    "subject_full_name": "Xi Jinping",
    "entry_price": 67,
    "pnl_dollars": "3.27",
    "roi": 42,
    "series": "KXTRUMPMEET",
    "remaining_in_series": 4,
    "stop_status": "no stop fired",
    "loss_dollars": "7.73",
    "resolution_date": "2026-04-30",
    "exit_price": 24,
    # Stop-loss intent (and trade_proposal_stop_loss reuses some)
    "trade_id": 17,
    "current_bid": 24,
    "drop": 51,
    "cost_basis_dollars": "$7.50",
    "current_value_dollars": "$2.40",
    "unrealized_dollars": "-$5.10",
    # Critical alerts
    "spend": "$10.45",
    "cap": "$10.00",
    "duration": "9 min",
    "last_success_et": "2026-04-25 14:14 ET",
    "attempts": 7,
    "last_error": "TimeoutError",
    "crash_time_et": "2026-04-25 14:00 ET",
    "restart_time_et": "2026-04-25 14:00:18 ET",
    "gap": 18,
    "exception_summary": "ValueError: invalid orderbook payload",
    "active_positions": 2,
    "crash_filename": "2026-04-25T14-00-00Z.log",
    "old_hash": "abc123",
    "new_hash": "def456",
    "diff_excerpt": "+ A qualifying interaction now requires...",
    "old_version": "v1",
    "new_version": "v2",
    # Phase 4 Part 2.2 (pre-live fix #2): bankroll sync auto-halt
    "failure_count": 3,
    "first_failure_time": "2026-04-26 14:08:42 UTC",
    "age": "16m",
    "balance": "$127.43",
    # Warning alerts
    "source_name": "ap_via_gnews",
    "duration_min": 35,
    "attempt_summary": "fail, fail, fail, fail",
    "active_count": 7,
    "total_count": 8,
    "query_duration": "612 ms",
    "threshold": "500 ms",
    "source": "reuters_via_gnews",
    "rejection_reason": "size_cap_below_one_contract",
    "rejection_detail": "per-trade cap of $0.50 too tight for one contract at 90c",
    # Info alerts
    "event_ticker": "KXTRUMPMEET-26MAY",
    "market_count": 9,
    "new_subjects_summary": "8 new subjects, 1 returning.",
    "removed_subjects_summary": "Removed: trudeau (April market resolved)",
    "snapshot_path": "kxtrumpmeet_26may.json",
    "original_aliases": '["thune", "john thune"]',
    "added_aliases_bulleted": (
        "  - senator thune\n"
        "  - sen. thune\n"
        "  - majority leader thune\n"
        "  - senate majority leader"
    ),
    "pct": 24.5,
    "projected": "$10.00",
    "remaining": "$7.55",
    "outage_duration": "12 min",
    # Command replies
    "execution_mode": "dry_run",
    "approval_mode": "human",
    "halt_status": "off",
    "bankroll": "$500.00",
    "deposit_status": "Kalshi balance reflects this amount",
    "month_pnl": "+$340.00",
    # Phase 4 Part 2.10 — last_heartbeat / heartbeat_age dropped
    # along with the heartbeat loop. uptime is the relevant
    # liveness indicator now.
    "uptime": "3d 4h",
    "position_list": (
        "KXTRUMPMEET-26APR-XJIN\n"
        "  11 contracts @ entry 67c | current 78c | unrealized +$1.21\n"
        "  Entry: 2h ago (reuters trigger)"
    ),
    "total_cost": "$7.73",
    "total_mtm": "+$8.10",
    "entry_time_et": "2026-04-25 12:14 ET",
    "fees": "$0.18",
    "headline": "Trump and Xi Jinping hold a phone call.",
    "published_time_et": "2026-04-25 12:09 ET",
    "lag": "5 min",
    "url": "https://example.com/article",
    "llm_reasoning": (
        "Article describes a phone call between Trump and Xi, which "
        "qualifies as an interaction per the market's resolution rules."
    ),
    "cap_one_amount": "$20.00",
    "cap_one_status": "binds",
    "cap_two_pct": "5%",
    "market_volume": "8500",
    "cap_two_amount": "$425.00",
    "binding_cap": "cap_one",
    "best_ask": 65,
    "expected_roi": 42,
    "n": 10,
    "trade_lines": (
        "#42 KXTRUMPMEET-26APR-PUTIN | YES @ 50c -> settled YES | +$5.00\n"
        "#41 KXTRUMPMEET-26APR-XJIN | YES @ 67c -> settled NO | -$7.73"
    ),
    "total_pnl": "+$28.40",
    "today": "$0.84",
    "week": "$2.10",
    "month": "$2.45",
    "avg_per_call": "$0.001",
    "current_price": 78,
    "unrealized_sign": "+",
    "unrealized_amount": "$1.21",
    "entry_relative_time": "2h ago",
    "command": "/halt",
    "usage": "(no args)",
    "resolution": "YES",
    "pnl": "+$5.00",
}


def _sample_for(field: str) -> Any:
    if field in _SAMPLES:
        return _SAMPLES[field]
    return f"<{field}>"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "filter",
        nargs="?",
        default="",
        help="Optional substring; only render templates whose name contains it",
    )
    args = parser.parse_args()

    names = [n for n in TEMPLATE_CATALOG if args.filter in n]
    if not names:
        print(f"no templates match {args.filter!r}", file=sys.stderr)
        return 1

    for name in names:
        template = TEMPLATE_CATALOG[name]
        fields = set(_FIELD_RE.findall(template.format))
        data = {f: _sample_for(f) for f in fields}
        rendered = render_template(name, data)
        bar = "─" * 60
        print(bar)
        print(f" {name}  [category={rendered.category}, audible={rendered.audible}]")
        print(bar)
        print(rendered.text)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
