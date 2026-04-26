"""Unit tests for trumpbot.utils.*"""

from __future__ import annotations

from datetime import UTC, datetime

from trumpbot.utils.timeutil import parse_iso, parse_iso_to_str, to_iso, utcnow_iso
from trumpbot.utils.url import canonicalize_url


class TestTime:
    def test_utcnow_iso_format(self) -> None:
        s = utcnow_iso()
        assert s.endswith("Z")
        assert "T" in s

    def test_to_iso_naive_datetime_treated_as_utc(self) -> None:
        dt = datetime(2026, 4, 25, 12, 0, 0)
        assert to_iso(dt).startswith("2026-04-25T12:00:00")

    def test_to_iso_aware_datetime_converted(self) -> None:
        dt = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        assert to_iso(dt) == "2026-04-25T12:00:00.000000Z"

    def test_parse_iso_handles_z(self) -> None:
        dt = parse_iso("2026-04-25T12:00:00Z")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.year == 2026

    def test_parse_iso_handles_rfc822(self) -> None:
        dt = parse_iso("Mon, 25 Apr 2026 12:00:00 GMT")
        assert dt is not None
        assert dt.year == 2026

    def test_parse_iso_handles_none(self) -> None:
        assert parse_iso(None) is None
        assert parse_iso("") is None
        assert parse_iso_to_str(None) is None

    def test_parse_iso_to_str_canonicalizes(self) -> None:
        out = parse_iso_to_str("2026-04-25T12:00:00+02:00")
        assert out == "2026-04-25T10:00:00.000000Z"


class TestUrl:
    def test_strips_utm(self) -> None:
        url = "https://example.com/article?utm_source=x&utm_medium=y&id=42"
        assert canonicalize_url(url) == "https://example.com/article?id=42"

    def test_lowercases_host(self) -> None:
        assert canonicalize_url("HTTPS://Example.COM/Article") == "https://example.com/Article"

    def test_strips_fragment(self) -> None:
        assert canonicalize_url("https://example.com/a#section-1") == "https://example.com/a"

    def test_strips_trailing_slash(self) -> None:
        assert canonicalize_url("https://example.com/a/") == "https://example.com/a"
        assert canonicalize_url("https://example.com/") == "https://example.com/"

    def test_sorts_remaining_query(self) -> None:
        url = "https://example.com/a?z=1&a=2&fbclid=foo"
        assert canonicalize_url(url) == "https://example.com/a?a=2&z=1"
