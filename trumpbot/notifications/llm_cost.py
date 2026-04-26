"""Anthropic LLM cost tracker.

Phase 3 Part 2 created the per-call ``llm_spend_log``. Phase 4 Part
2.8 added the ``llm_spend_daily`` rollup and the four-tier
:class:`CapStatus` API the new news-classifier reads on every call
(it needs to know whether to halt, throttle, or warn — not just a
boolean).

Two responsibilities:

1. **Record spend.** Components calling the Anthropic API (alias
   enrichment, news classifier) report the per-call cost in USDCents
   via :meth:`LLMCostGuard.record_spend`. The cost lands in
   ``llm_spend_log`` (per-call audit) AND ``llm_spend_daily`` (the
   rollup the cap-status query reads).
2. **Enforce a monthly cap.** Before a component fires an LLM call,
   it asks :meth:`LLMCostGuard.cap_status`:

       under_50         — full speed
       between_50_90    — full speed; one daily warning info-alert
       between_90_100   — every-other call (50 % throttle); per-call alert
       over_cap         — HARD HALT; calls return ``None`` and fall
                          back to the keyword-only path

   The ``is_under_cap`` boolean is preserved for the alias enricher
   (which existed before tiers).

Pricing model (current as of 2026-04-25):

  Anthropic claude-haiku-4-5
    input:  $0.25 / 1M tokens
    output: $1.25 / 1M tokens

The :func:`estimate_haiku_cost_cents` helper converts a usage tuple
into integer USDCents. Update the per-million-token rates here when
Anthropic changes them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    insert_llm_spend,
    llm_spend_count_since,
    llm_spend_since_cents,
    upsert_llm_spend_daily,
)

# Anthropic pricing in USDCents per token. Source:
# https://docs.anthropic.com/en/docs/about-claude/pricing as of
# 2026-04-25. claude-haiku-4-5 = $0.25 input / $1.25 output per
# million tokens, i.e. 0.025 / 0.125 USDCents per 1k tokens.
HAIKU_INPUT_CENTS_PER_TOKEN: float = 0.25 / 1_000_000  # = 2.5e-7 dollars/token
HAIKU_OUTPUT_CENTS_PER_TOKEN: float = 1.25 / 1_000_000


@dataclass(frozen=True)
class LLMCostGuardConfig:
    """Cost-guard knobs.

    ``monthly_cap_usd_cents`` is the hard cap. ``warn_threshold_pct``
    is the percentage at which the dispatcher fires
    ``alert_info_llm_spend_update`` (default 50 %).
    """

    monthly_cap_usd_cents: int = 1000  # $10.00 default
    warn_threshold_pct: float = 0.50


class CapStatus(str, Enum):
    """Four-tier MTD spend bucket. Returned by
    :meth:`LLMCostGuard.cap_status`."""

    UNDER_50 = "under_50"
    BETWEEN_50_90 = "between_50_90"
    BETWEEN_90_100 = "between_90_100"
    OVER_CAP = "over_cap"


def estimate_haiku_cost_cents(*, input_tokens: int, output_tokens: int) -> int:
    """Convert an Anthropic usage report into integer USDCents.

    Uses :func:`math.ceil` so partial cents always round up (Anthropic
    doesn't refund fractional cents and this matches the way the
    ``alert_critical_llm_cap`` guard should behave in the user's
    favor)."""
    cost = input_tokens * HAIKU_INPUT_CENTS_PER_TOKEN + output_tokens * HAIKU_OUTPUT_CENTS_PER_TOKEN
    return math.ceil(cost)


class LLMCostGuard:
    """Read + write helper around the ``llm_spend_log`` table."""

    def __init__(self, *, db: Database, config: LLMCostGuardConfig) -> None:
        self._db = db
        self._cfg = config

    def record_spend(
        self,
        *,
        component: str,
        model: str,
        cost_usd_cents: int,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_hit: bool = False,
        now_utc: datetime | None = None,
    ) -> None:
        """Persist one Anthropic API call's cost.

        Writes to BOTH ``llm_spend_log`` (audit trail) and
        ``llm_spend_daily`` (rollup the cap-status query reads). Both
        tables are updated atomically per call so they never drift.
        """
        n = now_utc or datetime.now(UTC)
        insert_llm_spend(
            self._db,
            component=component,
            model=model,
            cost_usd_cents=cost_usd_cents,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        upsert_llm_spend_daily(
            self._db,
            day_iso=n.date().isoformat(),
            cost_micro_usd=cost_usd_cents * 10_000,
            input_tokens=input_tokens or 0,
            output_tokens=output_tokens or 0,
            cache_hit=cache_hit,
        )

    def month_to_date_cents(self, *, now_utc: datetime | None = None) -> int:
        """Sum of :func:`record_spend` since the start of the current
        UTC month."""
        return llm_spend_since_cents(self._db, since_iso=_start_of_month(now_utc).isoformat())

    def is_under_cap(self, *, now_utc: datetime | None = None) -> bool:
        """True when MTD spend < ``monthly_cap_usd_cents``. Components
        check this before firing an LLM call; if False, fall back to a
        keyword path or skip enrichment entirely."""
        return self.month_to_date_cents(now_utc=now_utc) < self._cfg.monthly_cap_usd_cents

    def cap_status(self, *, now_utc: datetime | None = None) -> CapStatus:
        """Four-tier MTD spend bucket for the news classifier.

        Thresholds:
            under_50         spend < 50 % of cap
            between_50_90    50 % <= spend < 90 %
            between_90_100   90 % <= spend < 100 %
            over_cap         spend >= 100 %
        """
        cap = self._cfg.monthly_cap_usd_cents
        if cap <= 0:
            return CapStatus.OVER_CAP
        spend = self.month_to_date_cents(now_utc=now_utc)
        pct = spend / cap
        if pct >= 1.0:
            return CapStatus.OVER_CAP
        if pct >= 0.90:
            return CapStatus.BETWEEN_90_100
        if pct >= 0.50:
            return CapStatus.BETWEEN_50_90
        return CapStatus.UNDER_50

    def call_count_since_month_start(self, *, now_utc: datetime | None = None) -> int:
        return llm_spend_count_since(self._db, since_iso=_start_of_month(now_utc).isoformat())

    @property
    def monthly_cap_usd_cents(self) -> int:
        return self._cfg.monthly_cap_usd_cents

    @property
    def warn_threshold_pct(self) -> float:
        return self._cfg.warn_threshold_pct


def _start_of_month(now: datetime | None) -> datetime:
    n = now or datetime.now(UTC)
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


__all__ = [
    "HAIKU_INPUT_CENTS_PER_TOKEN",
    "HAIKU_OUTPUT_CENTS_PER_TOKEN",
    "CapStatus",
    "LLMCostGuard",
    "LLMCostGuardConfig",
    "estimate_haiku_cost_cents",
]
