"""LLM-driven subject-alias enrichment.

Phase 3 Part 2.

When :class:`MarketDiscoveryService` upserts a fresh subject (via the
``market_discovered`` event), this module calls Claude Haiku 4.5 to
generate the common ways news media refer to that person. The
extracted aliases are union-merged with whatever the auto-extractor
already pulled out of the market title.

Cost: ~$0.001 per call at Haiku 4.5 pricing. With 10-20 new subjects
per month, that's ~$0.02/month -- negligible. The same monthly cap
that gates the (future) classifier gates this; if the cap is hit,
enrichment is skipped and only the auto-extracted aliases are used.

Error handling: any LLM failure (timeout, malformed JSON, 401, cap
hit) logs a warning and KEEPS the auto-extracted aliases. The bot
keeps running; the matcher is never blocked on enrichment.

The 401-detection path fires :func:`AlertDispatcher.send` with
``alert_critical_anthropic_auth`` so the user sees it immediately.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    fetch_subject_aliases,
    mark_subject_llm_enriched,
)
from trumpbot.events.bus import Event
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
(input_tokens, output_tokens, raw_text). The default implementation
hits anthropic.AsyncAnthropic; tests pass a stub."""


@dataclass(frozen=True)
class AliasEnrichmentConfig:
    enabled: bool = True
    prompt_path: str = "trumpbot/news/prompts/alias_enrichment_v1.txt"
    prompt_version: str = "v1"
    model: str = "claude-haiku-4-5"
    max_tokens: int = 512
    """Cap on the LLM's response. 512 tokens is plenty for a small
    JSON array of aliases."""


class AliasEnricher:
    """Holds the prompt template + cost guard + alert dispatcher; one
    instance per daemon. The actual Anthropic client is injected as a
    callable so tests don't need real network."""

    def __init__(
        self,
        *,
        db: Database,
        cost_guard: LLMCostGuard,
        alerts: AlertDispatcher | None,
        config: AliasEnrichmentConfig,
        llm_call: LLMCallFn,
    ) -> None:
        self._db = db
        self._cost_guard = cost_guard
        self._alerts = alerts
        self._cfg = config
        self._llm = llm_call
        self._prompt = _load_prompt(Path(config.prompt_path))

    async def on_market_discovered(self, event: Event) -> None:
        """EventBus subscriber. Fires when a new market (and possibly
        a fresh subject) is discovered. We enrich only when this is
        the first market seen for the subject."""
        if not self._cfg.enabled:
            return
        payload = event.payload or {}
        subject_key = str(payload.get("subject_key") or "")
        full_name = str(payload.get("subject_full_name") or "")
        ticker = str(payload.get("ticker") or "")
        if not subject_key or not full_name:
            return

        existing = fetch_subject_aliases(self._db, subject_key)
        if existing is None:
            return  # discovery service should have inserted; bail safely
        already_enriched, current_aliases = existing
        if already_enriched:
            return  # idempotent: only enrich once

        if not self._cost_guard.is_under_cap():
            log.info("alias_enrichment_skipped_llm_cap_hit", subject_key=subject_key)
            return

        try:
            new_aliases = await self._call_and_parse(full_name)
        except _AnthropicAuthError:
            if self._alerts is not None:
                await self._alerts.send(
                    template_name="alert_critical_anthropic_auth",
                    data={},
                    dedup_key="anthropic_auth",
                    component="alias_enrichment",
                )
            return
        except Exception as exc:  # pragma: no cover -- defensive
            log.warning(
                "alias_enrichment_failed",
                subject_key=subject_key,
                error=repr(exc),
            )
            return

        merged = _dedupe_lower(current_aliases + new_aliases)
        added_only = [a for a in new_aliases if a not in current_aliases]
        mark_subject_llm_enriched(self._db, subject_key=subject_key, aliases=merged)

        if self._alerts is not None and added_only:
            bullets = "\n".join(f"  - {a}" for a in added_only)
            await self._alerts.send(
                template_name="alert_info_subject_enriched",
                data={
                    "subject_full_name": full_name,
                    "ticker": ticker,
                    "original_aliases": json.dumps(current_aliases),
                    "added_aliases_bulleted": bullets,
                    "total_count": len(merged),
                },
                component="alias_enrichment",
            )

    async def _call_and_parse(self, full_name: str) -> list[str]:
        """Call the LLM, record the spend, return the parsed alias
        list. Raises :class:`_AnthropicAuthError` on a 401 so the
        caller can fire the right alert; raises generic
        :class:`Exception` on anything else (caller logs + skips)."""
        prompt = self._prompt.format(full_name=full_name)
        try:
            input_tokens, output_tokens, raw = await self._llm(
                "You return JSON arrays of name aliases.",
                prompt,
            )
        except _AnthropicAuthError:
            raise
        cost = estimate_haiku_cost_cents(input_tokens=input_tokens, output_tokens=output_tokens)
        self._cost_guard.record_spend(
            component="alias_enrichment",
            model=self._cfg.model,
            cost_usd_cents=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return _parse_alias_list(raw)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AnthropicAuthError(Exception):
    """Raised by the LLM client wrapper on a 401. Surfaces through
    the enricher so the dedicated alert template fires."""


def _load_prompt(path: Path) -> str:
    if not path.exists():
        # Fallback: return a minimal inline prompt so the daemon can
        # still start even if the file went missing. Logged loudly.
        log.warning("alias_enrichment_prompt_missing", path=str(path))
        return (
            "Generate a JSON array of common name aliases for {full_name}. "
            "Lowercase. Last name only, first initial + last name, role-based "
            "references, honorifics. Return only the JSON array."
        )
    return path.read_text()


def _parse_alias_list(raw: str) -> list[str]:
    """Extract a JSON array of strings from the LLM response. The
    model sometimes wraps the array in code fences or explanatory
    text; we extract the first ``[...]`` block we can json.loads.
    Raises :class:`ValueError` on no valid array."""
    # Try the whole string first.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return _coerce_strings(parsed)
    except json.JSONDecodeError:
        pass
    # Fallback: pull out the first [...] substring.
    match = re.search(r"\[.*?\]", raw, flags=re.DOTALL)
    if match is None:
        raise ValueError("no JSON array found in LLM response")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, list):
        raise ValueError("expected JSON array, got something else")
    return _coerce_strings(parsed)


def _coerce_strings(items: list[Any]) -> list[str]:
    out: list[str] = []
    for x in items:
        if not isinstance(x, str):
            continue
        s = x.strip().lower()
        if s:
            out.append(s)
    return _dedupe_lower(out)


def _dedupe_lower(items: list[str]) -> list[str]:
    """Lowercase + dedupe preserving first occurrence."""
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        lx = x.strip().lower()
        if lx and lx not in seen:
            seen.add(lx)
            out.append(lx)
    return out


__all__ = [
    "AliasEnricher",
    "AliasEnrichmentConfig",
    "LLMCallFn",
    "_AnthropicAuthError",
]
