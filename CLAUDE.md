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
- **Phase 1.5** — LLM cascade. **Stage 1**
  (`trumpbot/news/matcher.py`) is an aggressively-inclusive keyword
  pre-filter: Trump alias + subject alias + interaction term anywhere
  in the headline+body, all word-boundary case-insensitive. **Stage 2**
  (`trumpbot/news/llm_classifier.py`) calls Claude Haiku 4.5 against
  the verbatim Kalshi resolution rules and emits a structured
  `ClassificationResult`. Stage 1 always writes `confidence=0.0`; the
  LLM patches the row in place with `classifier_type='llm_cascade'`
  and the real confidence. The decision engine still requires
  `interaction_occurred=True` AND `confidence >= 0.85` — neither
  trades by itself. **Built fully in Phase 4 Part 2.8.**
- **Phase 2** — decision layer with human-in-the-loop. Engine -> Risk ->
  Approval -> DryRunExecutor pipeline.
- **Phase 3 Part 1** — two-cap position sizing, order-book walking
  for slippage modeling, Kalshi fee modeling, FOK semantics in
  dry-run.
- **Phase 3 Part 2** — operational features. Telegram command
  surface (`/status`, `/positions`, `/halt`, `/resume`, `/why`,
  `/history`, `/spend`, `/mode`). Scheduled messages (daily
  digest, settlement notifications, source-health alerts).
  Categorized alert system (critical/warning/info). LLM-based
  subject-alias enrichment when new markets are discovered.
  (Phase 4 Part 2.9 removed the per-ticker `/snooze` and
  `/unsnooze` commands; `/halt` + `/resume` are the sole operator
  override. Phase 4 Part 2.10 removed the periodic heartbeat
  notification and the `/heartbeat` command; the morning daily
  digest is the regular status notification, `/status` is on
  demand.)

---

## Phase 2 strategy rules — LOCKED

Every numeric threshold below is part of the contract. Changing one means
updating the test suite (`tests/test_decision_engine.py`,
`tests/test_risk_manager.py`) and this section together, in the same commit.

### Entry rules — `DecisionEngine.evaluate_news_match`

A news match becomes a buy intent **only if all** of:

1. `match.interaction_occurred is True` — the LLM cascade explicitly
   classified the article as proving a qualifying interaction.
   Keyword-only matches never trigger trades. **Phase 4 Part 2.9
   removed the `confidence >= 0.85` threshold gate**: the trigger is
   yes/no, no gradient. The Haiku confidence float is recorded in
   `llm_classifications.parsed_confidence` for audit and shadow
   analysis but does not drive any decision.
2. `match.is_kalshi_approved is True` (source is on the Kalshi-approved
   list — all approved sources weighted equally; one confirms)
3. No open position in this ticker (one entry per cycle)
4. The article timestamp is inside the market's open/close window
   (fail-closed if the article is undated)
5. `market_state.yes_ask_cents <= 90` (max buy price ceiling — raised
   from 80 c to 90 c in Phase 4 Part 2.5 to capture the post-news leg
   between 80-90 c)
6. Sized position is at least 1 contract

**Sizing — Phase 3 Part 1 two-cap system:**

The trade size is the **lower** of two caps. Once the cap is chosen,
the dollar budget feeds an order-book walk (see "Walking the book"
below) that produces the actual quantity, average fill price,
slippage, and fees.

- **Cap one — hard fixed-dollar ceiling.** Default `$20.00`
  (`decision.position_size_hard_cap_cents = 2000`). Designed to be
  raised to $500–1000 once the strategy proves itself. Single
  config edit.
- **Cap two — 20 % of acceptable orderbook depth.** Default 20 %
  (`decision.position_size_orderbook_pct = 0.20`). The bot looks at
  the live YES-ask side, filters to levels at prices ≤
  `max_buy_price_cents` (90 c), sums the available contracts, and
  takes 20 % of that count. The dollar value is the contracts x
  volume-weighted-average-price across the same filtered levels, so
  `cap_two_cents` is comparable to `cap_one`:

  ```
  acceptable = [(p, q) for p, q in yes_ask_levels if p <= 90]
  available = sum(q for _, q in acceptable)
  cap_two_contracts = floor(available x 0.20)
  avg_price = sum(p x q for p, q in acceptable) // available
  cap_two_cents = cap_two_contracts x avg_price
  ```

  An empty book (or every level above the price ceiling) → engine
  skips the trade. **Phase 4 Part 2.6** rationale: total volume is a
  poor proxy for current liquidity. The orderbook-depth cap directly
  targets slippage by limiting position size to what the current
  book can absorb without moving prices materially.

- **Effective cap** = `min(cap_one, cap_two)`. Engine records which
  bound on the intent (`cap_binding ∈ cap_one / cap_two / tie`) AND
  the contract-count representation (`cap_two_contracts`).
- **Minimum trade guards.** Skip if walk fills fewer than
  `min_trade_size_contracts` (default 5) OR walk total cost is below
  `min_trade_value_cents` (default $2.00). Cap_two contributes its
  own pre-walk skip: if `cap_two_contracts < min_trade_size_contracts`
  (the 20 % calculation rounds to fewer than the floor), the engine
  returns None before the walker runs.

Bankroll governs the per-trade sufficiency check (proposed cost
must fit available cash) and is referenced in reasoning text. The
old aggregate "30 % of bankroll" exposure cap was removed in
**Phase 4 Part 2.3**; aggregate exposure is now bounded by the
operator's actual Kalshi deposit.

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

### Risk-manager checks — `RiskManager.evaluate`

The risk gate is the only path that can produce a `RiskApprovedOrder`. For
buy intents (entry/reentry), it runs in this order:

1. `enabled` — if False, reject everything
2. `halted` — if True, reject everything
3. **Price ceiling** — `yes_ask_cents <= max_buy_price_cents`
4. **Bankroll** — proposed cost <= `available_usd_cents`
5. **Per-trade size cap** — proposed cost <= `position_size_hard_cap_cents`
   (fixed dollar, default $20.00); the manager may **adjust the
   quantity downward** to fit, then re-check. Rejects only if the cap is
   so tight even one contract doesn't fit (`size_cap_below_one_contract`).

