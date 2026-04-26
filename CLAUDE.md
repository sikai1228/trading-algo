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
- **Phase 1.5** — LLM cascade enhanced ingestion (keyword shortlist -> Haiku
  classifier -> match row).
- **Phase 2** — decision layer with human-in-the-loop. Engine -> Risk ->
  Approval -> DryRunExecutor pipeline.
- **Phase 3 Part 1** — two-cap position sizing, order-book walking
  for slippage modeling, Kalshi fee modeling, FOK semantics in
  dry-run.
- **Phase 3 Part 2 (current)** — operational features. Full Telegram
  command surface (`/status`, `/positions`, `/halt`, `/resume`,
  `/snooze`, `/why`, `/history`, `/heartbeat`, `/spend`, `/mode`).
  Scheduled messages (heartbeat, daily digest, settlement
  notifications, source-health alerts). Categorized alert system
  (critical/warning/info). LLM-based subject-alias enrichment when new
  markets are discovered. **Still dry-run only** until Phase 4
  explicitly enables live order placement.

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

**Sizing — Phase 3 Part 1 two-cap system:**

The trade size is the **lower** of two caps. Once the cap is chosen,
the dollar budget feeds an order-book walk (see "Walking the book"
below) that produces the actual quantity, average fill price,
slippage, and fees.

- **Cap one — hard fixed-dollar ceiling.** Default `$20.00`
  (`decision.position_size_hard_cap_cents = 2000`). Designed to be
  raised to $500–1000 once the strategy proves itself. Single
  config edit.
- **Cap two — 5 % of market volume.** Default 5 %
  (`decision.position_size_volume_pct = 0.05`). `markets.volume` is
  Kalshi's contract-count field; we treat one contract as $1 of
  notional, so `cap_two_cents = volume x 100 x 0.05 = volume x 5`.
  Brand-new market with no recorded volume -> cap_two evaluates to
  $0 -> trading on that ticker is effectively disabled until volume
  develops.
- **Effective cap** = `min(cap_one, cap_two)`. Engine records which
  bound on the intent (`cap_binding ∈ cap_one / cap_two / tie`).
- **Minimum trade guards.** Skip if walk fills fewer than
  `min_trade_size_contracts` (default 5) OR walk total cost is below
  `min_trade_value_cents` (default $2.00).

Bankroll still governs the **30 % total-exposure cap** across all
open positions and is referenced in reasoning text. It no longer
dictates per-trade sizing.

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
6. **Per-trade size cap** — proposed cost <= `position_size_cap_usd_cents`
   (fixed dollar, default $20.00); the manager may **adjust the
   quantity downward** to fit, then re-check. Rejects only if the cap is
   so tight even one contract doesn't fit (`size_cap_below_one_contract`).

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
- Records every decision into `telegram_approvals`. Two columns split
  the outcome from how it arrived:
  - `decision` ∈ `approved` / `rejected` / `expired`
  - `decision_source` ∈ `telegram_button` (user tapped the inline
    keyboard) / `telegram_command` (reserved for the Phase 3
    `/approve <id>` flow) / `timeout` (no response within the per-
    intent window). On a Telegram send-failure the gate currently
    records `decision="expired"` + `decision_source="timeout"` and
    surfaces the actual error via structlog at `approval_send_failed`
    — promoting that to a dedicated `send_failed` source value is a
    Phase 3 follow-up.

### Execution — `DryRunExecutor`

- Phase 2 is dry-run only. The executor does **not** call Kalshi order
  endpoints. It records simulated fills into `trades` (`status='dry_run'`).
- Position marks update every 60 s using the WS in-memory book
  (`update_position_marks`).
- On market resolution, `close_resolved` settles YES at 100 c and NO at 0 c.

---

## Phase 3 Part 1 — walking the book, fees, FOK

Phase 3 Part 1 turned the dry-run pipeline from "simulated fill at the
top-of-book ask" into "walk the actual depth, charge Kalshi fees,
submit FOK so book drift kills the trade." Same code runs in the
backtester; v1 backtest results from before this change are no longer
comparable to v2.

