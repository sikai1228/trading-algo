"""Polling-based Telegram bot for the Phase-2 approval flow.

Built on :mod:`python-telegram-bot` (already pinned in pyproject.toml).
Just enough surface to:

- Send a message with an inline ``[APPROVE] [REJECT]`` keyboard
- Parse the user's button press
- Resolve a Future the :class:`ApprovalGate` is awaiting
- Honor the per-intent-type timeout (or no-timeout)
- Validate ``chat_id`` against the allowlist (single chat in v1)

Out of scope (Phase 3): ``/halt``, ``/resume``, ``/status``,
``/positions``, heartbeat messages, error alerts, command parsing
beyond approve/reject.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from trumpbot.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class _PendingApproval:
    intent_id: str
    intent_type: str
    chat_id: int
    message_id: int
    future: asyncio.Future[tuple[str, str]]
    """Resolves to (decision, decision_source)."""


class TelegramApprovalBot:
    """Thin async wrapper around the python-telegram-bot library.

    Not started/stopped automatically — callers (the daemon) lifecycle
    it explicitly. Tests typically don't instantiate this; they use a
    stub :class:`ApprovalRequester` instead.
    """

    chat_id: str | None  # advertised so ApprovalGate can record it

    def __init__(self, *, bot_token: str, chat_id: str) -> None:
        self._token = bot_token
        self.chat_id = chat_id
        self._chat_id_int = int(chat_id)
        # Application is generic in newer python-telegram-bot; we hold
        # it as Any to avoid pinning the precise generic params here.
        self._app: object | None = None
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        from telegram.ext import (
            ApplicationBuilder,
            CallbackQueryHandler,
        )

        builder = ApplicationBuilder().token(self._token)
        app = builder.build()
        app.add_handler(CallbackQueryHandler(self._on_button))
        await app.initialize()
        await app.start()
        if app.updater is not None:
            await app.updater.start_polling(drop_pending_updates=True)
        self._app = app
        log.info("telegram_bot_started")

    async def stop(self) -> None:
        if self._app is None:
            return
        app = self._app
        with contextlib.suppress(Exception):
            if app.updater is not None:  # type: ignore[attr-defined]
                await app.updater.stop()  # type: ignore[attr-defined]
            await app.stop()  # type: ignore[attr-defined]
            await app.shutdown()  # type: ignore[attr-defined]
        self._app = None
        log.info("telegram_bot_stopped")

    # -- ApprovalRequester protocol -----------------------------------

    async def send_request(self, *, intent_id: str, intent_type: str, message_text: str) -> int:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if self._app is None:
            raise RuntimeError("TelegramApprovalBot.start() has not been called")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve|{intent_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject|{intent_id}"),
                ]
            ]
        )
        message = await self._app.bot.send_message(  # type: ignore[attr-defined]
            chat_id=self._chat_id_int,
            text=message_text,
            reply_markup=keyboard,
        )
        async with self._lock:
            self._pending[intent_id] = _PendingApproval(
                intent_id=intent_id,
                intent_type=intent_type,
                chat_id=message.chat.id,
                message_id=message.message_id,
                future=asyncio.get_event_loop().create_future(),
            )
        return int(message.message_id)

    async def await_response(self, *, intent_id: str, timeout_sec: int | None) -> tuple[str, str]:
        async with self._lock:
            pending = self._pending.get(intent_id)
        if pending is None:
            raise RuntimeError(f"no pending approval for {intent_id}")
        try:
            if timeout_sec is None:
                # Unlimited (stop-loss / reentry per the locked rules).
                return await pending.future
            return await asyncio.wait_for(pending.future, timeout=timeout_sec)
        except TimeoutError:
            return ("expired", "timeout")
        finally:
            async with self._lock:
                self._pending.pop(intent_id, None)

    # -- Telegram callback --------------------------------------------

    async def _on_button(self, update, _context) -> None:  # type: ignore[no-untyped-def]
        query = update.callback_query
        if query is None:
            return
        await query.answer()  # acknowledge so the loading spinner stops
        if query.message is None or query.message.chat.id != self._chat_id_int:
            log.warning(
                "telegram_button_from_unauthorized_chat",
                chat_id=getattr(query.message, "chat", None),
            )
            return
        data = query.data or ""
        try:
            decision, intent_id = data.split("|", 1)
        except ValueError:
            return
        if decision not in {"approve", "reject"}:
            return
        async with self._lock:
            pending = self._pending.pop(intent_id, None)
        if pending is None or pending.future.done():
            return
        mapped = "approved" if decision == "approve" else "rejected"
        pending.future.set_result((mapped, "telegram_button"))
        # Strip the keyboard so the same press can't fire twice.
        with contextlib.suppress(Exception):
            await query.edit_message_reply_markup(reply_markup=None)


__all__ = ["TelegramApprovalBot"]
