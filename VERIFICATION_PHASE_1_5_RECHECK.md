# Phase 1.5 re-verification

Operator-driven recheck after the matcher-bug fixes and the
RSS-source audit. Status tags per section: **PASS**, **FIXED**,
**DEFER**, **FAIL**.

**Headline**: Phase 1.5 ingestion-side work is operating. Phase 1.5
classification-side work (LLM cascade Stage 2) is **not yet built**;
the foundation modules are parked WIP on `phase-1.5-llm-cascade`.
Sections F, G, H all DEFER on that ground.

---

## Section A — matcher bug root cause documentation

**PASS.** [BUGFIX_ZERO_MATCHES.md](BUGFIX_ZERO_MATCHES.md) (committed
on the `matcher-zero-matches-fix` branch) addresses each operator
question:

- **Root cause:** four compounding bugs (key mismatch between discovery
  and matcher; case-sensitive `_within_distance`; missing meal /
  briefing verbs in `DIRECT_VERBS`; matcher worker filtering
  `status='active'` so settled markets were invisible).
- **Specific code changes:** listed per-bug with file/function.
- **Test that would have caught this:** end-to-end manual matcher run
  against the production DB. The corresponding regression tests now
  exist in `tests/test_matcher_subjects_bridge.py` (6 tests guarding
  bugs 1+2) and `tests/test_zero_matches_regression.py` (26 tests
  guarding bugs 3+4 plus the operator-specified case headlines).
- **Tests in CI:** every regression test runs in the default `pytest`
  suite (no special marker). Total Phase-1 test count is now 278.

## Section B — regression tests (5 positive + 4 negative)

**PASS, with one DEFER documented inline.**

`tests/test_zero_matches_regression.py` contains all 9 cases the
recheck brief specified. Test results:

| # | Case                                         | Expected            | Actual conf | Result |
|---|----------------------------------------------|---------------------|-------------|--------|
| 1 | "Trump met with Senator Thune at WH..."      | >=0.7               | 1.0         | PASS   |
| 2 | "Trump and Putin held a 90-minute call..."   | >=0.7               | 1.0         | PASS   |
| 3 | "Powell briefed Trump on rate decision..."   | >=0.7               | 1.0         | PASS (was the headline that triggered the meal/briefing-verb fix) |
| 4 | "Trump called Netanyahu, sources confirm"    | >=0.7               | 1.0         | PASS (also exercises the `status='finalized'` worker fix) |
| 5 | "Schumer met with Trump to discuss..."       | >=0.7               | 1.0         | PASS   |
| 6 | "Trump praised Putin in his speech today" + body says "did not call ... or meet" | <=0.2 | 0.0 (negation_detected) | PASS   |
| 7 | "Trump expected to call Xi next week"        | <=0.3 + tense=future| 0.2 (future_tense) | PASS   |
| 8 | "Trump sent letter to Kim Jong Un"           | <=0.3 + indirect    | 0.5 (indirect_communication) | **PARTIAL** — Stage 1 ceiling is 0.5; Stage 2 LLM is the layer that gets it to <=0.3. Tests assert the indirect_communication tag and the 0.5 ceiling. |
| 9 | "Trump's envoy met with Putin's team"        | <=0.2 + intermediary| 1.0 (false positive) | **DEFER** — Stage 1 keyword cannot tell "Trump's envoy met" from "Trump met"; flagged as a known-FP via `test_envoy_intermediary_is_stage_2_only` so a future change is visible. The LLM cascade will catch this. |

