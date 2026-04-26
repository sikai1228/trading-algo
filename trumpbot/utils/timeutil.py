"""ISO 8601 UTC time helpers. The system uses UTC strings everywhere."""

from __future__ import annotations

from datetime import UTC, datetime

from dateutil import parser as dateutil_parser

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    """Return current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def utcnow_iso() -> str:
    """Return current time as an ISO 8601 UTC string with milliseconds."""
    return _format_iso(utcnow())


def _format_iso(dt: datetime) -> str:
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.strftime(ISO_FORMAT)


def to_iso(dt: datetime) -> str:
    """Format any datetime as an ISO 8601 UTC string."""
    return _format_iso(dt)


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-ish timestamp into a UTC-aware datetime, or None."""
    if value is None or value == "":
        return None
    parsed = dateutil_parser.parse(value)
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return parsed


def parse_iso_to_str(value: str | None) -> str | None:
    """Parse and re-emit a timestamp in canonical ISO UTC form."""
    parsed = parse_iso(value)
    return None if parsed is None else _format_iso(parsed)
