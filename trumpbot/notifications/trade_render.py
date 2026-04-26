"""Phase 4 Part 2.11 — render-side helpers for trade-notification
templates.

The standardized trade-notification spec (Deliverables 7-9) requires
several derived display values: ET-formatted timestamps, article-age
annotations, paywall hints on news links, settlement P&L math, and
the "potential loss if stops out" walk-the-bid-side calculation.

All math here is integer cents; conversion to dollar strings happens
at the very last moment via :func:`dollars` so display never reads
floats off price/cost values.

This module is pure: no I/O, no DB. Callers (executor, message
adapters) supply already-fetched values.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from trumpbot.execution.fees import calculate_exit_fee_cents

ET = ZoneInfo("America/New_York")

# Sources known to be paywalled (annotated in Telegram links so the
# operator knows whether they can click through). Conservative list —
# adding a hostname here only changes the visual annotation, not any
# trading logic.
_PAYWALLED_HOSTS: frozenset[str] = frozenset(
    {
        "nytimes.com",
        "www.nytimes.com",
        "wsj.com",
        "www.wsj.com",
        "bloomberg.com",
        "www.bloomberg.com",
        "ft.com",
        "www.ft.com",
        "washingtonpost.com",
        "www.washingtonpost.com",
        "theatlantic.com",
        "www.theatlantic.com",
        "newyorker.com",
        "www.newyorker.com",
    }
)

# Twitter / X source annotations.
_TWITTER_HOSTS: frozenset[str] = frozenset({"twitter.com", "x.com", "www.twitter.com", "www.x.com"})

# Per spec: max 200 chars for the LLM key_quote.
KEY_QUOTE_MAX_CHARS = 200

# Article-link display URL truncation (still link the full URL).
ARTICLE_LINK_MAX_CHARS = 300


# ---------------------------------------------------------------------------
# ET timestamp formatting
# ---------------------------------------------------------------------------


def now_et_long(now_utc: datetime | None = None) -> str:
    """``Apr 26, 2026 @ 14:23 ET`` — used at the top of trade
    notifications."""
    n = (now_utc or datetime.now(UTC)).astimezone(ET)
    return n.strftime("%b %d, %Y @ %H:%M ET")


def now_et_short(now_utc: datetime | None = None) -> str:
    """``14:23 ET`` — used inline (e.g. published_time_et)."""
    n = (now_utc or datetime.now(UTC)).astimezone(ET)
    return n.strftime("%H:%M ET")


def format_et_long(iso_ts: str) -> str:
    """Render an ISO-8601 UTC string as ``Apr 26, 2026 @ 14:23 ET``.

    Returns ``"unknown"`` on a missing or unparseable timestamp so the
    template caller doesn't have to guard against it."""
    parsed = _parse_iso(iso_ts)
    if parsed is None:
        return "unknown"
    return parsed.astimezone(ET).strftime("%b %d, %Y @ %H:%M ET")


def format_et_short(iso_ts: str) -> str:
    """Render an ISO-8601 UTC string as ``14:23 ET``."""
    parsed = _parse_iso(iso_ts)
    if parsed is None:
        return "unknown"
    return parsed.astimezone(ET).strftime("%H:%M ET")


def humanize_age_since(iso_ts: str, now_utc: datetime | None = None) -> str:
    """``5 min``, ``2 h``, ``3 d`` — age of a past timestamp.

    Returns ``"just now"`` for < 60 s, ``"unknown"`` if the timestamp
    is missing / unparseable, and capped at days for older. Used for
    "(published 2 h ago)" and "trade placed 12 s after signal".
    """
    parsed = _parse_iso(iso_ts)
    if parsed is None:
        return "unknown"
    n = now_utc or datetime.now(UTC)
    seconds = int((n - parsed).total_seconds())
    if seconds < 60:
        return "just now" if seconds <= 5 else f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    days = hours // 24
    return f"{days} d"


def _parse_iso(iso_ts: str | None) -> datetime | None:
    if not iso_ts:
        return None
    s = iso_ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Article links
# ---------------------------------------------------------------------------


def article_link_markdown(url: str) -> str:
    """Render an article URL as a Telegram-Markdown link with paywall
    or @-handle annotation.

    Examples:
        ``[reuters.com/world/...](https://reuters.com/world/foo)``
        ``[nytimes.com/2026/04/...](https://nytimes.com/2026/04/foo) (paywall)``
        ``[twitter.com/...](https://twitter.com/...) (@account)``

    Empty / invalid URL renders as ``"(no article link)"``.
    """
    if not url:
        return "(no article link)"
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if not host:
        return "(no article link)"

    # Display text: strip the scheme + truncate if obnoxiously long.
    display = url.split("://", 1)[-1]
    if len(display) > ARTICLE_LINK_MAX_CHARS:
        display = display[: ARTICLE_LINK_MAX_CHARS - 3] + "..."

    annotation = ""
    bare_host = host.removeprefix("www.")
    if host in _PAYWALLED_HOSTS or bare_host in {h.removeprefix("www.") for h in _PAYWALLED_HOSTS}:
        annotation = " (paywall)"
    elif host in _TWITTER_HOSTS or bare_host in {h.removeprefix("www.") for h in _TWITTER_HOSTS}:
        # Try to extract @handle from "/handle/status/...".
        path_segs = [p for p in (parsed.path or "").split("/") if p]
        annotation = f" (@{path_segs[0]})" if path_segs else " (X / Twitter)"
    return f"[{display}]({url}){annotation}"


