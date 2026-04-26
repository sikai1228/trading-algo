"""Categorized alert dispatcher.

Phase 3 Part 2.

Three severity tiers, each with a fixed Telegram-notification policy:

- **alert_critical** -- audible push (``disable_notification=False``).
  For events that need eyes immediately: LLM cap exceeded, Kalshi feed
  disconnected, Anthropic 401, daemon crash detected, contract rules
  changed.
- **alert_warning** -- silent (``disable_notification=True``). Things
  worth knowing about today but not urgent: source down >30 min, slow
  DB query, risk-rejected trade.
- **alert_info** -- silent. Routine events worth a heads-up: new month
  discovered, subject aliases enriched, LLM spend at 50 % of cap,
  source recovered.

Two integration points:

1. **Templates.** Alert text comes from
   :func:`trumpbot.notifications.templates.render_template`. The
   template's ``audible`` field determines the Telegram setting; the
   alert sender just trusts it.
2. **Audit log.** Every alert is also written to ``system_events``
   with the corresponding severity so the future observability
   backend has a unified timeline of what happened.

Optional ``dedup_key``: if supplied, the dispatcher consults
``alert_dedup`` and refuses to send a duplicate within
``dedup_window_seconds`` (default 1 hour). Used so a flapping news
source doesn't fire 12 alerts an hour.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trumpbot.db.connection import Database
from trumpbot.db.repositories import claim_alert_send, insert_system_event
from trumpbot.notifications.templates import (
    Category,
    RenderedMessage,
    render_template,
)
from trumpbot.utils.logging import get_logger

log = get_logger(__name__)


# Map alert categories to system_events.severity values.
_SEVERITY_FOR_CATEGORY: dict[Category, str] = {
    "alert_critical": "critical",
    "alert_warning": "warning",
    "alert_info": "info",
}


# A Telegram-send callable. Two args: the rendered text and a flag for
# whether the message should be audible (False -> silent).
TelegramSendFn = Callable[[str, bool], Awaitable[None]]


class AlertDispatcher:
    """Renders, dedups, persists, and sends categorized alerts.

    The dispatcher is constructed once at daemon startup with a
    :class:`Database` handle, a Telegram-send callable, and the dedup
    window in seconds. Components throughout the codebase call
    :meth:`send` with a template name + data dict; severity and
    audibility flow from the template definition.
    """

    def __init__(
        self,
        *,
        db: Database,
        send_fn: TelegramSendFn | None,
        dedup_window_seconds: int = 3600,
    ) -> None:
        self._db = db
        self._send_fn = send_fn
        self._dedup_window = dedup_window_seconds

    async def send(
        self,
        *,
        template_name: str,
        data: dict[str, Any],
        dedup_key: str | None = None,
        component: str = "alerts",
    ) -> RenderedMessage | None:
        """Render the template, dedup, audit-log, and send to Telegram.

        Returns the :class:`RenderedMessage` on success or ``None`` if
        the alert was suppressed by dedup. Telegram send failures are
        logged but do not raise -- the audit row is still written so
        the operational record is preserved even when Telegram is
        down.
        """
        rendered = render_template(template_name, data)
        if rendered.category not in _SEVERITY_FOR_CATEGORY:
            raise ValueError(
                f"template {template_name!r} has category {rendered.category!r} "
                f"which is not an alert category; use Telegram bot directly"
            )

        if dedup_key is not None:
            should_send = claim_alert_send(
                self._db,
                dedup_key=dedup_key,
                category=rendered.category,
                window_seconds=self._dedup_window,
            )
            if not should_send:
                log.info(
                    "alert_deduped",
                    template=template_name,
                    dedup_key=dedup_key,
                    window_sec=self._dedup_window,
                )
                return None

        # Audit-log first so the record exists even if Telegram fails.
        insert_system_event(
            self._db,
            event_type=template_name,
            severity=_SEVERITY_FOR_CATEGORY[rendered.category],
            component=component,
            message=rendered.text,
            detail={"dedup_key": dedup_key} if dedup_key else None,
        )

        if self._send_fn is None:
            log.info(
                "alert_send_skipped_no_telegram",
                template=template_name,
                category=rendered.category,
            )
            return rendered

        try:
            await self._send_fn(rendered.text, rendered.audible)
        except Exception as exc:  # pragma: no cover -- defensive
            log.error(
                "alert_send_failed",
                template=template_name,
                error=repr(exc),
            )
        return rendered


__all__ = ["AlertDispatcher", "TelegramSendFn"]
