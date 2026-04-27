# Truth Social end-to-end verification

**Date:** 2026-04-26
**Trigger:** Multi-PR sheet, PR 2 ("Verify Truth Social end-to-end
pipeline; document Trump-as-author handling").
**Author:** Investigation + targeted matcher fix in
`fix/the-information-ua-override` follow-up branch
`verify/truth-social-pipeline`.

PR #29 unblocked Truth Social ingestion (Safari UA fix); the
source-status audit (PR #30) confirmed 20 posts ingested on first
poll. This document verifies the **full pipeline** end-to-end: from
ingestion → Stage 1 keyword pre-filter → Stage 2 LLM cascade →
trade-intent gating.

The audit flagged that `llm_classifications` has zero rows since
Phase 1.5 deployed. This document explains why that's expected for
the 20 ingested posts AND validates that the pipeline would
classify a qualifying post correctly.

---

## Section 1 — Trump-as-author handling (matcher fix)

### Problem

Stage 1 requires three conditions:

A. Trump alias in `headline + body`
B. Subject alias in `headline + body`
C. Interaction term in `headline + body`

Truth Social posts come from `@realDonaldTrump`. They are
first-person and typically don't contain "Trump" — Trump is
writing them, so he doesn't refer to himself by name. Without
special handling, every Trump-meeting announcement on Truth Social
silently fails Stage 1 with `failed_pre_filter:no_trump`.

### Fix (this PR)

`NewsMatcher.match()` now accepts a `source: str | None = None`
parameter. When `source` matches one of `TRUMP_AUTHOR_SOURCES`
(currently just `truth_social:@realDonaldTrump`), the
"Trump alias appears in body" condition is satisfied implicitly
via the author. The body must still carry a tracked subject AND
an interaction term.

The implementation lives in `trumpbot/news/matcher.py`:

```python
TRUMP_AUTHOR_SOURCES: Final[tuple[str, ...]] = ("truth_social:@realDonaldTrump",)
TRUMP_AUTHOR_KEYWORD: Final[str] = "@realdonaldtrump (author)"

def _is_trump_author(source: str) -> bool:
    return any(source.startswith(prefix) for prefix in TRUMP_AUTHOR_SOURCES)

# inside match():
if trump_match is None and source is not None and _is_trump_author(source):
    trump_match = TRUMP_AUTHOR_KEYWORD
```

`MatcherWorker._process_batch` in `daemon.py` was updated to pass
`source=evt["source"]` so the rule actually fires in production.

The author-implicit `TRUMP_AUTHOR_KEYWORD` ends up in
`matched_keywords` so a future audit can grep for which Stage 1
passes were author-implicit vs. literal-text matches.

### Test coverage (`tests/test_news_matcher.py`)

12 new pinned tests in `TestTrumpAsAuthor` and
`TestIsTrumpAuthorHelper`:

- Synthetic Putin-call post (audit's canonical example) passes
  Stage 1 with `TRUMP_AUTHOR_KEYWORD` recorded.
- Truth Social post about a topic with no tracked subject still
  fails (`no_subject`).
- Truth Social post about a tracked subject with no interaction
  verb still fails (`no_interaction_term`) — pins the audit's
  Hakeem Jeffries case.
- A Truth Social post that DOES contain literal "Trump" gets the
  literal match (not the implicit-author keyword).
- Reuters article without "Trump" still fails Stage 1 — the
  implicit-author rule does NOT extend to non-Trump-authored
  sources.
- Default `source=None` falls back to literal-only matching
  (back-compat for legacy callers).
- `_is_trump_author` helper tests pin the source-prefix matching
  is exact and doesn't match `truth_social` alone or other handles.

All 41 matcher tests pass.

---

## Section 2 — Synthetic end-to-end test (operational)

The matcher unit tests prove Stage 1 admits a qualifying Truth
Social post. The full Stage 2 path (LLM call, classification row,
match-row patch) is exercised via the existing integration test at
`tests/test_phase_1_5_pipeline_e2e.py`, which already inserts
synthetic news events with Trump+subject+verb content and asserts
the row reaches the LLM. That test does not yet special-case
Truth Social author-implicit handling — but as of this PR, the
pipeline is now compatible with the case (the matcher accepts
source-derived Trump matches and would emit `passed_pre_filter`
for a Truth Social row carrying subject+verb in the body).

### Operational verification (post-deploy)

After this PR ships and the daemon redeploys, the following
sequence verifies the full pipeline reaches the LLM with a real
Anthropic call:

```sql
INSERT INTO news_events (
  source, is_kalshi_approved, headline, body_excerpt,
  url, url_canonical, detected_ts, raw_published_ts,
  has_photo, has_video
) VALUES (
  'truth_social:@realDonaldTrump',
  1,
  '(no text)',
  'Just got off the phone with my friend Vladimir Putin. We had a great conversation about ending the war in Ukraine. Many things discussed!',
  'https://truthsocial.com/@realDonaldTrump/posts/synthetic-test-001',
  'https://truthsocial.com/@realDonaldTrump/posts/synthetic-test-001',
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-2 minutes'),
  0, 0
);
```

After ~90 s (one matcher loop cycle), the following queries
should return populated rows:

```sql
-- Stage 1 should have passed
SELECT match_reason, classifier_type, matched_keywords, matched_subject
FROM news_market_matches
WHERE news_event_id = (
  SELECT id FROM news_events
  WHERE url = 'https://truthsocial.com/@realDonaldTrump/posts/synthetic-test-001'
);
-- Expect at least one row with match_reason='passed_pre_filter'
-- and matched_keywords containing TRUMP_AUTHOR_KEYWORD

-- Stage 2 (LLM cascade) should have classified
SELECT parsed_subject, parsed_interaction_occurred, parsed_tense,
       parsed_confidence, parsed_key_quote, error
FROM llm_classifications
WHERE news_event_id = (
  SELECT id FROM news_events
  WHERE url = 'https://truthsocial.com/@realDonaldTrump/posts/synthetic-test-001'
);
-- Expect: parsed_interaction_occurred=1, parsed_subject containing
-- "vladimirputin" (or however the cascade normalizes it), parsed_tense='past'
```

This is **operational** verification — it makes a real Anthropic
call and costs ~$0.0003. Run after deploying this PR and document
results inline in this file (or open a follow-up issue if it
doesn't behave as expected).

> **Note:** the synthetic row is not a problem for the trade
> pipeline because the URL doesn't resolve — even if Stage 2
> classifies it as a real interaction, the decision engine will
> not produce a trade intent without an open KXTRUMPMEET market
> for the subject *and* the engine's article-window check
> (rule 4) admits the timestamp.

---

## Section 3 — Manual review of the 20 ingested Truth Social posts

Per PR 2 spec deliverable 4, every Truth Social post ingested so
far was inspected to determine whether Stage 1 was correct and
whether any post should have produced an LLM classification but
didn't.

```sql
SELECT ne.id, substr(ne.body_excerpt, 1, 200) AS body,
       CASE WHEN nmm.match_reason='passed_pre_filter' THEN 'PASS' ELSE 'FAIL' END AS stage1
FROM news_events ne
LEFT JOIN news_market_matches nmm ON nmm.news_event_id = ne.id
WHERE ne.source = 'truth_social:@realDonaldTrump'
GROUP BY ne.id ORDER BY ne.detected_ts DESC;
```

| id | Synopsis | Stage 1 (current) | Should have passed? | Notes |
|---:|---|:---:|:---:|---|
| 1581 | Lafayette Park Fountains restoration | FAIL | No | No tracked subject, no interaction term |
| 1580 | Candace Owens criticism | FAIL | No | Owens not in subjects list |
| 1579 | Hakeem Jeffries rant | FAIL | No | Subject present but no interaction term — the audit's canonical "correctly rejected" case |
| 1578 | RT of Lincoln Memorial post | FAIL | No | RT noise |
| 1577 | RT of post 1581 | FAIL | No | RT noise |
| 1576 | RT URL only | FAIL | No | Empty body |
| 1575 | Empty body | FAIL | No | — |
| 1574 | SAVE AMERICA ACT call | FAIL | No | Policy commentary, no interaction event |
| 1573 | Cancelled Iran trip ("meet with the Iranians") | FAIL | No\* | "meet" present, but "the Iranians" is not a tracked subject (no Khamenei / Pezeshkian in subjects table). Strictly correct. Worth flagging — see follow-up |
| 1572 | NYT criticism re: Lafayette Park | FAIL | No | No subject, no interaction |
| 1571 | DC Secret Service / shooter response | FAIL | No | Domestic security; no tracked subject |
| 1570 | "Will give a press conference" | FAIL | No | Future event with press, not a tracked person |
| 1569 | Empty body | FAIL | No | — |
| 1568 | Empty body | FAIL | No | — |
| 1567 | Empty body | FAIL | No | — |
| 1566 | Security policy commentary | FAIL | No | No subject |
| 1565 | "Will be interviewed on 60 Minutes" | FAIL | No | Future media interview, not a tracked person |
| 1564 | Birthright citizenship link | FAIL | No | News-link share |
| 1563 | RT of post 1565 | FAIL | No | RT noise |
| 1562 | Empty body | FAIL | No | — |

**Verdict:** every one of the 20 posts is correctly handled by
Stage 1. Zero false negatives — there is no post that *should*
have been classified by the LLM but wasn't. The empty-body
clutter (6 of 20 posts) and RT noise (3 of 20) are the dominant
failure modes; both are correctly rejected by `no_subject`.

The interesting near-miss is **post 1573** ("I just cancelled
the trip of my representatives going to Islamabad, Pakistan,
to meet with the Iranians"). This is a CANCELLED interaction
with Iran, which would be a high-signal trade signal if there
were a `KXTRUMPMEET-26APR-IRAN-LEADER` market. There isn't —
none of Iran's leadership is in the `subjects` table (no
Khamenei, Pezeshkian, Araghchi). Adding Iranian leadership to
subjects when KXTRUMPMEET creates an Iran sub-market is the
right fix, and is not a matcher bug.

---

## Section 4 — Why `llm_classifications` is still empty

After the matcher fix and daemon redeploy, `llm_classifications`
will start populating when the next Trump post about a tracked
subject with an interaction verb lands. Until that happens, the
table will remain empty even though Stage 1 is now author-aware.

The audit's "0 LLM classifications in 25-min window" finding is
**not a bug** — it's a consequence of:

1. The 20 ingested Truth Social posts in the audit window are
   commentary/RT/empty (Section 3 review).
2. RSS articles in the same window predominantly fail Stage 1
   on `no_subject` because most general news isn't about a
   Trump-meeting event with one of the 17 currently-tracked
   subjects.

The follow-up scheduled in PR 4 deliverable 4 (re-check after
48 h of post-2.12 operation) is the right test of "is the
cascade actually firing under realistic load." This PR's
contribution is the matcher fix that ensures Truth Social can
reach the cascade when the news warrants it.

---

## Section 5 — Documentation update

Added "Truth Social handling" subsection to `CLAUDE.md` under
the Phase 1.5 LLM cascade description, documenting the
Trump-as-author rule, the source string convention, and how to
extend `TRUMP_AUTHOR_SOURCES` if Trump ever gets reinstated on
another platform.

## Appendix — Reproducing this audit

```bash
# Manual 20-post review query
sqlite3 ~/Library/Application\ Support/trumpbot/trumpbot.db \
  "SELECT ne.id, substr(ne.body_excerpt, 1, 200) AS body,
          CASE WHEN nmm.match_reason='passed_pre_filter' THEN 'PASS' ELSE 'FAIL' END AS stage1
   FROM news_events ne
   LEFT JOIN news_market_matches nmm ON nmm.news_event_id = ne.id
   WHERE ne.source = 'truth_social:@realDonaldTrump'
   GROUP BY ne.id ORDER BY ne.detected_ts DESC;"

# Run the new matcher tests
uv run pytest tests/test_news_matcher.py::TestTrumpAsAuthor -v
uv run pytest tests/test_news_matcher.py::TestIsTrumpAuthorHelper -v
```
