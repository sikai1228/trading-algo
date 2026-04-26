"""Pydantic v2 config models loaded from YAML at startup."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from trumpbot.news.sources import NewsSourceConfig

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class KalshiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Production elections endpoints. Verified working 2026-04-25 via a
    # signed /portfolio/balance call. The path segments after the host
    # must match the constants in trumpbot.kalshi.auth (API_PATH_PREFIX
    # and WS_AUTH_PATH); changing one without the other breaks signing.
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    api_key_id: str
    private_key_path: str
    private_key_passphrase: str | None = None
    target_series: list[str] = Field(default_factory=list)
    market_discovery_interval_sec: int = 300
    backfill_days: int = 60
    rate_per_sec: float = 100.0
    rate_burst: float = 40.0
    rate_limit_pct: float = 0.8


class DiscoveryConfig(BaseModel):
    """Monthly KXTRUMPMEET discovery loop."""

    model_config = ConfigDict(extra="forbid")

    series: str = "KXTRUMPMEET"
    poll_interval_sec: int = 3600  # 1 hour per the brief
    backfill_months: int = 2
    # ``"auto"`` resolves to the platform default at startup; see
    # trumpbot.platform_paths.
    snapshot_dir: str = "auto"
    initial_subjects_path: str | None = "auto"


class TelegramConfig(BaseModel):
    """Phase-1 heads-up notifier + Phase-2 approval-bot credentials.

    Same chat_id is used for both: the bot polls and validates inbound
    callbacks against this chat. Empty values disable Telegram cleanly.
    """

    model_config = ConfigDict(extra="forbid")

    bot_token: str | None = None
    chat_id: str | None = None


class DecisionPhaseConfig(BaseModel):
    """Phase-2 decision-layer thresholds (LOCKED by CLAUDE.md)."""

    # Phase 4 Part 2.9 — switched to ``extra="ignore"`` so an
    # un-migrated config.yaml carrying ``llm_confidence_threshold:
    # 0.85`` (the field removed in this PR) loads silently. The next
    # operator pass will strip it from the YAML; in the meantime
    # nothing breaks. New unrecognized fields still won't be silently
    # accepted in any place that matters because the engine just
    # ignores them.
    model_config = ConfigDict(extra="ignore")

    # Phase 4 Part 2.9 — ``llm_confidence_threshold`` was REMOVED.
    # The decision engine no longer gates on a confidence number; the
    # LLM's ``interaction_occurred`` boolean is the trade trigger. The
    # Haiku confidence float is recorded in
    # ``llm_classifications.parsed_confidence`` for audit only.

    max_buy_price_cents: int = 90
    """Hard ceiling on YES contract entry price (in cents). Trades
    above this are rejected by the engine and the risk gate. Raised
    from 80 to 90 (Phase 4 Part 2.5) — markets often trade in the
    80-90 cent band for several hours after a confirming news
    headline before snapping to $1.00, and the bot was missing those
    legs because of the old ceiling."""
    # Phase 4 Part 2.9 — ``position_size_base_pct`` was REMOVED. It
    # was the multiplier in the old "8 % of bankroll x confidence"
    # sizing path; the two-cap system replaced it in Phase 3 Part 1
    # and no production code has read this field since.

    # Phase 3 Part 1: two-cap system (was a single fixed cap in PR #10).
    position_size_hard_cap_cents: int = 2000
    """Cap one — hard fixed-dollar ceiling per trade, in USDCents.
    Default $20.00. Configurable via the YAML field
    ``decision.position_size_hard_cap_usd``."""

    position_size_orderbook_pct: float = 0.20
    """Cap two — fraction of YES contracts available at prices ≤
    ``max_buy_price_cents`` the bot is willing to take in a single
    trade. Default 20 %.

    Phase 4 Part 2.6 (rename + redefine): replaces the prior
    ``position_size_volume_pct`` (5 % of historical traded volume).
    Total volume is a poor proxy for current liquidity; the new
    semantics measure live orderbook depth so cap_two automatically
    tightens when the book is thin and expands when it's deep. See
    :class:`trumpbot.decision.engine.DecisionConfig` for the full
    formula."""

    min_trade_size_contracts: int = 5
    """Skip the trade entirely if the walk fills fewer than this."""

    min_trade_value_cents: int = 200
    """Skip if the walk's total cost is below this. Default $2.00."""

    # Phase 4 Part 2.3: the aggregate "total_exposure_cap_pct" field
    # was REMOVED. Aggregate exposure is now bounded by the operator's
    # actual Kalshi deposit (the bankroll-sufficiency check refuses
    # any trade that wouldn't fit) plus the two per-trade caps. See
    # CLAUDE.md "Phase 4 Part 2.3 — exposure cap removal".

    stop_loss_drop_cents: int = 50
    decision_loop_interval_sec: int = 10
    stop_loss_loop_interval_sec: int = 60
    position_marking_loop_interval_sec: int = 60
    reentry_loop_interval_sec: int = 30


class RiskPhaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    halted: bool = False


class ApprovalPhaseConfig(BaseModel):
    """Approval-flow timeouts.

    NOTE: ``mode`` is HARDCODED to ``"human"`` in v1 (see CLAUDE.md
    "Hardcoded human-in-the-loop"). It used to live here as a
    configurable field but was deliberately removed in Phase 4 — auto-
    approve must NOT be reachable through any config knob, only by
    deleting the constant in the code itself. The shadow_decisions
    table (Phase 4 Part 1) collects evidence for whether auto-approve
    would be safe in the future.
    """

    model_config = ConfigDict(extra="forbid")

    entry_timeout_sec: int = 180
    stop_loss_timeout_sec: int | None = None
    reentry_timeout_sec: int | None = None


class ExecutionPhaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "dry_run"


class BankrollConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starting_amount_usd: float = 500.00


# ---------------------------------------------------------------------------
# Phase 3 Part 2 — operational features
# ---------------------------------------------------------------------------


class NotificationsConfig(BaseModel):
    """Knobs for the digest / settlement / source-health loops + the
    categorized alert dispatcher's dedup window.

    Phase 4 Part 2.10 — switched to ``extra="ignore"`` so a legacy
    ``heartbeat_interval_minutes`` key in an un-migrated config.yaml
    loads silently (the field was removed alongside the heartbeat
    loop).
    """

    model_config = ConfigDict(extra="ignore")

    # Phase 4 Part 2.10 — ``heartbeat_interval_minutes`` was REMOVED.
    # The morning daily digest is the regular status notification
    # now; the field is silently ignored if still present in an
    # un-migrated config.yaml.

    digest_hour_utc: int = 12  # 12 UTC ~ 8 AM ET in standard time
    settlement_check_interval_seconds: int = 300  # 5 min
    source_health_check_interval_seconds: int = 300
    source_down_alert_threshold_minutes: int = 30
    db_slow_query_threshold_ms: int = 500
    kalshi_disconnect_alert_threshold_minutes: int = 5
    alert_dedup_window_minutes: int = 60
    rate_limit_commands_per_minute: int = 30


class TaxTrackingConfig(BaseModel):
    """Phase 4 Part 2.1 — tax tracking knobs.

    All amounts in the bot already use integer cents; this config is
    pure metadata about WHEN exports run and HOW the operator wants
    them formatted.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    """Master switch. Off → no monthly digest loop is started, but
    the tax columns are still populated on every trade lifecycle."""

    user_tax_year_start: str = "01-01"
    """MM-DD that opens the operator's tax year. Almost always
    01-01 (US calendar year). Configurable for non-US filers or
    fiscal-year operators down the road."""

    default_export_format: str = "csv"
    """One of csv / json / form_8949. /tax_export uses this when the
    user omits the format argument."""

    monthly_digest_enabled: bool = True
    """If False, the monthly_tax_digest_loop task is not started."""

    monthly_digest_day: int = 1
    """Calendar day of the month the digest fires."""

    monthly_digest_time_et: str = "09:00"
    """HH:MM local Eastern Time the digest fires. ET is the
    operator's display timezone (CLAUDE.md macOS deployment notes)."""


