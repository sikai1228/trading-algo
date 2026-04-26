"""Source configuration types shared between RSS, Twitter, and Truth Social pollers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SourceKind = Literal["rss", "twitter", "truth_social"]


class NewsSourceConfig(BaseModel):
    """Configuration for a single news source.

    Phase 4 Part 2.7: the per-source ``weight`` field was REMOVED.
    All sources are now treated equally in the engine's confidence
    math; the LLM cascade's confidence score is the only signal that
    feeds into the entry rule. ``weight`` keys in older config files
    are accepted-and-ignored via ``extra="allow"`` to keep deploys
    smooth, but they have no effect on trading.
    """

    # Phase 4 Part 2.7 — accept (but ignore) legacy `weight` keys so
    # an unmigrated config.yaml doesn't fail to load.
    model_config = ConfigDict(extra="allow")

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
