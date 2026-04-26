"""Minimal Telegram notification helper.

Phase 1 only needs one-way notifications (heads-up to the user when
the discovery service detects a new month). The full bidirectional
approval flow lands in Phase 3 with the proper Telegram bot.

If neither ``bot_token`` nor ``chat_id`` is configured the notifier
silently no-ops: the daemon must run without a Telegram setup, and
notifications are operational glue, not a blocking dependency.
"""

from __future__ import annotations

import httpx

from trumpbot.utils.logging import get_logger

log = get_logger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(
        self,
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_sec: float = 10.0,
    ) -> None:
        self._bot_token = bot_token or None
        self._chat_id = chat_id or None
        self._enabled = bool(self._bot_token and self._chat_id)
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_sec)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, text: str) -> bool:
        """POST a plain-text message. Returns True on a 200 from Telegram."""
        if not self._enabled:
            log.warning("telegram_notifier_disabled", message_preview=text[:120])
            return False
        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text}
        try:
            response = await self._http.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.error("telegram_notifier_http_error", error=repr(exc))
            return False
        if response.status_code != 200:
            log.error(
                "telegram_notifier_send_failed",
                status=response.status_code,
                body=response.text[:200],
            )
            return False
        return True

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
