"""Source configuration types shared between RSS, Twitter, and Truth Social pollers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceKind = Literal["rss", "twitter", "truth_social"]


class NewsSourceConfig(BaseModel):
    """Configuration for a single news source."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: SourceKind
    url: str | None = None
    handle: str | None = None
    poll_interval_sec: int = Field(default=90, ge=10)
    weight: float = Field(default=0.85, ge=0.0, le=1.0)
    is_kalshi_approved: bool = False


class FetchedItem(BaseModel):
    """One item produced by any poller before it is persisted."""

    model_config = ConfigDict(extra="ignore")

    source: str
    source_weight: float
    is_kalshi_approved: bool
    headline: str
    url: str | None
    body_excerpt: str | None
    author: str | None
    published_ts: str | None
    has_photo: bool = False
    has_video: bool = False
    raw_data: dict[str, str | int | float | None] | None = None
