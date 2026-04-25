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
