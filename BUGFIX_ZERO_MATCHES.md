# Bugfix: 528 articles → 0 matches

**Symptom (operator observation, 2026-04-25):** 24-hour observation
window ingested 528 news events across ~24 RSS sources. Zero of them
produced any non-zero confidence in `news_market_matches`. The matcher
was effectively dead.

**Outcome after fix (dry-run on the same 528 articles, same DB):**
5 non-zero matches. 2 are true positives (both about the Trump-Xi
summit). 3 are false positives of exactly the type the Phase-1.5
LLM cascade is designed to filter (verbs firing on irrelevant
subject mentions because the proximity window allows up to 200
characters between actor and verb).

## Root cause — three compounding bugs

### Bug 1 (PR #6) — discovery-side / matcher-side key mismatch

The discovery service writes `markets.subject = "vladimirputin"`
(the new normalized `subject_key` from PR #4) but the matcher's
hardcoded `DEFAULT_SUBJECT_ALIASES` is keyed by short forms like
`"putin"`. Every `(event, market)` pair went straight to the
`unknown_subject` path, confidence 0.

**Fix** (`trumpbot/daemon.py::MatcherWorker._process_batch`): build a
fresh `NewsMatcher` per batch whose alias dict merges
`DEFAULT_SUBJECT_ALIASES` with `subjects_alias_map(db)`. DB wins on
key conflict; both worlds coexist. Bridge tests pinned in
`tests/test_matcher_subjects_bridge.py`.

### Bug 2 (PR #6) — case-sensitive verb proximity

`_within_distance` did `re.escape(phrase)` without lowercasing while
the text was pre-lowercased upstream. `DEFAULT_SUBJECT_ALIASES` is
all-lowercase so it worked there. But the subjects table preserves
natural casing (`"Vladimir Putin"`, `"Putin"`) so DB-loaded aliases
never matched even after the bridge.

**Fix** (`trumpbot/news/matcher.py::_within_distance`): lowercase
both phrases. Same regex behavior, just case-insensitive.

### Bug 3 (this PR) — verb list missing common interaction verbs

The operator's manual diagnostic surfaced articles that should have
matched but did not. Tracing the matcher's reason field showed
`subject_and_trump_present_no_relevant_verb` — meaning the headline
contained both Trump and the subject but no verb the matcher
recognized as a conversation. Concrete examples:

| Headline                                                        | Verb missed   |
|-----------------------------------------------------------------|---------------|
| "Powell briefed Trump on rate decision during private meeting"  | `briefed`     |
| "Schumer dined with Trump at Mar-a-Lago last night"             | `dined with`  |
| "Trump and Tiger Woods had lunch at Mar-a-Lago yesterday"       | `had lunch`   |

The Kalshi contract explicitly lists "Working dinners, lunches, or
other meal meetings" as qualifying interactions, but the matcher's
`DIRECT_VERBS` tuple was missing every meal verb.

**Fix** (`trumpbot/news/matcher.py::DIRECT_VERBS`): added `briefed`,
`briefing`, `briefing with`, `dined`, `dined with`, `dinner`,
`dinner with`, `had dinner`, `had dinner with`, `lunch`, `lunch with`,
`lunched`, `lunched with`, `had lunch`, `had lunch with`, `breakfast`,
`breakfast with`, `had breakfast`, `had breakfast with`. The bare forms
(`lunch`, `dinner` without `with`) are anchored by the proximity check
so they don't fire on generic restaurant articles.

### Bug 4 (this PR) — matcher worker excluded settled markets

`MatcherWorker._process_batch` queried `list_active_markets(db)` —
which filters `WHERE status = 'active'`. As of the diagnostic snapshot
the Netanyahu market had already moved to `status='finalized'`, so
the matcher literally couldn't see it. Articles like "Trump called
Netanyahu" returned 0 not because the matcher logic failed but
because the candidate set was empty for that subject.

**Fix**: added `list_markets_for_matching(db)` (no status filter; all
markets with a non-null subject). The matcher worker now uses it.
The Phase-2 decision engine will narrow back to `status='active'`
before any trading — but for the observation period, we want to know
whether the matcher *would have* produced a signal for already-
resolved markets. That's how matcher quality gets calibrated.

## Regression tests

`tests/test_zero_matches_regression.py` (22 tests) pins:

- The five operator-specified case headlines all score >= 0.7.
- A `parametrize` guard that asserts every contract-relevant verb
  (`briefed`, `dined`, `lunch with`, `had dinner`, etc.) remains in
  `DIRECT_VERBS` — adding a verb to support a real headline must not
  silently get reverted.
- `list_markets_for_matching` returns markets with `status` in
  `{active, finalized, settled}`.
- An end-to-end test that seeds a `status='finalized'` market and
  confirms the matcher worker still scores 1.0 on a "Trump called
  Netanyahu" headline against it.

Plus the pre-existing 6 tests in `tests/test_matcher_subjects_bridge.py`
guarding bugs 1 and 2.

## Backfill

`scripts/reprocess_matches.py` re-runs the matcher against existing
`news_events` rows so a real-money observation period is not poisoned
by stale pre-fix `news_market_matches` rows.

```bash
# Dry-run first to see what changes
uv run python scripts/reprocess_matches.py --dry-run --hours 24

# Apply
uv run python scripts/reprocess_matches.py --hours 24
```

The script:
1. Selects `news_events` from the last N hours.
2. Deletes their existing `news_market_matches` rows.
3. Re-runs the (post-fix) matcher against the current `markets` and
   `subjects` snapshot.
4. Inserts fresh match rows.

Idempotent: re-running produces the same outcome.

## Acceptance check on real data (post-fix dry-run)

```
events          : 528
matches_deleted : 8976   (528 articles × ~17 active markets pre-fix)
matches_written : 11616  (528 articles × 22 markets post-fix)
nonzero_matches : 5
```

Of the 5 nonzero matches:

- 2 are clean true positives (both Trump-Xi summit articles).
- 3 are false positives caused by the matcher's permissive 200-char
  proximity window (e.g., "GOP called Howard Lutnick" near "Cornyn"
  in the body fired the `called` verb). These are exactly what the
  Phase-1.5 LLM cascade is designed to filter.

Per the Phase-1.5 brief: "Stage 1 must be conservative about when to
skip the LLM. False negatives (real signals filtered out) are worse
than false positives (LLM called on non-events). LLM costs are tiny;
missed signals cost real money." We accept the false-positive rate;
Stage 2 will catch them.

## Operator next steps

1. Pull this branch.
2. Re-run the smoke test — `matched (>0)` should jump from 0 to a
   small but nonzero number depending on what was happening in the
   news.
3. Run `scripts/reprocess_matches.py --hours 24` to backfill
   existing rows.
4. Continue with Phase 1.5 (currently parked on the
   `phase-1.5-llm-cascade` branch) — the LLM cascade will filter the
   remaining false positives.
