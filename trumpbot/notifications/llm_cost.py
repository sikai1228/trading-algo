"""Anthropic LLM cost tracker.

Phase 3 Part 2.

Two responsibilities:

1. **Record spend.** Components calling the Anthropic API (alias
   enrichment today; classifier in a future phase) report the per-call
   cost in USDCents via :meth:`LLMCostGuard.record_spend`. The cost
   lands in ``llm_spend_log``.
2. **Enforce a monthly cap.** Before a component fires an LLM call, it
   asks :meth:`LLMCostGuard.is_under_cap`. The guard sums month-to-date
   spend from ``llm_spend_log`` against the configured cap. The
   ``alert_critical_llm_cap`` and ``alert_info_llm_spend_update`` alerts
   are wired into the same threshold checks.

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

from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    insert_llm_spend,
    llm_spend_count_since,
    llm_spend_since_cents,
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
    ) -> None:
        """Persist one Anthropic API call's cost. Components call this
        immediately after a successful API request."""
        insert_llm_spend(
            self._db,
            component=component,
            model=model,
            cost_usd_cents=cost_usd_cents,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
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
    "LLMCostGuard",
    "LLMCostGuardConfig",
    "estimate_haiku_cost_cents",
]
