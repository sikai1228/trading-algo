# Deferred cleanup — items spotted during Phase 4 Part 2.9

Items found while doing the targeted-cleanup PR that are out of
scope for *this* PR. Each is a candidate for a future session, but
none block trading. Each entry lists what, why deferred, and the
risk profile of taking it on.

---

## Database column candidates for removal

The Phase 4 Part 2.9 cleanup PR did NOT drop any DB columns from
existing tables (other than the entire `snoozed_markets` table, which
is gone via migration 012). Dropping columns is a separate,
higher-risk operation: it requires writing a SQLite-compatible table
rebuild migration and verifying every read path doesn't reference
the column.

The columns below are candidates a future session can verify and
drop. Each was marked here after a grep across the codebase
confirmed no reads.

### `news_market_matches`

- `matched_keywords` — set by Stage 1 to the (trump_alias,
  subject_alias, interaction_term) triple that satisfied the
  pre-filter. Phase 4 Part 2.8 simplified Stage 1 to a flat boolean
  pre-filter; the keyword triple is already encoded in `match_reason`
  for failed rows and is not actionable for passed rows. The column
  is still written but no longer read by any production code path.

  Risk: low. The audit trail in `match_reason` already captures the
  same information for `failed_pre_filter:*` rows, and the LLM
  cascade record on `llm_classifications` captures the production
  signal for `passed_pre_filter` rows.

### `markets`

- `volume`, `open_interest` — written by the discovery service and
  surfaced in `/positions` and reasoning text but no longer drive
  any sizing decision (Phase 4 Part 2.6 redefined cap_two against
  the live orderbook; volume was the prior input).

  Risk: low for sizing math (already removed), medium for
  observability (the values appear in operator-facing reports).
  Suggest: keep for now, revisit when a future spec consolidates the
  reasoning-text data shape.

### `llm_classifications`

- `cost_micro_usd` — populated on every LLM call, but consumers read
  the per-call cost via `llm_spend_log` (audit trail) or
  `llm_spend_daily` (rollup), not via this column. It's denormalized
  data on the per-classification audit row.

  Risk: low. Removing requires a rebuild migration since this is in
  a young table.

---

## Dead constants / functions still wired through

### `position_size_base_pct` config field

REMOVED in this PR — see CLAUDE.md Phase 4 Part 2.9 section. Listed
here only as a worked example of "field removed but commented in
config.yaml so an un-migrated YAML still loads."

### `DecisionPhaseConfig.model_config = ConfigDict(extra="ignore")`

Phase 4 Part 2.9 switched the decision-phase config from
`extra="forbid"` to `extra="ignore"` so legacy keys (`position_size_base_pct`,
`llm_confidence_threshold`) load silently. After a future operator
pass strips both keys from deployed `~/.config/trumpbot/config.yaml`,
the schema can flip back to `extra="forbid"` to catch typos.

Risk: low. Need to verify all deployed configs are migrated first.

---

## Documentation drift

### `OPEN_ISSUES.md`

Still contains references to issues fixed in earlier PRs (snooze
plumbing, source weights). A future cleanup pass can prune
already-shipped items.

### `VERIFICATION_PHASE_4_FULL.md`, `VERIFICATION_PHASE_3_PART_2.md`

Both reference `/snooze`, `/unsnooze`, and source weights. They're
historical verification artifacts and accurate at the time of
writing, so leaving them as-is is fine; just be aware they describe
a past state of the code.

---

## How to take an item off this list

1. Verify the no-read claim with a fresh `grep` — codebase changes
   may have added a reader since this doc was written.
2. Write a migration if a column is being dropped.
3. Add a regression test asserting the field is gone (mirroring
   `tests/test_halt.py::test_snooze_repo_helpers_are_gone`).
4. Update CLAUDE.md.
5. Open a one-purpose PR; do NOT mix unrelated cleanups.
