"""Typed exception hierarchy for Kalshi API errors.

Three categories with distinct handling per CLAUDE.md:

- ``TransientError``: network errors, 5xx. Caller retries with
  exponential backoff (max 3 attempts).
- ``ValidationError``: 400/422 / Pydantic schema mismatch / malformed
  payload. Caller logs full request and response, re-raises, no retry.
- ``StateError``: insufficient funds, market closed, account suspended.
  Caller halts the affected service, writes a system_event row,
  alerts.

Always catch specific subclasses, never bare ``except``.
"""

from __future__ import annotations

from typing import Any


class KalshiError(Exception):
    """Base class for every error raised by the Kalshi client."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_method: str | None = None,
        request_path: str | None = None,
        response_body: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_method = request_method
        self.request_path = request_path
        self.response_body = response_body


class TransientError(KalshiError):
    """Network failure or 5xx. Safe to retry with backoff."""


class ValidationError(KalshiError):
    """Client-side schema mismatch or 4xx (excluding state-related). Bug; do not retry."""


class StateError(KalshiError):
    """Account or market state prevents the request (insufficient funds, closed market).

    Halt the affected service; require manual intervention.
    """
