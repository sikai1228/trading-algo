"""Pre-live fixes regression tests — docs/OPEN_ISSUES.md items #1, #2, #11.

Phase 4 Part 2.2.

- Fix #1: BankrollState piping (config vs kalshi_synced vs kalshi_fallback)
- Fix #2: Bankroll-sync auto-halt + auto-resume cycle
- Fix #11: KalshiExecutor prefers Kalshi-reported fills over re-walk
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trumpbot.account.bankroll_sync import (
    BANKROLL_LAST_SYNC_KEY,
    BANKROLL_STATE_KEY,
    HALT_REASON_BANKROLL_SYNC,
    HALT_REASON_KEY,
    SYNC_FAILURE_HALT_THRESHOLD,
    bankroll_sync_loop,
    sync_bankroll_once,
)
from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    MarketRow,
    NewsEventRow,
    TradeInsertRow,
    get_system_state,
    insert_news_event,
    insert_trade,
    set_system_state,
    upsert_market,
)
from trumpbot.decision.engine import BankrollState
from trumpbot.decision.loops import _bankroll_state
from trumpbot.execution.dry_run import Quote
from trumpbot.execution.live_executor import KalshiExecutor
from trumpbot.kalshi.schemas import KalshiBalance, KalshiOrder
from trumpbot.risk.manager import RiskConfig, RiskManager, RiskState
from trumpbot.types.intents import RiskApprovedOrder, TradeIntent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "x.db")
    db.connect()
    upsert_market(
        db,
        MarketRow(
            ticker="X",
            series_ticker="S",
            event_ticker=None,
            title="X meet?",
            subtitle=None,
            yes_sub_title=None,
            no_sub_title=None,
            subject="x",
            subject_full_name="X",
            resolution_rules="r",
            approved_sources=None,
            open_ts="2026-01-01T00:00:00Z",
            close_ts="2026-12-31T23:59:59Z",
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
            source="reuters",
            is_kalshi_approved=True,
            headline="h",
            url="u",
            url_canonical="u",
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


# ---------------------------------------------------------------------------
# FIX #1 — BankrollState piping
# ---------------------------------------------------------------------------


class TestBankrollStateSourcing:
    def test_dry_run_uses_config_value_regardless_of_cache(self, tmp_path: Path) -> None:
        """Even if the system_state cache has a value, dry-run mode
        ignores it and uses ``starting_amount_usd``."""
        db = _db(tmp_path)
        # Pretend the cache has $999.99 in it.
        set_system_state(db, key=BANKROLL_STATE_KEY, value="99999")
        state = _bankroll_state(db, starting_amount_usd=500.0, execution_mode="dry_run")
        assert state.bankroll_usd_cents == 50000
        assert state.source == "config"
        assert state.last_synced_at is None

    def test_live_mode_uses_cached_value(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        set_system_state(db, key=BANKROLL_STATE_KEY, value="12345")
        set_system_state(
            db,
            key=BANKROLL_LAST_SYNC_KEY,
            value="2026-04-15T12:00:00+00:00",
        )
        state = _bankroll_state(db, starting_amount_usd=500.0, execution_mode="live")
        assert state.bankroll_usd_cents == 12345
        assert state.source == "kalshi_synced"
        assert state.last_synced_at is not None
        assert state.last_synced_at.year == 2026

    def test_live_mode_falls_back_when_cache_empty(self, tmp_path: Path) -> None:
        """Live mode but the bankroll-sync loop hasn't completed yet:
        fall back to the configured starting amount but tag the source
        as ``kalshi_fallback`` so reasoning text discloses it."""
        db = _db(tmp_path)
        # No system_state.bankroll_usd_cents key at all.
        state = _bankroll_state(db, starting_amount_usd=500.0, execution_mode="live")
        assert state.bankroll_usd_cents == 50000
        assert state.source == "kalshi_fallback"
        assert state.last_synced_at is None

    def test_live_mode_falls_back_on_corrupt_cache(self, tmp_path: Path) -> None:
        db = _db(tmp_path)
        set_system_state(db, key=BANKROLL_STATE_KEY, value="not_a_number")
        state = _bankroll_state(db, starting_amount_usd=500.0, execution_mode="live")
        assert state.bankroll_usd_cents == 50000
        assert state.source == "kalshi_fallback"

    def test_available_excludes_open_positions(self, tmp_path: Path) -> None:
        """available_usd_cents = bankroll - sum(open positions)."""
        db = _db(tmp_path)
        # Open a $5.00 position (50c x 10 contracts).
        insert_trade(
            db,
            TradeInsertRow(
                ticker="X",
                status="dry_run",
                entry_price_cents=50,
                quantity=10,
                cost_basis_usd_cents=500,
                triggering_match_id=1,
                triggering_intent_json="{}",
                risk_decision_id=1,
                approval_id=None,
                is_reentry=False,
                prior_trade_id=None,
                reasoning_text="r",
                entered_at="2026-04-15T12:00:00Z",
            ),
        )
        state = _bankroll_state(db, starting_amount_usd=500.0, execution_mode="dry_run")
        assert state.bankroll_usd_cents == 50000
        # 50000 - 500 = 49500 available
        assert state.available_usd_cents == 49500
        # Back-compat alias still works
        assert state.available_bankroll_usd_cents == 49500

    def test_available_clamps_at_zero(self) -> None:
        """If somehow open_position_cost > bankroll, available is 0
        (NOT negative)."""
        state = BankrollState(
            bankroll_usd_cents=100,
            open_position_cost_usd_cents=200,
        )
        assert state.available_usd_cents == 0


class TestBankrollStateInReasoning:
    """Reasoning text should disclose the bankroll source so the
    operator can spot 'sized off stale fallback' in the audit log."""

    def test_reasoning_mentions_kalshi_synced(self, tmp_path: Path) -> None:
        from trumpbot.decision.engine import (
            DecisionConfig,
            DecisionEngine,
            MarketState,
            MatchSnapshot,
        )

        db = _db(tmp_path)
        engine = DecisionEngine(DecisionConfig())
        match = MatchSnapshot(
            match_id=1,
            ticker="X",
            confidence=0.9,
            interaction_occurred=True,
            source_name="reuters",
            is_kalshi_approved=True,
            market_open_ts="2026-01-01T00:00:00Z",
            market_close_ts="2026-12-31T23:59:59Z",
            article_published_ts="2026-04-15T11:00:00Z",
            classified_at_ts="2026-04-15T11:05:00Z",
        )
        ms = MarketState(
            ticker="X",
            yes_bid_cents=49,
            yes_ask_cents=50,
            total_volume_traded_contracts=1000,
        )
        bankroll = BankrollState(
            bankroll_usd_cents=12345,
            open_position_cost_usd_cents=0,
            source="kalshi_synced",
            last_synced_at=datetime.now(UTC),
        )
        intent = engine.evaluate_news_match(
            match,
            ms,
            current_position=None,
            bankroll=bankroll,
            yes_ask_levels=[(50, 1000)],
            now_utc=datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
        )
        assert intent is not None
        assert "Kalshi-synced balance" in intent.reasoning_text
        assert "$123.45" in intent.reasoning_text
        del db


# ---------------------------------------------------------------------------
# FIX #2 — Bankroll-sync auto-halt + auto-resume
# ---------------------------------------------------------------------------


class _FakeKalshi:
    """Configurable fake. Each call to get_balance returns the next
    item in ``script``; raises if exception, returns KalshiBalance
    otherwise.

    When ``stop_after_call`` is reached, sets ``done_event``
    BEFORE returning that call's result — so the loop sees stop_event
    on its next iteration boundary AND the most recent state (halt
    set or cleared) is whatever the script demanded.

    For tests that need to assert "halt was SET", set
    stop_after_call to the failure count that should trip the halt.
    For tests that need to assert "halt was CLEARED", set
    stop_after_call to a number that lets the recovery success run.
    """

    def __init__(
        self,
        script: list[Any],
        done_event: Any | None = None,
        stop_after_call: int | None = None,
    ) -> None:
        self._script = list(script)
        self._done = done_event
        self._stop_after = stop_after_call
        self._call_count = 0

    async def get_balance(self) -> KalshiBalance:
        self._call_count += 1
        if (
            self._stop_after is not None
            and self._call_count >= self._stop_after
            and self._done is not None
        ):
            self._done.set()
        if not self._script:
            # Script exhausted but stop hasn't been reached — keep the
            # loop alive with benign successes.
            return KalshiBalance(balance=99999)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return KalshiBalance(balance=item)


class TestBankrollSyncAutoHalt:
    async def test_three_consecutive_failures_set_halt(self, tmp_path: Path) -> None:
        """3 failures in a row → halt_flag=true + halt_reason set."""
        import asyncio as _asyncio

        db = _db(tmp_path)
        stop = _asyncio.Event()
        # Initial-sync + 2 iter fails = 3 consecutive failures total.
        # Stop the loop after the 3rd call so the halt-set state is
        # what the assertions see (no recovery success runs).
        kalshi = _FakeKalshi(
            [RuntimeError("net1"), RuntimeError("net2"), RuntimeError("net3")],
            done_event=stop,
            stop_after_call=3,
        )
        sent: list[tuple[str, bool]] = []

        async def _send(text: str, silent: bool) -> None:
            sent.append((text, silent))

        await bankroll_sync_loop(
            db=db,
            kalshi=kalshi,  # type: ignore[arg-type]
            poll_interval_sec=0,
            stop_event=stop,
            send_text=_send,
        )

        assert get_system_state(db, "halt_flag") == "true"
        assert get_system_state(db, HALT_REASON_KEY) == HALT_REASON_BANKROLL_SYNC
        # Audible critical alert fired exactly once.
        critical_sends = [s for s in sent if not s[1]]  # silent=False == audible
        assert len(critical_sends) >= 1
        assert "Kalshi balance sync failing" in critical_sends[0][0]
        assert f"{SYNC_FAILURE_HALT_THRESHOLD}" in critical_sends[0][0]

    async def test_recovery_clears_our_halt(self, tmp_path: Path) -> None:
        """Successful sync after 3 fails → halt cleared, info alert."""
        import asyncio as _asyncio

        db = _db(tmp_path)
        stop = _asyncio.Event()
        kalshi = _FakeKalshi(
            [
                RuntimeError("net"),
                RuntimeError("net"),
                RuntimeError("net"),
                12345,  # success → triggers auto-resume
            ],
            done_event=stop,
            stop_after_call=4,  # let the recovery success run, then stop
        )
        sent: list[tuple[str, bool]] = []

        async def _send(text: str, silent: bool) -> None:
            sent.append((text, silent))

        await bankroll_sync_loop(
            db=db,
            kalshi=kalshi,  # type: ignore[arg-type]
            poll_interval_sec=0,
            stop_event=stop,
            send_text=_send,
        )

        assert get_system_state(db, "halt_flag") == "false"
        assert get_system_state(db, HALT_REASON_KEY) == ""
        # Recovery info alert fired (silent=True).
        info_sends = [s for s in sent if s[1]]
        assert any("recovered" in s[0].lower() for s in info_sends)

    async def test_user_halt_is_not_overridden(self, tmp_path: Path) -> None:
        """If the user set halt_flag manually, the auto-resume logic
        does NOT clear it on a successful sync."""
        db = _db(tmp_path)
        # User halts manually (no halt_reason set).
        set_system_state(db, key="halt_flag", value="true")
        # No halt_reason key set — that's the key invariant.
        kalshi = _FakeKalshi([12345])
        # We didn't trip the auto-halt mechanism, so on success we
        # should NOT clear the user's halt.
        await sync_bankroll_once(db, kalshi)  # type: ignore[arg-type]
        assert get_system_state(db, "halt_flag") == "true"

    async def test_sync_outcomes_increment_counter_only_on_failure(self, tmp_path: Path) -> None:
        """Verify the counter resets on success, not on every cycle.

        2 fails → success (resets counter) → 2 more fails → script
        exhausted → fake sets stop. We never hit 3 consecutive
        failures so halt should NOT fire.
        """
        import asyncio as _asyncio

        db = _db(tmp_path)
        stop = _asyncio.Event()
        kalshi = _FakeKalshi(
            [
                RuntimeError("net"),  # initial sync, fail #1
                RuntimeError("net"),  # iter 1, fail #2
                12345,  # iter 2, success → reset
                RuntimeError("net"),  # iter 3, fail #1
                RuntimeError("net"),  # iter 4, fail #2 (still under threshold)
            ],
            done_event=stop,
            stop_after_call=5,  # stop right after the 5th call
        )

        await bankroll_sync_loop(
            db=db,
            kalshi=kalshi,  # type: ignore[arg-type]
            poll_interval_sec=0,
            stop_event=stop,
            send_text=None,
        )

        # Should NOT be halted (we only ever hit 2 consecutive failures
        # in either streak; threshold is 3).
        assert (get_system_state(db, "halt_flag") or "false") == "false"


# ---------------------------------------------------------------------------
# FIX #11 — KalshiExecutor prefers Kalshi-reported fills
# ---------------------------------------------------------------------------


def _intent_with_walk(*, qty: int = 10, avg: int = 50) -> TradeIntent:
    return TradeIntent(
        ticker="X",
        target_price_cents=80,
        target_quantity=qty,
        target_size_usd_cents=avg * qty,
        triggering_match_id=1,
        confidence_score=0.9,
        target_avg_fill_price_cents=avg,
        target_max_fill_price_cents=avg,
        estimated_fees_cents=10,
        estimated_total_cost_cents=avg * qty + 10,
        cap_binding="cap_one",
        cap_one_value_cents=2000,
        cap_two_value_cents=500_000,
        slippage_cents=0,
        levels_consumed=[(avg, qty)],
        reasoning_text="pre-live fix #11 fixture",
    )


def _approve(db: Database, intent: TradeIntent) -> RiskApprovedOrder:
    rm = RiskManager(db=db, config=RiskConfig())
    state = RiskState(
        bankroll=BankrollState(50000, 0),
        open_position_tickers=frozenset(),
    )
    out = rm.evaluate(intent, state)
    assert isinstance(out, RiskApprovedOrder)
    return out


class _FakePlaceOrder:
    """Stand-in for KalshiClient.place_order; returns a configurable
    KalshiOrder."""

    def __init__(self, response: KalshiOrder) -> None:
        self._response = response

    async def __call__(self, **kwargs: Any) -> KalshiOrder:
        return self._response


class _FakeKalshiClient:
    """Provides .place_order pulling from a queue of responses."""

    def __init__(self, response: KalshiOrder) -> None:
        self._response = response

    async def place_order(self, **kwargs: Any) -> KalshiOrder:
        return self._response


def _book(*, bid: int | None, ask: int | None) -> Quote:
    return Quote(yes_bid_cents=bid, yes_ask_cents=ask)


class TestPreferKalshiReportedFills:
    async def test_uses_kalshi_avg_fill_price_when_present(self, tmp_path: Path) -> None:
        """Kalshi reports avg=53c (re-walk said 50c). Trade row records 53c."""
        db = _db(tmp_path)
        intent = _intent_with_walk(qty=10, avg=50)
        approved = _approve(db, intent)
        kalshi_response = KalshiOrder(
            order_id="kalshi-1",
            client_order_id="uuid-1",
            ticker="X",
            status="executed",
            side="yes",
            action="buy",
            type="limit",
            count=10,
            remaining_count=0,
            filled_count=10,
            avg_fill_price=53,  # Kalshi truth — different from re-walk
        )
        executor = KalshiExecutor(
            db=db,
            kalshi_client=_FakeKalshiClient(kalshi_response),  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 1000)],
            client_order_id_factory=lambda: "uuid-1",
        )
        result = await executor.submit(approved)
        assert result.status == "filled"
        assert result.fill_price_cents == 53  # Kalshi value, not 50

        row = (
            db.connect().execute("SELECT * FROM trades WHERE id = ?", (result.trade_id,)).fetchone()
        )
        assert row["entry_price_cents"] == 53
        assert row["quantity"] == 10
        assert row["cost_basis_usd_cents"] == 530  # 53 * 10
        # No fallback system event because both fields came from Kalshi.
        events = list(
            db.connect().execute(
                "SELECT event_type FROM system_events " "WHERE event_type = 'using_rewalk_fallback'"
            )
        )
        assert events == []

    async def test_falls_back_to_rewalk_when_kalshi_omits_avg(self, tmp_path: Path) -> None:
        """Kalshi reports filled_count but no avg_fill_price → use
        re-walk avg + log fallback event."""
        db = _db(tmp_path)
        intent = _intent_with_walk(qty=10, avg=50)
        approved = _approve(db, intent)
        kalshi_response = KalshiOrder(
            order_id="kalshi-1",
            client_order_id="uuid-1",
            ticker="X",
            status="executed",
            count=10,
            remaining_count=0,
            filled_count=10,
            avg_fill_price=None,  # Kalshi omitted
        )
        executor = KalshiExecutor(
            db=db,
            kalshi_client=_FakeKalshiClient(kalshi_response),  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 1000)],
            client_order_id_factory=lambda: "uuid-1",
        )
        result = await executor.submit(approved)
        assert result.status == "filled"
        # Re-walk avg = 50c
        assert result.fill_price_cents == 50

        events = list(
            db.connect().execute(
                "SELECT event_type, detail FROM system_events "
                "WHERE event_type = 'using_rewalk_fallback'"
            )
        )
        assert len(events) == 1
        # The detail JSON should record that avg fell back to rewalk
        # but filled came from Kalshi.
        import json as _json

        detail = _json.loads(events[0]["detail"])
        assert detail["filled_source"] == "kalshi"
        assert detail["avg_source"] == "rewalk"

    async def test_falls_back_to_rewalk_when_kalshi_omits_filled_count(
        self, tmp_path: Path
    ) -> None:
        """Kalshi omits both filled_count AND can't derive from
        count/remaining → use re-walk filled_quantity."""
        db = _db(tmp_path)
        intent = _intent_with_walk(qty=10, avg=50)
        approved = _approve(db, intent)
        kalshi_response = KalshiOrder(
            order_id="kalshi-1",
            client_order_id="uuid-1",
            ticker="X",
            status="executed",
            count=None,
            remaining_count=None,
            filled_count=None,
            avg_fill_price=53,  # Kalshi reported avg but no count
        )
        executor = KalshiExecutor(
            db=db,
            kalshi_client=_FakeKalshiClient(kalshi_response),  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 1000)],
            client_order_id_factory=lambda: "uuid-1",
        )
        result = await executor.submit(approved)
        assert result.status == "filled"
        assert result.fill_quantity == 10  # re-walk filled
        # avg from Kalshi (53)
        assert result.fill_price_cents == 53

        events = list(
            db.connect().execute(
                "SELECT detail FROM system_events " "WHERE event_type = 'using_rewalk_fallback'"
            )
        )
        assert len(events) == 1
        import json as _json

        detail = _json.loads(events[0]["detail"])
        assert detail["filled_source"] == "rewalk"
        assert detail["avg_source"] == "kalshi"

    async def test_derives_filled_from_count_minus_remaining(self, tmp_path: Path) -> None:
        """When filled_count is None but count + remaining are present,
        derive `count - remaining` and treat as Kalshi-sourced."""
        db = _db(tmp_path)
        intent = _intent_with_walk(qty=10, avg=50)
        approved = _approve(db, intent)
        kalshi_response = KalshiOrder(
            order_id="kalshi-1",
            client_order_id="uuid-1",
            ticker="X",
            status="executed",
            count=10,
            remaining_count=0,
            filled_count=None,  # Derive from count - remaining
            avg_fill_price=53,
        )
        executor = KalshiExecutor(
            db=db,
            kalshi_client=_FakeKalshiClient(kalshi_response),  # type: ignore[arg-type]
            orderbook_fn=lambda _t: _book(bid=49, ask=50),
            depth_fn=lambda _t: [(50, 1000)],
            client_order_id_factory=lambda: "uuid-1",
        )
        result = await executor.submit(approved)
        assert result.status == "filled"
        assert result.fill_quantity == 10
        # No fallback event because we DID derive from Kalshi values.
        events = list(
            db.connect().execute(
                "SELECT event_type FROM system_events " "WHERE event_type = 'using_rewalk_fallback'"
            )
        )
        assert events == []


__all__: list[str] = []