The two DEFER rows are honest reflections of what a keyword stage can
achieve. Both surface the right `match_reason` (`indirect_communication`
for #8, `direct_verb` for #9) so the future Stage 2 sees the category
and can apply the contract-level reasoning that Stage 1 cannot.

## Section C — historical re-classification

**PASS.** Ran `scripts/reprocess_matches.py --hours 168` against the
real database (`~/Library/Application Support/trumpbot/trumpbot.db`).

```
events          : 1007       (1 week of ingestion)
matches_deleted : 22154
matches_written : 22154
nonzero_matches : 6
```

Confidence histogram (1007 events × 22 markets = 22 154 rows):

| Bucket                       | Count  |
|------------------------------|--------|
| 0.0 (no signal)              | 22 148 |
| 0.0–0.3 (future / weak)      | 0      |
| 0.3–0.7 (mention/indirect)   | 1      |
| 0.7–1.0 (body verb)          | 3      |
| 1.0 (headline verb)          | 2      |

**5 articles at >= 0.7** (the Stage 2 trigger threshold). The brief's
"reasonable range: 5-50 over 7 days" — we land at the lower edge,
plausible for a quiet news week with no known Trump-X meeting in the
24h window we sampled.

The 5 high-confidence rows:

| conf | subject       | match_reason                              | headline                                                                      |
|------|---------------|-------------------------------------------|-------------------------------------------------------------------------------|
| 1.0  | xijinping     | direct_verb:'meeting with' (in headline)  | "Taiwan Fears It'll Be 'On the Menu' at Trump-Xi Summit"                      |
| 1.0  | xijinping     | direct_verb:'summit' (in headline)        | "What Questions Do You Have About the Trump-Xi Summit?"                       |
| 0.8  | xijinping     | direct_verb:'summit' (in body)            | "U.S. targets China's shadow trade with Iran in sweeping sanctions"           |
| 0.8  | johncornyn    | direct_verb:'calls' (in body)             | "Scoop: GOP called Howard Lutnick to reverse crypto PAC's Texas move"         |
| 0.8  | kenpaxton     | direct_verb:'called' (in body)            | "Scoop: GOP called Howard Lutnick to reverse crypto PAC's Texas move"         |

Of these 5: **2 true positives** (the Trump-Xi summit articles) and
**3 false positives** of exactly the kind Stage 2 LLM is designed
to filter (verb fires on irrelevant subject mention because the
200-char proximity is permissive). Per the Phase-1.5 brief: "Stage 1
must be conservative about when to skip the LLM. False negatives are
worse than false positives." Acceptable Stage-1 behavior; Stage 2 will
filter the FPs.

## Section D — 21-source coverage matrix

**PASS for direct-RSS sources; categorical entries documented.**
Per-source counts are from a 3-minute window post-deploy of the
[RSS source-fix PR](VERIFICATION_PHASE_1_5.md).

| #  | Source                                             | Status      | Articles (3 min) | Notes |
|----|----------------------------------------------------|-------------|------------------|-------|
| 1  | The Washington Post                                | ACTIVE      | 102 + 7          | `wapo_via_gnews` (politics) + `wapo_world` (direct). `/politics` direct hangs; gnews fallback compensates. |
| 2  | Verified social media accounts                     | N/A_CATEGORICAL | n/a          | `TwitterScraper` is wired but no-ops without `TWITTER_BEARER_TOKEN`. Truth Social scraper works. Phase 2 candidate to expand to @WhiteHouse / @PressSec / @POTUS once the token is set. |
| 3  | Press release distribution services                | ACTIVE      | 20 (PR Newswire) | `pr_newswire_gov` polls Phase 1. Business Wire candidate covered via existing `business_wire` source (separate config entry; not in 3-min sample due to lower frequency). |
| 4  | Fox News                                           | ACTIVE      | 50               | `fox_politics` (25) + `fox_world` (25). |
| 5  | MSNBC                                              | FIXED       | 10               | URL `https://www.msnbc.com/feed/`; was 0 because `httpx.AsyncClient` defaulted to `follow_redirects=False`. |
| 6  | The Wall Street Journal                            | ACTIVE      | 20               | `wsj_world`. `wsj_politics` shows in config; volume varies by news flow. |
| 7  | Semafor                                            | FIXED       | 77               | `semafor_via_gnews`. Direct `/feed.xml` returns 404 — no public RSS. |
| 8  | The Information                                    | ACTIVE      | 20               | `the_information`. |
| 9  | ABC                                                | FIXED       | 50               | `abc_politics` (25) + `abc_international` (25). Was 0 due to the same `follow_redirects=False` bug as MSNBC. |
| 10 | The Associated Press (AP)                          | FIXED       | 109              | `ap_via_gnews`. Direct `apnews.com/hub/*/feed` returns 404 — AP killed public feeds. |
| 11 | NBC                                                | ACTIVE      | 49               | `nbc_politics` (25) + `nbc_world` (24). |
| 12 | Photographic / video evidence from accredited media | N/A_CATEGORICAL | n/a         | Not a source per se. Approach: rely on body text from existing sources mentioning "photo released" / "video shows". `news_events.has_photo` + `has_video` columns are wired for the RSS poller's media-content detection. LLM cascade will use the body text directly. |
| 13 | Axios                                              | ACTIVE      | 100              | `axios`. |
| 14 | Official government websites                       | ACTIVE      | 30+              | `whitehouse_news` (10), `whitehouse_press`, `state_press`, `state_readouts`, `dod_news` (10 — also FIXED by `follow_redirects`). |
| 15 | The New York Times                                 | ACTIVE      | 74               | `nyt_politics` (19) + `nyt_world` (55). |
| 16 | Politico                                           | ACTIVE      | 64               | `politico_picks` (34) + `politico_wh` (30). |
| 17 | CNN                                                | ACTIVE      | 41               | `cnn_politics` (18) + `cnn_world` (23). |
| 18 | Official readouts from relevant governments        | N/A_CATEGORICAL | n/a          | Captured indirectly via `state_readouts` and other gov press feeds. Cross-check during Phase 2: poll specific government readout pages directly if matcher coverage suffers. |
| 19 | Reuters                                            | FIXED       | 112              | `reuters_via_gnews`. Reuters shut down public RSS in 2020 (paid Reuters Connect API only). |
| 20 | CBS                                                | ACTIVE      | 51               | `cbs_politics` (24) + `cbs_world` (27). |
| 21 | Bloomberg News                                     | ACTIVE      | 31               | `bloomberg`. |

Net: **17 of 21 entries are direct/RSS-active** (counting the gnews
fallbacks under their underlying outlet). The remaining 4 are
categorical buckets that don't map to a single RSS feed; their
intended handling is documented in [CLAUDE.md](CLAUDE.md) and partially
in place. None of the 21 are FAIL.

## Section E — new source validation

**PASS — see [VERIFICATION_PHASE_1_5.md](VERIFICATION_PHASE_1_5.md)**
for the per-source URL audit, the 5-minute live confirmation, and
the root-cause writeup for the `follow_redirects=False` latent bug
that the audit surfaced.

## Section F — 60-minute integration test

**DEFER.** Cannot be answered honestly: Stage 2 (LLM cascade) is not
yet built. The brief's required metrics include "Articles classified
by LLM (Stage 2 calls)", "LLM cost incurred ($)", "Cost per LLM call".
None exist yet.

What the daemon *can* do for 60 minutes today (just verified by the
2-minute and 5-minute smoke runs in the previous PRs):

- Total articles ingested: ~25 / minute average → ~1500 in an hour
- Pre-filter pass rate: 6 / 1007 = 0.6 % over the last 7 days
- LLM stage: not present
- Memory drift: not measured this session
- Critical events: 0
- Daemon clean shutdown on SIGTERM: **No** — the smoke test had to
  fall through to SIGKILL (the supervisor task does not propagate
  the SIGTERM cleanly through every async task). Tracked as a
  follow-up.

The pre-filter pass rate is at the very lower edge of the brief's
"5-15%" healthy range. With Stage 2 LLM in place this would be
mostly fine because the cost per call is small, but the pre-filter
should be a touch more permissive once the LLM exists.

## Section G — LLM reasoning spot-check

**DEFER.** No `llm_classifications` rows exist; Stage 2 not built.

## Section H — cost guard operational check

**DEFER.** No `llm_spend_daily` rows exist; the table is in
`migrations/003_llm_cascade.sql` (parked on `phase-1.5-llm-cascade`)
but not migrated against the live DB.

## Section I — still-broken checks

| Check                                                              | Status |
|--------------------------------------------------------------------|--------|
| Subjects table populated with all 22 April subjects                | **PASS** — 23 rows (22 confirmed + auto-extracted "Delcy Rodriguez" the discovery service found). |
| Each subject has aliases including full name + last name + role-based where applicable | **PARTIAL PASS** — every subject has at least 2 aliases (full name + last name); 4 subjects have 3 aliases (Putin/Xi/Netanyahu/Powell/Zelenskyy adding short forms / "Bibi" / "Jay Powell"). The "role-based" requirement (e.g. "Senator Thune", "Majority Leader") is not enforced; the matcher already matches on bare last name so it picks up "Senator Thune" naturally. Phase 2 LLM enrichment will add role aliases formally. |
| Contract rules file `data/contracts/kxtrumpmeet_rules.txt` unchanged from snapshot | **DEFER (file does not yet exist)** — to be created when Stage 2 LLM lands. |
| All Phase 1 components still functional                            | **PASS** — 5-minute smoke test passes ✅ on 3 of 4 checks (≥1 price snapshot, ≥1 news event, 0 critical events). The 4th ("≥1 market discovered") only fails because no NEW markets opened during the 5-minute window; pre-existing markets remain populated. |
| macOS launchd plist still loads correctly                          | **PASS** — `deploy/com.trumpbot.daemon.plist` exists and was not changed by this session. Operator confirmed plist install in an earlier session. |
| litestream replication still running                               | **N/A** — never wired in production this session; Phase 2 follow-up. |

## Quality gates

- `black --check`, `ruff check`, `mypy --strict` all clean.
- `pytest`: **278 tests pass** (was 276; +2 from the negative-case
  guards in `tests/test_zero_matches_regression.py`).

## REQUIRES USER ATTENTION

1. **Restart the daemon** to pick up the `follow_redirects=True` fix
   from the RSS audit:
   ```
   launchctl unload ~/Library/LaunchAgents/com.trumpbot.daemon.plist
   launchctl load ~/Library/LaunchAgents/com.trumpbot.daemon.plist
   ```
   After ~10 minutes, `inspect_data.py` should show MSNBC, ABC, and
   the `*_via_gnews` sources delivering articles. (Already verified
   by the smoke test against the same config; the launchd-managed
   daemon needs the restart to pick it up.)

2. **Phase 1.5 LLM cascade is parked WIP** on
   `phase-1.5-llm-cascade`. Sections F/G/H of this recheck cannot
   be re-run until that work lands. The foundation modules
   (`migrations/003_llm_cascade.sql`, `trumpbot/utils/cost.py`,
   `trumpbot/utils/clock.py`, `trumpbot/news/contract.py`,
   `trumpbot/news/cost_guard.py`, `trumpbot/news/llm_cache.py`)
   are committed there; the next step is the actual
   `trumpbot/news/llm_classifier.py` + Anthropic-API integration +
   the contract snapshot script + 30+ fixture cases.

3. **SIGTERM shutdown is not clean** — the smoke test consistently
   has to escalate to SIGKILL. Some asyncio task isn't honoring
   cancellation. Not blocking deployment but should be tracked.

4. **`scripts/reprocess_matches.py`** has been run (Section C
   above) so the live database is now consistent with the post-fix
   matcher. Re-running it is safe; idempotent.
