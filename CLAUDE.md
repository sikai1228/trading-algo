# CLAUDE.md — engineering rules for the trumpbot repo

This file is the **living spec** for trumpbot. Every Claude session should read it
before making changes. It encodes the architectural commitments, the locked
strategy rules, and the type-safety conventions that the codebase depends on.

If a rule here ever conflicts with a later prompt, **stop and confirm** before
overwriting. These rules were chosen with the user and locked deliberately.

---

## Repo overview

Private repo `sikai1228/trading-algo`, default branch `main`. The working tree
is at `~/Desktop/Auto Trading/` (note the space). Python 3.11+, `uv` package
manager, hatchling build, mypy strict, ruff + black, pytest. Phases shipped so
far:

- **Phase 0** — bootstrap, lint/type/test scaffolding.
- **Phase 1** — read-only data collection (Kalshi REST/WS, news ingestion,
  matcher, daemon). No orders are placed.
- **Phase 1.5** — LLM cascade enhanced ingestion (keyword shortlist → Haiku
  classifier → match row).
- **Phase 2 (current)** — decision layer with human-in-the-loop. Engine →
  Risk → Approval → DryRunExecutor pipeline. **Still dry-run only** until
  the user explicitly enables live trading.

---

## Phase 2 strategy rules — LOCKED

Every numeric threshold below is part of the contract. Changing one means
updating the test suite (`tests/test_decision_engine.py`,
`tests/test_risk_manager.py`) and this section together, in the same commit.

### Entry rules — `DecisionEngine.evaluate_news_match`

A news match becomes a buy intent **only if all** of:

1. `match.confidence >= 0.85` (LLM cascade confidence threshold)
2. `match.interaction_occurred is True` (LLM explicitly classified the
   article as proving a qualifying interaction; keyword-only matches never
   trigger trades)
3. `match.is_kalshi_approved is True` (source is on the Kalshi-approved list)
4. No open position in this ticker (one entry per cycle)
5. The article timestamp is inside the market's open/close window
   (fail-closed if the article is undated)
6. `market_state.yes_ask_cents <= 80` (max buy price ceiling)
7. Sized position is at least 1 contract

**Sizing**:

- Base: `8% × confidence` of total bankroll
- Cap: `2%` during the **first 30 days** of live trading; `10%` thereafter
  (live-trading window driven by `bankroll.live_trading_started_at`; if
  None, the conservative first-30-days cap applies)
- Floor: `1%` of bankroll
- Quantity: `floor(target_size_usd_cents / yes_ask_cents)`; if the result is
  zero, the intent is dropped

### Stop-loss rules — `DecisionEngine.evaluate_stop_loss`

For each open position, every cycle:

- Trigger if `entry_price_cents - yes_bid_cents >= 50`
- Stop-loss is **never** auto-executed; it goes through the approval gate
  with **no timeout** so the user can wait as long as needed before deciding

### Re-entry rules — `DecisionEngine.evaluate_reentry`

After a position closes (stop or resolution), allow re-entering the same
market only if:

- Prior trade is in a closed status (`*_closed_stop` or `*_closed_resolved`)
- The new match is a **different** `match_id` from the one that triggered
  the prior trade (must be a fresh signal)
- All entry rules pass for the new signal
- Re-entry approvals also have **no timeout**

### Risk-manager checks — `RiskManager.check_intent`

The risk gate is the only path that can produce a `RiskApprovedOrder`. For
buy intents (entry/reentry), it runs in this order:

1. `enabled` — if False, reject everything
2. `halted` — if True, reject everything
3. **Price ceiling** — `yes_ask_cents <= max_buy_price_cents`
4. **Bankroll** — proposed cost <= `available_bankroll_usd_cents`
5. **Total exposure cap** — proposed cost + open exposure <= `30%` of bankroll
6. **Per-trade size cap** — proposed cost <= per-window cap (2% / 10%); the
   manager may **adjust the quantity downward** to fit, then re-check

For stop-loss intents, the gate confirms the position is still open and
emits a `RiskApprovedOrder` for the close.

### Approval gate — `ApprovalGate`

- Mode `human` (default): every intent is sent to Telegram and waits for the
  user to tap ✅ or ❌
