"""Source configuration types shared between RSS, Twitter, and Truth Social pollers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceKind = Literal["rss", "twitter", "truth_social"]


class NewsSourceConfig(BaseModel):
    """Configuration for a single news source.

    Phase 4 Part 2.7 removed the per-source ``weight`` field. Phase 4
    Part 2.9 tightened the schema to ``extra="forbid"`` so a legacy
    ``weight: 1.0`` key now fails loudly at config-load time instead
    of being silently ignored. This catches a partial revert before
    the daemon starts trading off the wrong assumption.

    All Kalshi-approved sources are treated equally: a single one
    confirming is enough.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: SourceKind
    url: str | None = None
    handle: str | None = None
    poll_interval_sec: int = Field(default=90, ge=10)
    is_kalshi_approved: bool = False


class FetchedItem(BaseModel):
    """One item produced by any poller before it is persisted."""

    model_config = ConfigDict(extra="ignore")

    source: str
    is_kalshi_approved: bool
    headline: str
    url: str | None
    body_excerpt: str | None
    author: str | None
    published_ts: str | None
    has_photo: bool = False
    has_video: bool = False
    raw_data: dict[str, str | int | float | None] | None = None
