"""Catalog tests for trumpbot/notifications/templates.py.

Two invariants pinned here:

1. Every template in :data:`TEMPLATE_CATALOG` renders successfully when
   called with a sample data dict containing every field its format
   string declares. This catches the "I added a field to the template
   but forgot to pass it from the call site" failure mode at the
   boundary, before the daemon tries to send to Telegram.

2. Audibility tier is enforced: only ``alert_critical_*`` templates
   may have ``audible=True``. Everything else (heartbeat, digest,
   warnings, info, command replies, trade outcomes, trade proposals)
   is silent.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from trumpbot.notifications.templates import (
    TEMPLATE_CATALOG,
    MessageTemplate,
    RenderedMessage,
    render_template,
)

# Discover the named fields used by every template via a regex over the
# format string. .format() field names are inside braces; we ignore
# escaped braces ``{{`` and ``}}``. This mirrors how Python's str.format
# parses fields.
_FIELD_RE = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _fields_used(template: MessageTemplate) -> set[str]:
    return set(_FIELD_RE.findall(template.format))


def _sample_value_for(field: str) -> Any:
    """Pick a plausible sample value per field name. Not perfect but
    good enough to render without TypeError."""
    if field.endswith(("_count", "_id", "_total", "_active", "_n")) or field == "n":
        return 5
    if field == "count":
        return 5
    if field.endswith(("_pct", "_min", "_dollars", "_amount")):
        return "$12.34"
    if field.endswith(("_et", "_time")):
        return "2026-04-25 14:23 ET"
    if field.endswith(("_pnl",)):
        return "+$23.40"
    return f"<{field}>"


# ---------------------------------------------------------------------------
# Catalog-wide invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TEMPLATE_CATALOG.keys()))
def test_every_template_renders_with_sample_data(name: str) -> None:
    template = TEMPLATE_CATALOG[name]
    fields = _fields_used(template)
    data = {f: _sample_value_for(f) for f in fields}
    rendered = render_template(name, data)
    assert isinstance(rendered, RenderedMessage)
    assert rendered.template_name == name
    assert rendered.text  # non-empty


def test_only_critical_alerts_are_audible() -> None:
    """Audibility tier from the spec: only alert_critical_* messages
    push an audible Telegram notification. Everything else (heartbeats,
    digest, warnings, info, command replies, trade outcomes,
    proposals) sends with disable_notification=True."""
    audible_names = {n for n, t in TEMPLATE_CATALOG.items() if t.audible}
    expected = {
        "alert_critical_llm_cap",
        "alert_critical_kalshi_disconnect",
        "alert_critical_anthropic_auth",
        "alert_critical_daemon_crash",
        "alert_critical_contract_changed",
        "alert_critical_resolution_rules_changed_midevent",
        # Phase 4 Part 1: live-mode critical alerts.
        "reconciliation_failed",
        "mode_switched_live",
    }
    assert audible_names == expected


def test_template_categories_match_naming_convention() -> None:
    """alert_critical_* must be category 'alert_critical' (etc.). This
    catches a copy/paste bug where a template's name says 'critical'
    but its category was left as 'info'."""
    for name, tpl in TEMPLATE_CATALOG.items():
        if name.startswith("alert_critical_"):
            assert tpl.category == "alert_critical", name
        elif name.startswith("alert_warning_"):
            assert tpl.category == "alert_warning", name
        elif name.startswith("alert_info_"):
            assert tpl.category == "alert_info", name
        elif name.startswith("trade_proposal_"):
            assert tpl.category == "trade_proposal", name
        elif name.startswith("trade_settled_") or name.startswith("trade_stopped_"):
            assert tpl.category == "trade_outcome", name
        elif name.startswith("command_reply_") or name.startswith("_"):
            assert tpl.category == "command_reply", name
        elif name == "heartbeat_periodic":
            assert tpl.category == "heartbeat"
        elif name == "daily_digest":
            assert tpl.category == "digest"


def test_render_unknown_template_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="unknown template"):
        render_template("nonexistent_name", {})


def test_render_missing_field_raises_keyerror() -> None:
    """If a caller forgets a required field, .format raises KeyError —
    surfacing the bug at the boundary instead of silently sending a
    half-rendered string."""
    with pytest.raises(KeyError):
        render_template("command_reply_heartbeat", {})  # needs time_et


def test_render_extra_fields_are_ignored() -> None:
    """Extra fields in the data dict are fine (Python's .format
    ignores them). Useful when a single data dict is shared across
    multiple templates."""
    rendered = render_template(
        "command_reply_heartbeat",
        {"time_et": "10:00 ET", "extra_unused_field": "ignored"},
    )
    assert "10:00 ET" in rendered.text


def test_rendered_message_carries_audibility() -> None:
    """The audibility flag must travel with the rendered text so the
    Telegram caller can pass it straight to disable_notification."""
    silent = render_template(
        "heartbeat_periodic",
        {
            "time_et": "10:00",
            "open_count": 0,
            "today_pnl": "+$0",
            "llm_today": "$0",
            "llm_cap": "$10",
            "sources_active": 8,
            "sources_total": 8,
        },
    )
    assert silent.audible is False
    audible = render_template("alert_critical_anthropic_auth", {})
    assert audible.audible is True


# ---------------------------------------------------------------------------
# Spot-check critical templates against the spec's exact text
# ---------------------------------------------------------------------------


def test_heartbeat_text_matches_spec() -> None:
    """The exact one-line heartbeat format the spec called for. If
    anyone tweaks it, this fails so the change is visible in PR
    review."""
    out = render_template(
        "heartbeat_periodic",
        {
            "time_et": "14:23",
            "open_count": 3,
            "today_pnl": "+$23.40",
            "llm_today": "$0.84",
            "llm_cap": "$10.00",
            "sources_active": 8,
            "sources_total": 8,
        },
    )
    assert out.text == ("✓ 14:23 | open: 3 | today: +$23.40 | LLM: $0.84/$10.00 | sources: 8/8")


def test_command_help_lists_every_command() -> None:
    """If we add a command we must mention it in /help. Testing by
    grep against the rendered help text — easy to spot a missing
    command in PR review."""
    help_text = render_template("command_reply_help", {}).text
    for cmd in (
        "/status",
        "/positions",
        "/why",
        "/history",
        "/spend",
        "/mode",
        "/halt",
        "/resume",
        "/snooze",
        "/unsnooze",
        "/heartbeat",
        "/help",
    ):
        assert cmd in help_text, f"{cmd} missing from /help text"


def test_alert_critical_anthropic_auth_has_no_required_fields() -> None:
    """Renders with no data — fixed-text alert. Useful to confirm
    callers don't have to scrape stack traces just to fire it."""
    out = render_template("alert_critical_anthropic_auth", {})
    assert "Anthropic API key invalid" in out.text
    assert out.audible is True


__all__: list[str] = []
