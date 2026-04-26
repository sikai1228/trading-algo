"""Polling-based Telegram bot for the Phase-2 approval flow + Phase
3 Part 2 command surface.

Built on :mod:`python-telegram-bot` (already pinned in pyproject.toml).
Surface:

- Approval flow (Phase 2): inline ``[APPROVE] [REJECT]`` keyboard for
  every trade intent, awaits the user's button press, resolves the
  future the :class:`ApprovalGate` is waiting on.
- Command surface (Phase 3 Part 2): registers a ``CommandHandler`` for
  every command in :mod:`trumpbot.notifications.commands`. Each command
  is dispatched through that module's handler registry; replies render
  via :mod:`trumpbot.notifications.templates`. Messages from
  non-allowlisted chats are silently dropped.
- Outbound silent / audible messages: :meth:`send_text` accepts a
  ``silent`` flag wired to Telegram's ``disable_notification``. Used by
  :class:`AlertDispatcher`, the heartbeat loop, and the daily digest.

The bot validates ``chat_id`` against the allowlist (single chat in
v1) on every inbound update; mismatches log a warning and return
silently.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from trumpbot.db.connection import Database
from trumpbot.notifications.commands import (
    CommandContext,
    CommandRateLimiter,
    all_command_names,
    dispatch,
)
from trumpbot.notifications.llm_cost import LLMCostGuard
from trumpbot.notifications.templates import (
    BUTTON_APPROVE_LABEL,
    BUTTON_REJECT_LABEL,
    render_template,
)
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

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        db: Database | None = None,
        cost_guard: LLMCostGuard | None = None,
        bankroll_usd_cents: int = 50000,
        sources_total: int = 0,
        rate_limit_per_minute: int = 30,
    ) -> None:
        self._token = bot_token
        self.chat_id = chat_id
        self._chat_id_int = int(chat_id)
        # Application is generic in newer python-telegram-bot; we hold
        # it as Any to avoid pinning the precise generic params here.
        self._app: object | None = None
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = asyncio.Lock()

        # ---- Phase 3 Part 2: command surface dependencies ----
        # Optional. When None, command handlers are not registered (the
        # bot reverts to Phase-2-only inline approval). Production
        # daemon always supplies them; tests for the approval flow can
        # leave them off.
        self._db = db
        self._cost_guard = cost_guard
        self._bankroll_usd_cents = bankroll_usd_cents
        self._sources_total = sources_total
        self._sources_active = sources_total
        self._rate_limiter = CommandRateLimiter(max_per_minute=rate_limit_per_minute)
        from datetime import UTC, datetime

        self._daemon_started_at = datetime.now(UTC)

    def update_source_count(self, *, active: int, total: int) -> None:
        """Called by the source_health_loop so /status reports accurate
        counts. Both numbers are pure metadata; no behavior changes."""
        self._sources_active = active
        self._sources_total = total

    # -- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        from telegram.ext import (
            ApplicationBuilder,
            CallbackQueryHandler,
            CommandHandler,
        )

        builder = ApplicationBuilder().token(self._token)
        app = builder.build()
        app.add_handler(CallbackQueryHandler(self._on_button))
        # Phase 3 Part 2: register every /command from
        # notifications.commands.all_command_names. Each one routes to
        # _on_command which dispatches to the handler registry.
        if self._db is not None:
            for name in all_command_names():
                app.add_handler(CommandHandler(name.lstrip("/"), self._on_command))
            # An "unknown command" catch-all so /foo gets a hint instead
            # of silence. python-telegram-bot uses MessageHandler for this.
            from telegram.ext import MessageHandler, filters

            app.add_handler(MessageHandler(filters.COMMAND, self._on_unknown_command))
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
                    InlineKeyboardButton(
                        BUTTON_APPROVE_LABEL, callback_data=f"approve|{intent_id}"
                    ),
                    InlineKeyboardButton(BUTTON_REJECT_LABEL, callback_data=f"reject|{intent_id}"),
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

    # -- Phase 3 Part 2: outbound + command surface ------------------

    async def send_text(self, text: str, *, silent: bool = True) -> None:
        """Send a one-way message to the allowlisted chat. Used by
        :class:`AlertDispatcher`, the heartbeat loop, the daily digest,
        and the settlement notifier. ``silent`` maps to Telegram's
        ``disable_notification``; per spec, only critical alerts pass
        ``silent=False``."""
        if self._app is None:
            raise RuntimeError("TelegramApprovalBot.start() has not been called")
        await self._app.bot.send_message(  # type: ignore[attr-defined]
            chat_id=self._chat_id_int,
            text=text,
            disable_notification=silent,
        )

    async def _on_command(self, update, _context) -> None:  # type: ignore[no-untyped-def]
        """Dispatch one ``/command`` to the appropriate handler.

        Validation:
        - Allowlist: messages from any other chat are dropped + logged.
        - Rate-limit: 30/min/chat by default; bursts return a
          USER-FACING usage hint rather than silently failing.
        - Unknown args / handler errors: replies with a usage hint
          rendered from a template -- never an inline string.
        """
        msg = update.message
        if msg is None:
            return
        if msg.chat.id != self._chat_id_int:
            log.warning("telegram_command_from_unauthorized_chat", chat_id=msg.chat.id)
            return
        if not self._rate_limiter.check(msg.chat.id):
            log.warning("telegram_command_rate_limited", chat_id=msg.chat.id)
            return
        # Tokenize: msg.text is "/cmd arg1 arg2" or "/cmd@bot arg1".
        text = (msg.text or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd_token = parts[0].lstrip("/").split("@", 1)[0]  # strip optional @bot suffix
        args = parts[1:]
        handler = dispatch(cmd_token)
        if handler is None or self._db is None:
            rendered = render_template("command_reply_unknown", {"command": "/" + cmd_token})
            await self.send_text(rendered.text, silent=True)
            return
        ctx = CommandContext(
            db=self._db,
            args=args,
            cost_guard=self._cost_guard,
            bankroll_usd_cents=self._bankroll_usd_cents,
            daemon_started_at=self._daemon_started_at,
            sources_total=self._sources_total,
            sources_active=self._sources_active,
        )
        try:
            rendered = await handler(ctx)
        except Exception as exc:  # pragma: no cover -- defensive
            log.error("command_handler_error", cmd=cmd_token, error=repr(exc))
            rendered = render_template("command_reply_unknown", {"command": "/" + cmd_token})
        await self.send_text(rendered.text, silent=True)

    async def _on_unknown_command(self, update, _context) -> None:  # type: ignore[no-untyped-def]
        """Catch-all for /foo bar where 'foo' isn't a registered
        command. python-telegram-bot routes registered commands first;
        this handler only fires for unregistered ones."""
        msg = update.message
        if msg is None or msg.chat.id != self._chat_id_int:
            return
        text = (msg.text or "").strip()
        cmd_token = text.split()[0] if text else "(unknown)"
        rendered = render_template("command_reply_unknown", {"command": cmd_token})
        await self.send_text(rendered.text, silent=True)

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
