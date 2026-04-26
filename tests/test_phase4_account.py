"""Phase 4 Part 1 — bankroll sync, reconciliation, settlement detector,
and shadow-decision capture.

Each test wires only the surface needed (no real Kalshi). The
:class:`_FakeKalshi` exposes just the methods the module under test
calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trumpbot.account.bankroll_sync import (
    BANKROLL_STATE_KEY,
    get_synced_bankroll_cents,
    sync_bankroll_once,
)
from trumpbot.account.reconcile import reconcile_once
from trumpbot.account.settlement_detector import detect_and_close_settlements
from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    NewsEventRow,
    ShadowSnapshot,
    get_system_state,
    get_trade_by_client_order_id,
    insert_news_event,
    insert_shadow_decision_at_send,
    shadow_report_summary,
    update_shadow_decision_at_decision,
    upsert_market,
)
from trumpbot.kalshi.schemas import KalshiBalance, KalshiOrder, KalshiPosition, KalshiSettlement


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "x.db")
    db.connect()
    upsert_market(
        db,
        MarketRow(
            ticker="X",
            series_ticker="KXTRUMPMEET",
            event_ticker=None,
            title="t",
            subtitle=None,
            yes_sub_title=None,
            no_sub_title=None,
            subject="x",
            subject_full_name="X",
            resolution_rules="r",
            approved_sources=None,
            open_ts="2026-04-01T00:00:00Z",
            close_ts="2026-04-30T23:59:59Z",
            expected_expiration_ts=None,
            status="active",
            last_price_cents=50,
            volume=1000,
            open_interest=200,
            raw_json=None,
        ),
    )
    eid = insert_news_event(
        db,
        NewsEventRow(
            source="reuters_via_gnews",
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
        conn.execute(
            "INSERT INTO news_market_matches (news_event_id, ticker, confidence, "
            "matched_subject, match_reason) VALUES (?, 'X', 0.9, 'x', 'test')",
            (eid,),
        )
        conn.execute(
            "INSERT INTO risk_decisions (intent_type, intent_json, decision, "
            "rejection_reason, rule_fired, reasoning_text) "
            "VALUES ('entry', '{}', 'approved', NULL, NULL, 't')"
        )
    return db


def _insert_pending_trade(db: Database, *, client_order_id: str, ticker: str = "X") -> int:
    with db.transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                ticker, side, action, status,
                entry_price_cents, quantity, cost_basis_usd_cents,
                triggering_match_id, triggering_intent_json,
                risk_decision_id, approval_id, is_reentry, prior_trade_id,
                reasoning_text, entered_at, client_order_id
            ) VALUES (
                ?, 'yes', 'buy', 'pending',
                50, 10, 500,
                1, '{}', 1, NULL, 0, NULL,
                'r', '2026-04-15T00:00:00Z', ?
            )
            """,
            (ticker, client_order_id),
        )
        return int(cur.lastrowid or 0)


