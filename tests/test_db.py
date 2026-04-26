"""Tests for the database connection helper, migrations, and repositories."""

from __future__ import annotations

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    NewsEventRow,
    NewsMatchRow,
    OrderbookSnapshotRow,
    PriceSnapshotRow,
    fetch_news_events_without_matches,
    get_market,
    insert_news_event,
    insert_news_matches,
    insert_orderbook_snapshot,
    insert_price_snapshot,
    insert_system_event,
    list_active_markets,
    recent_high_confidence_matches,
    recent_news_events,
    update_market_status,
    upsert_market,
)


class TestMigrations:
    def test_creates_all_phase1_tables(self, tmp_db: Database) -> None:
        conn = tmp_db.connect()
        names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for required in (
            "markets",
            "price_snapshots",
            "orderbook_snapshots",
            "news_events",
            "news_market_matches",
            "system_events",
            "trades",
            "trade_news_links",
            "risk_decisions",
            "telegram_approvals",
            "schema_migrations",
        ):
            assert required in names

    def test_foreign_keys_enabled(self, tmp_db: Database) -> None:
        conn = tmp_db.connect()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_idempotent(self, tmp_db: Database) -> None:
        # close and reopen — migrations should not re-apply
        tmp_db.close()
        tmp_db.connect()
        rows = list(tmp_db.connect().execute("SELECT filename FROM schema_migrations"))
        # Each migration file should appear exactly once even after reopen.
        filenames = sorted(r["filename"] for r in rows)
        assert filenames == sorted(set(filenames))
        assert "001_initial.sql" in filenames


class TestMarketRepo:
    def _row(self, ticker: str = "TRUMPCALL-PUTIN-2026", subject: str = "putin") -> MarketRow:
        return MarketRow(
            ticker=ticker,
            series_ticker="KXTRUMPCALL",
            event_ticker="EVT-1",
            title=f"Will Trump talk to {subject}?",
            subtitle=None,
            yes_sub_title=None,
            no_sub_title=None,
            subject=subject,
            resolution_rules="resolves YES if Trump and X speak by deadline",
            approved_sources=["reuters", "ap"],
            open_ts="2026-01-01T00:00:00.000000Z",
            close_ts="2026-12-31T23:59:59.000000Z",
            expected_expiration_ts=None,
            status="active",
            last_price_cents=42,
            volume=100,
            open_interest=50,
            raw_json={"ticker": ticker},
        )

    def test_upsert_then_fetch(self, tmp_db: Database) -> None:
        upsert_market(tmp_db, self._row())
        out = get_market(tmp_db, "TRUMPCALL-PUTIN-2026")
        assert out is not None
        assert out["subject"] == "putin"
        assert out["status"] == "active"

    def test_upsert_updates(self, tmp_db: Database) -> None:
        upsert_market(tmp_db, self._row())
        row2 = self._row()
        # change a field
        upsert_market(
            tmp_db,
            MarketRow(**{**row2.__dict__, "last_price_cents": 99}),
        )
        out = get_market(tmp_db, "TRUMPCALL-PUTIN-2026")
        assert out is not None
        assert out["last_price_cents"] == 99

    def test_list_active(self, tmp_db: Database) -> None:
        upsert_market(tmp_db, self._row(ticker="A", subject="putin"))
        upsert_market(tmp_db, self._row(ticker="B", subject="xi"))
        update_market_status(tmp_db, "B", "settled_yes")
        active = list_active_markets(tmp_db)
        assert {r["ticker"] for r in active} == {"A"}