- Mode `auto_approve`: dev/backtest only — never in production
- Per-intent timeout:
  - **entry**: 180 s (signal goes stale fast)
  - **stop-loss**: no timeout
  - **re-entry**: no timeout
- Records every decision into `telegram_approvals` (decision_source ∈
  `telegram_approve` / `telegram_reject` / `timeout` / `send_failed`)

### Execution — `DryRunExecutor`

- Phase 2 is dry-run only. The executor does **not** call Kalshi order
  endpoints. It records simulated fills into `trades` (`status='dry_run'`).
- Position marks update every 60 s using the WS in-memory book
  (`update_position_marks`).
- On market resolution, `close_resolved` settles YES at 100 ¢ and NO at 0 ¢.

---

## Type-system enforcement of risk gating

The single most important invariant in Phase 2: **only `RiskManager` can
produce a `RiskApprovedOrder`.** No other code path may construct one.

This is enforced at runtime by a sentinel:

```python
# trumpbot/types/intents.py
class _RiskApprovalToken:
    __slots__ = ()

RISK_APPROVAL_TOKEN: _RiskApprovalToken = _RiskApprovalToken()

class RiskApprovedOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, frozen=True)
    intent_type: Literal["entry", "reentry", "stop_loss"]
    intent: TradeIntent | ReentryIntent | StopLossIntent
    risk_decision_id: int
    risk_check_passed: Literal[True] = True
    adjusted_quantity: int | None = None
    approval_token: Annotated[_RiskApprovalToken, Field(exclude=True)]

    @field_validator("approval_token")
    @classmethod
    def _validate_token(cls, v: object) -> _RiskApprovalToken:
        if not isinstance(v, _RiskApprovalToken):
            raise TypeError(...)
        return v
```

`RISK_APPROVAL_TOKEN` is imported and passed only inside `RiskManager`. The
guard is pinned by `tests/test_risk_manager.py::TestRiskApprovedOrderConstructionGuard`.
**Do not weaken this**: any new code that needs to build a `RiskApprovedOrder`
must go through the risk manager.

### Money-math invariants

- All USD amounts are **integer cents**, typed as `USDCents` (`NewType`).
- All prices are **integer cents** (0–100), typed as `PriceCents`.
- All quantities are **integer contracts**, typed as `QuantityContracts`.
- **No `float` arithmetic on prices or USD amounts anywhere.** Confidence
  scores and percentage caps are floats; everything that hits the wallet is
  int.

### Pydantic models everywhere

All cross-module data uses Pydantic v2 models with `model_config = ConfigDict(extra="forbid", frozen=True)`. Never pass `dict[str, Any]` between modules.

---

## Human-in-the-loop pattern

Every intent flows through this pipeline (in `trumpbot/decision/loops.py`):

```
DecisionEngine.evaluate_*  →  RiskManager.check_intent  →  ApprovalGate.request_approval  →  DryRunExecutor.submit
       (pure)                  (gate, can adjust qty)         (Telegram, blocks)               (records to DB)
```

Four daemon loops drive this, all started from `daemon.py`:

- `decision_loop` — pulls unevaluated `news_market_matches`, runs the entry
  pipeline. Sleeps `decision.poll_interval_sec` between cycles.
- `stop_loss_loop` — for every open position, runs `evaluate_stop_loss` and
  the same gate → executor pipeline if triggered.
- `position_marking_loop` — every 60 s, updates `unrealized_pnl_usd_cents`
  for every open position from the WS book.
- `reentry_loop` — for every closed position, looks for a fresh match in the
  same ticker and runs the re-entry pipeline.

If Telegram isn't configured (`telegram.bot_token`/`chat_id` missing), the
daemon installs a `_StubRequester` that returns `("expired", "timeout")` for
every call. The pipeline still runs end-to-end for inspection; nothing is
ever auto-approved.

### Telegram message format — `trumpbot/approval/message_templates.py`

Three headers, one per intent type:

- `💰 TRADE PROPOSAL` — entry
- `🔄 RE-ENTRY OPPORTUNITY` — re-entry
- `⚠️ STOP-LOSS TRIGGER` — stop-loss