def _insert_live_trade(db: Database, *, ticker: str = "X") -> int:
    with db.transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                ticker, side, action, status,
                entry_price_cents, quantity, cost_basis_usd_cents,
                triggering_match_id, triggering_intent_json,
                risk_decision_id, approval_id, is_reentry, prior_trade_id,
                reasoning_text, entered_at, client_order_id, kalshi_order_id
            ) VALUES (
                ?, 'yes', 'buy', 'live',
                50, 10, 500,
                1, '{}', 1, NULL, 0, NULL,
                'r', '2026-04-15T00:00:00Z', 'live-uuid', 'live-kalshi'
            )
            """,
            (ticker,),
        )
        return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# Bankroll sync
# ---------------------------------------------------------------------------


class TestBankrollSync:
    async def test_sync_writes_state(self, tmp_path: Path) -> None:
        db = _db(tmp_path)

        class Fake:
            async def get_balance(self) -> KalshiBalance:
                return KalshiBalance(balance=12345)

        cents = await sync_bankroll_once(db, Fake())  # type: ignore[arg-type]
        assert cents == 12345
        assert get_system_state(db, BANKROLL_STATE_KEY) == "12345"
        assert get_synced_bankroll_cents(db, fallback_starting_amount_usd=500.0) == 12345

    async def test_sync_failure_returns_none(self, tmp_path: Path) -> None:
        db = _db(tmp_path)

        class Fake:
            async def get_balance(self) -> KalshiBalance:
                raise RuntimeError("network")

        cents = await sync_bankroll_once(db, Fake())  # type: ignore[arg-type]
        assert cents is None
        # State key untouched.
        assert get_system_state(db, BANKROLL_STATE_KEY) is None

    def test_fallback_when_state_missing(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        cents = get_synced_bankroll_cents(db, fallback_starting_amount_usd=500.0)
        assert cents == 50000


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class _FakeKalshi:
    def __init__(
        self,
        *,
        order_lookup: dict[str, KalshiOrder] | None = None,
        positions: list[KalshiPosition] | None = None,
        settlements: list[KalshiSettlement] | None = None,
    ) -> None:
        self._orders = order_lookup or {}
        self._positions = positions or []
        self._settlements = settlements or []

    async def get_order_by_client_id(self, client_order_id: str) -> KalshiOrder | None:
        return self._orders.get(client_order_id)

    async def get_positions(self, **_kwargs: Any) -> list[KalshiPosition]:
        return self._positions

    async def get_settlements(self, **_kwargs: Any) -> list[KalshiSettlement]:
        return self._settlements


class TestReconciliation:
    async def test_pending_recovered(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _insert_pending_trade(db, client_order_id="uuid-recovered")
        kalshi = _FakeKalshi(
            order_lookup={
                "uuid-recovered": KalshiOrder(
                    order_id="kalshi-r",
                    client_order_id="uuid-recovered",
                    ticker="X",
                    status="executed",
                    count=10,
                    remaining_count=0,
                    filled_count=10,
                    avg_fill_price=50,
                ),
            },
        )
        report = await reconcile_once(db=db, kalshi=kalshi)  # type: ignore[arg-type]
        assert report.succeeded
        # The pending row was promoted to live with the Kalshi order id.
        row = get_trade_by_client_order_id(db, "uuid-recovered")
        assert row is not None
        assert row["status"] == "live"
        assert row["kalshi_order_id"] == "kalshi-r"

    async def test_pending_lost(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _insert_pending_trade(db, client_order_id="uuid-lost")
        kalshi = _FakeKalshi(order_lookup={})  # Kalshi has no record.
        report = await reconcile_once(db=db, kalshi=kalshi)  # type: ignore[arg-type]
        assert report.succeeded
        row = get_trade_by_client_order_id(db, "uuid-lost")
        assert row is not None
        assert row["status"] == "killed_no_fill"
        assert any(d.kind == "pending_lost" for d in report.drifts)

    async def test_live_orphaned(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        _insert_live_trade(db)
        # Kalshi reports zero positions on the ticker.
        kalshi = _FakeKalshi(positions=[])
        report = await reconcile_once(db=db, kalshi=kalshi)  # type: ignore[arg-type]
        assert report.succeeded
        assert any(d.kind == "live_orphaned" for d in report.drifts)
        row = db.connect().execute("SELECT status FROM trades WHERE ticker = 'X'").fetchone()
        assert row["status"] == "reconcile_orphaned"

    async def test_position_unknown(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        # No local trades, but Kalshi reports a position.
        kalshi = _FakeKalshi(
            positions=[KalshiPosition(ticker="UNKNOWN", position=5)],
        )
        report = await reconcile_once(db=db, kalshi=kalshi)  # type: ignore[arg-type]
        assert report.succeeded
        assert any(d.kind == "position_unknown" for d in report.drifts)
        row = db.connect().execute("SELECT status FROM trades WHERE ticker = 'UNKNOWN'").fetchone()
        assert row["status"] == "live_imported"


# ---------------------------------------------------------------------------
# Settlement detector
# ---------------------------------------------------------------------------


class TestSettlementDetector:
    async def test_settles_yes(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        trade_id = _insert_live_trade(db)
        kalshi = _FakeKalshi(
            settlements=[KalshiSettlement(ticker="X", market_result="yes")],
        )
        n = await detect_and_close_settlements(db=db, kalshi=kalshi)  # type: ignore[arg-type]
        assert n == 1
        row = (
            db.connect()
            .execute(
                "SELECT status, exit_price_cents, realized_pnl_usd_cents "
                "FROM trades WHERE id = ?",
                (trade_id,),
            )
            .fetchone()
        )
        assert row["status"] == "live_closed_resolved_yes"
        assert row["exit_price_cents"] == 100
        # 10 contracts x (100c - 50c entry) = +500c realized.
        assert row["realized_pnl_usd_cents"] == 500

    async def test_settles_no(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        trade_id = _insert_live_trade(db)
        kalshi = _FakeKalshi(
            settlements=[KalshiSettlement(ticker="X", market_result="no")],
        )
        await detect_and_close_settlements(db=db, kalshi=kalshi)  # type: ignore[arg-type]
        row = (
            db.connect()
            .execute(
                "SELECT status, realized_pnl_usd_cents FROM trades WHERE id = ?",
                (trade_id,),
            )
            .fetchone()
        )
        assert row["status"] == "live_closed_resolved_no"
        # Loss: -500c (entire cost basis).
        assert row["realized_pnl_usd_cents"] == -500

    async def test_idempotent_skip_already_closed(self, tmp_path: Path) -> None:
        """Already-closed trades aren't re-settled."""
        db = _db(tmp_path)
        trade_id = _insert_live_trade(db)
        # Pre-close it.
        with db.transaction() as conn:
            conn.execute(
                "UPDATE trades SET status='live_closed_resolved_yes' WHERE id = ?",
                (trade_id,),
            )
        kalshi = _FakeKalshi(
            settlements=[KalshiSettlement(ticker="X", market_result="yes")],
        )
        n = await detect_and_close_settlements(db=db, kalshi=kalshi)  # type: ignore[arg-type]
        assert n == 0


