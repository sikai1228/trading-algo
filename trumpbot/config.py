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

    model_config = ConfigDict(extra="forbid")

    llm_confidence_threshold: float = 0.85
    max_buy_price_cents: int = 80
    position_size_base_pct: float = 0.08
    position_size_cap_first_30_days_pct: float = 0.02
    position_size_cap_after_30_days_pct: float = 0.10
    total_exposure_cap_pct: float = 0.30
    stop_loss_drop_cents: int = 50
    minimum_position_size_pct: float = 0.01
    decision_loop_interval_sec: int = 10
    stop_loss_loop_interval_sec: int = 60
    position_marking_loop_interval_sec: int = 60
    reentry_loop_interval_sec: int = 30


class RiskPhaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    halted: bool = False


class ApprovalPhaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "human"
    entry_timeout_sec: int = 180
    stop_loss_timeout_sec: int | None = None
    reentry_timeout_sec: int | None = None


class ExecutionPhaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "dry_run"
    live_trading_started_at: str | None = None
    """ISO-8601 UTC timestamp when live trading started (drives the
    30-day position-cap window)."""


class BankrollConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starting_amount_usd: float = 500.00


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
    model_config = ConfigDict(extra="forbid")

    heartbeat_interval_sec: int = 60


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