Each message includes the ticker, confidence, source, current orderbook,
proposed quantity & price, the engine's reasoning text, and the per-intent
timeout. The reply markup is a single inline keyboard row:
`[✅ Approve] [❌ Reject]`.

---

## Backtester — same engine, no shadow code

`trumpbot/backtest/replay.py` instantiates the production `DecisionEngine`
directly. The backtester replays historical `news_market_matches` rows
through the **same** engine class — there is no parallel implementation. The
guarantee is pinned by
`tests/test_backtester.py::test_backtester_uses_same_decision_engine_class`:

```python
from trumpbot.decision.engine import DecisionEngine as ProdEngine

bt = Backtester(db_path=..., starting_bankroll_usd=...)
assert isinstance(bt._engine, ProdEngine)
```

If you ever need to vary engine behavior in backtests (e.g. counterfactual
parameters), do it by passing a different `DecisionConfig`, **not** by
forking the engine.

CLI: `uv run python -m scripts.backtest --start 2026-04-01 --end 2026-04-30`.
Outputs a summary to stdout and a per-trade CSV to
`data/backtest_results/<ts>.csv`.

---

## Critical engineering rules (apply to every change)

1. **No `float` for money or prices.** Use `int` cents. The mypy strict
   gate will catch most slips, but review your diffs by hand.
2. **No `dict[str, Any]` across module boundaries.** Use Pydantic models.
3. **`httpx` defaults to `follow_redirects=False`.** Always pass
   `follow_redirects=True` on RSS / news fetches. (Bug fixed in PR #8 — do
   not regress.)
4. **All Kalshi REST signing must use `signed_resource_path()`** from
   `trumpbot/kalshi/auth.py` so the `/trade-api/v2` prefix is included. The
   manual API verification depends on this.
5. **One source of truth for subjects** — the matcher merges
   `DEFAULT_SUBJECT_ALIASES` with `subjects_alias_map(db)`. Discovery writes
   to the DB; never bypass.
6. **Subject-key normalization** — NFKD → ASCII → lowercase → `[a-z]`.
7. **Verb proximity is case-insensitive** — both phrases are lowercased
   before the distance check (regression fix in PR #7).
8. **Run all four gates before pushing**: `uv run black --check .` ·
   `uv run ruff check .` · `uv run mypy trumpbot/ tests/ scripts/` ·
   `uv run pytest -q`. Currently 341 tests passing across 94 source files.
9. **No live orders without an explicit user instruction.** The dry-run
   executor is the only execution path until Phase 3.
10. **Migrations are append-only**. Never edit a migration that has been
    applied to a real DB; add a new file with the next number.

---

## File map for Phase 2

- `trumpbot/types/intents.py` — `TradeIntent`, `ReentryIntent`,
  `StopLossIntent`, `RiskApprovedOrder`, NewType money aliases,
  `RISK_APPROVAL_TOKEN` sentinel
- `trumpbot/decision/engine.py` — `DecisionEngine`, `DecisionConfig`,
  `MatchSnapshot`, `MarketState`, `Position`, `BankrollState`
- `trumpbot/decision/loops.py` — the four daemon loops
- `trumpbot/risk/manager.py` — `RiskManager`, `RiskConfig`
- `trumpbot/execution/dry_run.py` — `DryRunExecutor`, `Quote`
- `trumpbot/approval/gate.py` — `ApprovalGate`, `ApprovalRequester`
- `trumpbot/approval/telegram_bot.py` — `TelegramApprovalBot`
- `trumpbot/approval/message_templates.py` — Telegram message formatter
- `trumpbot/backtest/replay.py` — `Backtester`, `BacktestResult`
- `scripts/backtest.py` — backtest CLI
- `migrations/004_phase2_trades.sql` — trades / risk_decisions /
  telegram_approvals schema
- `tests/test_decision_engine.py`, `tests/test_risk_manager.py`,
  `tests/test_dry_run_executor.py`, `tests/test_approval_gate.py`,
  `tests/test_backtester.py` — Phase 2 unit tests

---

## When picking up a new task

1. Read this file end to end.
2. Check `git status` and `git log --oneline -10`.
3. Run the four quality gates to confirm a green baseline.
4. Make changes; run gates again before committing.
5. Use small, focused commits inside one PR per phase.