class AliasEnrichmentConfig(BaseModel):
    """LLM-enrichment knobs. ``monthly_cap_usd_cents`` is the unified
    cap for all Anthropic spend (both alias enrichment and the news
    classifier draw from the same budget)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    prompt_path: str = "trumpbot/news/prompts/alias_enrichment_v1.txt"
    prompt_version: str = "v1"
    model: str = "claude-haiku-4-5"
    max_tokens: int = 512
    monthly_cap_usd_cents: int = 2000  # $20/month default (was $10 pre-2.8)


class LLMClassifierConfig(BaseModel):
    """Phase 4 Part 2.8 — Stage 2 LLM cascade for the news classifier.

    Shares the budget pool defined by ``alias_enrichment.monthly_cap_usd_cents``
    via :class:`LLMCostGuard`. ``enabled=False`` disables Stage 2
    entirely; the matcher then writes only ``classifier_type='keyword_only'``
    rows and the decision engine's ``interaction_occurred`` check
    blocks every trade — same behavior as before Phase 4 Part 2.8."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    model: str = "claude-haiku-4-5"
    max_input_tokens: int = 2000
    max_output_tokens: int = 250
    timeout_sec: int = 10
    prompt_path: str = "trumpbot/news/prompts/cascade_classifier_v1.txt"
    prompt_version: str = "v1"
    contract_path: str = "data/contracts/kxtrumpmeet_rules.txt"


class NewsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[NewsSourceConfig] = Field(default_factory=list)


class MatcherConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_aliases_path: str | None = None
    body_match_window_chars: int = 500
    verb_proximity_chars: int = 200
    poll_interval_sec: int = 5
    batch_size: int = 100


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = "/var/lib/trumpbot/trumpbot.db"


class HealthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 9090


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"
    format: str = "json"


class DaemonConfig(BaseModel):
    # Phase 4 Part 2.10 — switched to ``extra="ignore"`` so a legacy
    # ``heartbeat_interval_sec`` key in an un-migrated config.yaml
    # loads silently. The HeartbeatLogger and the field were both
    # removed; the section stays defined as a placeholder so future
    # daemon-level knobs can be added without a config-schema break.
    model_config = ConfigDict(extra="ignore")


class TrumpbotConfig(BaseModel):
    """Top-level config object."""

    model_config = ConfigDict(extra="forbid")

    kalshi: KalshiConfig
    news: NewsConfig = Field(default_factory=lambda: NewsConfig())
    matcher: MatcherConfig = Field(default_factory=lambda: MatcherConfig())
    database: DatabaseConfig = Field(default_factory=lambda: DatabaseConfig())
    health: HealthConfig = Field(default_factory=lambda: HealthConfig())
    logging: LoggingConfig = Field(default_factory=lambda: LoggingConfig())
    daemon: DaemonConfig = Field(default_factory=lambda: DaemonConfig())
    discovery: DiscoveryConfig = Field(default_factory=lambda: DiscoveryConfig())
    telegram: TelegramConfig = Field(default_factory=lambda: TelegramConfig())
    decision: DecisionPhaseConfig = Field(default_factory=lambda: DecisionPhaseConfig())
    risk: RiskPhaseConfig = Field(default_factory=lambda: RiskPhaseConfig())
    approval: ApprovalPhaseConfig = Field(default_factory=lambda: ApprovalPhaseConfig())
    execution: ExecutionPhaseConfig = Field(default_factory=lambda: ExecutionPhaseConfig())
    bankroll: BankrollConfig = Field(default_factory=lambda: BankrollConfig())
    # Phase 3 Part 2.
    notifications: NotificationsConfig = Field(default_factory=lambda: NotificationsConfig())
    alias_enrichment: AliasEnrichmentConfig = Field(default_factory=lambda: AliasEnrichmentConfig())
    # Phase 4 Part 2.8 — Stage 2 LLM cascade for the news classifier.
    llm_classifier: LLMClassifierConfig = Field(default_factory=lambda: LLMClassifierConfig())
    # Phase 4 Part 2.1 — tax tracking + exports.
    tax_tracking: TaxTrackingConfig = Field(default_factory=lambda: TaxTrackingConfig())


def _expand_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` placeholders with environment values."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            return os.environ.get(name, "")

        return ENV_VAR_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path: Path | str) -> TrumpbotConfig:
    """Load YAML config, expand ``${ENV}`` placeholders, parse with Pydantic."""
    raw = yaml.safe_load(Path(path).read_text())
    if raw is None:
        raise ValueError(f"config at {path} is empty")
    expanded = _expand_env(raw)
    return TrumpbotConfig.model_validate(expanded)