The aggregate "30 % of bankroll" exposure cap that used to live
between bankroll and per-trade was removed in **Phase 4 Part 2.3**.
Aggregate exposure is now bounded by the operator's Kalshi deposit
amount; the gate only enforces per-trade limits.

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
max_price_cents=90, fee_calculator=...)` returns an
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
{source} classified an article matching {ticker} at confidence {c},
with interaction_occurred=true.

Current YES ask is {ask}c (max-buy ceiling 90c).

Cap analysis: cap_one=$X, cap_two=$Y (20 % of {available} contracts
available under 90c ceiling: {cap_two_contracts} contracts ≈ $Y at
avg {avg_price}c). Binding: {cap_one|cap_two|tie}, sizing target $Z.

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
    "daily_digest",
    {"date": "2026-04-26", "closed_count": 5, ...}
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
   `digest`, `trade_proposal`, `trade_outcome`, `alert_critical`,
   `alert_warning`, `alert_info`, `command_reply`), `audible`
   (only `True` for `alert_critical_*`), and `format` string.
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
- `/help` — full command list

(Phase 4 Part 2.9 removed `/snooze` and `/unsnooze`; `/halt` +
`/resume` are the global override the operator uses. Phase 4 Part
2.10 removed `/heartbeat`; `/status` covers on-demand liveness with
richer information.)

Validation: messages from non-allowlisted chat IDs are silently
dropped with a warning log. Commands rate-limit at 30/min/chat
(defends against stolen-session abuse). Unknown commands reply with
the `command_reply_unknown` template.

### Halt plumbing

**`/halt`** sets `system_state.halt_flag='true'`. Both the decision
loop and the re-entry loop check `_is_halted(db)` at the start of
every cycle and early-return if halted. Stop-losses are NOT gated —
the user must still be able to approve emergency exits.

### Scheduled loops (in addition to the four Phase 2 decision loops)

(Phase 4 Part 2.10 removed `heartbeat_loop`; the morning daily
digest is the regular status notification, `/status` is on demand.)

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
  *Dropped by migration 012 in Phase 4 Part 2.9 along with the
  /snooze command.*
- `system_state` — generic key/value bag, seeded with `halt_flag='false'`.
- `source_status` — per-news-source health for the source-health loop.
- `alert_dedup` — short-window dedup of categorized-alert sends.
- `llm_spend_log` — every Anthropic API call's cost in USDCents.

### New `system_events.event_type` values

- `alert_critical_*` / `alert_warning_*` / `alert_info_*` — every alert
  send writes one of these (matches the template name for grep-ability).

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

### Approval mode

Phase 4 Part 2.11 re-introduced `cfg.approval.mode` after the
`shadow_decisions` audit demonstrated stable signals. The default
in `main` MUST stay `"human"` — committing `mode: "auto"` in the
example config or the daemon-default config is a release blocker.

- `"human"` (default): every entry intent sends a Telegram message
  and waits for the user's `[APPROVE]` / `[REJECT]` response.
- `"auto"`: entry intents bypass Telegram and synthesize an
  approved decision in-memory. The gate writes a `telegram_approvals`
  audit row with `decision_source='auto_approval'` so the table is
  queryable for every approval, regardless of channel. After the
  executor finishes, the daemon sends `trade_filled_auto` (success)
  or `trade_killed_auto` (FOK / error) to the operator.

Stop-loss and re-entry intents ALWAYS require human approval
regardless of the mode setting. That invariant is enforced inside
`ApprovalGate.request_approval`, not by config — pinned by
`tests/test_approval_gate.py::TestAutoApprovalMode::test_stop_loss_in_auto_mode_still_human`
and `::test_reentry_in_auto_mode_still_human`.

Switching to auto requires:

1. Edit `~/.config/trumpbot/config.yaml`: `approval.mode: "auto"`.
2. Restart the daemon (`deploy/setup_macos.sh`).
3. The daemon emits `log.warning("AUTO-APPROVAL ENABLED ...")` in
   stdout AND fires `alert_critical_auto_approval_enabled` (audible
   Telegram) so accidental enable is highly visible.
4. The pre-live checklist (`scripts/pre_live_checklist.py`) returns
   `passed=False` when the mode is non-human; the operator must
   acknowledge before going live.

The `shadow_decisions` table (Phase 4 Part 1, migration 007)
recorded the orderbook snapshot at message-send-time AND at
human-decision-time for every `TRADE PROPOSAL`. The `/shadow_report`
command aggregates these into a "what would have happened if
auto-approved?" summary. The shadow audit was the empirical
foundation for re-introducing the config knob in 2.11.

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

## Phase 4 Part 2.2 — pre-live fixes (#1, #2, #11 from docs/OPEN_ISSUES.md)

Three open issues documented in `docs/OPEN_ISSUES.md` were resolved before
flipping to live mode:

### Fix #1 — Live bankroll piped through BankrollState

In dry-run mode the engine sized off `cfg.bankroll.starting_amount_usd`
(intentional — dry-run is "trade against this fake bankroll"). In
live mode the same code path was reading from config, NOT from the
synced Kalshi balance written by `bankroll_sync_loop`. So every
live-mode sizing decision used the static config number.

The fix:

- `BankrollState` now carries `source: BankrollSource` (one of
  `config`, `kalshi_synced`, `kalshi_fallback`) and
  `last_synced_at: datetime | None`.
- `available_usd_cents` is the canonical "what the engine can spend"
  number. `available_bankroll_usd_cents` remains as a back-compat
  alias.
- `_bankroll_state` in `trumpbot/decision/loops.py` accepts an
  `execution_mode` parameter. In live mode it consults
  `system_state['bankroll_usd_cents']` and tags the result
  `kalshi_synced`. If the cache is empty (daemon just started), it
  falls back to the config value but tags the result
  `kalshi_fallback` so the reasoning text discloses the fallback.
- The four daemon loops (decision / stop-loss / position-marking /
  re-entry) thread `execution_mode=cfg.execution.mode` through to
  `_bankroll_state`.
- The reasoning text builder includes a `Bankroll: $XX ({source}, last
  synced Nm ago); available $YY after open positions.` paragraph so
  the audit log discloses provenance.

The risk gate already used `available_*_cents` for sizing decisions;
no risk-gate behavior change.

### Fix #2 — Bankroll-sync auto-halt + auto-resume

`bankroll_sync_loop` previously logged warnings on failure but never
halted trading. A prolonged Kalshi outage would silently let the bot
keep sizing off increasingly stale data.

The fix:

- Loop tracks `consecutive_failures` across iterations.
- After `SYNC_FAILURE_HALT_THRESHOLD = 3` consecutive failures:
  sets `system_state.halt_flag = 'true'` AND
  `system_state.halt_reason = 'bankroll_sync'`. Fires
  `alert_critical_bankroll_sync_failed` (audible).
- On the next successful sync, if and only if `halt_reason ==
  'bankroll_sync'`, clears the halt and fires
  `alert_info_bankroll_sync_recovered` (silent).
- The user's manual `/halt` is **never** overridden by the auto-resume
  logic. Only the bot's own auto-halt is auto-cleared. This is
  enforced by checking `halt_reason` before clearing
  (`_clear_halt_if_ours`).

The daemon plumbs `send_text` into the loop so the alerts can fire.

### Fix #11 — Prefer Kalshi-reported fills over re-walk

`KalshiExecutor._finalize_buy_success` previously wrote the trade
row's `entry_price_cents` / `quantity` / `cost_basis_usd_cents` from
the LOCAL re-walk. If Kalshi's matching engine filled slightly
differently (different micro-second snapshot of the book), the
in-memory row briefly diverged from Kalshi's truth.

The fix:

- Both `_finalize_buy_success` and the stop-loss path (`_submit_stop_loss`'s
  finalize) now PREFER `order.avg_fill_price` and `order.filled_count`
  from Kalshi's response.
- Fall back to re-walk numbers (or `intent.position_quantity` for
  stop-loss) only when Kalshi omits a field.
- When ANY field falls back, write a `using_rewalk_fallback`
  system_event with both the Kalshi values and the local fallback
  values for audit. Operator can grep for the event type to spot
  divergence over time.
- For derived counts (`count - remaining_count` when `filled_count`
  is None), tag the source as `kalshi` (still Kalshi truth, just
  reconstructed).

### Templates added

- `alert_critical_bankroll_sync_failed` (audible)
- `alert_info_bankroll_sync_recovered` (silent)

### Tests added

`tests/test_pre_live_fixes.py` — 15 regressions:

- `TestBankrollStateSourcing` (6 tests): dry-run uses config, live
  uses cache, fallback when cache empty / corrupt, available
  excludes open positions, available clamps at zero.
- `TestBankrollStateInReasoning` (1): reasoning text mentions
  `Kalshi-synced balance` + dollar amount.
- `TestBankrollSyncAutoHalt` (4): three failures set halt, recovery
  clears halt, user's manual halt is not overridden, counter resets
  on success.
- `TestPreferKalshiReportedFills` (4): uses Kalshi avg + count when
  present, falls back to re-walk + logs system_event when Kalshi
  omits, derives from `count - remaining` when `filled_count` is
  None.

---

## Phase 4 Part 2.3 — total exposure cap removed

The risk gate previously rejected trades that would push
`open_position_cost + new_trade > 30 % of bankroll`. That check was
removed.

### Why

For a single-account operator the aggregate exposure is already
bounded by what's actually in the Kalshi account. The
bankroll-sufficiency check (`target_size_usd_cents >
available_usd_cents`) refuses any trade that wouldn't fit; the two
per-trade caps (`cap_one` hard $ + `cap_two` 20 % of acceptable
orderbook depth — see Phase 4 Part 2.6) keep individual trades
small. The aggregate "30 % of bankroll"
percentage was duplicating the protection the deposit amount
already provides — and was actively interfering with running a
basket of small parallel positions, which is the natural way the
strategy expresses itself.

The operator now manages aggregate exposure by controlling the
deposit amount on Kalshi: deposit $200 if you want at most $200 at
risk; the bot can never trade beyond that.

### What changed (vs. earlier phases)

- `RiskConfig.total_exposure_cap_pct` field removed.
- `DecisionConfig.total_exposure_cap_pct` field removed.
- `cfg.decision.total_exposure_cap_pct` removed from `config.yaml`
  + `config.example.yaml`.
- `RiskManager._evaluate_buy` no longer runs the exposure check.
  The `exposure_cap_exceeded` rejection reason is gone.
- Daemon + backtester wiring updated; both stopped passing the
  field through.
- The `risk/base.py` abstract interface docstring updated to drop
  "total-exposure cap" from the enforced-cap list.
- `scripts/preview_templates.py` sample data swapped a removed
  rejection reason for a still-valid one.

### Tests

The old `test_exposure_cap_exceeded` was replaced with two
regressions in `tests/test_risk_manager.py`:

- `test_aggregate_exposure_no_longer_capped` — the same scenario
  that used to reject (busts the old 30 % cap) now APPROVES.
- `test_multiple_positions_open_until_bankroll_exhausted` — five
  successive $20 intents against a $500 bankroll all approve, even
  though cumulative deployed cost reaches $100 (well past the old
  $150 cap).

If a future change re-introduces the aggregate cap, both
regressions fire immediately.

### What stays

Per-trade limits are unchanged:

- Cap one — hard fixed-dollar ceiling
  (`position_size_hard_cap_cents`, default $20.00)
- Cap two — 20 % of YES contracts available at prices ≤
  `max_buy_price_cents` (Phase 4 Part 2.6 — was 5 % of historical
  volume)
- Bankroll sufficiency — refuses trades that won't fit the
  available cash
- Price ceiling — refuses trades above 90 ¢ (Phase 4 Part 2.5)
- Halt + snooze + all other risk gates

---

## Phase 4 Part 2.6 — cap_two redefined as orderbook depth

Cap two used to be **5 % of historical traded volume** on the
market (`markets.volume x $1/contract x 0.05`). It's now **20 %
of YES contracts available at prices ≤ `max_buy_price_cents`**
in the LIVE orderbook.

### Why

Total volume reflects historical activity, not current liquidity.
A market with thick history but a thin live book would let the bot
place a trade that destroys the book on entry. The new semantics
directly target slippage:

- **Thin book** → cap_two tightens automatically
- **Deep book** → cap_two expands
- **Empty / above-ceiling-only book** → engine skips the trade (no
  `cap_two_zero` workaround needed)

### Computation

```python
acceptable = [(p, q) for p, q in yes_ask_levels if p <= 90 and q > 0]
available = sum(q for _, q in acceptable)
cap_two_contracts = floor(available * 0.20)
avg_price = sum(p * q for p, q in acceptable) // available
cap_two_cents = cap_two_contracts * avg_price
```

`cap_two_cents` uses the volume-weighted average of the acceptable
levels so it stays comparable to `cap_one`. The dollar number is
what the engine compares; the contract count is what the operator
sees in the trade row.

### Code changes

- `DecisionConfig.position_size_volume_pct` (0.05) →
  `position_size_orderbook_pct` (0.20).
- New helper `_compute_cap_two_pure(yes_ask_levels, max_price_cents,
  orderbook_pct)` returns `(cap_two_contracts, cap_two_value_cents)`.
  Pure function, easy to unit-test.
- `TradeIntent` and `ReentryIntent` gain `cap_two_contracts: int = 0`
  so the audit trail records both representations.
- New skip case: if `cap_two_contracts < min_trade_size_contracts`
  the engine returns None before walking the book (the spec
  scenario "20 % rounds to fewer than min_trade_size_contracts").

### Migration

Migration 009 adds `trades.cap_two_contracts INTEGER`. Pre-2.6 rows
leave it NULL — they were sized under the old volume semantics and
there's no way to reconstruct the live orderbook snapshot.

### Templates + commands

- Trade-proposal Telegram body now reads "Cap two (20% of available
  contracts under 90c)" with `({cap_two_contracts} of
  {available_contracts} contracts)`.
- `/why <trade_id>` reports `cap_two_pct = "20%"` and shows the
  contract count from `trades.cap_two_contracts` (falls back to
  `n/a (pre-2.6)` for rows missing it).

### Tests

- 4 old `cap_two` tests in `test_decision_engine.py` rewritten for
  the new semantics; 5 new edge-case tests added (`empty book`,
  `book above ceiling`, `book too thin to meet min`,
  `volume-weighted avg`, `orderbook_pct read from config`).
- New `TestComputeCapTwoPure` class with 7 unit tests pinning the
  helper's math directly: empty levels → zero, all-above-ceiling
  → zero, single-level, multi-level volume-weighted, filter, zero-
  quantity skipped, tiny-book floors-to-zero.

---

## Phase 4 Part 2.7 — source weight removed

All news sources are now treated equally. The per-source `weight`
field, the `news_events.source_weight` column, the
`MatchSnapshot.source_weight` field, the `TradeIntent /
ReentryIntent.confirmation_weight` field, and every "source X
(weight=Y)" reference in reasoning text and Telegram templates
have been removed.

### Why

The weight × confidence multiplication was a hand-tuned heuristic
that the LLM cascade obsoletes: the cascade already evaluates
each article against the market's verbatim Kalshi resolution
rules, and its confidence score (0..1) captures whether the
article actually proves the qualifying interaction. Multiplying
by a per-source weight just dampens the LLM's signal with a
config-time prior that has no live-data backing.

The 0.85 confidence gate in the engine's entry rule is now the
sole signal-strength filter.

### Code changes

- `NewsSourceConfig`: `weight` field removed; `model_config` switched
  to `extra="allow"` so existing config.yaml files with `weight: …`
  keys load (silently ignored) without forcing a redeploy step.
- `FetchedItem`: `source_weight` field removed.
- `NewsEventRow` + `insert_news_event`: column + insert SQL dropped
  the field.
- All three pollers (`RSSPoller`, `TwitterScraper`,
  `TruthSocialScraper`) stopped reading `source.weight`.
- `MatchSnapshot.source_weight` removed.
- `TradeIntent.confirmation_weight` and `ReentryIntent.confirmation_weight`
  removed (and the `confirmation_weight = source_weight × confidence`
  computation in the engine).
- `queries.NewsEvidence.source_weight` removed.
- Reasoning text dropped the "(weight=…)" parenthetical from the
  source line.
- Telegram trade-proposal template dropped "Confirmation weight: {weight}"
  from the header.
- `/why` template dropped the "(weight ...)" annotation on the
  source line.
- `config/config.example.yaml`: every `weight: 1.0` (or 0.85, 0.9,
  0.95) inline-dict entry stripped from the news-source list.

### Migration 010

`ALTER TABLE news_events DROP COLUMN source_weight`. SQLite 3.35+
supports DROP COLUMN natively (the runtime ships 3.50.4). Existing
rows lose the value silently; no read path consults it after the
migration applies.

### Tests

`source_weight=` and `confirmation_weight=` kwargs were removed
from every test fixture (`source_weight=` in MatchSnapshot
builders, `confirmation_weight=` in TradeIntent / ReentryIntent
builders, `weight=` in NewsSourceConfig fixtures). One
`test_poll_persists_items` assertion that read
`rows[0]["source_weight"]` was dropped along with the column.

### Operator-facing impact

- The `/why <trade_id>` reply no longer shows source weight.
- Trade-proposal Telegram messages no longer show "Confirmation
  weight: X". Confidence is the only signal-strength number.
- Existing config.yaml files keep working — the `weight: 1.0`
  keys are silently ignored. The example config has them
  removed; redeployments don't require config edits.

---

## Phase 4 Part 2.8 — Phase 1.5 LLM cascade end-to-end

**The big fix.** Pre-2.8 the code documented Phase 1.5 but never
implemented Stage 2: `interaction_occurred` was hardcoded `False` in
`decision/loops.py:_row_to_snapshot`, so the engine returned `None`
for every match and the trades table stayed empty. The investigation
report at `docs/investigations/2026-04-26_reuters_putin_zelensky_miss.md`
pins the missing pieces.

Phase 4 Part 2.8 ships the cascade. Two architectural changes that
together unblock all trade firing:

### Stage 1 simplified — aggressive recall, no precision logic

`trumpbot/news/matcher.py` now does ONE thing: a 3-condition
pre-filter against the headline+body, all word-boundary,
case-insensitive, anywhere in the text:

A. `"trump"` (or another alias in `TRUMP_ALIASES`)
B. At least one alias of the market's subject (loaded via the
   `subjects` table merged on top of `DEFAULT_SUBJECT_ALIASES`)
C. At least one term from
   `trumpbot/news/interaction_terms.py:INTERACTION_TERMS`

Pass -> `confidence=0.0`, `match_reason="passed_pre_filter"`. The LLM
takes it from there.
Fail -> `confidence=0.0`,
`match_reason="failed_pre_filter:no_trump+no_subject"` (etc.). LLM is
**not** called — that's the cost guard.

What was removed (all migrated to the LLM): proximity windows,
verb-class hierarchy (DIRECT/MENTION/INDIRECT/FUTURE), negation
detection, future-tense detection, tier-based confidence scoring,
the article-window check (which moved to `DecisionEngine` rule 4).

The Reuters article that motivated the investigation
("Trump says he speaks with Putin: Fox News") now correctly passes
Stage 1 (Trump + Putin + "speaks") and is handed to the LLM, which
correctly rejects it as a habitual self-claim with no specific
dated event.

### Stage 2 deployed — LLM cascade

`trumpbot/news/llm_classifier.py` (`LLMClassifier`) calls Claude
Haiku 4.5 with:

- A system prompt locking the model to strict-JSON output
- The verbatim contract rules from
  `data/contracts/kxtrumpmeet_rules.txt` (re-read on every call;
  `alert_critical_contract_rules_changed` fires once per drift)
- The user prompt template at
  `trumpbot/news/prompts/cascade_classifier_v1.txt`
- The article headline + body excerpt + subject candidate aliases

The response is parsed against `ClassificationResult` (Pydantic
v2, `extra="forbid"`):

```python
class ClassificationResult(BaseModel):
    subject: str | None
    interaction_occurred: bool
    interaction_type: Literal["in_person", "phone", "video"] | None
    tense: Literal["past", "future", "ongoing", "ambiguous"]
    negated: bool
    indirect_only: bool
    confidence: float  # 0.0..1.0
    reasoning: str
