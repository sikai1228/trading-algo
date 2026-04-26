"""Bridge between the discovery service's `subjects` table and the matcher.

The discovery service writes `markets.subject = "vladimirputin"` (the
new normalized subject_key) but the matcher's hard-coded
`DEFAULT_SUBJECT_ALIASES` is keyed by short forms like `"putin"`.
Without the bridge, every match against a discovery-inserted market
returns confidence=0 with reason "unknown_subject".

These tests pin the bridge behavior:

- `subjects_alias_map` returns the JSON-decoded alias dict for every
  row in the subjects table.
- `MatcherWorker._build_merged_aliases` layers DB subjects on top of
  DEFAULT_SUBJECT_ALIASES (DB wins on key conflict; both worlds
  coexist).
- A discovery-style market + a positive headline produces a non-zero
  confidence match (regression for the smoke-test bug where 528
  events x 22 markets gave 0 matches).
"""

from __future__ import annotations

from pathlib import Path

from trumpbot.daemon import MatcherWorker
from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    NewsEventRow,
    SubjectRow,
    insert_news_event,
    list_subjects,
    subjects_alias_map,
    upsert_market,
    upsert_subject,
)
from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor
from trumpbot.events.bus import EventBus
from trumpbot.news.matcher import PASSED_REASON, NewsMatcher

# A throwaway extractor passed to MatcherWorker.matcher so the constructor
# is happy. _process_batch builds its own merged extractor on each batch,
# so this initial one is never actually used for matching.
_NULL_EXTRACTOR = SubjectExtractor(aliases={"_unused": ["unused"]})


def _seed_market(db: Database, ticker: str, subject_key: str, full_name: str) -> None:
    upsert_market(
        db,
        MarketRow(
            ticker=ticker,
            series_ticker="KXTRUMPMEET",
            event_ticker="KXTRUMPMEET-26APR",
            title=f"Donald Trump and {full_name} meet before May 1, 2026?",
            subtitle=full_name,
            yes_sub_title="Yes",
            no_sub_title="No",
            subject=subject_key,
            subject_full_name=full_name,
            resolution_rules="If they meet, resolves YES.",
            approved_sources=None,
            open_ts="2026-04-01T00:00:00.000000Z",
            close_ts="2026-04-30T23:59:59.000000Z",
            expected_expiration_ts=None,
            status="active",
            last_price_cents=42,
            volume=100,
            open_interest=50,
            raw_json=None,
        ),
    )


class TestSubjectsAliasMap:
    def test_returns_decoded_aliases(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "amap.db")
        db.connect()
        upsert_subject(
            db,
            SubjectRow(
                subject_key="vladimirputin",
                full_name="Vladimir Putin",
                aliases=["Vladimir Putin", "Putin"],
            ),
        )
        upsert_subject(
            db,
            SubjectRow(
                subject_key="xijinping",
                full_name="Xi Jinping",
                aliases=["Xi Jinping", "Xi"],
            ),
        )
        out = subjects_alias_map(db)
        assert out["vladimirputin"] == ["Vladimir Putin", "Putin"]
        assert sorted(out["xijinping"]) == sorted(["Xi Jinping", "Xi"])
        db.close()

    def test_empty_table_returns_empty_dict(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "empty.db")
        db.connect()
        assert subjects_alias_map(db) == {}
        db.close()

    def test_list_subjects_orders_by_key(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "list.db")
        db.connect()
        for key, name in [("zwoods", "Tiger Woods"), ("aputin", "Vladimir Putin")]:
            upsert_subject(db, SubjectRow(subject_key=key, full_name=name, aliases=[name]))
        keys = [r["subject_key"] for r in list_subjects(db)]
        assert keys == ["aputin", "zwoods"]
        db.close()