# ---------------------------------------------------------------------------
# Shadow decisions
# ---------------------------------------------------------------------------


class TestShadowDecisions:
    def test_insert_at_send_creates_pending_row(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        snap = ShadowSnapshot(
            yes_ask_cents=50,
            avg_fill_cents=51,
            filled_quantity=10,
            estimated_cost_cents=510,
            orderbook_json="[[50,5],[51,5]]",
        )
        rid = insert_shadow_decision_at_send(
            db,
            intent_id="intent-1",
            intent_type="entry",
            ticker="X",
            sent_at_iso="2026-04-15T12:00:00Z",
            snapshot=snap,
        )
        assert rid > 0
        row = db.connect().execute("SELECT * FROM shadow_decisions WHERE id = ?", (rid,)).fetchone()
        assert row["human_decision"] == "pending"
        assert row["shadow_yes_ask_at_send_cents"] == 50

    def test_update_at_decision_computes_diffs(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        snap_send = ShadowSnapshot(
            yes_ask_cents=50,
            avg_fill_cents=50,
            filled_quantity=10,
            estimated_cost_cents=500,
            orderbook_json="[[50,10]]",
        )
        insert_shadow_decision_at_send(
            db,
            intent_id="intent-2",
            intent_type="entry",
            ticker="X",
            sent_at_iso="2026-04-15T12:00:00Z",
            snapshot=snap_send,
        )
        # Book moved up to 60c by decision time.
        snap_decision = ShadowSnapshot(
            yes_ask_cents=60,
            avg_fill_cents=60,
            filled_quantity=10,
            estimated_cost_cents=600,
            orderbook_json="[[60,10]]",
        )
        update_shadow_decision_at_decision(
            db,
            intent_id="intent-2",
            decision_made_at_iso="2026-04-15T12:00:30Z",
            human_decision="approved",
            snapshot=snap_decision,
        )
        row = (
            db.connect()
            .execute("SELECT * FROM shadow_decisions WHERE intent_id = 'intent-2'")
            .fetchone()
        )
        assert row["human_decision"] == "approved"
        assert row["actual_yes_ask_at_decision_cents"] == 60
        # price_movement: 60 - 50 = +10
        assert row["price_movement_cents"] == 10
        # decision_lag_seconds: ~30s
        assert row["decision_lag_seconds"] >= 25

    def test_summary_aggregates(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        snap = ShadowSnapshot(
            yes_ask_cents=50,
            avg_fill_cents=50,
            filled_quantity=10,
            estimated_cost_cents=500,
            orderbook_json="[[50,10]]",
        )
        for i in range(3):
            insert_shadow_decision_at_send(
                db,
                intent_id=f"i-{i}",
                intent_type="entry",
                ticker="X",
                sent_at_iso="2026-04-15T12:00:00Z",
                snapshot=snap,
            )
            update_shadow_decision_at_decision(
                db,
                intent_id=f"i-{i}",
                decision_made_at_iso="2026-04-15T12:00:30Z",
                human_decision="approved" if i == 0 else "rejected",
                snapshot=snap,
            )
        summary = shadow_report_summary(db, since_iso="2026-04-01T00:00:00Z")
        assert summary["total_proposals"] == 3
        assert summary["approved_count"] == 1
        assert summary["rejected_count"] == 2


# ---------------------------------------------------------------------------
# Migration sanity
# ---------------------------------------------------------------------------


def test_migration_007_adds_columns(tmp_path: Path) -> None:
    db = _db(tmp_path)
    cols = [r["name"] for r in db.connect().execute("PRAGMA table_info(trades)")]
    assert "client_order_id" in cols
    assert "kalshi_order_id" in cols


def test_migration_007_creates_shadow_decisions(tmp_path: Path) -> None:
    db = _db(tmp_path)
    rows = list(
        db.connect().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_decisions'"
        )
    )
    assert len(rows) == 1


def test_migration_007_unique_client_order_id(tmp_path: Path) -> None:
    """Two trades with the same client_order_id must collide."""
    import sqlite3

    db = _db(tmp_path)
    _insert_pending_trade(db, client_order_id="dup-uuid", ticker="X")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_pending_trade(db, client_order_id="dup-uuid", ticker="X")


__all__: list[str] = []
