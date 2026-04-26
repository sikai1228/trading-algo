# Open issues across Phases 1 → 4 Part 2.1

Status as of `main` after PR #15. Bugs that have been fixed are NOT
listed here — only items that remain open. Each entry has a severity,
the file/function involved, and a one-line action note.

---

## Open DEFERs (documented but not fixed)

### 1. Live bankroll cache not piped into `BankrollState`

- **Severity:** **High** — recommended to fix before live deployment.
- **Where:** `trumpbot/decision/loops.py` → `_bankroll_state(...)`
- **Symptom:** `bankroll_sync_loop` updates
  `system_state['bankroll_usd_cents']` from Kalshi every 5 min, but
  `_bankroll_state` still returns
  `BankrollState(bankroll_usd_cents = int(round(starting_amount_usd * 100)))`
  using `cfg.bankroll.starting_amount_usd`. So in live mode every
  sizing decision uses the static config value, not the actual
  Kalshi balance.
- **Fix sketch:** in `_bankroll_state`, when
  `cfg.execution.mode == "live"`, call
  `get_synced_bankroll_cents(db, fallback_starting_amount_usd=...)`.
  Thread the mode (or just the cents value) through the loop
  signatures.

### 2. No halt after consecutive bankroll-sync failures

- **Severity:** Medium.
- **Where:** `trumpbot/account/bankroll_sync.py`
- **Symptom:** Spec called for halting trades after 3 consecutive
  sync failures. Current loop logs a warning + `bankroll_sync_failed`
  system_event each time but never zeroes the cache or halts. If
  Kalshi `/portfolio/balance` is down for hours, the bot keeps
  trading off the last known good number.
- **Fix sketch:** track consecutive failure count in module state;
  on the 3rd, write `system_state.halt_flag='true'` + send
  `alert_critical_*` (need a new template).