class TestMergedAliases:
    def test_db_subjects_added_to_defaults(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "merge.db")
        db.connect()
        upsert_subject(
            db,
            SubjectRow(
                subject_key="vladimirputin",
                full_name="Vladimir Putin",
                aliases=["Vladimir Putin", "Putin"],
            ),
        )
        worker = MatcherWorker(
            db=db,
            matcher=NewsMatcher(extractor=_NULL_EXTRACTOR),
            event_bus=EventBus(),
        )
        merged = worker._build_merged_aliases()
        # DEFAULT_SUBJECT_ALIASES keys still present (e.g. "putin").
        assert "putin" in merged
        # The discovery-service key is also present.
        assert merged["vladimirputin"] == ["Vladimir Putin", "Putin"]
        db.close()

    def test_db_value_wins_on_key_conflict(self, tmp_path: Path) -> None:
        """If the same subject_key appears in both DEFAULT_SUBJECT_ALIASES
        and the subjects table, DB wins (operator intent)."""
        db = Database(tmp_path / "conflict.db")
        db.connect()
        # Both DEFAULT_SUBJECT_ALIASES and the DB use "putin" as the key.
        upsert_subject(
            db,
            SubjectRow(
                subject_key="putin",
                full_name="Vladimir Putin",
                aliases=["DB-only Alias"],
            ),
        )
        worker = MatcherWorker(
            db=db,
            matcher=NewsMatcher(extractor=_NULL_EXTRACTOR),
            event_bus=EventBus(),
        )
        merged = worker._build_merged_aliases()
        # DB value should override DEFAULT_SUBJECT_ALIASES.
        assert merged["putin"] == ["DB-only Alias"]
        # And keys not in the DB fall back to DEFAULTs.
        assert merged["xi"] == DEFAULT_SUBJECT_ALIASES["xi"]
        db.close()


class TestEndToEndMatch:
    """Regression: discovery-style market + Trump-talked-to-X headline
    must produce a positive match. This was returning 0 before the bridge."""

    async def test_vladimirputin_subject_matches_putin_headline(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "e2e.db")
        db.connect()

        # Seed via the same path the discovery service uses.
        upsert_subject(
            db,
            SubjectRow(
                subject_key="vladimirputin",
                full_name="Vladimir Putin",
                aliases=["Vladimir Putin", "Putin"],
            ),
        )
        _seed_market(db, "KXTRUMPMEET-26APR-VPUT", "vladimirputin", "Vladimir Putin")

        # A representative news event.
        insert_news_event(
            db,
            NewsEventRow(
                source="reuters",
                is_kalshi_approved=True,
                headline="Trump spoke with Putin about Ukraine",
                url="https://example.com/x",
                url_canonical="https://example.com/x",
                body_excerpt="Per a White House readout.",
                author=None,
                raw_published_ts="2026-04-25T12:00:00Z",
                detected_ts="2026-04-25T12:00:01Z",
                has_photo=False,
                has_video=False,
                raw_data=None,
            ),
        )

        worker = MatcherWorker(
            db=db,
            matcher=NewsMatcher(extractor=_NULL_EXTRACTOR),
            event_bus=EventBus(),
        )
        processed = await worker._process_batch()
        assert processed == 1

        rows = list(
            db.connect().execute(
                "SELECT confidence, matched_subject, match_reason FROM news_market_matches"
            )
        )
        assert len(rows) == 1
        # Phase 4 Part 2.8: Stage 1 always emits confidence=0.0; the
        # LLM cascade (not active in this test) is what would set it
        # to >= 0.85.
        assert rows[0]["confidence"] == 0.0
        assert rows[0]["matched_subject"] == "vladimirputin"
        assert rows[0]["match_reason"] == PASSED_REASON
        db.close()

    async def test_unknown_subject_still_returns_zero(self, tmp_path: Path) -> None:
        """A subject NOT in DEFAULT_SUBJECT_ALIASES and NOT in the
        subjects table must still hit the unknown_subject path —
        the bridge doesn't silently invent aliases."""
        db = Database(tmp_path / "unknown.db")
        db.connect()
        _seed_market(db, "KXTRUMPMEET-26APR-XXX", "completelyunknown", "Unknown Person")
        insert_news_event(
            db,
            NewsEventRow(
                source="reuters",
                is_kalshi_approved=True,
                headline="Trump spoke with Unknown Person",
                url="https://example.com/u",
                url_canonical="https://example.com/u",
                body_excerpt=None,
                author=None,
                raw_published_ts=None,
                detected_ts="2026-04-25T12:00:00Z",
                has_photo=False,
                has_video=False,
                raw_data=None,
            ),
        )
        worker = MatcherWorker(
            db=db,
            matcher=NewsMatcher(extractor=_NULL_EXTRACTOR),
            event_bus=EventBus(),
        )
        await worker._process_batch()
        rows = list(
            db.connect().execute("SELECT confidence, match_reason FROM news_market_matches")
        )
        assert len(rows) == 1
        assert rows[0]["confidence"] == 0.0
        # Phase 4 Part 2.8: failed pre-filter reasons are namespaced
        # under "failed_pre_filter:" with the specific cause appended.
        assert "unknown_subject" in rows[0]["match_reason"]
        assert rows[0]["match_reason"].startswith("failed_pre_filter:")
        db.close()
