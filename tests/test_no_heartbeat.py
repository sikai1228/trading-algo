"""Phase 4 Part 2.10 regression tests — heartbeat is gone.

Pin the surface stays gone:

- The /heartbeat command is unrecognized (dispatcher returns None).
- ``HeartbeatLogger`` is no longer importable from trumpbot.daemon.
- ``heartbeat_loop`` and ``_build_heartbeat_data`` are no longer
  importable from trumpbot.notifications.scheduled.
- ``heartbeat_periodic`` and ``command_reply_heartbeat`` are no
  longer in TEMPLATE_CATALOG.
- The ``heartbeat`` Category literal is gone from templates.py.
- /status template no longer renders a "Last heartbeat:" line.
- /help no longer mentions /heartbeat.

Catches a revert that brings the heartbeat plumbing back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trumpbot.notifications.commands import all_command_names, dispatch
from trumpbot.notifications.templates import TEMPLATE_CATALOG, render_template


def test_heartbeat_command_returns_none() -> None:
    assert dispatch("heartbeat") is None
    assert dispatch("/heartbeat") is None
    assert dispatch("HEARTBEAT") is None
    assert "/heartbeat" not in set(all_command_names())


def test_heartbeat_logger_class_is_gone() -> None:
    from trumpbot import daemon

    assert not hasattr(daemon, "HeartbeatLogger"), (
        "trumpbot.daemon.HeartbeatLogger was removed in Phase 4 Part 2.10. "
        "If it's back, revert the revert."
    )


def test_scheduled_heartbeat_helpers_are_gone() -> None:
    from trumpbot.notifications import scheduled

    for name in (
        "heartbeat_loop",
        "_build_heartbeat_data",
        "_seconds_until_next_aligned_tick",
    ):
        assert not hasattr(scheduled, name), (
            f"trumpbot.notifications.scheduled.{name} was removed in "
            "Phase 4 Part 2.10. If it's back, revert the revert."
        )


def test_heartbeat_templates_not_in_catalog() -> None:
    for name in ("heartbeat_periodic", "command_reply_heartbeat"):
        assert name not in TEMPLATE_CATALOG, f"Template {name!r} was removed in Phase 4 Part 2.10."


def test_heartbeat_category_literal_removed() -> None:
    """The ``heartbeat`` Category literal was dropped from
    templates.py. Verify it's not in any rendered template's
    declared category. Compare via the underlying string so the
    Literal-narrowed type doesn't make this a tautology at the
    static-type level."""
    for tpl in TEMPLATE_CATALOG.values():
        assert (
            str(tpl.category) != "heartbeat"
        ), "No template should still claim the heartbeat category"


def test_status_template_has_no_last_heartbeat_line() -> None:
    """Phase 4 Part 2.10 dropped the ``Last heartbeat:`` line from
    ``command_reply_status``. The template now only carries a
    ``Daemon uptime:`` indicator."""
    out = render_template(
        "command_reply_status",
        {
            "execution_mode": "dry_run",
            "approval_mode": "human",
            "halt_status": "off",
            "bankroll": "$500.00",
            "deposit_status": "Kalshi balance reflects this amount",
            "open_count": 0,
            "unrealized_pnl": "+$0",
            "today_pnl": "+$0",
            "month_pnl": "+$0",
            "sources_active": 8,
            "sources_total": 8,
            "llm_mtd": "$0",
            "llm_cap": "$20",
            "llm_pct": "0%",
            "uptime": "3d 4h",
        },
    )
    text = out.text.lower()
    assert "last heartbeat" not in text
    assert "heartbeat_age" not in text
    # Sanity: uptime IS still shown.
    assert "daemon uptime: 3d 4h" in out.text.lower()


def test_help_does_not_advertise_heartbeat() -> None:
    text = render_template("command_reply_help", {}).text
    assert "/heartbeat" not in text


def test_no_heartbeat_config_fields() -> None:
    """``heartbeat_interval_minutes`` and ``heartbeat_interval_sec``
    were removed from the Pydantic schema. If they come back as
    typed fields, this test fires."""
    from trumpbot.config import DaemonConfig, NotificationsConfig

    notif_fields = set(NotificationsConfig.model_fields.keys())
    daemon_fields = set(DaemonConfig.model_fields.keys())
    assert "heartbeat_interval_minutes" not in notif_fields
    assert "heartbeat_interval_sec" not in daemon_fields


def test_legacy_heartbeat_keys_load_silently(tmp_path: Path) -> None:
    """Legacy YAMLs with `heartbeat_interval_*` keys must still load —
    DaemonConfig and NotificationsConfig switched to ``extra="ignore"``
    in Phase 4 Part 2.10 so the operator's un-migrated config doesn't
    fail at startup. Pinned so the silent-ignore behavior doesn't
    accidentally flip back to ``extra="forbid"`` in the future."""
    from trumpbot.config import load_config

    p = tmp_path / "config.yaml"
    p.write_text(
        """
kalshi:
  api_key_id: "x"
  private_key_path: "/tmp/key.pem"
  target_series:
    - KXTRUMPCALL
daemon:
  heartbeat_interval_sec: 60
notifications:
  heartbeat_interval_minutes: 60
  digest_hour_utc: 12
"""
    )
    cfg = load_config(p)
    # Sanity: digest_hour_utc was picked up; the legacy heartbeat
    # keys were silently ignored.
    assert cfg.notifications.digest_hour_utc == 12


# ---------------------------------------------------------------------------
# Daemon task-list inspection: no "heartbeat" task is registered.
# This is a structural test against the daemon source so a future
# refactor that re-adds heartbeat fails loudly.
# ---------------------------------------------------------------------------


def test_daemon_does_not_spawn_heartbeat_task() -> None:
    """No literal ``"heartbeat"`` or ``"heartbeat_loop"`` task name in
    the daemon's task-registration block."""
    import inspect

    from trumpbot import daemon

    src = inspect.getsource(daemon._amain)
    # Permitted hits: descriptive comments mentioning the removal.
    # Forbidden: any "heartbeat": create_task(...) shape that would
    # actually register a task.
    forbidden_substrings = [
        '"heartbeat":',  # tasks dict key
        '"heartbeat_loop":',
        'tasks["heartbeat',
        "tasks['heartbeat",
        "HeartbeatLogger(",
        "heartbeat.run",
        "heartbeat.stop()",
        "heartbeat_loop(",
    ]
    for s in forbidden_substrings:
        assert s not in src, (
            f"daemon._amain contains {s!r} — heartbeat plumbing has "
            "been re-introduced. Phase 4 Part 2.10 removed it."
        )


@pytest.mark.parametrize(
    "module_path",
    [
        "trumpbot.notifications.commands",
        "trumpbot.notifications.scheduled",
        "trumpbot.notifications.templates",
    ],
)
def test_no_handle_heartbeat_in_modules(module_path: str) -> None:
    """No callable named ``handle_heartbeat`` or ``heartbeat_loop``
    in the production notification modules."""
    import importlib

    mod = importlib.import_module(module_path)
    for name in ("handle_heartbeat", "heartbeat_loop"):
        assert not hasattr(mod, name), f"{module_path}.{name} was removed in Phase 4 Part 2.10."