### Walking the book — `trumpbot/execution/slippage.py`

`walk_orderbook_for_buy(yes_ask_levels, target_dollars_cents,
max_price_cents=80, fee_calculator=...)` returns an
`OrderbookWalkResult` with `filled_quantity`, `total_cost_cents`,
`average_fill_price_cents` (banker's-rounded), `levels_consumed`
(audit trail of which prices we ate), `slippage_cents` (avg fill −
best ask), `estimated_fees_cents`, `max_price_reached_cents`. All
math integer-cents; banker's rounding for determinism (two identical
walks always produce byte-identical results).

`merge_to_yes_asks(yes_levels, no_levels)` does the standard NO-bid
inversion: a NO bid at 35 c becomes an implied YES ask at 65 c. The
walker takes the merged unified ask side.

### Kalshi fees — `trumpbot/execution/fees.py`

`calculate_entry_fee_cents(price_cents, quantity)` and
`calculate_exit_fee_cents(...)`. Formula: `ceil(0.07 x Q x P x (100 −
P) / 100)` in cents. Peaks at P = 50 c (1.75 c/contract), tapers
toward zero at the resolution extremes. The 0.07 constant is from
https://kalshi.com/docs/fees as of 2026-04-25; if Kalshi updates the
schedule, update `FEE_RATE` and the test fixtures together.

### FOK semantics — two layers of re-walk

The dry-run executor mirrors what we'll do in Phase 4 with real
Fill-or-Kill orders:

1. **Engine** walks at decision time and writes the predicted
   `target_avg_fill_price_cents` onto the `TradeIntent`.
2. **ApprovalGate** re-walks at user-approval time. If avg fill drifts
   > 5 c from the original target OR quantity drifts > 20 %, the
   approval is downgraded to "rejected" and a
   `fok_killed_book_moved` system event is logged. The user
   effectively approves "trade this signal under current rules", not
   "trade these specific contracts".
3. **DryRunExecutor.submit** re-walks again at submission time. Strict
   rule: re-walk must fill `target_quantity` at average ≤
   `target_avg_fill_price_cents`. Anything less -> kill, no row
   written, `fok_killed_book_moved` or
   `fok_killed_insufficient_liquidity` system event logged.

### Reasoning-text format

Every `TradeIntent` carries a reasoning string with this structure
(rendered into Telegram and persisted to `trades.reasoning_text`):

```
{source} (weight={w}) classified an article matching {ticker} at
confidence {c}, with interaction_occurred=true.

Current YES ask is {ask}c (max-buy ceiling 80c).

Cap analysis: cap_one=$X, cap_two=$Y (5 % of {volume} contracts of
market volume). Binding: {cap_one|cap_two|tie}, sizing target $Z.

Order-book walk for $Z: N contracts filled across L levels [N1 @ P1,
N2 @ P2, ...] at avg P_avg c (best ask: {best}, slippage: {slip}c).
Estimated Kalshi entry fees: $F.

Total expected cost: $cost (entry) + $F (fees) = $total.

If resolves YES at $1.00, gross P&L = $payoff − $total = $pnl,
ROI = R%.
```

### DB columns added by migration 005

`trades` gains `cap_binding`, `cap_one_value_cents`,
`cap_two_value_cents`, `target_avg_fill_price_cents`,
`actual_avg_fill_price_cents`, `slippage_cents`, `entry_fees_cents`,
`exit_fees_cents`, `levels_consumed_json` (JSON array of [price, qty]
pairs). All NULLABLE -> existing Phase-2 dry-run rows remain valid.

New `system_events.event_type` values:
- `fok_killed_book_moved` — gate or executor re-walk rejected
- `fok_killed_insufficient_liquidity` — executor re-walk found no depth

---

## Phase 3 Part 2 — operations: templates, commands, alerts, enrichment

### Single-source-of-truth for Telegram messages

**Every byte of text the user sees in Telegram lives in
`trumpbot/notifications/templates.py`.** Code that needs to send a
message references a template by name and passes a data dict; it does
NOT construct strings inline. The grep test in CI enforces this.

