"""Integration tests for MarketDiscoveryService against the captured fixture.

These tests use a fake KalshiClient that returns the kxtrumpmeet_26apr
fixture, exercising the full persistence + snapshot + idempotency path.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from trumpbot.db.connection import Database
from trumpbot.db.repositories import get_subject
from trumpbot.discovery.event_ticker import EventTicker
from trumpbot.discovery.service import MarketDiscoveryService
from trumpbot.events.bus import Event, EventBus
from trumpbot.kalshi.exceptions import TransientError
from trumpbot.kalshi.schemas import KalshiEventResponse

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "kxtrumpmeet_26apr_response.json"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, today: date) -> None:
        self._today = today

    def today(self) -> date:
        return self._today


class _FakeKalshi:
    """Returns canned event responses keyed by event_ticker."""

    def __init__(self, responses: dict[str, KalshiEventResponse | Exception]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def get_event(self, event_ticker: str) -> KalshiEventResponse:
        self.calls.append(event_ticker)
        out = self._responses.get(event_ticker)
        if out is None:
            return KalshiEventResponse.model_validate(
                {"event": {"event_ticker": event_ticker, "title": ""}, "markets": []}
            )
        if isinstance(out, Exception):
            raise out
        return out


class _RecordingTelegram:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.enabled = True

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixture loaders
# ---------------------------------------------------------------------------


@pytest.fixture()
def kxtrumpmeet_26apr() -> KalshiEventResponse:
    raw: dict[str, Any] = json.loads(FIXTURE_PATH.read_text())
    return KalshiEventResponse.model_validate(raw)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "discovery.db")
    d.connect()
    return d


def _make_service(
    *,
    db: Database,
    client: _FakeKalshi,
    snapshot_dir: Path,
    today: date = date(2026, 4, 15),
    telegram: _RecordingTelegram | None = None,
) -> tuple[MarketDiscoveryService, EventBus]:
    bus = EventBus()
    service = MarketDiscoveryService(
        client=client,  # type: ignore[arg-type]
        db=db,
        event_bus=bus,
        telegram=telegram,  # type: ignore[arg-type]
        series="KXTRUMPMEET",
        poll_interval_sec=3600,
        snapshot_dir=snapshot_dir,
        clock=_FakeClock(today),
    )
    return service, bus


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPollOnce:
    async def test_persists_all_markets_from_fixture(
        self, db: Database, kxtrumpmeet_26apr: KalshiEventResponse, tmp_path: Path
    ) -> None:
        client = _FakeKalshi({"KXTRUMPMEET-26APR": kxtrumpmeet_26apr})
        service, _bus = _make_service(db=db, client=client, snapshot_dir=tmp_path / "snap")
        await service.poll_once()
        rows = list(db.connect().execute("SELECT * FROM markets ORDER BY ticker"))
        tickers = [r["ticker"] for r in rows]
        assert tickers == [
            "KXTRUMPMEET-26APR-BNET",
            "KXTRUMPMEET-26APR-JTHU",
            "KXTRUMPMEET-26APR-MCM",
            "KXTRUMPMEET-26APR-VPUT",
            "KXTRUMPMEET-26APR-XJIN",
        ]
        # Subject extraction populated subjects table.
        assert get_subject(db, "vladimirputin") is not None
        assert get_subject(db, "mariacorinamachado") is not None
        # Subject metadata captured from market title.
        putin_market = next(r for r in rows if r["ticker"] == "KXTRUMPMEET-26APR-VPUT")
        assert putin_market["subject"] == "vladimirputin"
        assert putin_market["subject_full_name"] == "Vladimir Putin"
        assert putin_market["resolution_rules"].startswith("If Donald Trump and Vladimir Putin")

    async def test_idempotency_no_duplicates(
        self, db: Database, kxtrumpmeet_26apr: KalshiEventResponse, tmp_path: Path
    ) -> None:
        client = _FakeKalshi({"KXTRUMPMEET-26APR": kxtrumpmeet_26apr})
        service, _bus = _make_service(db=db, client=client, snapshot_dir=tmp_path / "snap")
        await service.poll_once()
        await service.poll_once()
        await service.poll_once()
        count = db.connect().execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        assert count == 5

    async def test_snapshot_files_written(
        self, db: Database, kxtrumpmeet_26apr: KalshiEventResponse, tmp_path: Path
    ) -> None:
        snap = tmp_path / "snap"
        client = _FakeKalshi({"KXTRUMPMEET-26APR": kxtrumpmeet_26apr})
        service, _bus = _make_service(db=db, client=client, snapshot_dir=snap)
        await service.poll_once()
        assert (snap / "KXTRUMPMEET-26APR.json").exists()
        md = (snap / "kxtrumpmeet_26apr_summary.md").read_text()
        assert "Total markets: 5" in md
        assert "Vladimir Putin" in md
        assert "María Corina Machado" in md

    async def test_resolution_rules_immutable(
        self, db: Database, kxtrumpmeet_26apr: KalshiEventResponse, tmp_path: Path
    ) -> None:
        # First poll populates.
        client = _FakeKalshi({"KXTRUMPMEET-26APR": kxtrumpmeet_26apr})
        telegram = _RecordingTelegram()
        service, _bus = _make_service(
            db=db, client=client, snapshot_dir=tmp_path / "snap", telegram=telegram
        )
        await service.poll_once()

        # Second poll: same event, but with rewritten resolution rules.
        mutated_payload = json.loads(FIXTURE_PATH.read_text())
        mutated_payload["markets"][0]["rules_primary"] = "Different rules text"
        mutated = KalshiEventResponse.model_validate(mutated_payload)
        client._responses["KXTRUMPMEET-26APR"] = mutated
        await service.poll_once()

        # Original rules retained.
        row = (
            db.connect()
            .execute(
                "SELECT resolution_rules FROM markets WHERE ticker = ?",
                ("KXTRUMPMEET-26APR-VPUT",),
            )
            .fetchone()
        )
        assert "If Donald Trump and Vladimir Putin" in row["resolution_rules"]
        assert "Different rules text" not in row["resolution_rules"]

        # Critical system event written.
        sys_events = list(
            db.connect().execute(
                "SELECT * FROM system_events WHERE event_type = 'resolution_rules_changed'"
            )
        )
        assert len(sys_events) == 1

        # Telegram alert sent.
        assert any("changed mid-event" in m for m in telegram.messages)

    async def test_telegram_notification_on_first_event(
        self, db: Database, kxtrumpmeet_26apr: KalshiEventResponse, tmp_path: Path
    ) -> None:
        telegram = _RecordingTelegram()
        client = _FakeKalshi({"KXTRUMPMEET-26APR": kxtrumpmeet_26apr})
        service, _bus = _make_service(
            db=db, client=client, snapshot_dir=tmp_path / "snap", telegram=telegram
        )
        await service.poll_once()
        # Exactly one "new month" notification with the event ticker.
        new_month = [m for m in telegram.messages if "New month" in m]
        assert len(new_month) == 1
        assert "KXTRUMPMEET-26APR" in new_month[0]

        # Second poll should NOT re-notify.
        await service.poll_once()
        assert sum("New month" in m for m in telegram.messages) == 1

    async def test_subject_extraction_failure_recorded(self, db: Database, tmp_path: Path) -> None:
        # Manually craft an event with one bad title and one good one.
        payload = {
            "event": {"event_ticker": "KXTRUMPMEET-26APR", "title": "April"},
            "markets": [
                {
                    "ticker": "KXTRUMPMEET-26APR-OK",
                    "title": "Donald Trump and Vladimir Putin meet before May 1, 2026?",
                    "rules_primary": "If they meet, resolves YES.",
                    "open_time": "2026-04-01T00:00:00Z",
                    "close_time": "2026-04-30T23:59:59Z",
                    "status": "active",
                },
                {
                    "ticker": "KXTRUMPMEET-26APR-BAD",
                    "title": "Some completely unrelated headline",
                    "rules_primary": "Rules.",
                    "open_time": "2026-04-01T00:00:00Z",
                    "close_time": "2026-04-30T23:59:59Z",
                    "status": "active",
                },
            ],
        }
        client = _FakeKalshi({"KXTRUMPMEET-26APR": KalshiEventResponse.model_validate(payload)})
        service, _bus = _make_service(db=db, client=client, snapshot_dir=tmp_path / "snap")
        await service.poll_once()
        # Good market inserted, bad one skipped.
        tickers = [
            r["ticker"] for r in db.connect().execute("SELECT ticker FROM markets ORDER BY ticker")
        ]
        assert tickers == ["KXTRUMPMEET-26APR-OK"]
        # System event recorded.
        sys_events = list(
            db.connect().execute(
                "SELECT * FROM system_events WHERE event_type = 'subject_extraction_failed'"
            )
        )
        assert len(sys_events) == 1

    async def test_market_discovered_event_published(
        self, db: Database, kxtrumpmeet_26apr: KalshiEventResponse, tmp_path: Path
    ) -> None:
        seen: list[Event] = []

        async def capture(event: Event) -> None:
            seen.append(event)

        client = _FakeKalshi({"KXTRUMPMEET-26APR": kxtrumpmeet_26apr})
        service, bus = _make_service(db=db, client=client, snapshot_dir=tmp_path / "snap")
        bus.subscribe("market_discovered", capture)
        bus.subscribe("new_event_detected", capture)
        await service.poll_once()
        market_events = [e for e in seen if e.type == "market_discovered"]
        new_event_events = [e for e in seen if e.type == "new_event_detected"]
        assert len(market_events) == 5
        assert len(new_event_events) == 1


class TestBackoff:
    async def test_failure_streak_records_system_event(self, db: Database, tmp_path: Path) -> None:
        client = _FakeKalshi(
            {
                "KXTRUMPMEET-26APR": TransientError("kalshi 503", status_code=503),
                "KXTRUMPMEET-26MAY": TransientError("kalshi 503", status_code=503),
            }
        )
        service, _bus = _make_service(
            db=db,
            client=client,
            snapshot_dir=tmp_path / "snap",
            today=date(2026, 4, 15),
        )
        await service.poll_once()
        rows = list(
            db.connect().execute(
                "SELECT * FROM system_events WHERE event_type = 'event_fetch_failed'"
            )
        )
        # Two events failed (current + next month).
        assert len(rows) == 2

    def test_backoff_schedule_progression(self, db: Database, tmp_path: Path) -> None:
        # Direct unit on the internal helper.
        client = _FakeKalshi({})
        service, _bus = _make_service(db=db, client=client, snapshot_dir=tmp_path / "snap")
        service._failure_streak["X"] = 0
        assert service._backoff_for("X") == 0
        for streak, expected in [(1, 60), (2, 300), (3, 900), (4, 3600), (10, 3600)]:
            service._failure_streak["X"] = streak
            assert service._backoff_for("X") == expected


class TestNextMonthAudit:
    async def test_late_month_logs_status_for_next_month(
        self, db: Database, tmp_path: Path
    ) -> None:
        # On April 26 the service should additionally record whether
        # KXTRUMPMEET-26MAY has been seen yet.
        client = _FakeKalshi({})  # both current and next month return empty
        service, _bus = _make_service(
            db=db, client=client, snapshot_dir=tmp_path / "snap", today=date(2026, 4, 26)
        )
        await service.poll_once()
        rows = list(
            db.connect().execute(
                "SELECT * FROM system_events "
                "WHERE event_type IN ('next_month_event_opened', 'next_month_event_not_yet_open')"
            )
        )
        assert len(rows) == 1
        assert rows[0]["event_type"] == "next_month_event_not_yet_open"

    async def test_early_month_does_not_log_next_month_status(
        self, db: Database, tmp_path: Path
    ) -> None:
        client = _FakeKalshi({})
        service, _bus = _make_service(
            db=db, client=client, snapshot_dir=tmp_path / "snap", today=date(2026, 4, 15)
        )
        await service.poll_once()
        rows = list(
            db.connect().execute(
                "SELECT * FROM system_events "
                "WHERE event_type IN ('next_month_event_opened', 'next_month_event_not_yet_open')"
            )
        )
        assert rows == []


def test_event_ticker_dataclass_immutable() -> None:
    """`EventTicker` is frozen so the discovery service can stash it
    in caches/dicts safely."""
    et = EventTicker(series="KXTRUMPMEET", year=2026, month=4)
    with pytest.raises(dataclasses.FrozenInstanceError):
        et.year = 2027  # type: ignore[misc]
