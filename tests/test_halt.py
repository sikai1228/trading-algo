"""Regression tests for the /halt plumbing in
``trumpbot/decision/loops.py``.

These pin the core operational invariant: when the user has issued
``/halt``, no new trade proposals fire from the decision or re-entry
loops. Stop-loss exits intentionally bypass /halt — emergency exits
must always reach the user.

Phase 4 Part 2.9 removed the per-ticker /snooze plumbing. /halt is
the sole global override.

The tests poke the per-cycle helper directly (``_run_decision_cycle``)
instead of running the full asyncio loop -- behavior is the same,
faster.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
            intent=None,  # type: ignore[arg-type]
            reason="x",
            detail="x",
            rule_fired="x",
            risk_decision_id=0,
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
# /halt blocks NEW entries; stop-losses on existing positions must still
# fire so the user can approve emergency exits.
# ---------------------------------------------------------------------------


def test_stop_loss_loop_does_not_check_halt() -> None:
    """The stop_loss_loop body must not call _is_halted -- the spec is
    explicit that stop-losses bypass /halt. This is a structural test
    against the decision/loops.py source so a future refactor that adds
    the check fails loudly."""
    import inspect

    from trumpbot.decision import loops

    src = inspect.getsource(loops.stop_loss_loop)
    assert "_is_halted" not in src, (
        "stop_loss_loop must NOT gate on /halt -- emergency exits must "
        "always reach the user. Remove the _is_halted check."
    )


# ---------------------------------------------------------------------------
# Phase 4 Part 2.9 — snooze removal regression
# ---------------------------------------------------------------------------


def test_snooze_repo_helpers_are_gone() -> None:
    """is_market_snoozed / upsert_snoozed_market / delete_snoozed_market /
    list_active_snoozed_markets must NOT be importable. Catches a
    revert that brings the snooze plumbing back."""
    from trumpbot.db import repositories

    for name in (
        "is_market_snoozed",
        "upsert_snoozed_market",
        "delete_snoozed_market",
        "list_active_snoozed_markets",
        "SnoozedMarketRow",
    ):
        assert not hasattr(repositories, name), (
            f"trumpbot.db.repositories.{name} was removed in Phase 4 Part 2.9 "
            "(snooze deletion). If it's back, revert the revert."
        )


def test_snoozed_markets_table_is_dropped(tmp_path: Path) -> None:
    """Migration 012 drops the snoozed_markets table. Verify a fresh
    Database (which runs all migrations) does not contain it."""
    db = Database(tmp_path / "fresh.db")
    db.connect()
    conn = db.connect()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='snoozed_markets'"
    ).fetchone()
    db.close()
    assert row is None, "snoozed_markets table must be gone after migration 012"