```python
from trumpbot.notifications.templates import render_template

rendered = render_template(
    "heartbeat_periodic",
    {"time_et": "14:23 ET", "open_count": 3, ...}
)
await telegram_bot.send_text(rendered.text, silent=not rendered.audible)
```

**Editing a Telegram message:**
1. Edit the relevant template in `TEMPLATE_CATALOG` in
   `trumpbot/notifications/templates.py`.
2. Run `uv run pytest tests/test_templates.py` — every template is
   rendered with sample data, so a missing field surfaces immediately.
3. Visual review: `uv run python -m scripts.preview_templates [name_substring]`
   renders any subset to stdout for copy review.
4. If you renamed a field or added a required one, update the call
   sites that supply the data dict.

**Adding a new template:**
1. Add an entry to `TEMPLATE_CATALOG` with `category` (one of:
   `heartbeat`, `digest`, `trade_proposal`, `trade_outcome`,
   `alert_critical`, `alert_warning`, `alert_info`, `command_reply`),
   `audible` (only `True` for `alert_critical_*`), and `format` string.
2. Document available fields in a comment above the template.
3. Add a unit test rendering it.
4. Update `scripts/preview_templates.py`'s `_SAMPLES` dict if you
   introduced new field names.

**UI literals** (button labels) are in the same file as
`BUTTON_APPROVE_LABEL` / `BUTTON_REJECT_LABEL` constants so the
single-source rule covers the entire user-facing surface.

### Categorized alerts — `trumpbot/notifications/alerts.py`

Three severity tiers, each with a fixed Telegram-notification policy:

- **alert_critical** — audible push (`disable_notification=False`).
  Reserved for events needing immediate attention: LLM cap exceeded,
  Kalshi feed disconnected, Anthropic 401, daemon crash, contract
  rules changed, mid-event resolution-rules change.
- **alert_warning** — silent. Things worth knowing today but not
  urgent: source down >30 min, slow DB query, risk-rejected trade,
  market with no resolution rules.
- **alert_info** — silent, no notification. Routine events: new month
  discovered, subject aliases enriched, LLM spend at 50 % of cap,
  source recovered.

`AlertDispatcher.send(template_name=..., data=..., dedup_key=None)`
renders the template, optionally dedups via `alert_dedup` (1-hour
window by default), writes a `system_events` row at the matching
severity, and sends to Telegram with the template's audibility.

### Telegram command surface

`trumpbot/notifications/commands.py` registers the following with
`TelegramApprovalBot` at startup:

- `/status` — bot state, P&L, sources, LLM spend
- `/positions` — open trades + mark-to-market
- `/why <trade_id>` — full reasoning for a specific trade
- `/history [N]` — last N closed trades (default 10)
- `/spend` — LLM spend (today / week / month) + projection
- `/mode` — current execution + approval mode
- `/halt` — pause new trade proposals (sets
  `system_state.halt_flag = 'true'`)
- `/resume` — resume new trade proposals
- `/snooze <ticker> [duration]` — silence one market (e.g.
  `/snooze X 24h`); duration accepts `24h`, `30m`, `3d`, `2h30m`
- `/unsnooze <ticker>` — resume one market
- `/heartbeat` — quick liveness check
- `/help` — full command list

Validation: messages from non-allowlisted chat IDs are silently
dropped with a warning log. Commands rate-limit at 30/min/chat
(defends against stolen-session abuse). Unknown commands reply with
the `command_reply_unknown` template.

### Halt + snooze plumbing

**`/halt`** sets `system_state.halt_flag='true'`. Both the decision
loop and the re-entry loop check `_is_halted(db)` at the start of
every cycle and early-return if halted. Stop-losses are NOT gated —
the user must still be able to approve emergency exits.

**`/snooze <ticker>`** writes a row to `snoozed_markets`. The decision
loop calls `is_market_snoozed(db, ticker)` per match and skips with a
`trade_skipped_snoozed` system event. Snoozes auto-expire when
`snoozed_until` passes; no cleanup task needed.

