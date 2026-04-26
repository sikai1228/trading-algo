"""Phase 4 Part 2.11 — render-side helpers.

Pinned behavior:
- ET timestamp formatting (long, short, age).
- Article-link Markdown with paywall + @-handle annotation.
- Key-quote truncation at word boundary.
- Settlement P&L math (cents, integer, includes exit fee).
- Potential-loss walk and the no-bidbook fallback.
- Dollars / signed dollars / percent_from_bps formatters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trumpbot.notifications.trade_render import (
    article_link_markdown,
    compute_potential_loss_cents,
    compute_settlement_pnl,
    dollars,
    dollars_signed,
    format_et_long,
    format_et_short,
    humanize_age_since,
    now_et_long,
    now_et_short,
    percent_from_bps,
    render_key_quote,
)

# ---------------------------------------------------------------------------
# ET timestamps
# ---------------------------------------------------------------------------


class TestETTimestamps:
    def test_now_et_long_format_shape(self) -> None:
        s = now_et_long(datetime(2026, 4, 26, 18, 23, tzinfo=UTC))
        # 18:23 UTC = 14:23 ET (EDT in late April)
        assert s == "Apr 26, 2026 @ 14:23 ET"

    def test_now_et_short_format_shape(self) -> None:
        assert now_et_short(datetime(2026, 4, 26, 18, 23, tzinfo=UTC)) == "14:23 ET"

    def test_format_et_long_handles_z_suffix(self) -> None:
        assert format_et_long("2026-04-26T18:23:00Z") == "Apr 26, 2026 @ 14:23 ET"

    def test_format_et_long_handles_tz_offset(self) -> None:
        assert format_et_long("2026-04-26T18:23:00+00:00") == "Apr 26, 2026 @ 14:23 ET"

    def test_format_et_short_handles_z_suffix(self) -> None:
        assert format_et_short("2026-04-26T18:23:00Z") == "14:23 ET"

    def test_format_et_unknown_on_garbage(self) -> None:
        assert format_et_long("not a timestamp") == "unknown"
        assert format_et_short("") == "unknown"

    def test_humanize_age_seconds(self) -> None:
        n = datetime(2026, 4, 26, 12, 0, 30, tzinfo=UTC)
        assert humanize_age_since("2026-04-26T12:00:00Z", now_utc=n) == "30 s"

    def test_humanize_age_just_now(self) -> None:
        n = datetime(2026, 4, 26, 12, 0, 3, tzinfo=UTC)
        assert humanize_age_since("2026-04-26T12:00:00Z", now_utc=n) == "just now"

    def test_humanize_age_minutes(self) -> None:
        n = datetime(2026, 4, 26, 12, 30, tzinfo=UTC)
        assert humanize_age_since("2026-04-26T12:00:00Z", now_utc=n) == "30 min"

    def test_humanize_age_hours(self) -> None:
        n = datetime(2026, 4, 26, 14, 0, tzinfo=UTC)
        assert humanize_age_since("2026-04-26T12:00:00Z", now_utc=n) == "2 h"

    def test_humanize_age_days(self) -> None:
        n = datetime(2026, 4, 28, 12, 0, tzinfo=UTC) + timedelta(hours=1)
        assert humanize_age_since("2026-04-26T12:00:00Z", now_utc=n) == "2 d"

    def test_humanize_age_unknown_on_garbage(self) -> None:
        assert humanize_age_since("") == "unknown"
        assert humanize_age_since(None) == "unknown"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Article links
# ---------------------------------------------------------------------------


class TestArticleLinkMarkdown:
    def test_plain_link(self) -> None:
        out = article_link_markdown("https://reuters.com/world/foo")
        assert out.startswith("[")
        assert "](https://reuters.com/world/foo)" in out
        assert "(paywall)" not in out
        assert "(@" not in out

    def test_paywall_annotation(self) -> None:
        out = article_link_markdown("https://nytimes.com/2026/04/foo")
        assert "(paywall)" in out

    def test_paywall_www_variant(self) -> None:
        out = article_link_markdown("https://www.wsj.com/articles/foo")
        assert "(paywall)" in out

    def test_twitter_handle_annotation(self) -> None:
        out = article_link_markdown("https://twitter.com/realDonaldTrump/status/123")
        assert "(@realDonaldTrump)" in out

    def test_x_dot_com_handle(self) -> None:
        out = article_link_markdown("https://x.com/SecretarySomeone/status/456")
        assert "(@SecretarySomeone)" in out

    def test_long_url_display_truncated(self) -> None:
        long_path = "/x" * 200  # 400 chars
        url = f"https://example.com{long_path}"
        out = article_link_markdown(url)
        # The full URL still appears in the link target.
        assert f"({url})" in out
        # The display text is bounded.
        display = out.split("](")[0][1:]
        assert len(display) <= 300

    def test_empty_url(self) -> None:
        assert article_link_markdown("") == "(no article link)"


# ---------------------------------------------------------------------------
# Key-quote rendering
# ---------------------------------------------------------------------------


class TestRenderKeyQuote:
    def test_short_quote_passes_through(self) -> None:
        assert render_key_quote("Hello world.") == "Hello world."

    def test_strips_outer_whitespace(self) -> None:
        assert render_key_quote("   Hi.   ") == "Hi."

    def test_empty_returns_placeholder(self) -> None:
        assert render_key_quote("") == "(no quote)"
        assert render_key_quote("   ") == "(no quote)"

    def test_truncates_at_word_boundary(self) -> None:
        s = "word " * 100  # 500 chars-ish
        out = render_key_quote(s, max_chars=50)
        assert len(out) <= 53  # 50 + "..."
        assert out.endswith("...")

    def test_unicode_passthrough(self) -> None:
        s = "Trump met Pope Francis at the Vatican — readout follows."
        assert render_key_quote(s) == s

    def test_quotes_within_quotes_preserved(self) -> None:
        s = 'Trump said: "I called him this morning."'
        assert render_key_quote(s) == s

    def test_long_unicode_truncated(self) -> None:
        s = "héllo " * 100
        out = render_key_quote(s, max_chars=30)
        assert out.endswith("...")
        assert len(out) <= 33


# ---------------------------------------------------------------------------
# P&L math
# ---------------------------------------------------------------------------


class TestSettlementPnl:
    def test_basic_yes_settlement(self) -> None:
        # Bought 10 contracts at 60c, fees $0.05.
        settlement, exit_fees, profit, roi_bps = compute_settlement_pnl(
            quantity=10,
            cost_basis_cents=600,
            entry_fees_cents=5,
        )
        assert settlement == 1000
        # Exit fee at 100c is 0 (Kalshi formula caps at extremes).
        assert exit_fees == 0
        # Profit = 1000 - 600 - 5 - 0 = 395
        assert profit == 395
        # ROI = 395 / 600 = 65.83%; 65.83 * 100 = 6583 bps
        assert roi_bps == 6583

    def test_zero_cost_basis_safe(self) -> None:
        _, _, _, roi_bps = compute_settlement_pnl(
            quantity=0, cost_basis_cents=0, entry_fees_cents=0
        )
        assert roi_bps == 0


class TestPotentialLoss:
    def test_with_thick_bid_book(self) -> None:
        # Bought 10 @ 60c. Bids at 30/35/40 cents, plenty of depth.
        bid_levels = [(40, 5), (35, 10), (30, 100)]
        loss = compute_potential_loss_cents(
            quantity=10,
            cost_basis_cents=600,
            entry_fees_cents=5,
            entry_price_cents=60,
            yes_bid_levels=bid_levels,
        )
        # Walk: take 5 @ 40c + 5 @ 35c = 200 + 175 = 375 / 10 = 37c avg
        # Exit proceeds = 10 * 37 = 370. Exit fees ~ small.
        # Loss = 600 + 5 - 370 + fees -> roughly 235 + fees
        assert loss > 200
        assert loss < 300

    def test_no_bid_book_falls_back_to_floor(self) -> None:
        # No bids -> fall back to entry - 50c floor.
        loss = compute_potential_loss_cents(
            quantity=10,
            cost_basis_cents=600,
            entry_fees_cents=5,
            entry_price_cents=60,
            yes_bid_levels=None,
        )
        # Floor = max(1, 60-50) = 10c. Proceeds = 100. Loss = 605 - 100 + fees
        assert loss > 500

    def test_zero_quantity_returns_zero(self) -> None:
        assert (
            compute_potential_loss_cents(
                quantity=0,
                cost_basis_cents=0,
                entry_fees_cents=0,
                entry_price_cents=50,
                yes_bid_levels=None,
            )
            == 0
        )


# ---------------------------------------------------------------------------
# Money formatters
# ---------------------------------------------------------------------------


class TestMoneyFormatters:
    def test_dollars(self) -> None:
        assert dollars(1234) == "$12.34"
        assert dollars(0) == "$0.00"
        assert dollars(1_000_000) == "$10000.00"

    def test_dollars_signed(self) -> None:
        assert dollars_signed(1234) == "+$12.34"
        assert dollars_signed(-1234) == "-$12.34"
        assert dollars_signed(0) == "+$0.00"

    def test_percent_from_bps(self) -> None:
        assert percent_from_bps(1234) == "+12.34%"
        assert percent_from_bps(-1234) == "-12.34%"
        assert percent_from_bps(0) == "+0.00%"
