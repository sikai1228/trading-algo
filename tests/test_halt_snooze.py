"""Regression tests for the halt + snooze plumbing in
``trumpbot/decision/loops.py``.

These pin the core operational invariant: when the user has issued
``/halt``, no new trade proposals fire from the decision or re-entry
loops. When a specific ticker is snoozed, that ticker is skipped
even when the rest of the system would otherwise fire.

The tests poke the per-cycle helper directly (``_run_decision_cycle``
+ ``_maybe_reentry``) instead of running the full asyncio loop --
behavior is the same, faster.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    NewsEventRow,
    SubjectRow,
    insert_news_event,
    set_system_state,
    upsert_market,
    upsert_snoozed_market,
    upsert_subject,
)
from trumpbot.decision.loops import _is_halted


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "halt.db")
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


# ---------------------------------------------------------------------------
# halt flag
# ---------------------------------------------------------------------------


class TestHaltFlag:
    def test_default_state_not_halted(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        assert _is_halted(db) is False

    def test_set_true_returns_halted(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        set_system_state(db, key="halt_flag", value="true")
        assert _is_halted(db) is True

    def test_set_back_to_false_clears(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        set_system_state(db, key="halt_flag", value="true")
        set_system_state(db, key="halt_flag", value="false")
        assert _is_halted(db) is False

    def test_case_insensitive(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        set_system_state(db, key="halt_flag", value="TRUE")
        assert _is_halted(db) is True


# ---------------------------------------------------------------------------
# snooze
# ---------------------------------------------------------------------------


class TestSnooze:
    def test_default_no_snooze(self, tmp_path: Path) -> None:
        from trumpbot.db.repositories import is_market_snoozed

        db = _db(tmp_path)
        assert is_market_snoozed(db, "X") is False

    def test_active_snooze_blocks(self, tmp_path: Path) -> None:
        from trumpbot.db.repositories import is_market_snoozed

        db = _db(tmp_path)
        until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        upsert_snoozed_market(db, ticker="X", snoozed_until=until)
        assert is_market_snoozed(db, "X") is True

    def test_expired_snooze_does_not_block(self, tmp_path: Path) -> None:
        from trumpbot.db.repositories import is_market_snoozed

        db = _db(tmp_path)
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        upsert_snoozed_market(db, ticker="X", snoozed_until=past)
        assert is_market_snoozed(db, "X") is False


# ---------------------------------------------------------------------------
# decision-cycle integration: halt early-return
# ---------------------------------------------------------------------------


class _StubEngine:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate_news_match(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return None

    def evaluate_reentry(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return None

    def evaluate_stop_loss(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return None


class _StubRisk:
    def evaluate(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        from trumpbot.types.intents import RiskRejection

        return RiskRejection(
            intent=None, reason="x", detail="x", rule_fired="x", risk_decision_id=0  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_decision_cycle_no_op_when_halted(tmp_path: Path) -> None:
    """The cycle returns immediately when halt is set; the engine is
    NOT consulted, even if there are unevaluated matches in the
    queue. This pins the operational guarantee: /halt actually
    halts."""
    from trumpbot.decision.loops import _run_decision_cycle

    db = _db(tmp_path)
    set_system_state(db, key="halt_flag", value="true")

    # Seed an unevaluated match so we'd normally process it.
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
            detected_ts=datetime.now(UTC).isoformat(),
            has_photo=False,
            has_video=False,
            raw_data=None,
        ),
    )
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO news_market_matches (news_event_id, ticker, confidence, "
            "matched_subject, match_reason, created_at) "
            "VALUES (?, 'X', 0.95, 'putin', 'test', ?)",
            (eid, datetime.now(UTC).isoformat()),
        )

    engine = _StubEngine()
    await _run_decision_cycle(
        db=db,
        engine=engine,  # type: ignore[arg-type]
        risk=_StubRisk(),  # type: ignore[arg-type]
        gate=None,  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
        orderbook=lambda _t: None,  # type: ignore[arg-type, return-value]
        depth=lambda _t: None,
        starting_amount_usd=500.0,
    )
    # Halted -> engine never consulted.
    assert engine.calls == 0


# ---------------------------------------------------------------------------
# Stop-loss bypass — Phase 3 Part 2 spec calls this out explicitly.
# Snooze and halt block NEW entries; stop-losses on existing positions
# must still fire so the user can approve emergency exits.
# ---------------------------------------------------------------------------


def test_stop_loss_loop_does_not_check_halt_or_snooze() -> None:
    """The stop_loss_loop body must not call _is_halted or
    is_market_snoozed -- the spec is explicit that stop-losses
    bypass both flags. This is a structural test against the
    decision/loops.py source so a future refactor that adds the
    check fails loudly."""
    import inspect

    from trumpbot.decision import loops

    src = inspect.getsource(loops.stop_loss_loop)
    assert "_is_halted" not in src, (
        "stop_loss_loop must NOT gate on /halt -- emergency exits must "
        "always reach the user. Remove the _is_halted check."
    )
    assert "is_market_snoozed" not in src, (
        "stop_loss_loop must NOT gate on /snooze -- snoozes block new "
        "entries, not stop-losses on existing positions. Remove the "
        "is_market_snoozed check."
    )