```

On success: a row goes into `llm_classifications` (the audit table),
and `news_market_matches.classifier_type` flips from `keyword_only`
to `llm_cascade` with the LLM's confidence, picked subject, and FK
to the classification row. The decision engine reads the joined view
via `_row_to_snapshot` and the gate fires when
`interaction_occurred=True` AND `confidence >= 0.85`.

On failure (timeout / parse / API error): one retry, then return
`None`; an audit row with non-NULL `error` is still written, the
match row stays `keyword_only`, no trade. On 401: raises
`AnthropicAuthError`, audit row written, daemon fires
`alert_critical_anthropic_auth`. On cap-hit: skip silently, no LLM
call, no audit row (the match row stays `keyword_only`).

### Cost guard — four-tier `CapStatus`

`trumpbot/notifications/llm_cost.py` adds:

- `CapStatus.UNDER_50` — full speed
- `CapStatus.BETWEEN_50_90` — full speed; one daily warning
  (`alert_info_llm_spend_update`)
- `CapStatus.BETWEEN_90_100` — every-other call; per-call alert
- `CapStatus.OVER_CAP` — HARD HALT; calls return `None`

The default cap was raised from $10/mo to $20/mo because the
classifier issues one call per Stage-1 pre-filter pass (~hundreds
per busy news day vs. tens per month for alias enrichment).

Every `record_spend` writes to BOTH `llm_spend_log` (per-call audit)
AND `llm_spend_daily` (the rollup the cap-status query reads). The
two are kept in lockstep.

### DB schema additions (migration 011)

- `llm_classifications` — one row per LLM call (success OR failure),
  with `parsed_*` columns matching `ClassificationResult` and
  `error` for failures.
- `news_market_matches` gains `classifier_type` (default
  `'keyword_only'`) and `llm_classification_id` (FK to
  `llm_classifications`). Both nullable; existing rows backfill
  cleanly.
- `llm_spend_daily` — denormalized day rollup (date PK + totals).
  `llm_spend_log` (migration 006) remains the per-call audit trail;
  both are written together.

### Operator workflow

1. After deploy, run `scripts/snapshot_contract.py` once to capture
   the live Kalshi rules text and write
   `data/contracts/kxtrumpmeet_rules.txt`. (The repo ships with the
   user-provided text; running the snapshotter ensures the hash
   matches Kalshi's current copy.)
2. Run `scripts/backfill_classifications.py --hours 168` to
   re-classify the last week's news_events. Cost is typically
   under $1.
3. Watch `/spend` in Telegram for daily totals.
4. If the contract text changes mid-month, re-run
   `scripts/snapshot_contract.py`; the daemon's hash-drift alert
   will fire automatically on the next call.

---

## Phase 4 Part 2.9 — targeted cleanup

Removes accumulated dead code, defensive workarounds, and unused
features after the LLM cascade landed. Pipeline behavior unchanged;
less code, fewer config fields, fewer dead paths.

### What was removed

1. **Per-ticker `/snooze` and `/unsnooze`.** `/halt` + `/resume`
   are the sole operator override now. Removed: command handlers
   in `notifications/commands.py`, repo helpers
   (`upsert_snoozed_market`, `delete_snoozed_market`,
   `is_market_snoozed`, `list_active_snoozed_markets`,
   `SnoozedMarketRow`), decision-loop checks, the
   `snoozed_markets` table (migration 012), the
   `command_reply_snooze` / `command_reply_unsnooze` templates,
   the `parse_duration` helper, the `trade_skipped_snoozed`
   system-event type, and the `snoozed_count` field on
   `command_reply_status` / `command_reply_mode`. Stop-losses
   were never gated by snooze and are not affected. Pinned by
   `tests/test_halt.py::test_snooze_repo_helpers_are_gone` and
   `test_snoozed_markets_table_is_dropped`.

2. **`llm_confidence_threshold` engine gate.** The trade trigger
   is now the LLM's `interaction_occurred` boolean alone. Yes or
   no, no gradient. The Haiku confidence float is recorded in
   `llm_classifications.parsed_confidence` for audit and shadow
   analysis but does not drive any decision. Removed from
   `DecisionConfig`, `DecisionPhaseConfig` (config field),
   `cfg.decision.llm_confidence_threshold` wiring in `daemon.py`,
   `backtester.replay._matches_in_window` (now filters on
   `parsed_interaction_occurred = 1` directly), and the
   `Confidence: {confidence}` line on `trade_proposal_entry` /
   `trade_proposal_reentry` Telegram messages. The prompt
   template at `trumpbot/news/prompts/cascade_classifier_v1.txt`
   was updated to tell the LLM "confidence is logged for audit
   but does not gate trade decisions; if the article is
   ambiguous, return `interaction_occurred=false`, do not hedge
   with low confidence." Pinned by
   `tests/test_decision_engine.py::test_low_confidence_with_interaction_true_still_produces_intent`
   and `::test_high_confidence_but_interaction_false_returns_none`.

3. **`position_size_base_pct` config field.** Dead since Phase 3
   Part 1 (the two-cap system replaced confidence-weighted
   sizing); the field was wired through but no production code
   read it. Removed from `DecisionConfig`,
   `DecisionPhaseConfig`, `daemon.py`, and `config.example.yaml`.

4. **Defensive workarounds in `_row_to_snapshot`.** The Phase
   1.5 LLM cascade is now in production (PR #22, migration 011),
   so the try/except suppressing missing `classifier_type` /
   `parsed_interaction_occurred` columns is gone. The function
   now reads them as required SQL columns; the
   `_fetch_unevaluated_matches` query always JOINs
   `llm_classifications` so they're always present.

5. **Source-weight schema tightened.** `NewsSourceConfig`
   switched from `extra="allow"` (silently ignore legacy
   `weight: 1.0` keys) to `extra="forbid"` (loudly reject them).
   Catches a partial revert before the daemon starts. The
   deployed `~/.config/trumpbot/config.yaml` was migrated
   (`weight:` keys stripped) before this change shipped. Pinned
   by `tests/test_config.py::test_legacy_weight_field_rejected`.

### What stays unchanged

Tax tracking; `/halt` and `/resume`; the LLM cascade and
classifier prompt (just the doc-string + the classifier
soft-prompt got the "confidence is logged only" note); decision
engine logic; risk manager; executor; Telegram approval flow
beyond the two field removals; settlement detection;
reconciliation; bankroll sync; backtester signature.

### Operator-facing impact

- `/snooze X 24h` and `/unsnooze X` are gone. Use `/halt` to
  pause everything; trade proposals fire again on `/resume`.
- Trade-proposal Telegram messages no longer show
  `Confidence: 0.92`. The LLM's yes/no answer is the gate; the
  number invited second-guessing of a value that doesn't drive
  anything.
- Existing `~/.config/trumpbot/config.yaml` files keep working
  if they still contain `position_size_base_pct: 0.08` or
  `llm_confidence_threshold: 0.85` — `DecisionPhaseConfig` was
  switched to `extra="ignore"` so legacy fields load silently.
  After all deployed configs are migrated, a follow-up PR can
  flip back to `extra="forbid"`.
- `weight: 1.0` keys on news sources DO now fail at config-load
  time. Strip them with `sed -E 's/, weight: [0-9.]+//g' -i
  ~/.config/trumpbot/config.yaml` before redeploying.

### Migration 012

Drops the `snoozed_markets` table and its index. Append-only;
existing rows are lost (the table was operator state, not audit
trail; nothing to preserve).

### File map for Phase 4 Part 2.9

- `migrations/012_phase4_part_2_9_drop_snoozed_markets.sql`
- `tests/test_halt.py` (renamed from `test_halt_snooze.py`,
  snooze sections removed, regression tests added)
- `docs/deferred_cleanup.md` (new — non-blocking items spotted
  during this PR)
- `trumpbot/news/sources.py` — `extra="forbid"`
- `trumpbot/notifications/commands.py` — handlers + dispatch
  entries removed
- `trumpbot/notifications/templates.py` — templates + status
  fields removed
- `trumpbot/notifications/scheduled.py` — unused import dropped
- `trumpbot/decision/loops.py` — snooze guard removed,
  `_row_to_snapshot` tightened
- `trumpbot/decision/engine.py` — confidence gate + base-pct
  field removed
- `trumpbot/db/repositories.py` — snooze helpers removed
- `trumpbot/config.py` — config-field removals + `extra="ignore"`
  on `DecisionPhaseConfig`
- `trumpbot/daemon.py` — wiring updated
- `trumpbot/backtest/replay.py` — query updated to JOIN
  `llm_classifications`
- `trumpbot/news/llm_classifier.py` — docstring updated
- `trumpbot/news/prompts/cascade_classifier_v1.txt` —
  confidence-is-logged-only note added
- `trumpbot/approval/message_templates.py` — confidence data
  field removed from entry / re-entry data dicts
- `config/config.example.yaml` — legacy fields commented out
- `scripts/preview_templates.py` — sample data refreshed

---

## Phase 4 Part 2.10 — heartbeat removed

The periodic heartbeat is gone. The morning daily digest is the
regular status notification; `/status` is on demand. Two
heartbeat layers were removed in this PR:

### What was removed

1. **`HeartbeatLogger` class in `trumpbot/daemon.py`.** Wrote a
   structured-log "heartbeat" event every 60 seconds with active
   markets, ingested news count, matcher backlog, and per-source
   poll timestamps. Added log noise without earning its keep —
   the healthcheck endpoint (`/healthz` on port 9090) is the
   machine-readable liveness probe; the daily digest covers
   anything an operator would actually look at.

2. **`heartbeat_loop` in `trumpbot/notifications/scheduled.py`.**
   Sent the `heartbeat_periodic` Telegram template every N minutes
   (default 60, aligned to wall-clock hour). Removed along with
   `_build_heartbeat_data` and `_seconds_until_next_aligned_tick`.
   The morning daily digest is the regular status notification now.

3. **`/heartbeat` Telegram command + `handle_heartbeat` handler.**
   `/status` answers the on-demand "is it alive?" question with
   richer information.

4. **`heartbeat_periodic` and `command_reply_heartbeat` templates**
   are gone from `TEMPLATE_CATALOG`. The `heartbeat` value was
   removed from the `Category` Literal in `templates.py`.

5. **`Last heartbeat: ... (heartbeat_age ago)` line** dropped
   from the `command_reply_status` template. The reply still
   shows `Daemon uptime: ...`, which is the relevant liveness
   indicator now.

6. **Config fields:**
   - `notifications.heartbeat_interval_minutes` (controlled
     `heartbeat_loop` cadence)
   - `daemon.heartbeat_interval_sec` (controlled
     `HeartbeatLogger` cadence)

   Both Pydantic sections (`NotificationsConfig`, `DaemonConfig`)
   switched to `extra="ignore"` so legacy YAMLs with the old keys
   load silently.

7. **Wording cleanup in `_ALERT_WARNING_DB_SLOW`** — "Heartbeat
   query took ..." became "Diagnostic query took ..." since the
   only writer of that "heartbeat query" was the now-removed
   `HeartbeatLogger`.

### What stays

- `daily_digest_loop` and the `daily_digest` template
- `monthly_tax_digest_loop` and `monthly_tax_digest` template
- `/status` command (the on-demand replacement for `/heartbeat`)
- `/halt`, `/resume`, settlement notifications, alert system
- The Kalshi WebSocket protocol-level ping/pong (`KalshiWS._heartbeat`)
  — that's transport-layer keepalive, not user-facing

### Operator-facing impact

- No more `✓ HH:MM | open: N | today: ...` Telegram pings.
- `/heartbeat` returns the unknown-command reply.
- `/status` reply no longer shows "Last heartbeat" / "heartbeat_age";
  it shows "Daemon uptime" instead.
- Existing `~/.config/trumpbot/config.yaml` files keep working
  whether or not they still contain `heartbeat_interval_minutes:` /
  `heartbeat_interval_sec:` — those keys are silently ignored.
- The structured stdout log loses the "heartbeat" event every 60s.
  `journalctl`/`tail -f` against the daemon's stdout is now
  quieter; use the `/healthz` HTTP endpoint or the daily digest
  for liveness indication.

### Tests

`tests/test_no_heartbeat.py` is the new regression file. Pins:

- Dispatcher returns `None` for `/heartbeat`
- `HeartbeatLogger`, `heartbeat_loop`, `_build_heartbeat_data`,
  `_seconds_until_next_aligned_tick`, `handle_heartbeat` all
  un-importable
- `heartbeat_periodic` and `command_reply_heartbeat` not in the
  catalog
- `heartbeat` not in any template's category
- `/status` rendered text contains no "Last heartbeat" / "heartbeat_age"
  line
- `/help` rendered text does not list `/heartbeat`
- `heartbeat_interval_minutes` not in `NotificationsConfig.model_fields`
- `heartbeat_interval_sec` not in `DaemonConfig.model_fields`
- A YAML carrying both legacy keys still loads (the silent-ignore
  behavior must not flip back to `extra="forbid"`)
- Source inspection on `daemon._amain` rejects any `tasks["heartbeat":]`
  / `HeartbeatLogger(` / `heartbeat.run` / `heartbeat.stop()` /
  `heartbeat_loop(` substring

### File map for Phase 4 Part 2.10

- `tests/test_no_heartbeat.py` (new — regression)
- `tests/test_halt.py`, `tests/test_commands.py`,
  `tests/test_templates.py`, `tests/test_alerts.py`,
  `tests/test_scheduled.py` — heartbeat sections deleted, /help
  + /status assertions updated
- `trumpbot/daemon.py` — `HeartbeatLogger` class deleted, task
  registration + shutdown call removed
- `trumpbot/notifications/scheduled.py` — `heartbeat_loop`,
  `_build_heartbeat_data`, `_seconds_until_next_aligned_tick`
  removed; `__all__` updated
- `trumpbot/notifications/commands.py` — `handle_heartbeat`
  removed, dispatch entry removed, `last_heartbeat` /
  `heartbeat_age` fields dropped from `/status` data dict
- `trumpbot/notifications/templates.py` — `_HEARTBEAT_PERIODIC`
  and `_COMMAND_REPLY_HEARTBEAT` templates removed; `heartbeat`
  removed from `Category` Literal; `Last heartbeat:` line dropped
  from `_COMMAND_REPLY_STATUS`; `/heartbeat` line dropped from
  `_COMMAND_REPLY_HELP`; `_ALERT_WARNING_DB_SLOW` text wording
  fixed to no longer say "Heartbeat query"
- `trumpbot/config.py` — `heartbeat_interval_minutes` removed
  from `NotificationsConfig`, `heartbeat_interval_sec` removed
  from `DaemonConfig`, both switched to `extra="ignore"`
- `config/config.example.yaml` — heartbeat fields commented out
- `scripts/preview_templates.py` — sample data refreshed

---

## Phase 4 Part 2.11 — auto-approval mode + standardized trade notifications

Two related changes that share infrastructure:

1. **`cfg.approval.mode`** is reachable as a config knob again. See
   "Approval mode" above for the contract; `"human"` (default) sends
   every entry intent to Telegram, `"auto"` skips the prompt and
   informs the user after the executor finishes. Stop-loss and
   re-entry are always human-in-the-loop.

2. **All trade-related Telegram messages share the same six
   information categories**: timestamp ET, market (ticker / subject /
   title), entry contracts + price, potential P&L (settlement +
   stop), reasoning (key quote from the article), and article link.

### Schema additions (migration 013)

- `llm_classifications.parsed_key_quote` — verbatim sentence the LLM
  extracts from the article supporting its decision. Rendered into
  the trade-proposal Telegram messages.
- `trades.triggering_article_url`, `trades.triggering_source`,
  `trades.triggering_headline`, `trades.triggering_key_quote`,
  `trades.triggering_published_ts` — article-context audit captured
  on every trade-row insert from the joined news_event +
  llm_classification.
- `telegram_approvals.decision_source` CHECK widened to admit
  `'auto_approval'`. Existing values (`telegram_button`,
  `telegram_command`, `timeout`) unchanged. SQLite couldn't ALTER
  the constraint so the table was rebuilt; existing rows preserved.

### Prompt v2

`trumpbot/news/prompts/cascade_classifier_v2.txt` adds the
`key_quote` field to the JSON schema and updates the LLM
instructions to extract a verbatim 200-char-max quote supporting
its decision. The classifier defaults switched to
`prompt_path=cascade_classifier_v2.txt`, `prompt_version="v2"`,
and `max_output_tokens=320` (was 250) to fit the new field.
`ClassificationResult.key_quote` was added with default `""` so
back-compat rows from the v1 prompt still parse.

### TradeIntent + ReentryIntent article context

Five new fields on each: `triggering_article_url`,
`triggering_source`, `triggering_headline`, `triggering_key_quote`,
`triggering_published_ts`. Defaulted to empty strings for
back-compat with synthetic test fixtures. Threaded through the
DecisionEngine (which reads them off `MatchSnapshot`, populated by
`_row_to_snapshot` joining news_events + llm_classifications) and
persisted to the new trade-row columns by both executors.

### ApprovalGate

The hardcoded `APPROVAL_MODE = "human"` constant in
`trumpbot/approval/gate.py` was REMOVED. `ApprovalGateConfig` gains
a `mode: str = "human"` field. `request_approval` checks the mode
for entry intents; for `mode="auto"` it calls `_auto_approve`,
which writes the audit row and returns immediately. Stop-loss and
re-entry intents bypass the auto branch unconditionally.

### Daemon startup

`_amain` logs `APPROVAL MODE: HUMAN` (or `AUTO`) at boot, writes a
`human_approval_enabled` / `auto_approval_enabled` system_event,
and fires `alert_critical_auto_approval_enabled` (audible Telegram)
when mode is `"auto"`. The alert is dispatched after `AlertDispatcher`
is built so it actually reaches the operator.

### Standardized templates

- `_TRADE_PROPOSAL_ENTRY` / `_TRADE_PROPOSAL_REENTRY` /
  `_TRADE_PROPOSAL_STOP_LOSS` were rebuilt to share the six-category
  layout. The shared body block (`_PROPOSAL_BODY_V2`) carries every
  field the entry + re-entry templates need.
- `_TRADE_FILLED_AUTO` — fires after a successful auto-approved
  fill. Shows actual fill price, total spent, settlement P&L, key
  quote, article link.
- `_TRADE_KILLED_AUTO` — fires after an auto-approved order is
  killed (FOK book-moved, no-fill, or executor error). Shows the
  kill reason + kind so the operator can investigate.
- `_ALERT_CRITICAL_AUTO_APPROVAL_ENABLED` — startup banner.

### Render helpers

`trumpbot/notifications/trade_render.py` is a new module of pure
helpers consumed by the message adapter and the auto-confirmation
path:

- `now_et_long` / `now_et_short` / `format_et_long` / `format_et_short`
  / `humanize_age_since` — ET timestamp formatting via
  `zoneinfo.ZoneInfo("America/New_York")`.
- `article_link_markdown` — Telegram-Markdown link with paywall
  annotation for known sources (NYT, WSJ, Bloomberg, FT, WaPo,
  Atlantic, New Yorker) and `@handle` extraction for X / Twitter.
- `render_key_quote` — strips whitespace, truncates at word
  boundary if > 200 chars.
- `compute_settlement_pnl` — integer-cents math for "if resolves
  YES at $1.00" (settlement, exit fees, net profit, ROI in basis
  points).
- `compute_potential_loss_cents` — walks the bid-side orderbook to
  estimate "approx loss if stops out at 50c drop"; falls back to a
  uniform `entry - stop_drop_cents` floor when the live book isn't
  available.
- `dollars` / `dollars_signed` / `percent_from_bps` — display
  formatters using `decimal.Decimal` (no float drift).

All functions are pure; no I/O, no DB. Tests in
`tests/test_trade_render.py` pin the math + formatting.

### `_approve_and_submit` post-execute hook

`decision/loops.py:_approve_and_submit` accepts an optional
`auto_notify: AutoNotifyFn` callable. When the approval source was
`auto_approval`, the loop calls `_send_auto_confirmation(...)`
which renders `trade_filled_auto` (on success) or
`trade_killed_auto` (on rejection) and dispatches via the notifier.
Best-effort: failures are swallowed so a missed auto-message never
blocks the next cycle.

### Pre-live checklist

`_check_approval_mode_hardcoded()` (renamed conceptually but not
function-name-wise) now reads `cfg.approval.mode` and returns
`passed=False` if it isn't `"human"`. Going live with auto requires
explicit acknowledgement.

### Test coverage (669 tests passing)

- `test_approval_gate.py::TestAutoApprovalMode` — pins the four
  branches (entry/auto, entry/human, stop-loss/auto, reentry/auto)
  and the audit-row + system_event writes.
- `test_kalshi_executor.py::test_approval_mode_defaults_to_human`
  + `::test_approval_mode_field_present_and_validated` +
  `::test_approval_mode_constant_not_re_added` — pins the default,
  the `Literal["human", "auto"]` validation, and the removal of the
  hardcoded module-level constant.
- `test_trade_render.py` — 25+ tests for the new helpers
  (timestamps, paywall annotation, key-quote truncation, P&L math,
  formatters).
- `test_templates.py::test_only_critical_alerts_are_audible` —
  added `alert_critical_auto_approval_enabled` to the audible-
  template allowlist.
- The existing `test_entry_message_contains_required_fields` was
  updated to assert the six new info categories
  (⏱ / 📍 / 💵 / 📈 / 📉 / 📰) instead of the old "BUY YES" body.

### File map for Phase 4 Part 2.11

- `migrations/013_phase4_part_2_11_auto_approval_and_article_context.sql`
- `trumpbot/news/prompts/cascade_classifier_v2.txt` (new)
- `trumpbot/news/llm_classifier.py` —
  `ClassificationResult.key_quote`; default prompt path bumped to v2
- `trumpbot/db/repositories.py` —
  `insert_llm_classification` writes `parsed_key_quote`;
  `TradeInsertRow` + `insert_trade` carry the 5 article-context
  columns; widened `decision_source` rebuild
- `trumpbot/types/intents.py` — five new fields on `TradeIntent` +
  `ReentryIntent`; `ApprovalDecision.decision_source` Literal
  widened to include `auto_approval`
- `trumpbot/decision/engine.py` — `MatchSnapshot` gains article
  fields; `evaluate_news_match` + `evaluate_reentry` thread them
  into the intent
- `trumpbot/decision/loops.py` — `_fetch_unevaluated_matches` joins
  `news_events`; `_row_to_snapshot` populates the article fields;
  new `AutoNotifyFn` type alias; `_approve_and_submit` post-execute
  hook; `_send_auto_confirmation` helper
- `trumpbot/execution/dry_run.py` + `live_executor.py` — persist
  the article-context columns on trade insert
- `trumpbot/approval/gate.py` — `APPROVAL_MODE` constant gone;
  `ApprovalGateConfig.mode`; `_auto_approve` method; auto branch in
  `request_approval`
- `trumpbot/approval/message_templates.py` — rewritten data
  adapters; consume `trade_render` helpers
- `trumpbot/notifications/trade_render.py` (new) — render helpers
- `trumpbot/notifications/templates.py` — overhauled
  `_TRADE_PROPOSAL_ENTRY` / `_REENTRY` / `_STOP_LOSS`; new
  `_TRADE_FILLED_AUTO` / `_TRADE_KILLED_AUTO` /
  `_ALERT_CRITICAL_AUTO_APPROVAL_ENABLED`
- `trumpbot/config.py` — `ApprovalPhaseConfig.mode`; bumped
  classifier defaults to v2
- `trumpbot/daemon.py` — wires `cfg.approval.mode` into the gate;
  startup logging + audible alert; `auto_notify` callable plumbed
  into `decision_loop`
- `config/config.example.yaml` — `approval.mode` documented;
  classifier defaults bumped
- `scripts/pre_live_checklist.py` — checks `cfg.approval.mode == "human"`
- `tests/test_approval_gate.py`, `tests/test_kalshi_executor.py`,
  `tests/test_templates.py`, `tests/test_trade_render.py` (new)

### Deferred (deferred_cleanup.md)

- `prior_closed_age` in re-entry template renders as `"unknown"`
  pending threading the prior-trade close timestamp onto
  `ReentryIntent`.
- `news_context` in stop-loss template renders as
  `"(no recent matches indexed)"` pending wiring the "last 6 hours
  of news_market_matches for this ticker" query into the message
  adapter (would require DB access from the adapter, which is
  currently pure).
- The end-to-end "spawn the daemon, insert a synthetic match, watch
  trade_filled_auto land within 10 s" test is deferred to a future
  integration suite — the unit tests pin the gate behavior, the
  template renders, and the helpers' math; the integration glue is
  validated manually post-deploy.

---

## Phase 4 Part 2.12 — RSS ingestion fixes + verification

Resolved findings from
`docs/investigations/rss_ingestion_analysis.md`. Five concrete
fixes plus three verification documents.

### Active news sources (post-cleanup)

The deployed source list is now 26 entries (down from 38). The
investigation found 12 sources actively harmful or non-functional;
this PR removed them.

**Direct RSS feeds (verified working, fresh content within ~2 h):**
`bloomberg`, `nyt_politics`, `nyt_world`, `wapo_politics` (renamed
from `wapo_via_gnews` and switched to the direct WaPo feed),
`wapo_world`, `axios`, `msnbc`, `nbc_politics`, `nbc_world`,
`cbs_politics`, `cbs_world`, `fox_politics`, `fox_world`,
`abc_politics`, `abc_international`, `politico_wh`,
`the_information`, `dod_news`, `pr_newswire_gov`.

**Verified social media:**
`truth_social:@realDonaldTrump` (newly fixed by the User-Agent
swap; see below). Six `twitter:*` handles remain configured but
are silently disabled until `TWITTER_BEARER_TOKEN` is set.

**Removed (with the failure mode that disqualified each):**

- `reuters_via_gnews`, `ap_via_gnews`, `wapo_via_gnews`,
  `semafor_via_gnews` — Google News proxies surfaced ~99 % stale
  content (only 1 of 117 ingested Reuters articles in 24 h was
  actually fresh; the rest were 1+ months old).
- `wsj_politics` — HTTP 403 (paywalled syndication).
- `wsj_world` — HTTP 200 but feed content frozen at 2025-01.
- `cnn_politics`, `cnn_world` — CNN abandoned RSS infrastructure;
  feeds last updated 2024-06 / 2023-09.
- `politico_picks` — HTTP 403 in the deployed environment.
- `whitehouse_press` — HTTP 404, endpoint no longer exists.
- `whitehouse_news` — Stalled feed, no fresh updates.
- `state_press`, `state_readouts`, `business_wire` — return HTTP
  200 with 0 entries; never ingested a single article.

Reuters and AP have **no working free direct RSS** (Reuters
requires paid Refinitiv subscription; AP retired public RSS in
2023). The bot does not currently ingest these sources. Their
content is referenced indirectly when other sources cite Reuters /
AP reporting. Future option: paid news API integration if 30+ days
of operational data show consistent Reuters-exclusive missed
signals.

### User-Agent swap (high-value fix)

The investigation found the previous bot User-Agent
(`"trump-market-bot/1.0 research project"`) was being rejected by
Truth Social and Politico with HTTP 403, while a real-Safari UA
returned 200 with the same request body. **Truth Social ingested
zero events in its entire deployment lifetime** because of this.

Both `trumpbot/news/rss.py` and `trumpbot/news/truthsocial.py`
switched to:

```python
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15"
)
```

Both modules' docstrings warn against reverting without
re-verifying.

### `If-Modified-Since` / `If-None-Match` conditional requests

`RSSPoller` now stashes the `Last-Modified` and `ETag` response
headers from each source's last 200, and sends them back as
`If-Modified-Since` / `If-None-Match` on the next poll. On HTTP
304 the poller skips parsing entirely. State is in-memory; first
poll after a daemon restart sends no conditional header (fine,
gets a normal 200, dedup handles repeats). Cuts real bandwidth
~70-90 % on well-behaved feeds.

Pinned by:
- `tests/test_rss_poller.py::test_conditional_request_sent_on_second_poll`
- `tests/test_rss_poller.py::test_304_response_skips_parsing`

### Freshness guard before LLM cascade

Added `STALE_ARTICLE_HOURS = 48` and `_article_is_stale()` helper
in `trumpbot/daemon.py`. `MatcherWorker._classify_and_patch` now
checks `raw_published_ts` at the top of each loop iteration; if
the article is older than 48 h or its timestamp is missing /
unparseable, the LLM call is skipped and the match row is patched
with `classifier_type='keyword_only'` + `match_reason='skipped_stale'`.

48 h is intentionally above the engine's 24 h article-window check
(rule 4 of `evaluate_news_match`) so an article that just barely
squeaks inside the engine's window still reaches Stage 2.

This is defense-in-depth — the source-list cleanup already removes
~99 % of stale-article volume by removing the Google News proxies.
The guard prevents future regression.

Pinned by `tests/test_rss_freshness_guard.py` (10 tests covering
the constant, the boundary, None / empty / unparseable inputs,
Z-suffix and TZ-offset ISO formats).

### Verification documents (read-only)

- `docs/investigations/dedup_verification.md` — confirms zero
  duplicates across 7 days of `news_events`. The deduplication
  via `URL canonicalization` + `UNIQUE(url_canonical)` index is
  working correctly. The 116 stale Reuters articles in 24 h are
  116 unique articles, not duplicates.
- `docs/investigations/pipeline_order_verification.md` — traces
  the news pipeline. Pre-fix, no freshness check existed before
  the LLM call. Measurable cost impact today: $0 (only because
  the cascade had just shipped and the daemon kept restarting).
  Post-fix: freshness guard skips stale articles before the LLM
  call.
- `docs/investigations/feed_capacity_verification.md` —
  measured rotation rates for every working source. The
  fastest-rotating feed (`bloomberg`) keeps articles in window for
  19.6 hours; at 90 s polls that's 800x margin. **No source
  rotates faster than the poll cadence.** Reducing the poll
  interval would not catch articles the current cadence misses.

### Operator workflow

After redeploy, expect:

- 26 source entries instead of 38; 12 stale-content / broken
  sources gone from the source list.
- Truth Social begins producing events for the first time
  (UA fix; check `news_events` for `source LIKE 'truth_social%'`
  rows).
- `politico_picks` may also recover (was getting 403 with bot UA;
  not in the active list anymore but if re-added it would work).
- Daemon log shows fewer `source_failure` system_events (the
  WSJ / CNN / WhiteHouse-press / business_wire sources are no
  longer being polled).
- Bandwidth meaningfully lower on bandwidth-aware hosts because
  of the conditional-request headers.

### Migrated config

The deployed `~/.config/trumpbot/config.yaml` was updated
in-place to match. A backup at `config.yaml.pre-2.12.bak` lives
beside it.

### File map for Phase 4 Part 2.12

- `config/config.example.yaml` — source list rebuilt; comments
  document each removed source's failure mode.
- `trumpbot/news/rss.py` — UA constant changed; conditional-request
  headers added to `_poll_source`; `_last_modified` and `_etag`
  in-memory dicts added to `RSSPoller`.
- `trumpbot/news/truthsocial.py` — UA constant changed; docstring
  updated with the why.
- `trumpbot/daemon.py` — `STALE_ARTICLE_HOURS` constant +
  `_article_is_stale` helper; freshness guard in
  `MatcherWorker._classify_and_patch`.
- `tests/test_rss_poller.py` — two new conditional-request tests.
- `tests/test_rss_freshness_guard.py` (new) — 10 tests pinning
  the freshness-guard helper.
- `tests/test_phase_1_5_pipeline_e2e.py` — fixture timestamps
  switched to `now_iso` so the freshness guard doesn't skip the
  LLM in synthetic e2e tests.
- `docs/investigations/dedup_verification.md` (new)
- `docs/investigations/pipeline_order_verification.md` (new)
- `docs/investigations/feed_capacity_verification.md` (new)

---

## Source-status audit follow-ups (PR #30 → PR #31, #32, #33)

The per-source status audit
(`docs/investigations/source_status_audit.md`) drove a small
sheet of follow-up PRs. None of them changed strategy logic; all
hardened ingestion and observability.

### PR #31 — Per-source User-Agent override (`the_information` 403)

The PR #29 Safari UA swap unblocked Truth Social and Politico but
regressed `the_information` (Safari → 403, Chrome / feedparser /
old bot UA → 200; verified per-UA matrix in audit Section 4a).

`NewsSourceConfig.user_agent_override: str | None = None` now
exists. When set on a source, `RSSPoller._poll_source` puts it in
the per-request `User-Agent` header, overriding the client-default
Safari UA for that one fetch. Only `the_information` uses it
(Chrome 121); the other 18 RSS sources continue with the global
Safari UA unchanged. Pinned by 4 tests in `tests/test_rss_poller.py`.

### PR #32 — Truth Social end-to-end verification (Trump-as-author)

Truth Social posts come from `@realDonaldTrump`. They are
first-person and typically don't contain "Trump" by name — Trump
is writing them. Pre-PR-#32 the Stage 1 keyword pre-filter
required the literal string "trump" anywhere in the text, so
every Trump-meeting announcement on Truth Social silently failed
Stage 1 with `failed_pre_filter:no_trump`.

**The fix:** `NewsMatcher.match()` accepts an optional
`source: str | None = None` parameter. When `source` matches one
of `TRUMP_AUTHOR_SOURCES` (currently just
`truth_social:@realDonaldTrump`), the "Trump alias appears in
body" condition is satisfied implicitly via the author. The body
must still carry a tracked subject AND an interaction term —
the rule does not waive conditions B or C.

```python
# trumpbot/news/matcher.py
TRUMP_AUTHOR_SOURCES: Final[tuple[str, ...]] = ("truth_social:@realDonaldTrump",)
TRUMP_AUTHOR_KEYWORD: Final[str] = "@realdonaldtrump (author)"
```

The author-implicit `TRUMP_AUTHOR_KEYWORD` is appended to the
match row's `matched_keywords` so a future audit can grep for
which Stage 1 passes were author-implicit vs. literal-text
matches. `MatcherWorker._process_batch` in `daemon.py` was
updated to thread `source=evt["source"]` through.

**To extend** (e.g. if Trump's X account ever returns and its
verified-handle posts qualify under the contract's
"verified social media accounts" provision): append the new
source string prefix to `TRUMP_AUTHOR_SOURCES`. Source-string
convention is `<scraper_kind>:<handle>`, e.g.
`twitter:@realDonaldTrump`.

**Why not also waive subject or interaction term for Trump
posts?** Because the contract resolves on a *qualifying
interaction event*, not on Trump posting. A Trump rant about a
tracked subject without a meeting verb (the audit's real-world
Hakeem Jeffries case — `id 1579`) should NOT trigger an LLM
call; the matcher correctly rejects those with
`failed_pre_filter:no_interaction_term`.

`docs/investigations/truth_social_verification.md` documents the
manual review of all 20 Truth Social posts ingested at audit
time. Every one is correctly handled. Pinned by 12 tests in
`TestTrumpAsAuthor` and `TestIsTrumpAuthorHelper`.

### PR #33 — Rotation-paused alert

The audit found `fox_politics` and `politico_wh` returning 200
with a stale newest-item timestamp (~7 h old at probe time);
`dod_news` worse at 52 h. The pre-existing `source_health_loop`
only alerts on absence of *ingested* events, not on absence of
*fresh feed content*, so a feed that keeps returning 200 with
the same 5 stale articles wouldn't trigger today.

(This section will be filled in when PR #33 lands.)

---

## Phase 4 deployment readiness

Phase 4 Part 1 + Part 2.1 are verified end-to-end. The combined
verification (`docs/VERIFICATION_PHASE_4_FULL.md`) ran 47 checks across
the 11 spec sections; result was 47/47 PASS with zero critical
bugs. Six items are deferred to Phase 4 Part 2.2 with documented
reasoning (none block live trading).

To go live:

1. Run `uv run python -m scripts.pre_live_checklist`. All six checks
   must pass.
2. Deposit ≥ $100 on Kalshi.
3. Verify production credentials in `~/.config/trumpbot/secrets.env`.
4. Send `/status` to the bot from your phone, confirm reply.
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
11. **All new markdown files go in `docs/`.** Only `CLAUDE.md` and
    `README.md` live at the repo root; everything else
    (`OPEN_ISSUES.md`, verification reports, bugfix notes,
    investigation reports, deferred-cleanup logs) belongs under
    `docs/`. Investigation reports go under
    `docs/investigations/`. The historical files were moved into
    `docs/` after they originally landed at root, so any older
    references in the codebase use bare filenames; treat that as a
    documentation-debt smell to fix when you touch them.

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