- **Documented as intentional design** (CLAUDE.md "better stale than
  frozen") — revisit after live experience.

### 3. `pre_live_checklist.py` lacks Telegram round-trip check

- **Severity:** Low.
- **Where:** `scripts/pre_live_checklist.py`
- **Symptom:** Spec listed Telegram round-trip as one of six checks;
  shipped with five (auth, bankroll, reconciliation, dry-run history,
  risk caps) plus the hardcoded approval-mode check. Async-loop
  dependency made the Telegram check awkward to add.
- **Fix sketch:** wire an `asyncio.run(...)` block that sends
  `/heartbeat` synthetically and waits for the bot's reply.
- **Mitigation today:** runbook step "send `/heartbeat` from your
  phone, confirm reply" before flipping to live.

### 4. `import_kalshi_1099.py` discrepancies don't auto-Telegram

- **Severity:** Low.
- **Where:** `scripts/import_kalshi_1099.py`
- **Symptom:** Script writes `1099_reconciliation.txt` and exits
  non-zero on $1+ discrepancies, but the operator has to read the
  file. No Telegram alert fires.
- **Fix sketch:** post-write, if `discrepancies` is non-empty, fire
  an `alert_warning_1099_discrepancy` template (need to add it).
- **Acceptable for first filing** — operator runs the script
  interactively.

### 5. Monthly tax digest never live-fired

- **Severity:** Low.
- **Where:** `trumpbot/notifications/scheduled.py:monthly_tax_digest_loop`
- **Symptom:** Calendar-month-boundary timing means a real fire
  requires waiting ~30 days. Helpers (`_seconds_until_next_monthly_tick`,
  `_previous_month_bounds`) and template render are unit-tested,
  and the daemon registers the task at startup, but the full
  fire-and-write path has only been verified by simulation.
- **Fix sketch:** add an integration test that monkey-patches
  `datetime.now(UTC)` to advance the clock past a month boundary
  inside a 1-second sleep loop.
- **Acceptable** — first real fire will surface any issue.

### 6. Form 8949 import to TurboTax untested

- **Severity:** Low.
- **Where:** `trumpbot/exports/tax_exports.py:export_form_8949_format`
- **Symptom:** Column names match the official IRS spec exactly,
  but actual import into TurboTax / FreeTaxUSA / TaxSlayer hasn't
  been tested. Requires a real license + filing data.
- **Fix sketch:** N/A — verified at first real filing.

---

## Latent issues / sharp edges

Not currently broken, but worth knowing about.

### 7. `_send_text(text, silent)` uses positional bool

- **Severity:** Low (latent).
- **Where:** `trumpbot/daemon.py` and the loops in
  `trumpbot/notifications/scheduled.py`.
- **Concern:** A bool positional argument is easy to flip
  accidentally — a caller writing
  `await send_text(rendered.text, False)` thinking "silent=False"
  vs "audible=False" could swap the meaning. Current call sites
  are correct.
- **Fix sketch:** make `silent` keyword-only (`*, silent: bool`).

### 8. `_insert_imported_position` toggles FK pragma at connection level

- **Severity:** Low (latent).
- **Where:** `trumpbot/account/reconcile.py:_insert_imported_position`
- **Concern:** Disables `PRAGMA foreign_keys` on the singleton
  `db.connect()` connection for one INSERT, then re-enables. If two
  reconciliation passes overlap (they don't today — daemon runs
  reconcile once at startup), the second pass could see FKs off.
- **Fix sketch:** create a separate sqlite3.Connection just for the
  imported-position insert.

### 9. Settlement detector relies on status-as-idempotency-key

- **Severity:** Low (latent).
- **Where:** `trumpbot/account/settlement_detector.py:detect_and_close_settlements`
- **Concern:** The detector skips already-closed trades by checking
  `if row["status"] != "live"`. If Kalshi reports the same
  settlement in two consecutive 5-minute polls AND the row hadn't
  closed yet, the second pass would re-close it (the first close
  flips status, so the second IS skipped). Relies on the lifecycle
  status semantics holding.
- **Fix sketch:** key idempotency on a dedup table or on
  `disposed_date IS NOT NULL`.

### 10. `holding_period_days` returns 0 for same-day round trips

- **Severity:** Low (latent — irrelevant today).
- **Where:** `trumpbot/db/repositories.py:close_trade` (and
  migration 008's backfill formula).
- **Concern:** SQLite `julianday(disposed) - julianday(acquired)` on
  date-only strings yields integer days. A trade entered and exited
  on the same calendar day records `holding_period_days = 0`. IRS
  treats same-day as "1 day held" for short-term gains. No impact
  on Kalshi markets (resolve over hours-to-weeks). A high-frequency
  strategy would care.
- **Fix sketch:** clamp to `MAX(1, julianday_diff)` in `close_trade`
  + migration 008 backfill.

### 11. KalshiExecutor records re-walk numbers, not Kalshi-reported fill

- **Severity:** Medium (latent).
- **Where:** `trumpbot/execution/live_executor.py:_finalize_buy_success`
- **Concern:** After Kalshi acks the order, the trade row's
  `entry_price_cents` / `quantity` / `cost_basis_usd_cents` come
  from the local re-walk, NOT from Kalshi's reported fill. If
  Kalshi's matching engine fills slightly differently from our
  reproduction (different micro-second snapshot), the in-memory row
  is briefly inconsistent. Reconciliation corrects this on next
  startup (it pulls Kalshi's actual numbers via `get_order`), but
  there's a window where `/positions` would show our re-walk
  numbers, not Kalshi's truth.
- **Fix sketch:** prefer `order.avg_fill_price` and
  `order.filled_count` over re-walk numbers in
  `_finalize_buy_success` when both are present. Fall back to
  re-walk only when Kalshi omits them.

### 12. `LLMCostGuard` cap check is racy under concurrency

- **Severity:** Very low (latent).
- **Where:** `trumpbot/notifications/llm_cost.py`
- **Concern:** Each call queries the cap, decides, then issues the
  Anthropic request. Two concurrent enrichments can both pass the
  check and push monthly spend ~1 call's worth over the cap.
  Current cadence makes this practically unreachable.
- **Fix sketch:** acquire a row lock or use SQLite `INSERT ... RETURNING`
  + post-check rollback.

### 13. `RISK_APPROVAL_TOKEN` is a module-level singleton

- **Severity:** Very low (architectural defense-in-depth).
- **Where:** `trumpbot/types/intents.py`
- **Concern:** The token is importable from anywhere; the runtime
  `isinstance(_RiskApprovalToken)` check protects against
  *accidental* `RiskApprovedOrder` construction but not deliberate
  bypass. The risk gate is nonetheless the only sanctioned producer.
- **Fix sketch:** None practical without breaking the import graph.
  Documented design choice.

### 14. FOK re-walk tolerance is hardcoded

- **Severity:** Low (latent).
- **Where:** `trumpbot/approval/gate.py:FOK_AVG_DRIFT_TOLERANCE_CENTS = 5`
  and `FOK_QTY_DRIFT_TOLERANCE_PCT = 0.20`.
- **Concern:** Will need tuning once we have live-trade data. Right
  now the constants are baked into the module.
- **Fix sketch:** move to `cfg.approval.fok_*_tolerance` after a
  few weeks of live-mode signal observation.

### 15. `snoozed_markets` table grows unboundedly slowly

- **Severity:** Cosmetic.
- **Where:** `trumpbot/db/repositories.py` snooze helpers.
- **Concern:** Snoozes auto-expire on `snoozed_until` but no cleanup
  task removes stale rows. Table grows by one row per `/snooze` call.
- **Fix sketch:** add a daily cleanup task or `WHERE snoozed_until > now`
  to `list_active_snoozed_markets` (already done — so this is purely
  a row-count concern).

---

## Recommended order

Before flipping `cfg.execution.mode = "live"`:

1. **Fix issue #1 (live bankroll → BankrollState)** — single most
   impactful change. Without it, every live-mode sizing decision
   uses stale config rather than the synced Kalshi balance.

Optional but defensive:

2. **Fix issue #2 (halt after 3 consecutive sync failures)** — guards
   against a long Kalshi outage allowing trading off stale balance.
3. **Fix issue #11 (prefer Kalshi-reported fills over re-walk)** — gets
   `/positions` to truth-status faster after each fill.

Acceptable to defer:

- Issues #3, #4, #5, #6 — low-impact, mitigated by manual runbook
  steps or first-real-use observation.
- Issues #7–#15 — latent / cosmetic; address opportunistically.
