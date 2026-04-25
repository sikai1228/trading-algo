"""Tests for event-ticker date arithmetic."""

from __future__ import annotations

from datetime import date

import pytest

from trumpbot.discovery.event_ticker import (
    DEFAULT_SERIES,
    EventTicker,
    current_event_ticker,
    event_ticker_for,
    is_late_month,
    next_month_event_ticker,
)


class TestRender:
    @pytest.mark.parametrize(
        ("year", "month", "expected"),
        [
            (2026, 1, "KXTRUMPMEET-26JAN"),
            (2026, 4, "KXTRUMPMEET-26APR"),
            (2026, 12, "KXTRUMPMEET-26DEC"),
            (2027, 1, "KXTRUMPMEET-27JAN"),
            (2099, 9, "KXTRUMPMEET-99SEP"),
        ],
    )
    def test_str_renders_yy_and_mmm(self, year: int, month: int, expected: str) -> None:
        assert str(EventTicker(series=DEFAULT_SERIES, year=year, month=month)) == expected

    def test_invalid_month_raises(self) -> None:
        with pytest.raises(ValueError):
            _ = EventTicker(series=DEFAULT_SERIES, year=2026, month=13).mmm


class TestCurrent:
    @pytest.mark.parametrize(
        ("today", "expected"),
        [
            (date(2026, 1, 1), "KXTRUMPMEET-26JAN"),
            (date(2026, 3, 31), "KXTRUMPMEET-26MAR"),
            (date(2026, 4, 25), "KXTRUMPMEET-26APR"),
            (date(2026, 12, 31), "KXTRUMPMEET-26DEC"),
        ],
    )
    def test_current(self, today: date, expected: str) -> None:
        assert str(current_event_ticker(today)) == expected


class TestNextMonth:
    @pytest.mark.parametrize(
        ("today", "expected"),
        [
            (date(2026, 1, 1), "KXTRUMPMEET-26FEB"),
            (date(2026, 4, 25), "KXTRUMPMEET-26MAY"),
            (date(2026, 11, 30), "KXTRUMPMEET-26DEC"),
            # Year rollover
            (date(2026, 12, 1), "KXTRUMPMEET-27JAN"),
            (date(2026, 12, 31), "KXTRUMPMEET-27JAN"),
        ],
    )
    def test_rolls_over_year(self, today: date, expected: str) -> None:
        assert str(next_month_event_ticker(today)) == expected


class TestLateMonth:
    @pytest.mark.parametrize(
        ("today", "expected"),
        [
            (date(2026, 4, 1), False),
            (date(2026, 4, 24), False),
            (date(2026, 4, 25), True),
            (date(2026, 4, 30), True),
            (date(2026, 12, 31), True),
        ],
    )
    def test_threshold(self, today: date, expected: bool) -> None:
        assert is_late_month(today) is expected


class TestSeriesParameter:
    def test_alternate_series(self) -> None:
        assert str(event_ticker_for(2026, 4, series="KXTRUMPCALL")) == "KXTRUMPCALL-26APR"