# ---------------------------------------------------------------------------
# Key-quote rendering
# ---------------------------------------------------------------------------


def render_key_quote(quote: str, max_chars: int = KEY_QUOTE_MAX_CHARS) -> str:
    """Truncate a quote to ``max_chars`` at a word boundary if possible.

    Strips outer whitespace. Returns ``"(no quote)"`` if the input is
    empty after stripping; the LLM is supposed to provide one but the
    field is defaulted to "" so back-compat rows reach this path."""
    cleaned = (quote or "").strip()
    if not cleaned:
        return "(no quote)"
    if len(cleaned) <= max_chars:
        return cleaned
    truncated = cleaned[:max_chars]
    # Try to back off to the last whitespace within the truncation.
    space = truncated.rfind(" ")
    if space > max_chars * 0.6:  # only if backing off doesn't lose too much
        truncated = truncated[:space]
    return truncated.rstrip() + "..."


# ---------------------------------------------------------------------------
# P&L math (Deliverable 8)
# ---------------------------------------------------------------------------


def compute_settlement_pnl(
    *,
    quantity: int,
    cost_basis_cents: int,
    entry_fees_cents: int,
) -> tuple[int, int, int, int]:
    """Return ``(settlement_cents, exit_fees_cents, net_profit_cents, roi_bps)``
    assuming a YES resolution at $1.00.

    All inputs are integer cents. ``roi_bps`` is basis points
    (10000 = 100%); the caller divides by 100 to render a percent.
    """
    settlement_cents = quantity * 100
    exit_fees_cents = calculate_exit_fee_cents(price_cents=100, quantity=quantity)
    net_profit_cents = settlement_cents - cost_basis_cents - entry_fees_cents - exit_fees_cents
    roi_bps = 0 if cost_basis_cents <= 0 else (net_profit_cents * 10_000) // cost_basis_cents
    return settlement_cents, exit_fees_cents, net_profit_cents, roi_bps


def compute_potential_loss_cents(
    *,
    quantity: int,
    cost_basis_cents: int,
    entry_fees_cents: int,
    entry_price_cents: int,
    yes_bid_levels: Sequence[tuple[int, int]] | None,
    stop_drop_cents: int = 50,
) -> int:
    """Estimate the dollar loss if the stop-loss fires.

    The stop fires when the YES bid falls ``stop_drop_cents`` below
    entry. We exit at market by walking the bid side; the proceeds
    are quantity x walked-avg-price minus exit fees, minus the
    cost_basis + entry_fees.

    When the live bid book isn't available (``yes_bid_levels`` None or
    empty) we fall back to a worst-case ``estimated_stop_price =
    max(1, entry_price - stop_drop_cents)``, simulating exiting the
    full quantity at that uniform price.

    Returns a POSITIVE integer cents value (the magnitude of the
    expected loss). Zero or negative inputs return 0 to keep the
    template arithmetic well-defined.
    """
    if quantity <= 0 or cost_basis_cents <= 0:
        return 0

    # Walk the bid side for ``quantity`` contracts, taking the
    # highest-priced bids first. yes_bid_levels are pairs of
    # (price_cents, quantity_at_level).
    bid_walk_avg: int | None = None
    if yes_bid_levels:
        sorted_bids = sorted(yes_bid_levels, key=lambda lv: -lv[0])
        remaining = quantity
        cost = 0
        for price_c, qty_at_level in sorted_bids:
            if remaining <= 0:
                break
            take = min(remaining, qty_at_level)
            cost += take * price_c
            remaining -= take
        if remaining < quantity:
            filled = quantity - remaining
            bid_walk_avg = (cost // filled) if filled > 0 else None

    if bid_walk_avg is None:
        # Fallback: assume the stop fires at the simple linear floor.
        bid_walk_avg = max(1, entry_price_cents - stop_drop_cents)

    exit_proceeds = quantity * bid_walk_avg
    exit_fees = calculate_exit_fee_cents(price_cents=bid_walk_avg, quantity=quantity)
    net = cost_basis_cents + entry_fees_cents - exit_proceeds + exit_fees
    return max(0, net)


# ---------------------------------------------------------------------------
# Dollars / cents formatters
# ---------------------------------------------------------------------------


def dollars(cents: int) -> str:
    """``$12.34`` (no sign)."""
    return f"${Decimal(cents) / 100:.2f}"


def dollars_signed(cents: int) -> str:
    """``+$12.34`` or ``-$12.34``."""
    sign = "+" if cents >= 0 else "-"
    return f"{sign}${abs(Decimal(cents)) / 100:.2f}"


def percent_from_bps(bps: int) -> str:
    """``+12.34%`` or ``-1.23%``."""
    pct = Decimal(bps) / 100
    sign = "+" if pct >= 0 else "-"
    return f"{sign}{abs(pct):.2f}%"


__all__ = [
    "ARTICLE_LINK_MAX_CHARS",
    "KEY_QUOTE_MAX_CHARS",
    "article_link_markdown",
    "compute_potential_loss_cents",
    "compute_settlement_pnl",
    "dollars",
    "dollars_signed",
    "format_et_long",
    "format_et_short",
    "humanize_age_since",
    "now_et_long",
    "now_et_short",
    "percent_from_bps",
    "render_key_quote",
]
