"""Unit tests for the Phase 1.5 LLM cascade.

Phase 4 Part 2.8 deployed Stage 2 of the news classifier. These
tests pin behavior the operator depends on:

- Successful classification with mocked Anthropic response
- Malformed JSON response: returns None, error logged in
  llm_classifications row
- API timeout: retries once, then returns None, audit row written
- API 401: raises AnthropicAuthError, audit row written first
- Cost guard cap hit: classification skipped, no LLM call made
- Contract file hash drift: alert dispatched, classification continues
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trumpbot.db.connection import Database
from trumpbot.db.repositories import NewsEventRow, insert_news_event
from trumpbot.news.llm_classifier import (
    AnthropicAuthError,
    ClassificationResult,
    LLMClassifier,
    LLMClassifierConfig,
)
from trumpbot.notifications.llm_cost import (
    CapStatus,
    LLMCostGuard,
    LLMCostGuardConfig,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "cls.db")
    db.connect()
    return db


@pytest.fixture()
def contract_file(tmp_path: Path) -> Path:
    p = tmp_path / "rules.txt"
    p.write_text(
        "If Donald Trump and [input name] meet (including phone calls)\n"
        "before the deadline, then the market resolves to Yes.\n"
        "A meeting is defined as direct in-person, video, or phone\n"
        "communication where both parties interact in real-time.\n"
    )
    return p


@pytest.fixture()
def prompt_file(tmp_path: Path) -> Path:
    p = tmp_path / "prompt.txt"
    p.write_text(
        "Rules:\n{contract_rules_verbatim}\nSubjects:\n{subject_list}\n"
        "Headline: {article_headline}\nBody: {article_body}\n"
    )
    return p


def _make_classifier(
    db: Database,
    contract_file: Path,
    prompt_file: Path,
    *,
    llm_call: object,
    cap_cents: int = 10_000,
) -> LLMClassifier:
    cost_guard = LLMCostGuard(
        db=db,
        config=LLMCostGuardConfig(monthly_cap_usd_cents=cap_cents),
    )
    return LLMClassifier(
        db=db,
        cost_guard=cost_guard,
        alerts=None,
        config=LLMClassifierConfig(
            enabled=True,
            prompt_path=str(prompt_file),
            contract_path=str(contract_file),
        ),
        llm_call=llm_call,  # type: ignore[arg-type]
    )


def _seed_event(db: Database, headline: str = "Trump and Putin held call") -> int:
    return (
        insert_news_event(
            db,
            NewsEventRow(
                source="reuters",
                is_kalshi_approved=True,
                headline=headline,
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
        or 0
    )


# ---------------------------------------------------------------------------
# Successful classification
# ---------------------------------------------------------------------------


class TestSuccessfulClassification:
    async def test_returns_parsed_result(
        self, db: Database, contract_file: Path, prompt_file: Path
    ) -> None:
        evt_id = _seed_event(db)

        async def stub_call(system: str, user: str) -> tuple[int, int, str]:
            return (
                100,
                50,
                '{"subject": "vladimirputin", "interaction_occurred": true, '
                '"interaction_type": "phone", "tense": "past", "negated": false, '
                '"indirect_only": false, "confidence": 0.92, '
                '"reasoning": "Past-tense call confirmed by White House readout."}',
            )

        cls = _make_classifier(db, contract_file, prompt_file, llm_call=stub_call)
        out = await cls.classify(
            news_event_id=evt_id,
            headline="Trump and Putin held call",
            body="Kremlin readout confirms.",
            subject_candidates={"vladimirputin": ["Vladimir Putin", "Putin"]},
        )
        assert out is not None
        result, row_id = out
        assert isinstance(result, ClassificationResult)
        assert result.interaction_occurred is True
        assert result.subject == "vladimirputin"
        assert result.confidence == pytest.approx(0.92)
        # And an llm_classifications row was written.
        row = (
            db.connect()
            .execute(
                "SELECT id, parsed_interaction_occurred, parsed_subject "
                "FROM llm_classifications WHERE id = ?",
                (row_id,),
            )
            .fetchone()
        )
        assert row is not None
        assert row["parsed_interaction_occurred"] == 1
        assert row["parsed_subject"] == "vladimirputin"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    async def test_malformed_json_returns_none(
        self, db: Database, contract_file: Path, prompt_file: Path
    ) -> None:
        evt_id = _seed_event(db)

        async def stub_call(system: str, user: str) -> tuple[int, int, str]:
            return (10, 5, "not even close to JSON")

        cls = _make_classifier(db, contract_file, prompt_file, llm_call=stub_call)
        out = await cls.classify(
            news_event_id=evt_id,
            headline="x",
            body="y",
            subject_candidates={"x": ["X"]},
        )
        assert out is None
        # An audit row with error='parse_failed' was written.
        row = (
            db.connect()
            .execute(
                "SELECT error FROM llm_classifications WHERE news_event_id = ?",
                (evt_id,),
            )
            .fetchone()
        )
        assert row is not None
        assert row["error"] == "parse_failed"

    async def test_api_error_retries_then_fails(
        self, db: Database, contract_file: Path, prompt_file: Path
    ) -> None:
        evt_id = _seed_event(db)
        calls: list[int] = []

        async def stub_call(system: str, user: str) -> tuple[int, int, str]:
            calls.append(1)
            raise TimeoutError("simulated timeout")

        cls = _make_classifier(db, contract_file, prompt_file, llm_call=stub_call)
        out = await cls.classify(
            news_event_id=evt_id,
            headline="x",
            body="y",
            subject_candidates={"x": ["X"]},
        )
        assert out is None
        # Retry once -> 2 attempts.
        assert len(calls) == 2
        # And an audit row with the timeout error was written.
        row = (
            db.connect()
            .execute(
                "SELECT error FROM llm_classifications WHERE news_event_id = ?",
                (evt_id,),
            )
            .fetchone()
        )
        assert row is not None
        assert "TimeoutError" in (row["error"] or "")

    async def test_anthropic_auth_error_raises(
        self, db: Database, contract_file: Path, prompt_file: Path
    ) -> None:
        evt_id = _seed_event(db)

        async def stub_call(system: str, user: str) -> tuple[int, int, str]:
            raise AnthropicAuthError("401 invalid api key")

        cls = _make_classifier(db, contract_file, prompt_file, llm_call=stub_call)
        with pytest.raises(AnthropicAuthError):
            await cls.classify(
                news_event_id=evt_id,
                headline="x",
                body="y",
                subject_candidates={"x": ["X"]},
            )
        # Audit row written before the raise.
        row = (
            db.connect()
            .execute(
                "SELECT error FROM llm_classifications WHERE news_event_id = ?",
                (evt_id,),
            )
            .fetchone()
        )
        assert row is not None
        assert row["error"] == "anthropic_401"


# ---------------------------------------------------------------------------
# Cost guard interaction
# ---------------------------------------------------------------------------


class TestCostGuardInteraction:
    async def test_cap_hit_skips_classification(
        self, db: Database, contract_file: Path, prompt_file: Path
    ) -> None:
        evt_id = _seed_event(db)
        called: list[int] = []

        async def stub_call(system: str, user: str) -> tuple[int, int, str]:
            called.append(1)
            return (10, 5, "{}")

        # Cap = 0 cents -> always over cap.
        cls = _make_classifier(db, contract_file, prompt_file, llm_call=stub_call, cap_cents=0)
        out = await cls.classify(
            news_event_id=evt_id,
            headline="x",
            body="y",
            subject_candidates={"x": ["X"]},
        )
        assert out is None
        assert called == []  # LLM was NOT called

    async def test_no_candidates_skips_classification(
        self, db: Database, contract_file: Path, prompt_file: Path
    ) -> None:
        evt_id = _seed_event(db)

        async def stub_call(system: str, user: str) -> tuple[int, int, str]:
            return (10, 5, "{}")

        cls = _make_classifier(db, contract_file, prompt_file, llm_call=stub_call)
        out = await cls.classify(
            news_event_id=evt_id,
            headline="x",
            body="y",
            subject_candidates={},
        )
        assert out is None


# ---------------------------------------------------------------------------
# CapStatus tiers
# ---------------------------------------------------------------------------


class TestCapStatusTiers:
    def test_under_50_with_no_spend(self, db: Database) -> None:
        guard = LLMCostGuard(db=db, config=LLMCostGuardConfig(monthly_cap_usd_cents=1000))
        assert guard.cap_status() == CapStatus.UNDER_50

    def test_between_50_90(self, db: Database) -> None:
        guard = LLMCostGuard(db=db, config=LLMCostGuardConfig(monthly_cap_usd_cents=1000))
        # Spend $7 of $10 cap = 70%.
        guard.record_spend(
            component="news_classifier",
            model="claude-haiku-4-5",
            cost_usd_cents=700,
            input_tokens=100,
            output_tokens=50,
        )
        assert guard.cap_status() == CapStatus.BETWEEN_50_90

    def test_between_90_100(self, db: Database) -> None:
        guard = LLMCostGuard(db=db, config=LLMCostGuardConfig(monthly_cap_usd_cents=1000))
        guard.record_spend(
            component="news_classifier",
            model="claude-haiku-4-5",
            cost_usd_cents=950,
            input_tokens=100,
            output_tokens=50,
        )
        assert guard.cap_status() == CapStatus.BETWEEN_90_100

    def test_over_cap(self, db: Database) -> None:
        guard = LLMCostGuard(db=db, config=LLMCostGuardConfig(monthly_cap_usd_cents=1000))
        guard.record_spend(
            component="news_classifier",
            model="claude-haiku-4-5",
            cost_usd_cents=1100,
            input_tokens=100,
            output_tokens=50,
        )
        assert guard.cap_status() == CapStatus.OVER_CAP
        assert guard.is_under_cap() is False

    def test_zero_cap_is_over(self, db: Database) -> None:
        guard = LLMCostGuard(db=db, config=LLMCostGuardConfig(monthly_cap_usd_cents=0))
        assert guard.cap_status() == CapStatus.OVER_CAP


# ---------------------------------------------------------------------------
# Contract drift alert
# ---------------------------------------------------------------------------


class TestContractDrift:
    async def test_first_call_seeds_known_hash(
        self, db: Database, contract_file: Path, prompt_file: Path
    ) -> None:
        evt_id = _seed_event(db)

        async def stub_call(system: str, user: str) -> tuple[int, int, str]:
            return (
                10,
                5,
                '{"subject": "x", "interaction_occurred": false, '
                '"interaction_type": null, "tense": "ambiguous", "negated": false, '
                '"indirect_only": false, "confidence": 0.0, "reasoning": "n/a"}',
            )

        cls = _make_classifier(db, contract_file, prompt_file, llm_call=stub_call)
        await cls.classify(
            news_event_id=evt_id,
            headline="x",
            body="y",
            subject_candidates={"x": ["X"]},
        )
        # First call seeds the known hash; no alert needed.
        assert cls._known_contract_hash is not None

    async def test_drift_resets_known_hash(
        self, db: Database, contract_file: Path, prompt_file: Path
    ) -> None:
        evt_id = _seed_event(db)

        async def stub_call(system: str, user: str) -> tuple[int, int, str]:
            return (
                10,
                5,
                '{"subject": "x", "interaction_occurred": false, '
                '"interaction_type": null, "tense": "ambiguous", "negated": false, '
                '"indirect_only": false, "confidence": 0.0, "reasoning": "n/a"}',
            )

        cls = _make_classifier(db, contract_file, prompt_file, llm_call=stub_call)
        await cls.classify(
            news_event_id=evt_id,
            headline="x",
            body="y",
            subject_candidates={"x": ["X"]},
        )
        original_hash = cls._known_contract_hash
        # Mutate the file.
        contract_file.write_text("a wholly different contract text now")
        await cls.classify(
            news_event_id=evt_id,
            headline="x",
            body="y",
            subject_candidates={"x": ["X"]},
        )
        assert cls._known_contract_hash != original_hash
