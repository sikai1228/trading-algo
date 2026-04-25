"""ApprovalGate — sits between RiskManager and Executor.

Phase 2 always runs in human-approval mode (auto-mode is wired in the
architecture but not enabled). Asks the user via Telegram to approve
each entry / re-entry / stop-loss intent, awaits the response within a
per-intent-type timeout, returns the decision.

Entry timeout: 180 s (configurable). Stop-loss / re-entry: no timeout
per the locked strategy rules.

The gate does not talk to Telegram directly — it delegates to a
:class:`TelegramApprovalBot` (or a test double). This keeps the gate's
core logic pure-async and makes mocking trivial.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trumpbot.approval.message_templates import format_message
from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    insert_telegram_approval,
    update_telegram_approval,
)
from trumpbot.types.intents import (
    ApprovalDecision,
    RiskApprovedOrder,
    StopLossIntent,
)
from trumpbot.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ApprovalGateConfig:
    mode: str = "human"
    entry_timeout_sec: int = 180
    stop_loss_timeout_sec: int | None = None
    reentry_timeout_sec: int | None = None


class ApprovalRequester:
    """Protocol-style hook for the actual Telegram I/O.

    Real implementation: :class:`TelegramApprovalBot`. Tests pass a
    stub that resolves the future themselves.
    """

    async def send_request(self, *, intent_id: str, intent_type: str, message_text: str) -> int:
        raise NotImplementedError

    async def await_response(self, *, intent_id: str, timeout_sec: int | None) -> tuple[str, str]:
        """Return (decision, decision_source) where decision is one of
        ``approved`` / ``rejected`` / ``expired`` and decision_source
        is one of the values defined on :class:`ApprovalDecision`."""
        raise NotImplementedError


class ApprovalGate:
    def __init__(
        self,
        *,
        db: Database,
        config: ApprovalGateConfig,
        requester: ApprovalRequester,
    ) -> None:
        self._db = db
        self._cfg = config
        self._requester = requester

    async def request_approval(self, approved: RiskApprovedOrder) -> ApprovalDecision:
        intent = approved.intent
        timeout_sec = self._timeout_for(approved)
        message_text = format_message(approved)
        expires_at = (
            (datetime.now(UTC) + timedelta(seconds=timeout_sec)).isoformat()
            if timeout_sec
            else None
        )
        chat_id = getattr(self._requester, "chat_id", None)
        record_id = insert_telegram_approval(
            self._db,
            intent_type=intent.intent_type,
            intent_json=intent.model_dump_json(),
            message_text=message_text,
            chat_id=chat_id,
            expires_at=expires_at,
        )
        try:
            telegram_message_id = await self._requester.send_request(
                intent_id=intent.intent_id,
                intent_type=intent.intent_type,
                message_text=message_text,
            )
        except Exception as exc:
            log.error("approval_send_failed", intent_id=intent.intent_id, error=repr(exc))
            update_telegram_approval(
                self._db,
                approval_id=record_id,
                decision="expired",
                decision_source="timeout",
            )
            return ApprovalDecision(
                intent_id=intent.intent_id,
                decision="expired",
                decided_at=datetime.now(UTC),
                decision_source="timeout",
                rejected_reason=f"send_failed: {exc!r}",
                approval_record_id=record_id,
            )

        try:
            decision, source = await self._requester.await_response(
                intent_id=intent.intent_id, timeout_sec=timeout_sec
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("approval_await_failed", intent_id=intent.intent_id, error=repr(exc))
            decision, source = "expired", "timeout"

        update_telegram_approval(
            self._db,
            approval_id=record_id,
            decision=decision,
            decision_source=source,
            telegram_message_id=telegram_message_id,
        )
        return ApprovalDecision(
            intent_id=intent.intent_id,
            decision=decision,  # type: ignore[arg-type]
            decided_at=datetime.now(UTC),
            decision_source=source,  # type: ignore[arg-type]
            approval_record_id=record_id,
        )

    def _timeout_for(self, approved: RiskApprovedOrder) -> int | None:
        if isinstance(approved.intent, StopLossIntent):
            return self._cfg.stop_loss_timeout_sec
        if approved.intent.intent_type == "reentry":
            return self._cfg.reentry_timeout_sec
        return self._cfg.entry_timeout_sec


__all__ = ["ApprovalGate", "ApprovalGateConfig", "ApprovalRequester"]
