"""AlertDispatcher tests.

Pinned behavior:

- Audibility comes from the template (alert_critical_* -> audible=True;
  everything else silent).
- Every send writes a corresponding system_events row at the right
  severity for the future observability backend.
- Dedup suppresses duplicate alerts within the configured window.
- Non-alert templates are rejected (you can't send a heartbeat through
  the alert dispatcher).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from trumpbot.db.connection import Database
from trumpbot.notifications.alerts import AlertDispatcher
from trumpbot.notifications.templates import RenderedMessage


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "alerts.db")
    db.connect()
    return db


class _Recorder:
    """Telegram-send stub that records every call's (text, audible)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, text: str, audible: bool) -> None:
        self.calls.append((text, audible))


def _make(tmp_path: Path, **kwargs: int | None) -> tuple[AlertDispatcher, Database, _Recorder]:
    db = _db(tmp_path)
    rec = _Recorder()
    dispatcher = AlertDispatcher(
        db=db,
        send_fn=rec,
        dedup_window_seconds=int(kwargs.get("dedup_window_seconds", 3600) or 3600),
    )
    return dispatcher, db, rec


# ---------------------------------------------------------------------------
# Audibility tier
# ---------------------------------------------------------------------------


class TestAudibility:
    @pytest.mark.asyncio
    async def test_critical_alert_sent_audible(self, tmp_path: Path) -> None:
        dispatcher, _db_, rec = _make(tmp_path)
        out = await dispatcher.send(template_name="alert_critical_anthropic_auth", data={})
        assert out is not None
        assert rec.calls == [(out.text, True)]

    @pytest.mark.asyncio
    async def test_warning_alert_sent_silent(self, tmp_path: Path) -> None:
        dispatcher, _, rec = _make(tmp_path)
        await dispatcher.send(
            template_name="alert_warning_db_slow",
            data={"query_duration": "612 ms", "threshold": "500 ms"},
        )
        assert rec.calls and rec.calls[0][1] is False

    @pytest.mark.asyncio
    async def test_info_alert_sent_silent(self, tmp_path: Path) -> None:
        dispatcher, _, rec = _make(tmp_path)
        await dispatcher.send(
            template_name="alert_info_source_recovered",
            data={
                "source_name": "ap_via_gnews",
                "time_et": "10:00 ET",
                "outage_duration": "5 min",
            },
        )
        assert rec.calls and rec.calls[0][1] is False


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


class TestAuditLogging:
    @pytest.mark.asyncio
    async def test_every_alert_writes_system_events_row(self, tmp_path: Path) -> None:
        dispatcher, db, _ = _make(tmp_path)
        await dispatcher.send(
            template_name="alert_warning_db_slow",
            data={"query_duration": "612 ms", "threshold": "500 ms"},
            component="db_health",
        )
        rows = list(
            db.connect().execute(
                "SELECT event_type, severity, component, message FROM system_events"
            )
        )
        assert len(rows) == 1
        assert rows[0]["event_type"] == "alert_warning_db_slow"
        assert rows[0]["severity"] == "warning"
        assert rows[0]["component"] == "db_health"
        assert "612 ms" in rows[0]["message"]

    @pytest.mark.asyncio
    async def test_critical_severity_recorded_as_critical(self, tmp_path: Path) -> None:
        dispatcher, db, _ = _make(tmp_path)
        await dispatcher.send(template_name="alert_critical_anthropic_auth", data={})
        row = db.connect().execute("SELECT severity FROM system_events").fetchone()
        assert row["severity"] == "critical"

    @pytest.mark.asyncio
    async def test_audit_row_written_even_when_telegram_unconfigured(self, tmp_path: Path) -> None:
        """If send_fn is None (e.g. dev box without Telegram token),
        the audit log still records the alert."""
        db = _db(tmp_path)
        dispatcher = AlertDispatcher(db=db, send_fn=None)
        rendered = await dispatcher.send(template_name="alert_critical_anthropic_auth", data={})
        assert rendered is not None  # rendering happened
        rows = list(db.connect().execute("SELECT event_type FROM system_events"))
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


class TestDedup:
    @pytest.mark.asyncio
    async def test_duplicate_alert_within_window_suppressed(self, tmp_path: Path) -> None:
        dispatcher, db, rec = _make(tmp_path, dedup_window_seconds=3600)
        first = await dispatcher.send(
            template_name="alert_warning_source_down",
            data={
                "source_name": "ap",
                "last_success_et": "10:00",
                "duration_min": 35,
                "attempt_summary": "fail x4",
                "active_count": 7,
                "total_count": 8,
            },
            dedup_key="src:ap",
        )
        second = await dispatcher.send(
            template_name="alert_warning_source_down",
            data={
                "source_name": "ap",
                "last_success_et": "10:30",
                "duration_min": 65,
                "attempt_summary": "fail x8",
                "active_count": 7,
                "total_count": 8,
            },
            dedup_key="src:ap",
        )
        # First sends; second is dropped.
        assert isinstance(first, RenderedMessage)
        assert second is None
        assert len(rec.calls) == 1
        # Audit row only for the first send.
        n_rows = db.connect().execute("SELECT COUNT(*) FROM system_events").fetchone()[0]
        assert n_rows == 1

    @pytest.mark.asyncio
    async def test_different_dedup_keys_send_independently(self, tmp_path: Path) -> None:
        dispatcher, _, rec = _make(tmp_path)
        for source in ("ap", "reuters"):
            await dispatcher.send(
                template_name="alert_warning_source_down",
                data={
                    "source_name": source,
                    "last_success_et": "10:00",
                    "duration_min": 35,
                    "attempt_summary": "fail x4",
                    "active_count": 7,
                    "total_count": 8,
                },
                dedup_key=f"src:{source}",
            )
        assert len(rec.calls) == 2

    @pytest.mark.asyncio
    async def test_no_dedup_key_means_no_suppression(self, tmp_path: Path) -> None:
        dispatcher, _, rec = _make(tmp_path)
        for _ in range(3):
            await dispatcher.send(template_name="alert_critical_anthropic_auth", data={})
        assert len(rec.calls) == 3


# ---------------------------------------------------------------------------
# Misuse rejection
# ---------------------------------------------------------------------------


class TestMisuse:
    @pytest.mark.asyncio
    async def test_non_alert_template_rejected(self, tmp_path: Path) -> None:
        """Sending a heartbeat through the alert dispatcher would
        misroute the audit severity. Reject at the boundary."""
        dispatcher, _, _ = _make(tmp_path)
        with pytest.raises(ValueError, match="not an alert category"):
            await dispatcher.send(
                template_name="heartbeat_periodic",
                data={
                    "time_et": "10:00",
                    "open_count": 0,
                    "today_pnl": "+$0",
                    "llm_today": "$0",
                    "llm_cap": "$10",
                    "sources_active": 8,
                    "sources_total": 8,
                },
            )


# Ensure the asyncio plugin captures these tests; we import to avoid an
# unused-import lint warning on Callable / Awaitable.
_ = (Callable, Awaitable)