### Scheduled loops (in addition to the four Phase 2 decision loops)

- `heartbeat_loop` — 15 min (configurable). Sends one-line
  `heartbeat_periodic` to Telegram.
- `daily_digest_loop` — once per day at `notifications.digest_hour_utc`
  (default 12 UTC = 8 AM ET in standard time). Renders `daily_digest`.
- `settlement_notification_loop` — 5 min. Detects markets that resolved
  while we held a position, sends `trade_settled_yes` /
  `trade_settled_no`.
- `source_health_loop` — 5 min. Walks `source_status`; fires
  `alert_warning_source_down` when a source is stale >30 min,
  `alert_info_source_recovered` on recovery. Source-down alerts are
  deduped per source per hour.

### Subject-alias LLM enrichment

`trumpbot/news/alias_enrichment.py` subscribes to the
`market_discovered` event. For each fresh subject, it calls Claude
Haiku 4.5 with `trumpbot/news/prompts/alias_enrichment_v1.txt` to
generate the common ways news media refer to that person. The aliases
are union-merged with whatever the auto-extractor already pulled out
of the market title; `subjects.llm_enriched` flips to True so the
work is idempotent.

Cost guard: `LLMCostGuard` in `trumpbot/notifications/llm_cost.py`
records every call's spend in `llm_spend_log` and gates calls against
`alias_enrichment.monthly_cap_usd_cents` (default $10/month). When
cap is hit, enrichment is skipped (auto-extracted aliases survive)
and `alert_critical_llm_cap` fires once per month. A 401 from
Anthropic fires `alert_critical_anthropic_auth` (with a dedup so it
doesn't repeat).

### DB schema additions (migration 006)

- `snoozed_markets` — per-ticker `/snooze` state (FK to markets).
- `system_state` — generic key/value bag, seeded with `halt_flag='false'`.
- `source_status` — per-news-source health for the source-health loop.
- `alert_dedup` — short-window dedup of categorized-alert sends.
- `llm_spend_log` — every Anthropic API call's cost in USDCents.

### New `system_events.event_type` values

- `alert_critical_*` / `alert_warning_*` / `alert_info_*` — every alert
  send writes one of these (matches the template name for grep-ability).
- `trade_skipped_snoozed` — decision loop skipped a match because the
  ticker is snoozed.

---

## Phase 4 Part 1 — live trading executor + reconciliation

Phase 4 promotes the bot from dry-run to real Kalshi orders. The
strategy contract from Phases 2 / 3 doesn't change: same engine, same
risk gate, same approval flow. What changes is the executor at the
bottom of the pipeline — and a halo of supporting infrastructure
that exists only to keep live trading safe.

### Executor switching — `cfg.execution.mode`

The daemon picks `DryRunExecutor` or `KalshiExecutor` based on
`cfg.execution.mode`. Both implement the same `async submit` /
`update_position_marks` / `close_resolved` surface, so the loops in
`trumpbot/decision/loops.py` are oblivious to which is wired in. The
`Executor` union in `loops.py` is the type all four loops accept.

`DryRunExecutor.submit` was made `async` in Phase 4 even though it
doesn't await anything internally — so the call site in
`_approve_and_submit` is uniform. Don't add a sync overload back.

### Live executor — `trumpbot/execution/live_executor.py`

`KalshiExecutor` runs the same Phase 3 FOK gate before talking to
Kalshi (re-walk, kill if avg drift / qty drift), then:

1. Mints a UUIDv4 `client_order_id`.
2. Inserts a `trades` row with `status='pending'` and the
   `client_order_id` populated. **This insert MUST happen BEFORE the
   API call** — otherwise a network failure mid-submit leaves
   reconciliation unable to find the row. Pinned by the
   `test_pending_row_inserted_before_api_call` regression test.
3. Calls `KalshiClient.place_order(time_in_force='FOK',
   client_order_id=...)`. The Kalshi client is configured with
   `retry_on_transient=False` for `POST /portfolio/orders` so a
   transient failure surfaces immediately rather than risking a
   duplicate.
4. On success, updates the row to `status='live'` (or
   `killed_no_fill` if Kalshi reported FOK rejection).
5. On exception, categorizes via `trumpbot/kalshi/errors.py` and
   stores the appropriate terminal status:
   - `ValidationError` → `error_validation` (code bug, alert user)
   - `TransientError` → `error_transient` (reconciliation will look
     up by `client_order_id` on next restart)
   - `StateError` → `error_validation` + **HALT the bot** (insufficient
     funds / market closed / account suspended)
   - `ValidationError` containing `duplicate_client_order_id` →
     promote to `live` optimistically (the original submission
     landed; we lost only the response)

### Hardcoded human-in-the-loop

Auto-approval is **NOT** reachable through any config knob in v1.
The constant `APPROVAL_MODE = "human"` in
`trumpbot/approval/gate.py` is the only switch. The Phase 2
`cfg.approval.mode` field was deliberately removed in Phase 4 — adding
it back is forbidden, regression-tested by
`test_approval_mode_hardcoded_human` in
`tests/test_kalshi_executor.py`.

The `shadow_decisions` table (Phase 4 Part 1, migration 007) records
the orderbook snapshot at message-send-time AND at human-decision-time
for every `TRADE PROPOSAL`. The `/shadow_report` command aggregates
these into a "what would have happened if auto-approved?" summary.
This is data-only: the bot still always asks the human. The table
is the empirical foundation for the eventual auto-approve graduation
decision.

### Bankroll syncing — `trumpbot/account/bankroll_sync.py`

In live mode the daemon runs `bankroll_sync_loop` every 5 minutes.
It calls `KalshiClient.get_balance` and stores the integer-cents
balance in `system_state['bankroll_usd_cents']`. Consumers call
`get_synced_bankroll_cents()` which falls back to
`cfg.bankroll.starting_amount_usd` (cents) when the sync hasn't
written yet. Loop never zeroes the cache on failure — sizing off
slightly-stale data beats freezing trading.

### Startup reconciliation — `trumpbot/account/reconcile.py`

In live mode the daemon runs `reconcile_once` BEFORE starting the
trading loops. It cross-references local `trades` rows against
Kalshi `/orders` and `/positions`:

1. **Pending without ack** → look up by `client_order_id`. If Kalshi
   has it, promote to `live`; if not, mark `killed_no_fill`.
2. **Live without position** → tag `reconcile_orphaned`; require
   `/reconcile_resolve <trade_id>` to acknowledge.
3. **Position without trade** → insert a `live_imported` row;
   require `/reconcile_resolve` too.

If reconciliation fails to reach Kalshi, the daemon retries every
60 s and refuses to start trading until the call succeeds. A
`reconciliation_failed` audible alert fires once on failure; a
`reconciliation_drift` warning fires when drift is detected.

### Live settlement detector — `trumpbot/account/settlement_detector.py`

Runs every 5 minutes in live mode. Polls `/portfolio/settlements`
and closes any open `live` trade whose market resolved:

- `market_result == 'yes'` → 100c → `live_closed_resolved_yes`
- `market_result == 'no'`  → 0c → `live_closed_resolved_no`
- `market_result == 'void'` → entry price → `live_closed_resolved_no`

Idempotent — already-closed trades are skipped.

### Trade lifecycle states (migration 007)

The Phase 2 `trades.status` CHECK constraint was widened in
migration 007 to admit Phase 4 statuses. The `trades` table was
rebuilt (SQLite can't ALTER CHECK constraints) with FK pragma
disabled across the swap to preserve `trade_news_links` rows.
Existing data is bit-for-bit preserved.

| status | meaning |
|---|---|
| `dry_run` | Phase 2 simulated open position |
| `dry_run_closed_*` | Phase 2 simulated close (stop / resolution) |
| `pending` | Phase 4 submitted to Kalshi, no ack received yet |
| `live` | Phase 4 filled, position open |
| `live_closed_stop` | Phase 4 closed by stop-loss |
| `live_closed_resolved_yes` | Phase 4 settled YES |
| `live_closed_resolved_no` | Phase 4 settled NO |
| `killed_book_moved` | FOK kill, executor's re-walk drifted |
| `killed_no_fill` | FOK kill, Kalshi rejected (book too thin) |
| `error_validation` | Kalshi 4xx, code bug |
| `error_transient` | Kalshi 5xx / network, reconciliation pending |
| `live_imported` | reconciliation found unknown Kalshi position |
| `reconcile_orphaned` | local `live` row but Kalshi has no position |

### Idempotency via `client_order_id`

UUIDv4 we generate locally. Persisted to `trades.client_order_id`
BEFORE the Kalshi API call. Kalshi treats it as a primary key —
re-submitting the same value returns the original order. Two
columns enforce uniqueness via partial indexes (only NULL for
dry-run rows): `idx_trades_client_order_id` and
`idx_trades_kalshi_order_id`.

### Pre-live checklist — `scripts/pre_live_checklist.py`

Run before flipping `cfg.execution.mode = "live"`:

```
uv run python -m scripts.pre_live_checklist
```

Verifies: Kalshi auth, bankroll >= $50, reconciliation clean,
recent dry-run history, risk caps configured, approval mode
hardcoded. Exits 0 only on all-green.

### Audit columns added by migration 007

- `trades.client_order_id` — UUIDv4 idempotency key
- `trades.kalshi_order_id` — Kalshi server-side id
- `shadow_decisions` table — full counterfactual record per
  `TRADE PROPOSAL`. Backs `/shadow_report`.

### New `/commands` (Phase 4 Part 1)

- `/shadow_report [Nd]` — auto-approve simulation summary, default
  7-day window.
- `/reconcile_resolve <trade_id>` — acknowledge a
  `reconcile_orphaned` or `live_imported` row.

---

## Phase 4 Part 2.1 — tax tracking + exports

Every trade is potentially a taxable event. The bot captures all the
tax-relevant data on the trade row at lifecycle time so year-end
exports (CSV / Form 8949 / Kalshi 1099-B reconciliation) read from a
stable per-trade record without recomputing anything from raw data.

**Strategy choice:** capture EVERYTHING at trade time. We never try
to figure out at filing time what the operator will need; the
`TaxExporter` filters from the captured columns.

### DB schema (migration 008)

Adds 7 columns to the `trades` table:

| column | populated at | notes |
|---|---|---|
| `acquired_date` | `insert_trade` | `entered_at[:10]` |
| `disposed_date` | `close_trade` | `exited_at[:10]` |
| `holding_period_days` | `close_trade` | SQLite `julianday()` diff |
| `acquisition_cost_cents` | `insert_trade` | `cost_basis_usd_cents + entry_fees` |
| `disposal_proceeds_cents` | `close_trade` | `exit_price * qty - exit_fees` |
| `realized_gain_loss_cents` | `close_trade` | `proceeds - cost` |
| `tax_year` | `close_trade` | year of `disposed_date` |

Backfill SQL in the same migration computes these for every existing
closed Phase-2/3 trade so historical data is exportable from day one.
Open trades leave the columns NULL — meaningful only at close.

`tax_year` is the year of **disposal**, not entry. A trade entered
Dec 30, 2025 and closed Jan 5, 2026 belongs to tax year 2026 (mirrors
how the IRS treats disposal date as the gain-recognition trigger).

Indexes: `idx_trades_tax_year`, `idx_trades_disposed_date`.

### TaxExporter — `trumpbot/exports/tax_exports.py`

Pure-function exporter over the captured columns. Four methods:

- `export_yearly_summary(year) -> YearlySummary` — totals, win/loss,
  largest gain/loss with the market that produced them, average
  holding period, per-ticker breakdown.
- `export_trade_log(year, format="csv"|"json")` — full per-trade detail.
  CSV columns are LOCKED:
  ```
  trade_id, ticker, market_description, acquired_date, disposed_date,
  holding_period_days, quantity, acquisition_cost_usd,
  disposal_proceeds_usd, realized_gain_loss_usd, status,
  resolution_outcome, notes
  ```
- `export_form_8949_format(year)` — IRS Form 8949 column layout with
  `Adjustment` and `Code` left blank for the operator's accountant.
- `export_kalshi_reconciliation(year)` — line-item totals + per-trade
  detail in the shape Kalshi's 1099-B uses, suitable for filing-time
  comparison.

**Money rule (carries over from Phase 2):** storage stays in integer
cents. Conversion to dollar strings happens ONLY at the export
boundary via `_dollars_str` / `_bare_dollars` (Decimal-based, no
float drift). $17.49 always renders as $17.49, never $17.490000001.

### Telegram commands

- `/tax_summary [year]` — aggregated stats for the year (default:
  current calendar year). Renders `command_reply_tax_summary`.
- `/tax_export [year] [csv|json|form_8949]` — writes a filing-ready
  file under `<db_dir>/exports/annual/<year>/` and tells the operator
  the path. Format defaults to `csv`.
- `/tax_reconcile [year]` — writes the Kalshi 1099-B reconciliation
  JSON for line-item comparison once Kalshi issues the form.

### Monthly digest — `monthly_tax_digest_loop`

Fires on `cfg.tax_tracking.monthly_digest_day` of the month at
`cfg.tax_tracking.monthly_digest_time_et` Eastern. Computes the
previous month's stats, writes a CSV to
`<db_dir>/exports/monthly/YYYY-MM.csv`, sends the
`monthly_tax_digest` template to Telegram.

The loop is idempotent — if the daemon is down across the firing
instant, the next iteration silently skips that month (committed
CSVs in `data/exports/monthly/` cover the audit trail anyway).

Gated on `cfg.tax_tracking.monthly_digest_enabled`. Set to False to
skip the loop; the per-trade tax columns continue to be populated.

### Exit fees enter the close lifecycle

A behavioral change introduced by Part 2.1: stop-loss exits now net
the Kalshi exit fee against `disposal_proceeds_cents` so the
realized gain/loss reflects the operator's actual cash. Settlement
exits at $0 / $1 incur 0 fees by Kalshi's formula and pass
`exit_fees_cents=0` explicitly for the audit trail.

The pre-Phase-4-Part-2.1 dry-run-executor test
`test_closes_at_current_bid` was updated to import
`calculate_exit_fee_cents` and assert the fee-aware P&L.

### Annual export workflow

Once a year (typically February when Kalshi issues the 1099-B):

1. `uv run python -m scripts.generate_annual_export --year 2026`
   — writes 5 files to `<db_dir>/exports/annual/2026/`:
   `full_trade_log.csv`, `yearly_summary.json`,
   `form_8949_format.csv`, `kalshi_reconciliation.json`,
   `README.md` (explains each file).
2. `uv run python -m scripts.import_kalshi_1099 --file <pdf> --year 2026`
   — extracts totals from Kalshi's PDF (via `pypdf`, lazy-imported)
   and compares to the bot's records. Exits non-zero on
   discrepancies. Writes `1099_reconciliation.txt` in the same
   directory. `--allow-manual-paste` lets the operator type the
   numbers if PDF parsing fails.

### Config — `cfg.tax_tracking`

```yaml
tax_tracking:
  enabled: true
  user_tax_year_start: "01-01"
  default_export_format: "csv"
  monthly_digest_enabled: true
  monthly_digest_day: 1
  monthly_digest_time_et: "09:00"
```

### Templates added

- `command_reply_tax_summary`
- `command_reply_tax_export`
- `command_reply_tax_reconcile`
- `monthly_tax_digest`

### File map for Phase 4 Part 2.1

- `migrations/008_phase4_part_2_1_tax.sql` — schema + backfill
- `trumpbot/exports/__init__.py`
- `trumpbot/exports/tax_exports.py` — `TaxExporter`, dollar helpers
- `trumpbot/notifications/commands.py` — three new tax handlers
- `trumpbot/notifications/scheduled.py` — `monthly_tax_digest_loop`
- `trumpbot/notifications/templates.py` — four new templates
- `trumpbot/config.py` — `TaxTrackingConfig`
- `scripts/generate_annual_export.py`
- `scripts/import_kalshi_1099.py`
- `tests/test_phase4_tax.py` — 28 regressions

---

## Phase 4 deployment readiness

Phase 4 Part 1 + Part 2.1 are verified end-to-end. The combined
verification (`VERIFICATION_PHASE_4_FULL.md`) ran 47 checks across
the 11 spec sections; result was 47/47 PASS with zero critical
bugs. Six items are deferred to Phase 4 Part 2.2 with documented
reasoning (none block live trading).

To go live:

1. Run `uv run python -m scripts.pre_live_checklist`. All six checks
   must pass.
2. Deposit ≥ $100 on Kalshi.
3. Verify production credentials in `~/.config/trumpbot/secrets.env`.
4. Send `/heartbeat` to the bot from your phone, confirm reply.
5. Edit `cfg.execution.mode = "live"` in `config.yaml`.
6. `deploy/setup_macos.sh` to redeploy.
7. Watch `~/Library/Logs/trumpbot/stdout.log` for the
   `mode_switched_live` audible alert and `reconciliation_ok`.
8. First real Telegram approval — read carefully before tapping ✅.

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
DecisionEngine.evaluate_*  ->  RiskManager.check_intent  ->  ApprovalGate.request_approval  ->  DryRunExecutor.submit
       (pure)                  (gate, can adjust qty)         (Telegram, blocks)               (records to DB)
```

Four daemon loops drive this, all started from `daemon.py`:

- `decision_loop` — pulls unevaluated `news_market_matches`, runs the entry
  pipeline. Sleeps `decision.poll_interval_sec` between cycles.
- `stop_loss_loop` — for every open position, runs `evaluate_stop_loss` and
  the same gate -> executor pipeline if triggered.
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
6. **Subject-key normalization** — NFKD -> ASCII -> lowercase -> `[a-z]`.
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

## macOS deployment — the launchd-TCC + secrets gotcha

Two bugs we hit on first redeploy that aren't obvious from reading
the code:

1. **launchd doesn't source `~/.config/trumpbot/secrets.env`.** The
   daemon's config loader does `${TELEGRAM_BOT_TOKEN}` env-var
   substitution; without those vars set, `cfg.telegram.bot_token`
   is empty, the Telegram bot isn't constructed, and all 4 scheduled
   loops are skipped (`task_count: 8` instead of `12`). Symptom:
   bot is silent in Telegram even though the daemon process is alive.

2. **macOS TCC blocks launchd from reading `~/Desktop/`.** Since
   Mojave, launchd-spawned processes need "Full Disk Access" to
   read Desktop / Documents / Downloads. The wrapper script must
   live OUTSIDE `~/Desktop/`. Symptom: launchd logs `exit 127` and
   `/bin/zsh: can't open input file: ...`.

**Fix shipped:** `deploy/run_trumpbot.sh` is the shell wrapper that
sources `secrets.env` and execs the daemon. `deploy/setup_macos.sh`
copies the wrapper to `~/Library/Application Support/trumpbot/bin/`
(TCC-friendly), templates the plist with `$HOME`, and (re)loads the
agent. Run it on every redeploy:

```
deploy/setup_macos.sh
```

The plist's `ProgramArguments` is `/bin/zsh` + the wrapper path —
NOT the python entrypoint directly. That way launchd doesn't need
to know about uv or Desktop or env vars; the wrapper handles all
three.

All user-facing timestamps go through `zoneinfo.ZoneInfo("America/
New_York")` and render as `HH:MM ET` (auto-handles EST vs EDT).
Database storage stays UTC ISO-8601; only the display layer is ET.

---

## When picking up a new task

1. Read this file end to end.
2. Check `git status` and `git log --oneline -10`.
3. Run the four quality gates to confirm a green baseline.
4. Make changes; run gates again before committing.
5. Use small, focused commits inside one PR per phase.
