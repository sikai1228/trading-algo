"""Alias-enrichment tests.

The Anthropic call is stubbed via :class:`AliasEnricher`'s injected
``llm_call`` callable. Tests cover the happy path, the JSON-recovery
path (LLM wraps the array in chatter), the cap-hit path, the 401
path (which fires alert_critical_anthropic_auth), the
already-enriched path (idempotent skip), and malformed-LLM-output
recovery.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    SubjectRow,
    fetch_subject_aliases,
    upsert_subject,
)
from trumpbot.events.bus import Event
from trumpbot.news.alias_enrichment import (
    AliasEnricher,
    AliasEnrichmentConfig,
    _AnthropicAuthError,
    _coerce_strings,
    _parse_alias_list,
)
from trumpbot.notifications.alerts import AlertDispatcher
from trumpbot.notifications.llm_cost import LLMCostGuard, LLMCostGuardConfig


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "alias.db")
    db.connect()
    upsert_subject(
        db,
        SubjectRow(
            subject_key="putin",
            full_name="Vladimir Putin",
            aliases=["putin", "vladimir putin"],
        ),
    )
    return db


def _stub_llm(
    payload: str, *, in_tok: int = 50, out_tok: int = 30
) -> Callable[..., Awaitable[tuple[int, int, str]]]:
    async def _fn(_system: str, _user: str) -> tuple[int, int, str]:
        return (in_tok, out_tok, payload)

    return _fn


def _enricher(
    db: Database,
    *,
    llm_call: Callable[..., Awaitable[tuple[int, int, str]]],
    cap_cents: int = 1000,
    enabled: bool = True,
) -> tuple[AliasEnricher, AlertDispatcher]:
    cost_guard = LLMCostGuard(db=db, config=LLMCostGuardConfig(monthly_cap_usd_cents=cap_cents))
    dispatcher = AlertDispatcher(db=db, send_fn=None)
    enricher = AliasEnricher(
        db=db,
        cost_guard=cost_guard,
        alerts=dispatcher,
        config=AliasEnrichmentConfig(enabled=enabled),
        llm_call=llm_call,
    )
    return enricher, dispatcher


def _event(subject: str = "putin", full_name: str = "Vladimir Putin", ticker: str = "X") -> Event:
    return Event(
        type="market_discovered",
        payload={
            "ticker": ticker,
            "subject_key": subject,
            "subject_full_name": full_name,
            "event_ticker": "KX-26APR",
        },
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_enriches_and_marks_subject(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        llm = _stub_llm('["putin", "vlad", "president putin", "kremlin chief"]')
        enricher, _ = _enricher(db, llm_call=llm)
        await enricher.on_market_discovered(_event())
        result = fetch_subject_aliases(db, "putin")
        assert result is not None
        enriched, aliases = result
        assert enriched is True
        assert "vlad" in aliases
        assert "president putin" in aliases
        # Originals preserved.
        assert "vladimir putin" in aliases

    @pytest.mark.asyncio
    async def test_records_spend(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        llm = _stub_llm('["a", "b"]', in_tok=1000, out_tok=500)
        enricher, _ = _enricher(db, llm_call=llm)
        await enricher.on_market_discovered(_event())
        # 1000 * (0.25/1M) + 500 * (1.25/1M) = 0.00025 + 0.000625 = 0.000875 cents
        # ceil = 1 cent recorded.
        rows = list(db.connect().execute("SELECT cost_usd_cents FROM llm_spend_log"))
        assert len(rows) == 1
        assert rows[0][0] >= 1

    @pytest.mark.asyncio
    async def test_already_enriched_skips_call(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        # Mark as already-enriched.
        with db.transaction() as conn:
            conn.execute("UPDATE subjects SET llm_enriched = 1 WHERE subject_key = ?", ("putin",))
        called = {"count": 0}

        async def llm(_s: str, _u: str) -> tuple[int, int, str]:
            called["count"] += 1
            return (10, 10, "[]")

        enricher, _ = _enricher(db, llm_call=llm)
        await enricher.on_market_discovered(_event())
        assert called["count"] == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_missing_subject_safely_returns(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        enricher, _ = _enricher(db, llm_call=_stub_llm("[]"))
        # Subject not in DB -> safe no-op.
        await enricher.on_market_discovered(_event(subject="zzz_missing", full_name="Z Z"))

    @pytest.mark.asyncio
    async def test_disabled_skips_call(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        called = {"count": 0}

        async def llm(_s: str, _u: str) -> tuple[int, int, str]:
            called["count"] += 1
            return (10, 10, "[]")

        enricher, _ = _enricher(db, llm_call=llm, enabled=False)
        await enricher.on_market_discovered(_event())
        assert called["count"] == 0

    @pytest.mark.asyncio
    async def test_cap_hit_skips_call(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        # Pre-record spend exceeding the cap.
        from trumpbot.db.repositories import insert_llm_spend

        insert_llm_spend(db, component="x", model="y", cost_usd_cents=2000)
        called = {"count": 0}

        async def llm(_s: str, _u: str) -> tuple[int, int, str]:
            called["count"] += 1
            return (10, 10, "[]")

        enricher, _ = _enricher(db, llm_call=llm, cap_cents=1000)
        await enricher.on_market_discovered(_event())
        assert called["count"] == 0

    @pytest.mark.asyncio
    async def test_401_fires_alert_keeps_aliases(self, tmp_path: Path) -> None:
        db = _db(tmp_path)

        async def llm(_s: str, _u: str) -> tuple[int, int, str]:
            raise _AnthropicAuthError("401")

        enricher, _ = _enricher(db, llm_call=llm)
        await enricher.on_market_discovered(_event())
        # Subject NOT marked enriched.
        result = fetch_subject_aliases(db, "putin")
        assert result is not None
        enriched, aliases = result
        assert enriched is False
        assert aliases == ["putin", "vladimir putin"]
        # alert_critical_anthropic_auth was logged.
        ev = (
            db.connect()
            .execute(
                "SELECT event_type FROM system_events "
                "WHERE event_type = 'alert_critical_anthropic_auth'"
            )
            .fetchone()
        )
        assert ev is not None

    @pytest.mark.asyncio
    async def test_malformed_json_logged_subject_unchanged(self, tmp_path: Path) -> None:
        db = _db(tmp_path)

        async def llm(_s: str, _u: str) -> tuple[int, int, str]:
            return (10, 10, "this is not json at all")

        enricher, _ = _enricher(db, llm_call=llm)
        await enricher.on_market_discovered(_event())
        result = fetch_subject_aliases(db, "putin")
        assert result is not None
        enriched, aliases = result
        assert enriched is False
        assert aliases == ["putin", "vladimir putin"]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestParser:
    def test_parses_clean_array(self) -> None:
        assert _parse_alias_list('["a", "b"]') == ["a", "b"]

    def test_recovers_from_chatter(self) -> None:
        raw = 'Sure! Here\'s the JSON: ["thune", "sen. thune"] -- enjoy.'
        assert _parse_alias_list(raw) == ["thune", "sen. thune"]

    def test_lowercases_and_dedupes(self) -> None:
        assert _coerce_strings(["Thune", "thune", "Sen. Thune"]) == [
            "thune",
            "sen. thune",
        ]

    def test_filters_non_strings(self) -> None:
        assert _coerce_strings(["a", 42, None, "b"]) == ["a", "b"]

    def test_empty_array(self) -> None:
        assert _parse_alias_list("[]") == []

    def test_no_array_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_alias_list("just text")