class TestSnapshotRepo:
    def test_insert_price_snapshot(self, tmp_db: Database) -> None:
        upsert_market(
            tmp_db,
            MarketRow(
                ticker="T",
                series_ticker="S",
                event_ticker=None,
                title="t",
                subtitle=None,
                yes_sub_title=None,
                no_sub_title=None,
                subject="x",
                resolution_rules="r",
                approved_sources=None,
                open_ts="2026-01-01T00:00:00.000000Z",
                close_ts=None,
                expected_expiration_ts=None,
                status="active",
                last_price_cents=None,
                volume=None,
                open_interest=None,
                raw_json=None,
            ),
        )
        insert_price_snapshot(
            tmp_db,
            PriceSnapshotRow(
                ticker="T",
                yes_bid_cents=50,
                yes_ask_cents=52,
                no_bid_cents=48,
                no_ask_cents=50,
                yes_bid_size=10,
                yes_ask_size=20,
                no_bid_size=15,
                no_ask_size=25,
                last_trade_price_cents=51,
                volume_24h=100,
                ts="2026-04-25T00:00:00.000000Z",
                snapshot_reason="periodic",
            ),
        )
        out = list(tmp_db.connect().execute("SELECT * FROM price_snapshots"))
        assert len(out) == 1
        assert out[0]["snapshot_reason"] == "periodic"

    def test_insert_orderbook_snapshot(self, tmp_db: Database) -> None:
        upsert_market(
            tmp_db,
            MarketRow(
                ticker="T",
                series_ticker="S",
                event_ticker=None,
                title="t",
                subtitle=None,
                yes_sub_title=None,
                no_sub_title=None,
                subject="x",
                resolution_rules="r",
                approved_sources=None,
                open_ts="2026-01-01T00:00:00.000000Z",
                close_ts=None,
                expected_expiration_ts=None,
                status="active",
                last_price_cents=None,
                volume=None,
                open_interest=None,
                raw_json=None,
            ),
        )
        insert_orderbook_snapshot(
            tmp_db,
            OrderbookSnapshotRow(
                ticker="T",
                yes_levels=[(50, 100), (49, 200)],
                no_levels=[(50, 100), (51, 50)],
                ts="2026-04-25T00:00:00.000000Z",
            ),
        )
        out = list(tmp_db.connect().execute("SELECT * FROM orderbook_snapshots"))
        assert len(out) == 1
        assert "[50, 100]" in out[0]["yes_levels"]


class TestNewsRepo:
    def _event(self, url: str = "https://example.com/a") -> NewsEventRow:
        return NewsEventRow(
            source="reuters",
            is_kalshi_approved=True,
            headline="Trump and Putin spoke",
            url=url,
            url_canonical=url,
            body_excerpt="They had a phone call today.",
            author=None,
            raw_published_ts="2026-04-25T00:00:00.000000Z",
            detected_ts="2026-04-25T00:00:01.000000Z",
            has_photo=False,
            has_video=False,
            raw_data={"id": "abc"},
        )

    def test_insert_dedup_by_url_canonical(self, tmp_db: Database) -> None:
        first = insert_news_event(tmp_db, self._event())
        assert first is not None
        second = insert_news_event(tmp_db, self._event())
        assert second is None

    def test_recent_news_events_orders_by_detected_ts(self, tmp_db: Database) -> None:
        insert_news_event(tmp_db, self._event(url="https://e.com/1"))
        ev2 = self._event(url="https://e.com/2")
        ev2 = NewsEventRow(**{**ev2.__dict__, "detected_ts": "2026-04-25T00:00:02.000000Z"})
        insert_news_event(tmp_db, ev2)
        out = recent_news_events(tmp_db, limit=10)
        assert out[0]["url"] == "https://e.com/2"

    def test_unmatched_news_events(self, tmp_db: Database) -> None:
        upsert_market(
            tmp_db,
            MarketRow(
                ticker="T",
                series_ticker="S",
                event_ticker=None,
                title="t",
                subtitle=None,
                yes_sub_title=None,
                no_sub_title=None,
                subject="putin",
                resolution_rules="r",
                approved_sources=None,
                open_ts="2026-01-01T00:00:00.000000Z",
                close_ts=None,
                expected_expiration_ts=None,
                status="active",
                last_price_cents=None,
                volume=None,
                open_interest=None,
                raw_json=None,
            ),
        )
        ev_id = insert_news_event(tmp_db, self._event())
        assert ev_id is not None
        unmatched = fetch_news_events_without_matches(tmp_db)
        assert len(unmatched) == 1
        insert_news_matches(
            tmp_db,
            [
                NewsMatchRow(
                    news_event_id=ev_id,
                    ticker="T",
                    confidence=0.8,
                    matched_subject="putin",
                    matched_keywords=["putin", "spoke"],
                    match_reason="direct_verb",
                )
            ],
        )
        unmatched = fetch_news_events_without_matches(tmp_db)
        assert unmatched == []
        assert recent_high_confidence_matches(tmp_db)[0]["ticker"] == "T"


class TestSystemEvents:
    def test_insert(self, tmp_db: Database) -> None:
        insert_system_event(
            tmp_db,
            event_type="startup",
            severity="info",
            component="test",
            message="hello",
            detail={"k": 1},
        )
        rows = list(tmp_db.connect().execute("SELECT * FROM system_events"))
        assert len(rows) == 1
        assert rows[0]["event_type"] == "startup"
