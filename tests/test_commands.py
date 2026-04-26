"""Telegram-command-handler tests.

Each handler is exercised via :class:`CommandContext` against a fresh
SQLite DB. The Telegram boundary is NOT involved here -- we verify
that handlers produce the right :class:`RenderedMessage` and write the
right rows. The Telegram-bot wiring is tested separately.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    get_system_state,
    insert_llm_spend,
    upsert_market,
)
from trumpbot.notifications.commands import (
    CommandContext,
    CommandRateLimiter,
    all_command_names,
    dispatch,
)
from trumpbot.notifications.llm_cost import LLMCostGuard, LLMCostGuardConfig


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "cmd.db")
    db.connect()
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


def _ctx(db: Database, *, args: list[str] | None = None) -> CommandContext:
    return CommandContext(
        db=db,
        args=args or [],
        cost_guard=LLMCostGuard(db=db, config=LLMCostGuardConfig(monthly_cap_usd_cents=1000)),
        bankroll_usd_cents=50000,
        daemon_started_at=datetime.now(UTC) - timedelta(hours=2),
        sources_total=8,
        sources_active=8,
    )


# ---------------------------------------------------------------------------
# Dispatch + registry
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_known_command_returns_handler(self) -> None:
        for name in (
            "halt",
            "resume",
            "status",
            "positions",
            "why",
            "history",
            "spend",
            "mode",
            "help",
        ):
            assert dispatch(name) is not None
            assert dispatch(f"/{name}") is not None
            assert dispatch(name.upper()) is not None  # case-insensitive

    def test_unknown_command_returns_none(self) -> None:
        assert dispatch("nosuchcommand") is None

    def test_all_command_names_includes_every_handler(self) -> None:
        names = set(all_command_names())
        assert "/halt" in names
        assert "/resume" in names
        assert "/help" in names

    def test_snooze_commands_removed(self) -> None:
        """Phase 4 Part 2.9: /snooze and /unsnooze are gone. The
        dispatcher must return None so the unknown-command reply
        fires."""
        assert dispatch("snooze") is None
        assert dispatch("unsnooze") is None
        assert dispatch("/snooze") is None
        assert "/snooze" not in set(all_command_names())
        assert "/unsnooze" not in set(all_command_names())


# ---------------------------------------------------------------------------
# Halt + resume + state inspection
# ---------------------------------------------------------------------------


class TestHaltResume:
    @pytest.mark.asyncio
    async def test_halt_sets_flag(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        out = await dispatch("halt")(_ctx(db))  # type: ignore[misc]
        assert "HALTED" in out.text
        assert get_system_state(db, "halt_flag") == "true"

    @pytest.mark.asyncio
    async def test_resume_clears_flag(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        await dispatch("halt")(_ctx(db))  # type: ignore[misc]
        out = await dispatch("resume")(_ctx(db))  # type: ignore[misc]
        assert "RESUMED" in out.text
        assert get_system_state(db, "halt_flag") == "false"

    @pytest.mark.asyncio
    async def test_status_shows_halt_state(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        out = await dispatch("status")(_ctx(db))  # type: ignore[misc]
        assert "Halt: off" in out.text
        await dispatch("halt")(_ctx(db))  # type: ignore[misc]
        out2 = await dispatch("status")(_ctx(db))  # type: ignore[misc]
        assert "Halt: ON" in out2.text


# Phase 4 Part 2.9 — TestSnooze was REMOVED with the /snooze and
# /unsnooze handlers. See test_halt.py for the regression test that
# pins the snooze surface stays gone.


# ---------------------------------------------------------------------------
# /history + /positions + /spend + /why
# ---------------------------------------------------------------------------


class TestReadOnlyCommands:
    @pytest.mark.asyncio
    async def test_positions_empty(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        out = await dispatch("positions")(_ctx(db))  # type: ignore[misc]
        assert "(no open positions)" in out.text

    @pytest.mark.asyncio
    async def test_history_empty(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        out = await dispatch("history")(_ctx(db))  # type: ignore[misc]
        assert "Last 0 closed trades" in out.text or "(no closed trades)" in out.text

    @pytest.mark.asyncio
    async def test_spend_with_no_calls(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        out = await dispatch("spend")(_ctx(db))  # type: ignore[misc]
        assert "$0.00" in out.text

    @pytest.mark.asyncio
    async def test_spend_after_recording_calls(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        insert_llm_spend(db, component="alias", model="haiku", cost_usd_cents=15)
        insert_llm_spend(db, component="alias", model="haiku", cost_usd_cents=25)
        out = await dispatch("spend")(_ctx(db))  # type: ignore[misc]
        assert "$0.40" in out.text  # 15 + 25 = 40 cents = $0.40

    @pytest.mark.asyncio
    async def test_why_unknown_trade(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        out = await dispatch("why")(_ctx(db, args=["999"]))  # type: ignore[misc]
        assert "no trade #999" in out.text

    @pytest.mark.asyncio
    async def test_why_no_args(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        out = await dispatch("why")(_ctx(db))  # type: ignore[misc]
        assert "Usage:" in out.text


# ---------------------------------------------------------------------------
# Trivial commands
# ---------------------------------------------------------------------------


class TestSimpleCommands:
    # Phase 4 Part 2.10 — test_heartbeat_returns_alive REMOVED with
    # the /heartbeat command. See tests/test_no_heartbeat.py for the
    # regression test pinning the surface stays gone.

    @pytest.mark.asyncio
    async def test_help_lists_commands(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        out = await dispatch("help")(_ctx(db))  # type: ignore[misc]
        for c in ("/status", "/halt", "/resume"):
            assert c in out.text
        # Heartbeat is gone (Phase 4 Part 2.10).
        assert "/heartbeat" not in out.text

    @pytest.mark.asyncio
    async def test_mode_shows_dry_run(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        out = await dispatch("mode")(_ctx(db))  # type: ignore[misc]
        assert "Execution: dry_run" in out.text


# Phase 4 Part 2.9 — TestParseDuration was REMOVED with parse_duration.
# It was only used by /snooze; both are gone.


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_allows_under_limit(self) -> None:
        rl = CommandRateLimiter(max_per_minute=3)
        chat = 12345
        assert rl.check(chat) is True
        assert rl.check(chat) is True
        assert rl.check(chat) is True

    def test_blocks_over_limit(self) -> None:
        rl = CommandRateLimiter(max_per_minute=3)
        chat = 12345
        for _ in range(3):
            rl.check(chat)
        assert rl.check(chat) is False

    def test_per_chat_independent(self) -> None:
        rl = CommandRateLimiter(max_per_minute=2)
        for _ in range(2):
            rl.check(111)
        # 111 is at limit; 222 is fresh.
        assert rl.check(111) is False
        assert rl.check(222) is True
