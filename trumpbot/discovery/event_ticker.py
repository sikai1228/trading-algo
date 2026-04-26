"""Event-ticker computation for monthly Kalshi events.

Kalshi's monthly KXTRUMPMEET events follow the pattern
``KXTRUMPMEET-{YY}{MMM}`` where ``YY`` is the two-digit year and
``MMM`` is the three-letter month abbreviation in uppercase.

Examples:
    2026-04-25 → KXTRUMPMEET-26APR
    2026-12-15 current → KXTRUMPMEET-26DEC, next → KXTRUMPMEET-27JAN

This module is a pure function from (date, series) → ticker. It does
not consult Kalshi or the database; the discovery service combines it
with REST calls to determine which events are actually open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

DEFAULT_SERIES = "KXTRUMPMEET"

_MONTH_ABBR = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)


@dataclass(frozen=True)
class EventTicker:
    """Fully-qualified Kalshi monthly event identifier."""

    series: str
    year: int  # 4-digit, e.g. 2026
    month: int  # 1..12

    @property
    def yy(self) -> str:
        return f"{self.year % 100:02d}"

    @property
    def mmm(self) -> str:
        if not 1 <= self.month <= 12:
            raise ValueError(f"month out of range: {self.month}")
        return _MONTH_ABBR[self.month - 1]

    def __str__(self) -> str:
        return f"{self.series}-{self.yy}{self.mmm}"


def event_ticker_for(year: int, month: int, series: str = DEFAULT_SERIES) -> EventTicker:
    """Return the EventTicker for the given (series, year, month)."""
    return EventTicker(series=series, year=year, month=month)


def current_event_ticker(today: date, series: str = DEFAULT_SERIES) -> EventTicker:
    """The event ticker covering the month that ``today`` falls in."""
    return event_ticker_for(today.year, today.month, series)


def next_month_event_ticker(today: date, series: str = DEFAULT_SERIES) -> EventTicker:
    """The event ticker for the calendar month immediately after ``today``'s.

    Handles December → January year-rollover.
    """
    if today.month == 12:
        return event_ticker_for(today.year + 1, 1, series)
    return event_ticker_for(today.year, today.month + 1, series)


def is_late_month(today: date) -> bool:
    """True from the 25th of the month onwards.

    Used by the discovery service to fire the additional
    ``next_month_event_opened`` / ``next_month_event_not_yet_open``
    audit log on day-25 onward.
    """
    return today.day >= 25
