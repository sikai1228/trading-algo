"""Scheduled-loop tests.

Each loop is exercised by running one iteration's worth of work
manually against a fixture DB. The asyncio sleep + wait_for parts
aren't exercised end-to-end (those are timing-dependent and we trust
asyncio); the tests verify the renderable side: the right template
fires with the right data, the dedup logic holds, the digest math is
right.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    SubjectRow,
    TradeInsertRow,
    insert_llm_spend,
    insert_trade,
    upsert_market,
    upsert_source_status,
    upsert_subject,
)
from trumpbot.notifications.alerts import AlertDispatcher
from trumpbot.notifications.llm_cost import LLMCostGuard, LLMCostGuardConfig
from trumpbot.notifications.scheduled import (
    _build_digest_data,
    _build_heartbeat_data,
    _check_source_health,
    _humanize_duration,
    _process_settlements,
    _seconds_until_next_aligned_tick,
    _seconds_until_next_hour,
)


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "sched.db")
    db.connect()
    upsert_subject(db, SubjectRow(subject_key="putin", full_name="V P", aliases=["putin"]))
    upsert_market(
        db,
        MarketRow(
            ticker="X",
            series_ticker="KX",
            event_ticker="KX-26APR",
            title="t",
            subtitle=None,
            yes_sub_title=None,
            no_sub_title=None,
            subject="putin",
            subject_full_name="V P",
            resolution_rules="r",
            approved_sources=None,
            open_ts="2026-04-01T00:00:00Z",
            close_ts="2026-04-30T23:59:59Z",
            expected_expiration_ts=None,
            status="active",
            last_price_cents=50,
            volume=1000,
            open_interest=50,
            raw_json=None,
        ),
    )
    return db


def _seed_match(db: Database) -> int:
    from trumpbot.db.repositories import NewsEventRow, insert_news_event

    eid = insert_news_event(
        db,
        NewsEventRow(
            source="ap",
            source_weight=1.0,
            is_kalshi_approved=True,
            headline="h",
            url="https://e.com/1",
            url_canonical="https://e.com/1",
            body_excerpt=None,
            author=None,
            raw_published_ts=None,
            detected_ts="2026-04-15T12:00:00Z",
            has_photo=False,
            has_video=False,
            raw_data=None,
        ),
    )
    assert eid is not None
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO news_market_matches (news_event_id, ticker, confidence, "
            "matched_subject, match_reason) VALUES (?, 'X', 0.9, 'putin', 'test')",
            (eid,),
        )
        return int(cur.lastrowid or 0)


def _open_trade(db: Database, *, status: str = "dry_run") -> int:
    match_id = _seed_match(db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO risk_decisions (intent_type, intent_json, decision, "
            "rejection_reason, rule_fired, reasoning_text) "
            "VALUES ('entry', '{}', 'approved', NULL, NULL, 't')"
        )
    return insert_trade(
        db,
        TradeInsertRow(
            ticker="X",
            status=status,
            entry_price_cents=50,
            quantity=10,
            cost_basis_usd_cents=500,
            triggering_match_id=match_id,
            triggering_intent_json="{}",
            risk_decision_id=1,
            approval_id=None,
            is_reentry=False,
            prior_trade_id=None,
            reasoning_text="r",
            entered_at="2026-04-15T12:00:00Z",
        ),
    )


# ---------------------------------------------------------------------------
# Heartbeat data
# ---------------------------------------------------------------------------


class TestHeartbeatData:
    def test_no_open_positions(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        cg = LLMCostGuard(db=db, config=LLMCostGuardConfig(monthly_cap_usd_cents=1000))
        data = _build_heartbeat_data(db, cg, sources_provider=lambda: (5, 8))
        assert data["open_count"] == 0
        assert data["sources_active"] == 5
        assert data["sources_total"] == 8
        assert data["llm_today"] == "$0.00"
        assert data["llm_cap"] == "$10.00"
        assert "+" in data["today_pnl"] or "-" in data["today_pnl"]

    def test_falls_back_to_source_status_when_no_provider(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        upsert_source_status(db, source_name="ap", current_status="active")
        upsert_source_status(db, source_name="reuters", current_status="down")
        cg = LLMCostGuard(db=db, config=LLMCostGuardConfig(monthly_cap_usd_cents=1000))
        data = _build_heartbeat_data(db, cg, sources_provider=None)
        assert data["sources_total"] == 2
        assert data["sources_active"] == 1


# ---------------------------------------------------------------------------
# Digest math
# ---------------------------------------------------------------------------


class TestDigestData:
    def test_empty_db_renders_safely(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        cg = LLMCostGuard(db=db, config=LLMCostGuardConfig(monthly_cap_usd_cents=1000))
        data = _build_digest_data(db, cg)
        assert data["closed_count"] == 0
        assert data["wins"] == 0
        assert data["llm_pct"] == "0%"


# ---------------------------------------------------------------------------
# Settlement notification
# ---------------------------------------------------------------------------


class TestSettlementNotification:
    @pytest.mark.asyncio
    async def test_yes_settlement_sends_yes_template(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _open_trade(db)
        with db.transaction() as conn:
            conn.execute("UPDATE markets SET status = 'settled_yes' WHERE ticker = 'X'")
        sent: list[tuple[str, bool]] = []

        async def send(text: str, silent: bool) -> None:
            sent.append((text, silent))

        await _process_settlements(db, send)
        assert len(sent) == 1
        text, silent = sent[0]
        assert "Resolution: YES at $1.00" in text
        assert silent is True

    @pytest.mark.asyncio
    async def test_no_settlement_sends_no_template(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _open_trade(db)
        with db.transaction() as conn:
            conn.execute("UPDATE markets SET status = 'settled_no' WHERE ticker = 'X'")
        sent: list[tuple[str, bool]] = []

        async def send(text: str, silent: bool) -> None:
            sent.append((text, silent))

        await _process_settlements(db, send)
        assert len(sent) == 1
        assert "Resolution: NO at $0" in sent[0][0]

    @pytest.mark.asyncio
    async def test_active_market_no_notification(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _open_trade(db)
        # market still 'active'; no settlement -> nothing sent.
        sent: list[tuple[str, bool]] = []

        async def send(text: str, silent: bool) -> None:
            sent.append((text, silent))

        await _process_settlements(db, send)
        assert sent == []


# ---------------------------------------------------------------------------
# Source health
# ---------------------------------------------------------------------------


class TestSourceHealth:
    @pytest.mark.asyncio
    async def test_old_active_source_marked_down_and_alerted(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        old = (datetime.now(UTC) - timedelta(minutes=45)).isoformat()
        upsert_source_status(
            db,
            source_name="ap",
            current_status="active",
            last_successful_poll=old,
        )
        dispatcher = AlertDispatcher(db=db, send_fn=None)
        await _check_source_health(db, dispatcher, threshold_minutes=30)
        ev = (
            db.connect()
            .execute(
                "SELECT event_type FROM system_events "
                "WHERE event_type = 'alert_warning_source_down'"
            )
            .fetchone()
        )
        assert ev is not None

    @pytest.mark.asyncio
    async def test_recovered_source_marked_active_and_info_alert(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        recent = datetime.now(UTC).isoformat()
        upsert_source_status(
            db,
            source_name="ap",
            current_status="down",
            last_successful_poll=recent,
        )
        dispatcher = AlertDispatcher(db=db, send_fn=None)
        await _check_source_health(db, dispatcher, threshold_minutes=30)
        ev = (
            db.connect()
            .execute(
                "SELECT event_type FROM system_events "
                "WHERE event_type = 'alert_info_source_recovered'"
            )
            .fetchone()
        )
        assert ev is not None

    @pytest.mark.asyncio
    async def test_dedup_suppresses_repeat_down_alert(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        old = (datetime.now(UTC) - timedelta(minutes=45)).isoformat()
        upsert_source_status(
            db,
            source_name="ap",
            current_status="active",
            last_successful_poll=old,
        )
        dispatcher = AlertDispatcher(db=db, send_fn=None)
        await _check_source_health(db, dispatcher, threshold_minutes=30)
        # Re-set to "active" so the loop fires again (simulating two
        # 5-min ticks). Dedup should kick in.
        upsert_source_status(
            db,
            source_name="ap",
            current_status="active",
            last_successful_poll=old,
        )
        await _check_source_health(db, dispatcher, threshold_minutes=30)
        rows = list(
            db.connect().execute(
                "SELECT id FROM system_events " "WHERE event_type = 'alert_warning_source_down'"
            )
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_seconds_until_next_hour_today(self) -> None:
        now = datetime(2026, 4, 25, 6, 30, tzinfo=UTC)
        # 8:00 UTC is 1.5 hours = 5400s away.
        secs = _seconds_until_next_hour(8, now=now)
        assert secs == 5400.0

    def test_seconds_until_next_hour_already_passed_today(self) -> None:
        now = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)
        # 8:00 UTC has passed; next is tomorrow at 8:00 = 23 hours = 82800s.
        secs = _seconds_until_next_hour(8, now=now)
        assert secs == 82800.0

    def test_humanize_duration(self) -> None:
        assert _humanize_duration(timedelta(seconds=45)) == "45s"
        assert _humanize_duration(timedelta(seconds=300)) == "5 min"
        assert _humanize_duration(timedelta(seconds=4000)) == "1h"

    # ----- Heartbeat aligned-tick helper (Phase 4 Part 2.4) -----

    def test_aligned_tick_60min_at_quarter_past_returns_to_top_of_hour(self) -> None:
        """interval=60: from 14:23 next tick is 15:00 → 37 min."""
        now = datetime(2026, 4, 25, 14, 23, tzinfo=UTC)
        secs = _seconds_until_next_aligned_tick(60, now=now)
        assert secs == 37 * 60

    def test_aligned_tick_60min_exactly_on_hour_skips_to_next(self) -> None:
        """interval=60: from 14:00 exactly we advance one full hour
        to 15:00 (never fire twice on the same boundary)."""
        now = datetime(2026, 4, 25, 14, 0, 0, tzinfo=UTC)
        secs = _seconds_until_next_aligned_tick(60, now=now)
        assert secs == 60 * 60

    def test_aligned_tick_15min_at_23_returns_30(self) -> None:
        """interval=15: from 14:23 next tick is 14:30 → 7 min."""
        now = datetime(2026, 4, 25, 14, 23, tzinfo=UTC)
        secs = _seconds_until_next_aligned_tick(15, now=now)
        assert secs == 7 * 60

    def test_aligned_tick_15min_at_46_rolls_to_next_hour(self) -> None:
        """interval=15: from 14:46 next tick is 15:00 → 14 min.
        Verifies the spill-into-next-hour branch."""
        now = datetime(2026, 4, 25, 14, 46, tzinfo=UTC)
        secs = _seconds_until_next_aligned_tick(15, now=now)
        assert secs == 14 * 60

    def test_aligned_tick_60min_at_59_rolls_to_next_hour(self) -> None:
        """interval=60: from 14:59 next tick is 15:00 → 1 min.
        Edge case: very close to the boundary."""
        now = datetime(2026, 4, 25, 14, 59, tzinfo=UTC)
        secs = _seconds_until_next_aligned_tick(60, now=now)
        assert secs == 60


# Suppress unused-import warning.
_ = (insert_llm_spend,)
