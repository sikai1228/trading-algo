"""Phase 1.5 LLM cascade — Stage 2 of the news classifier.

Stage 1 (:mod:`trumpbot.news.matcher`) is the aggressively-inclusive
pre-filter: Trump + subject alias + interaction term anywhere in the
text. Stage 2 — this module — is the precision filter. It feeds
the article + verbatim contract rules to Claude Haiku 4.5, gets a
strict-JSON classification back, and writes one row to
``llm_classifications`` per call (success or failure).

The only thing the rest of the system reads off the LLM for trade
gating is the ``parsed_interaction_occurred`` boolean. Phase 4 Part
2.9 removed the ``llm_confidence_threshold`` engine gate; the LLM's
yes/no answer is the sole signal-strength filter. The
``parsed_confidence`` float is still recorded in
``llm_classifications.parsed_confidence`` for audit and shadow
analysis but does not drive any decision.

- ``DecisionEngine.evaluate_news_match`` requires
  ``interaction_occurred is True``.
- The matcher row's ``confidence`` and ``classifier_type`` are
  overwritten in-place after a successful classification so
  ``/why`` and shadow reports show the LLM's score even though the
  engine doesn't gate on it.

Failure modes are first-class:

- Cap hit -> classifier returns ``None``; matcher row stays
  ``classifier_type='keyword_only'``, confidence 0.0. No trade.
- Timeout / 5xx / parse error -> retry once, then return ``None``;
  failure row written to ``llm_classifications`` with non-NULL
  ``error`` for the audit trail.
- 401 -> raises :class:`AnthropicAuthError`; caller fires
  ``alert_critical_anthropic_auth``. No trade.
- Contract hash drift -> single ``alert_critical_contract_rules_changed``
  per process lifetime, then continues with the new hash. The matcher
  row IS still produced — the alert is informational.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    insert_llm_classification,
)
from trumpbot.notifications.alerts import AlertDispatcher
from trumpbot.notifications.llm_cost import (
    LLMCostGuard,
    estimate_haiku_cost_cents,
)
from trumpbot.utils.logging import get_logger

log = get_logger(__name__)

# Type alias: the (input_tokens, output_tokens, response_text) tuple
# the Anthropic call returns. Pulled out so tests can stub easily.
LLMCallFn = Callable[[str, str], Awaitable[tuple[int, int, str]]]
"""Async LLM call: takes (system_prompt, user_prompt), returns
(input_tokens, output_tokens, raw_text)."""


class AnthropicAuthError(Exception):
    """Raised by the LLM client wrapper on a 401."""


# ---------------------------------------------------------------------------
# Pydantic model — strict JSON shape we expect back from the LLM
# ---------------------------------------------------------------------------


class ClassificationResult(BaseModel):
    """One classification per (news_event, subject candidate set)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str | None
    interaction_occurred: bool
    interaction_type: Literal["in_person", "phone", "video"] | None = None
    tense: Literal["past", "future", "ongoing", "ambiguous"]
    negated: bool
    indirect_only: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    key_quote: str = ""
    """Verbatim quote from the article supporting the LLM's decision.
    Phase 4 Part 2.11 added this so trade-approval Telegram messages
    can render the operator-facing reasoning with the article's own
    words. The model's instructions ask for max 200 chars; a longer
    quote is truncated at the template-render boundary rather than
    rejected by the parser. Defaulted to empty string for back-compat
    with the v1 prompt during the prompt-version transition."""


# ---------------------------------------------------------------------------
# Config + classifier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMClassifierConfig:
    enabled: bool = True
    model: str = "claude-haiku-4-5"
    max_input_tokens: int = 2000
    max_output_tokens: int = 250
    timeout_sec: int = 10
    # Phase 4 Part 2.11 — bumped to v2 to extract a verbatim
    # ``key_quote`` from the article. The v1 file is retained for
    # archive only; production loads v2.
    prompt_path: str = "trumpbot/news/prompts/cascade_classifier_v2.txt"
    prompt_version: str = "v2"
    contract_path: str = "data/contracts/kxtrumpmeet_rules.txt"


class LLMClassifier:
    """Stateful classifier; one instance per daemon process.

    Holds: prompt template, contract bytes (re-read on every call so
    operator can hot-edit), last-seen contract hash (for drift alert),
    cost guard, alert dispatcher, and the injected LLM-call function.

    Usage in the matcher worker:

        result = await classifier.classify(
            news_event_id=evt["id"],
            headline=evt["headline"],
            body=evt["body_excerpt"],
            subject_candidates={"vladimirputin": ["Putin", "Vladimir Putin"]},
        )
        if result is None:
            # cap hit / failure — write keyword_only match row and move on
            ...
        else:
            # write llm_cascade match row with confidence=result.confidence,
            # matched_subject=result.subject, llm_classification_id=<row id>
            ...
    """

    def __init__(
        self,
        *,
        db: Database,
        cost_guard: LLMCostGuard,
        alerts: AlertDispatcher | None,
        config: LLMClassifierConfig,
        llm_call: LLMCallFn,
    ) -> None:
        self._db = db
        self._cost_guard = cost_guard
        self._alerts = alerts
        self._cfg = config
        self._llm = llm_call
        self._prompt_template = _load_text(Path(config.prompt_path))
        self._known_contract_hash: str | None = None

    async def classify(
        self,
        *,
        news_event_id: int,
        headline: str,
        body: str | None,
        subject_candidates: dict[str, list[str]],
    ) -> tuple[ClassificationResult, int] | None:
        """Classify one article.

        Returns ``(result, llm_classifications_id)`` on success.
        Returns ``None`` if the cap is hit, the LLM errored, or the
        response could not be parsed. Raises
        :class:`AnthropicAuthError` on a 401 so the caller can fire
        the critical alert.

        ``subject_candidates`` is the set of subject_keys whose aliases
        survived Stage 1 against this article. The LLM picks one (or
        ``null``) and the picked key is what the matcher row's
        ``matched_subject`` becomes.
        """
        if not self._cfg.enabled:
            return None
        if not self._cost_guard.is_under_cap():
            log.info("llm_classify_skipped_cap_hit", news_event_id=news_event_id)
            return None
        if not subject_candidates:
            log.debug("llm_classify_skipped_no_candidates", news_event_id=news_event_id)
            return None

        contract_rules = _load_text(Path(self._cfg.contract_path))
        contract_hash = hashlib.sha256(contract_rules.encode("utf-8")).hexdigest()
        await self._maybe_alert_contract_drift(contract_hash)

        subject_list_str = _format_subject_list(subject_candidates)
        body_excerpt = (body or "").strip()
        # Cheap heuristic to keep the prompt under max_input_tokens.
        # Token estimate: ~4 chars/token; budget = max_input_tokens * 4.
        budget_chars = max(500, self._cfg.max_input_tokens * 4 - len(self._prompt_template) - 1000)
        if len(body_excerpt) > budget_chars:
            body_excerpt = body_excerpt[:budget_chars] + "\n[truncated]"

        user_prompt = self._prompt_template.format(
            contract_rules_verbatim=contract_rules,
            subject_list=subject_list_str,
            article_headline=headline,
            article_body=body_excerpt or "(no body)",
        )
        system_prompt = (
            "You are a strict JSON classifier. Return ONLY a JSON object "
            "matching the requested schema. No prose."
        )

        request_payload = json.dumps(
            {
                "model": self._cfg.model,
                "subject_candidates": list(subject_candidates.keys()),
                "headline": headline,
                "body_chars": len(body_excerpt),
                "prompt_version": self._cfg.prompt_version,
            }
        )

        try:
            input_tokens, output_tokens, response_text = await self._call_with_one_retry(
                system_prompt, user_prompt
            )
        except AnthropicAuthError:
            # Audit row first, then propagate.
            insert_llm_classification(
                db=self._db,
                news_event_id=news_event_id,
                prompt_version=self._cfg.prompt_version,
                contract_hash=contract_hash,
                model=self._cfg.model,
                request_payload=request_payload,
                response_text=None,
                parsed=None,
                input_tokens=None,
                output_tokens=None,
                cost_micro_usd=None,
                error="anthropic_401",
            )
            raise
        except Exception as exc:
            log.warning("llm_classify_call_failed", error=repr(exc))
            insert_llm_classification(
                db=self._db,
                news_event_id=news_event_id,
                prompt_version=self._cfg.prompt_version,
                contract_hash=contract_hash,
                model=self._cfg.model,
                request_payload=request_payload,
                response_text=None,
                parsed=None,
                input_tokens=None,
                output_tokens=None,
                cost_micro_usd=None,
                error=f"{type(exc).__name__}: {exc!s}"[:500],
            )
            return None

        cost_cents = estimate_haiku_cost_cents(
            input_tokens=input_tokens, output_tokens=output_tokens
        )
        # Record spend whether or not parse succeeds — Anthropic charges either way.
        self._cost_guard.record_spend(
            component="news_classifier",
            model=self._cfg.model,
            cost_usd_cents=cost_cents,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        result = _try_parse(response_text)
        if result is None:
            log.warning("llm_classify_parse_failed", raw=response_text[:300])
            row_id = insert_llm_classification(
                db=self._db,
                news_event_id=news_event_id,
                prompt_version=self._cfg.prompt_version,
                contract_hash=contract_hash,
                model=self._cfg.model,
                request_payload=request_payload,
                response_text=response_text,
                parsed=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micro_usd=cost_cents * 10_000,  # cents -> micro USD
                error="parse_failed",
            )
            del row_id  # not surfaced on parse failure
            return None

        # Validate the picked subject against our candidates. If the
        # LLM hallucinates a key not in the candidate set, we drop it
        # to None — defensive, but rare.
        picked = result.subject
        if picked is not None and picked not in subject_candidates:
            log.warning(
                "llm_classify_subject_not_in_candidates",
                picked=picked,
                candidates=list(subject_candidates.keys()),
            )
            result = result.model_copy(update={"subject": None, "interaction_occurred": False})

        row_id = insert_llm_classification(
            db=self._db,
            news_event_id=news_event_id,
            prompt_version=self._cfg.prompt_version,
            contract_hash=contract_hash,
            model=self._cfg.model,
            request_payload=request_payload,
            response_text=response_text,
            parsed=result,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micro_usd=cost_cents * 10_000,
            error=None,
        )
        return result, row_id

    # ------------------------------------------------------------------
    async def _call_with_one_retry(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[int, int, str]:
        """Call the LLM; retry once on a generic exception, propagate
        :class:`AnthropicAuthError` immediately."""
        try:
            return await self._llm(system_prompt, user_prompt)
        except AnthropicAuthError:
            raise
        except Exception as exc:
            log.info("llm_classify_retrying", error=repr(exc))
            return await self._llm(system_prompt, user_prompt)

    async def _maybe_alert_contract_drift(self, contract_hash: str) -> None:
        if self._known_contract_hash is None:
            self._known_contract_hash = contract_hash
            return
        if contract_hash == self._known_contract_hash:
            return
        old = self._known_contract_hash
        self._known_contract_hash = contract_hash
        log.warning("contract_rules_hash_drift", old=old, new=contract_hash)
        if self._alerts is not None:
            await self._alerts.send(
                template_name="alert_critical_contract_rules_changed",
                data={"old_hash": old[:12], "new_hash": contract_hash[:12]},
                dedup_key="contract_rules_changed",
                component="llm_classifier",
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_text(path: Path) -> str:
    if not path.exists():
        log.warning("llm_classifier_file_missing", path=str(path))
        return ""
    return path.read_text()


def _format_subject_list(subject_candidates: dict[str, list[str]]) -> str:
    """Render the candidates as a bulleted list the LLM can parse."""
    lines: list[str] = []
    for key, aliases in subject_candidates.items():
        # Cap aliases to keep the prompt bounded.
        shown = aliases[:8]
        lines.append(f"- {key}: {', '.join(shown)}")
    return "\n".join(lines) if lines else "(none)"


def _try_parse(raw: str) -> ClassificationResult | None:
    """Parse a strict-JSON Claude response into ClassificationResult.

    Tolerates code-fenced blocks. Returns None on any error so the
    caller can record an audit row and continue.
    """
    if not raw:
        return None
    candidates: list[str] = [raw]
    # Pull out the first {...} block as a fallback.
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match is not None:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data: dict[str, Any] = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            return ClassificationResult(**data)
        except Exception:
            continue
    return None


__all__ = [
    "AnthropicAuthError",
    "ClassificationResult",
    "LLMCallFn",
    "LLMClassifier",
    "LLMClassifierConfig",
]
