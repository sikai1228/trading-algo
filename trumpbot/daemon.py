"""Top-level orchestrator: starts every Phase 1 task, manages shutdown.

Runs the Kalshi WS feed, market discovery, RSS poller, Twitter scraper,
Truth Social scraper, news matcher worker, and healthcheck server
concurrently. SIGTERM triggers graceful shutdown: each task stops
accepting new work, drains pending writes, and exits.

(Phase 4 Part 2.10 removed the heartbeat logger and the periodic
Telegram heartbeat. The morning daily digest is the regular status
notification; /status is on demand.)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prometheus_client import Counter, Gauge

from trumpbot.config import TrumpbotConfig, load_config
from trumpbot.db.connection import Database
from trumpbot.db.repositories import (
    LLMMatchUpdate,
    NewsMatchRow,
    SubjectRow,
    fetch_news_events_without_matches,
    insert_news_match_returning_id,
    insert_news_matches,
    insert_system_event,
    list_active_markets,
    list_markets_for_matching,
    update_match_with_classification,
    upsert_subject,
)
from trumpbot.discovery.service import MarketDiscoveryService
from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor
from trumpbot.events.bus import Event, EventBus
from trumpbot.health.server import HealthcheckServer
from trumpbot.kalshi.auth import load_private_key
from trumpbot.kalshi.client import KalshiClient
from trumpbot.kalshi.exceptions import StateError
from trumpbot.market_data.kalshi_ws import KalshiWebSocketFeed
from trumpbot.news.llm_classifier import (
    AnthropicAuthError as ClassifierAuthError,
)
from trumpbot.news.llm_classifier import (
    LLMClassifier,
)
from trumpbot.news.matcher import PASSED_REASON, MarketContext, NewsMatcher
from trumpbot.news.rss import RSSPoller
from trumpbot.news.truthsocial import TruthSocialScraper
from trumpbot.news.twitter import TwitterScraper
from trumpbot.notifications.telegram import TelegramNotifier
from trumpbot.platform_paths import current_platform_paths, resolve_path
from trumpbot.utils.logging import configure_logging, get_logger

log = get_logger(__name__)

NEWS_INGESTED = Counter(
    "trumpbot_news_events_ingested_total", "Total news events written to DB", ["source"]
)
MATCHES_WRITTEN = Counter(
    "trumpbot_news_matches_total", "Total matcher rows written", ["confidence_bucket"]
)
ACTIVE_MARKETS = Gauge("trumpbot_active_markets", "Markets currently in 'active' status")
WS_CONNECTED = Gauge("trumpbot_kalshi_ws_connected", "1 if WS connection is open")
TASK_HEALTHY = Gauge("trumpbot_task_healthy", "1 if task healthy", ["task"])

# Phase 4 Part 2.12 — articles older than this are skipped before the
# LLM cascade (Stage 2). Prevents wasted Anthropic spend on
# historical content that proxies (Google News, Bing) periodically
# surface as "new". 48 h is conservatively above the 24 h
# DecisionEngine entry-window check so an article that's just barely
# inside the engine's window still reaches Stage 2.
STALE_ARTICLE_HOURS: int = 48


def _article_is_stale(raw_published_ts: str | None, threshold_hours: int) -> bool:
    """True if the article was published more than ``threshold_hours``
    ago. ``None`` / unparseable timestamps are TREATED AS STALE so
    the LLM never spends budget on an article whose publish time we
    can't verify."""
    if not raw_published_ts:
        return True
    s = raw_published_ts.replace("Z", "+00:00")
    try:
        published = datetime.fromisoformat(s)
    except ValueError:
        return True
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    age = datetime.now(UTC) - published
    return age.total_seconds() > threshold_hours * 3600


async def _amain(config_path: Path) -> int:
    cfg = load_config(config_path)
    configure_logging(cfg.logging.level)
    paths = current_platform_paths()
    log.info(
        "trumpbot_starting",
        config_path=str(config_path),
        platform=paths.config_dir.parts[1] if len(paths.config_dir.parts) > 1 else "unknown",
    )

    # Resolve "auto" path sentinels against the per-OS defaults so the
    # same config.yaml works on macOS and Linux without editing.
    db_path = resolve_path(cfg.database.path, paths.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    private_key_path = resolve_path(cfg.kalshi.private_key_path, paths.private_key_path)
    snapshot_dir = resolve_path(cfg.discovery.snapshot_dir, paths.snapshot_dir)

    db = Database(db_path)
    db.connect()
    insert_system_event(
        db,
        event_type="startup",
        severity="info",
        component="daemon",
        message="trumpbot daemon starting",
        detail={
            "platform": paths.config_dir.parts[1] if len(paths.config_dir.parts) > 1 else "unknown",
            "database_path": str(db_path),
        },
    )

    # Idempotently seed the subjects table from the YAML so the matcher
    # has metadata even before the discovery service has run.
    initial_subjects_path = (
        resolve_path(cfg.discovery.initial_subjects_path, paths.initial_subjects_path)
        if cfg.discovery.initial_subjects_path
        else None
    )
    if initial_subjects_path is not None:
        _seed_subjects(db, initial_subjects_path)

    extractor = _load_extractor(cfg)
    private_key = load_private_key(
        private_key_path,
        passphrase=(
            cfg.kalshi.private_key_passphrase.encode()
            if cfg.kalshi.private_key_passphrase
            else None
        ),
    )

    rest_client = KalshiClient(
        api_key_id=cfg.kalshi.api_key_id,
        private_key=private_key,
        base_url=cfg.kalshi.base_url,
        rate_per_sec=cfg.kalshi.rate_per_sec,
        burst=cfg.kalshi.rate_burst,
        rate_limit_pct=cfg.kalshi.rate_limit_pct,
    )
    bus = EventBus()
    telegram = TelegramNotifier(
        bot_token=cfg.telegram.bot_token,
        chat_id=cfg.telegram.chat_id,
    )
    discovery = MarketDiscoveryService(
        client=rest_client,
        db=db,
        event_bus=bus,
        telegram=telegram,
        series=cfg.discovery.series,
        poll_interval_sec=cfg.discovery.poll_interval_sec,
        snapshot_dir=snapshot_dir,
    )
    ws_feed = KalshiWebSocketFeed(
        rest_client=rest_client,
        db=db,
        event_bus=bus,
        api_key_id=cfg.kalshi.api_key_id,
        ws_url=cfg.kalshi.ws_url,
    )
    rss_poller = RSSPoller(sources=cfg.news.sources, db=db, event_bus=bus)
    twitter_scraper = TwitterScraper(handles=cfg.news.sources, db=db, event_bus=bus)
    truth_scraper = TruthSocialScraper(handles=cfg.news.sources, db=db, event_bus=bus)
    matcher_worker = MatcherWorker(
        db=db,
        matcher=NewsMatcher(extractor=extractor),
        event_bus=bus,
        poll_interval_sec=cfg.matcher.poll_interval_sec,
        batch_size=cfg.matcher.batch_size,
    )
    # Phase 4 Part 2.10 — HeartbeatLogger removed. The DB-only periodic
    # liveness logger added log noise without earning its keep; the
    # daily digest, /status on demand, and the healthcheck endpoint
    # cover the operator's "is it alive?" needs.

    # ---- Phase 2 decision layer ----
    from trumpbot.approval.gate import ApprovalGate, ApprovalGateConfig
    from trumpbot.approval.telegram_bot import TelegramApprovalBot
    from trumpbot.decision.engine import DecisionConfig as DecCfg
    from trumpbot.decision.engine import DecisionEngine
    from trumpbot.decision.loops import (
        AutoNotifyFn,
        decision_loop,
        position_marking_loop,
        reentry_loop,
        stop_loss_loop,
    )
    from trumpbot.execution.dry_run import DryRunExecutor, Quote
    from trumpbot.risk.manager import RiskConfig as RskCfg
    from trumpbot.risk.manager import RiskManager

    decision_engine = DecisionEngine(
        DecCfg(
            # Phase 4 Part 2.9 — llm_confidence_threshold and
            # position_size_base_pct removed; the LLM's
            # ``interaction_occurred`` boolean is the sole gate, and
            # the two caps + walk drive sizing.
            max_buy_price_cents=cfg.decision.max_buy_price_cents,
            position_size_hard_cap_cents=cfg.decision.position_size_hard_cap_cents,
            position_size_orderbook_pct=cfg.decision.position_size_orderbook_pct,
            min_trade_size_contracts=cfg.decision.min_trade_size_contracts,
            min_trade_value_cents=cfg.decision.min_trade_value_cents,
            stop_loss_drop_cents=cfg.decision.stop_loss_drop_cents,
        )
    )
    risk_manager = RiskManager(
        db=db,
        config=RskCfg(
            enabled=cfg.risk.enabled,
            max_buy_price_cents=cfg.decision.max_buy_price_cents,
            position_size_hard_cap_cents=cfg.decision.position_size_hard_cap_cents,
            halted=cfg.risk.halted,
        ),
    )

    # Telegram bot — Phase 2 needs the approval flow. Falls back to a
    # stub requester if no token is configured (the loops will record
    # "send_failed" expirations rather than crashing).
    # Phase 3 Part 2: LLM cost guard + alert dispatcher live alongside
    # the Telegram bot. Both are constructed BEFORE the bot so we can
    # pass them in as command-handler context.
    from trumpbot.notifications.alerts import AlertDispatcher
    from trumpbot.notifications.llm_cost import LLMCostGuard, LLMCostGuardConfig

    cost_guard = LLMCostGuard(
        db=db,
        config=LLMCostGuardConfig(
            monthly_cap_usd_cents=cfg.alias_enrichment.monthly_cap_usd_cents,
        ),
    )

    telegram_bot: TelegramApprovalBot | None = None
    requester: object
    # Phase 4 Part 2.1 — exports live next to the database so the
    # operator can browse / commit them with the rest of the
    # persistent state.
    exports_dir = db_path.parent / "exports"
    if cfg.telegram.bot_token and cfg.telegram.chat_id:
        telegram_bot = TelegramApprovalBot(
            bot_token=cfg.telegram.bot_token,
            chat_id=cfg.telegram.chat_id,
            db=db,
            cost_guard=cost_guard,
            bankroll_usd_cents=int(round(cfg.bankroll.starting_amount_usd * 100)),
            exports_dir=exports_dir,
            default_export_format=cfg.tax_tracking.default_export_format,
        )
        await telegram_bot.start()
        requester = telegram_bot
    else:
        # No-op requester so the gate is callable in headless setups.
        from trumpbot.approval.gate import ApprovalRequester

        class _StubRequester(ApprovalRequester):
            chat_id = None

            async def send_request(
                self, *, intent_id: str, intent_type: str, message_text: str
            ) -> int:
                raise RuntimeError("Telegram not configured")

            async def await_response(
                self, *, intent_id: str, timeout_sec: int | None
            ) -> tuple[str, str]:
                return ("expired", "timeout")

        requester = _StubRequester()

    def _orderbook(ticker: str) -> Quote:
        # Read from the WS feed's in-memory book if available; fall back
        # to (None, None) if the ticker isn't subscribed.
        book = ws_feed._books.get(ticker)  # direct internal read
        if book is None:
            return Quote(yes_bid_cents=None, yes_ask_cents=None)
        return Quote(yes_bid_cents=book.best_yes_bid(), yes_ask_cents=book.best_yes_ask())

    def _depth(ticker: str) -> list[tuple[int, int]] | None:
        """Phase 3 Part 1: full YES-ask depth for the walker. NO bids
        are inverted to implied YES asks via :func:`merge_to_yes_asks`
        and merged with the YES side."""
        from trumpbot.execution.slippage import merge_to_yes_asks

        book = ws_feed._books.get(ticker)
        if book is None:
            return None
        return merge_to_yes_asks(
            book.yes_levels_sorted(),
            book.no_levels_sorted(),
        )

    approval_gate = ApprovalGate(
        db=db,
        config=ApprovalGateConfig(
            mode=cfg.approval.mode,
            entry_timeout_sec=cfg.approval.entry_timeout_sec,
            stop_loss_timeout_sec=cfg.approval.stop_loss_timeout_sec,
            reentry_timeout_sec=cfg.approval.reentry_timeout_sec,
        ),
        requester=requester,  # type: ignore[arg-type]
        depth_fn=_depth,
    )

    # Phase 4 Part 2.11 — log the approval mode prominently and fire
    # a critical Telegram alert when auto-approval is enabled. Makes
    # accidental auto-mode highly visible.
    log.info(f"APPROVAL MODE: {cfg.approval.mode.upper()}")
    if cfg.approval.mode == "auto":
        log.warning(
            "AUTO-APPROVAL ENABLED. Entry trades will fire without "
            "Telegram approval. Stop-loss and re-entry still require "
            "human approval."
        )
        insert_system_event(
            db,
            event_type="auto_approval_enabled",
            severity="warning",
            component="daemon",
            message="Daemon started with approval.mode=auto",
        )
    else:
        insert_system_event(
            db,
            event_type="human_approval_enabled",
            severity="info",
            component="daemon",
            message="Daemon started with approval.mode=human",
        )

    # ---- Phase 4 Part 1: live executor switch + reconciliation ----
    # Pick executor based on cfg.execution.mode. The two flavors
    # share the async submit / update_position_marks / close_resolved
    # surface; loops are oblivious.
    from trumpbot.execution.live_executor import HaltCallback, KalshiExecutor

    def _halt_bot(reason: str) -> None:
        from trumpbot.db.repositories import set_system_state

        set_system_state(db, key="halt_flag", value="true")
        insert_system_event(
            db,
            event_type="halted_by_state_error",
            severity="error",
            component="kalshi_executor",
            message=f"halted by StateError: {reason[:240]}",
            detail={"reason": reason},
        )
        log.error("bot_halted_by_state_error", reason=reason)

    executor: DryRunExecutor | KalshiExecutor
    if cfg.execution.mode == "live":
        executor = KalshiExecutor(
            db=db,
            kalshi_client=rest_client,
            orderbook_fn=_orderbook,
            depth_fn=_depth,
            halt_callback=HaltCallback(callback=_halt_bot),
        )
        log.info("execution_mode_live")
    else:
        executor = DryRunExecutor(db=db, orderbook_fn=_orderbook, depth_fn=_depth)
        log.info("execution_mode_dry_run")

    bus.subscribe("news_event_ingested", _make_news_metric_handler())
    bus.subscribe("market_discovered", _make_market_metric_handler(db))
    bus.subscribe("market_status_changed", _make_market_metric_handler(db))
    # Auto-subscribe the WS feed to every market the discovery service
    # finds. Without this, a fresh deployment with no markets in the DB
    # at startup time would never produce price snapshots — the smoke
    # test specifically asserts at least one snapshot within 60 s.
    bus.subscribe("market_discovered", _make_ws_subscribe_handler(ws_feed))

    health = HealthcheckServer(
        health_check=_make_health_check(ws_feed, discovery, matcher_worker),
        host=cfg.health.host,
        port=cfg.health.port,
    )
    await health.start()

    # Subscribe WS feed to every active market discovered so far.
    for row in list_active_markets(db):
        await ws_feed.subscribe(row["ticker"])

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await rss_poller.start()
    await twitter_scraper.start()
    await truth_scraper.start()

    # Phase 4 Part 2.11 — auto-approval Telegram confirmation hook
    # used by ``decision_loop`` for entry intents whose approval
    # source was 'auto_approval'. None when telegram isn't wired
    # (the loop's auto path swallows the no-op silently).
    auto_notify: AutoNotifyFn | None
    if telegram_bot is not None:

        async def _auto_notify(template_name: str, data: dict[str, object]) -> None:
            from trumpbot.notifications.templates import render_template

            assert telegram_bot is not None  # guarded by outer if
            rendered = render_template(template_name, data)
            await telegram_bot.send_text(rendered.text, silent=True)

        auto_notify = _auto_notify
    else:
        auto_notify = None

    tasks: dict[str, asyncio.Task[None]] = {
        "discovery": asyncio.create_task(_supervised(discovery.run, "discovery", critical=True)),
        "kalshi_ws": asyncio.create_task(_supervised(ws_feed.run, "kalshi_ws", critical=True)),
        "matcher": asyncio.create_task(_supervised(matcher_worker.run, "matcher", critical=False)),
        # Phase 4 Part 2.10 — heartbeat task removed.
        "decision_loop": asyncio.create_task(
            _supervised(
                lambda: decision_loop(
                    db=db,
                    engine=decision_engine,
                    risk=risk_manager,
                    gate=approval_gate,
                    executor=executor,
                    orderbook=_orderbook,
                    depth=_depth,
                    starting_amount_usd=cfg.bankroll.starting_amount_usd,
                    execution_mode=cfg.execution.mode,
                    poll_interval_sec=cfg.decision.decision_loop_interval_sec,
                    stop_event=stop_event,
                    auto_notify=auto_notify,
                ),
                "decision_loop",
                critical=False,
            )
        ),
        "stop_loss_loop": asyncio.create_task(
            _supervised(
                lambda: stop_loss_loop(
                    db=db,
                    engine=decision_engine,
                    risk=risk_manager,
                    gate=approval_gate,
                    executor=executor,
                    orderbook=_orderbook,
                    depth=_depth,
                    starting_amount_usd=cfg.bankroll.starting_amount_usd,
                    execution_mode=cfg.execution.mode,
                    poll_interval_sec=cfg.decision.stop_loss_loop_interval_sec,
                    stop_event=stop_event,
                ),
                "stop_loss_loop",
                critical=False,
            )
        ),
        "position_marking_loop": asyncio.create_task(
            _supervised(
                lambda: position_marking_loop(
                    executor=executor,  # Executor union: dry-run or live
                    poll_interval_sec=cfg.decision.position_marking_loop_interval_sec,
                    stop_event=stop_event,
                ),
                "position_marking_loop",
                critical=False,
            )
        ),
        "reentry_loop": asyncio.create_task(
            _supervised(
                lambda: reentry_loop(
                    db=db,
                    engine=decision_engine,
                    risk=risk_manager,
                    gate=approval_gate,
                    executor=executor,
                    orderbook=_orderbook,
                    depth=_depth,
                    starting_amount_usd=cfg.bankroll.starting_amount_usd,
                    execution_mode=cfg.execution.mode,
                    poll_interval_sec=cfg.decision.reentry_loop_interval_sec,
                    stop_event=stop_event,
                ),
                "reentry_loop",
                critical=False,
            )
        ),
    }

    # ---- Phase 3 Part 2: alerts + scheduled loops + alias enrichment ----
    alert_dispatcher = AlertDispatcher(
        db=db,
        send_fn=(
            (lambda text, audible: telegram_bot.send_text(text, silent=not audible))
            if telegram_bot is not None
            else None
        ),
        dedup_window_seconds=cfg.notifications.alert_dedup_window_minutes * 60,
    )

    # Phase 4 Part 2.11 — fire the audible auto-approval-enabled
    # alert once on startup so accidental auto-mode surfaces in
    # Telegram immediately. Dispatcher needed Telegram wiring;
    # we deferred this to here.
    if cfg.approval.mode == "auto":
        from zoneinfo import ZoneInfo

        time_et = (
            datetime.now(UTC).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
        )
        with contextlib.suppress(Exception):
            await alert_dispatcher.send(
                template_name="alert_critical_auto_approval_enabled",
                data={"time_et": time_et},
                dedup_key="auto_approval_enabled",
                component="daemon",
            )

    # Subscribe alias-enrichment to market_discovered events. The
    # enricher only fires when ANTHROPIC_API_KEY is configured AND
    # alias_enrichment.enabled is True; otherwise we install a no-op
    # subscriber so the bus event isn't unhandled.
    anthropic_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))

    if (
        cfg.alias_enrichment.enabled
        and cfg.kalshi.api_key_id  # any LLM-enabled deployment will have keys
        and anthropic_key_present
    ):
        # Build a thin async wrapper around anthropic.AsyncAnthropic
        # that translates a 401 into _AnthropicAuthError.
        from trumpbot.news.alias_enrichment import (
            AliasEnricher,
            AliasEnrichmentConfig,
            _AnthropicAuthError,
        )

        def _make_llm_call(
            *,
            model: str,
            max_tokens: int,
            auth_error: type[Exception],
        ) -> Callable[[str, str], Awaitable[tuple[int, int, str]]]:
            async def _llm_call(system_prompt: str, user_prompt: str) -> tuple[int, int, str]:
                from anthropic import AsyncAnthropic
                from anthropic._exceptions import AuthenticationError

                client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
                try:
                    msg = await client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_prompt}],
                    )
                except AuthenticationError as exc:
                    raise auth_error(str(exc)) from exc
                text = ""
                for block in msg.content:
                    if hasattr(block, "text"):
                        text = block.text
                        break
                return (msg.usage.input_tokens, msg.usage.output_tokens, text)

            return _llm_call

        enricher = AliasEnricher(
            db=db,
            cost_guard=cost_guard,
            alerts=alert_dispatcher,
            config=AliasEnrichmentConfig(
                enabled=True,
                prompt_path=cfg.alias_enrichment.prompt_path,
                prompt_version=cfg.alias_enrichment.prompt_version,
                model=cfg.alias_enrichment.model,
                max_tokens=cfg.alias_enrichment.max_tokens,
            ),
            llm_call=_make_llm_call(
                model=cfg.alias_enrichment.model,
                max_tokens=cfg.alias_enrichment.max_tokens,
                auth_error=_AnthropicAuthError,
            ),
        )
        bus.subscribe("market_discovered", enricher.on_market_discovered)

    # ---- Phase 4 Part 2.8 — Stage 2 LLM cascade (news classifier) ----
    # Built here (after cost_guard + alert_dispatcher exist) and
    # attached to the matcher worker before its task is scheduled.
    if cfg.llm_classifier.enabled and anthropic_key_present:
        from trumpbot.news.llm_classifier import (
            AnthropicAuthError as _ClsAuthError,
        )
        from trumpbot.news.llm_classifier import (
            LLMClassifierConfig,
        )

        async def _cls_llm_call(system_prompt: str, user_prompt: str) -> tuple[int, int, str]:
            from anthropic import AsyncAnthropic
            from anthropic._exceptions import AuthenticationError

            client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
            try:
                msg = await client.messages.create(
                    model=cfg.llm_classifier.model,
                    max_tokens=cfg.llm_classifier.max_output_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
            except AuthenticationError as exc:
                raise _ClsAuthError(str(exc)) from exc
            text = ""
            for block in msg.content:
                if hasattr(block, "text"):
                    text = block.text
                    break
            return (msg.usage.input_tokens, msg.usage.output_tokens, text)

        classifier = LLMClassifier(
            db=db,
            cost_guard=cost_guard,
            alerts=alert_dispatcher,
            config=LLMClassifierConfig(
                enabled=True,
                model=cfg.llm_classifier.model,
                max_input_tokens=cfg.llm_classifier.max_input_tokens,
                max_output_tokens=cfg.llm_classifier.max_output_tokens,
                timeout_sec=cfg.llm_classifier.timeout_sec,
                prompt_path=cfg.llm_classifier.prompt_path,
                prompt_version=cfg.llm_classifier.prompt_version,
                contract_path=cfg.llm_classifier.contract_path,
            ),
            llm_call=_cls_llm_call,
        )
        matcher_worker.attach_classifier(
            classifier=classifier,
            alert_dispatcher=alert_dispatcher,
        )
        log.info("llm_classifier_attached", model=cfg.llm_classifier.model)
    else:
        log.info(
            "llm_classifier_disabled",
            cfg_enabled=cfg.llm_classifier.enabled,
            anthropic_key_present=anthropic_key_present,
        )

    # Scheduled loops: daily digest, settlement, source health.
    # All silent-by-default; only the source-health loop fires alerts
    # (via the dispatcher), and only critical ones are audible.
    # (Phase 4 Part 2.10 removed the heartbeat_loop. The morning
    # daily digest is the regular status notification.)
    if telegram_bot is not None:
        from trumpbot.notifications.scheduled import (
            daily_digest_loop,
            monthly_tax_digest_loop,
            settlement_notification_loop,
            source_health_loop,
        )

        async def _send_text(text: str, silent: bool) -> None:
            assert telegram_bot is not None  # guarded by outer if
            await telegram_bot.send_text(text, silent=silent)

        tasks["daily_digest_loop"] = asyncio.create_task(
            _supervised(
                lambda: daily_digest_loop(
                    db=db,
                    send_text=_send_text,
                    cost_guard=cost_guard,
                    digest_hour_utc=cfg.notifications.digest_hour_utc,
                    stop_event=stop_event,
                ),
                "daily_digest_loop",
                critical=False,
            )
        )
        tasks["settlement_notification_loop"] = asyncio.create_task(
            _supervised(
                lambda: settlement_notification_loop(
                    db=db,
                    send_text=_send_text,
                    interval_seconds=cfg.notifications.settlement_check_interval_seconds,
                    stop_event=stop_event,
                ),
                "settlement_notification_loop",
                critical=False,
            )
        )
        tasks["source_health_loop"] = asyncio.create_task(
            _supervised(
                lambda: source_health_loop(
                    db=db,
                    dispatcher=alert_dispatcher,
                    interval_seconds=cfg.notifications.source_health_check_interval_seconds,
                    down_threshold_minutes=cfg.notifications.source_down_alert_threshold_minutes,
                    stop_event=stop_event,
                ),
                "source_health_loop",
                critical=False,
            )
        )
        # Phase 4 Part 2.1 — monthly tax digest. Off by default if the
        # operator disables it in config; per-trade tax columns are
        # populated regardless.
        if cfg.tax_tracking.monthly_digest_enabled:
            tasks["monthly_tax_digest_loop"] = asyncio.create_task(
                _supervised(
                    lambda: monthly_tax_digest_loop(
                        db=db,
                        send_text=_send_text,
                        exports_dir=exports_dir,
                        fire_day=cfg.tax_tracking.monthly_digest_day,
                        fire_time_et=cfg.tax_tracking.monthly_digest_time_et,
                        stop_event=stop_event,
                    ),
                    "monthly_tax_digest_loop",
                    critical=False,
                )
            )

    # ---- Phase 4 Part 1: live-mode side tasks ------------------------
    # Bankroll sync + settlement detector run only in live mode (they
    # poll Kalshi REST endpoints that don't apply to a dry-run session).
    # Startup reconciliation runs as a one-shot before the main loops
    # are allowed to do anything.
    if cfg.execution.mode == "live":
        from trumpbot.account.bankroll_sync import bankroll_sync_loop
        from trumpbot.account.reconcile import reconcile_once
        from trumpbot.account.settlement_detector import settlement_loop
        from trumpbot.notifications.templates import render_template

        # ---- Startup reconciliation (gating) ----
        log.info("startup_reconciliation_starting")
        recon = await reconcile_once(db=db, kalshi=rest_client)
        if not recon.succeeded:
            log.error("startup_reconciliation_failed")
            if telegram_bot is not None:
                with contextlib.suppress(Exception):
                    rendered = render_template(
                        "reconciliation_failed",
                        {"detail": "could not reach Kalshi for /orders or /positions"},
                    )
                    await telegram_bot.send_text(rendered.text, silent=False)
            insert_system_event(
                db,
                event_type="startup_reconciliation_failed",
                severity="error",
                component="daemon",
                message=("startup reconciliation failed; trading loops gated"),
            )
            # Re-try every 60s until reconciliation succeeds, gating
            # the main loops until then. Cheap; Kalshi outages don't
            # last long.
            while not stop_event.is_set():
                await asyncio.sleep(60)
                recon = await reconcile_once(db=db, kalshi=rest_client)
                if recon.succeeded:
                    break
        if recon.has_drift and telegram_bot is not None:
            with contextlib.suppress(Exception):
                summary = "\n".join(f"  • [{d.kind}] {d.ticker}: {d.detail}" for d in recon.drifts)
                rendered = render_template("reconciliation_drift", {"drift_summary": summary})
                await telegram_bot.send_text(rendered.text, silent=True)
        elif telegram_bot is not None:
            with contextlib.suppress(Exception):
                rendered = render_template(
                    "reconciliation_ok",
                    {
                        "pending_count": recon.pending_count,
                        "live_count": recon.live_count,
                        "kalshi_position_count": recon.kalshi_position_count,
                    },
                )
                await telegram_bot.send_text(rendered.text, silent=True)

        # ---- Mode-switched alert (audible critical) ----
        if telegram_bot is not None:
            with contextlib.suppress(Exception):
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo

                now_et = _dt.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
                bal = await rest_client.get_balance()
                rendered = render_template(
                    "mode_switched_live",
                    {"bankroll": f"${bal.balance / 100:.2f}", "time_et": now_et},
                )
                await telegram_bot.send_text(rendered.text, silent=False)

        # ---- Bankroll sync + settlement detector loops ----
        async def _settlement_notifier(
            *,
            ticker: str,
            result: str,
            realized_pnl_cents: int,
            quantity: int,
            payoff_cents: int,
        ) -> None:
            if telegram_bot is None:
                return
            template_name = "trade_settled_yes" if result == "yes" else "trade_settled_no"
            data = {
                "ticker": ticker,
                "subject_full_name": "(market)",
                "quantity": quantity,
                "entry_price": payoff_cents,  # best-effort
                "pnl_dollars": f"{realized_pnl_cents / 100:.2f}",
                "roi": "n/a",
                "series": "n/a",
                "remaining_in_series": "n/a",
                "stop_status": "exit at resolution",
                "loss_dollars": f"{abs(realized_pnl_cents) / 100:.2f}",
                "resolution_date": "today",
            }
            try:
                rendered = render_template(template_name, data)
                await telegram_bot.send_text(rendered.text, silent=True)
            except Exception as exc:
                log.warning("settlement_notify_failed", error=repr(exc))

        # Pre-live fix #2: pass the Telegram send fn so the loop can
        # fire alert_critical_bankroll_sync_failed / alert_info_bankroll_
        # sync_recovered. Defined locally because _send_text may not
        # have been bound if telegram_bot is None (defensive — live
        # mode in practice always has a Telegram bot, but don't trip
        # NameError if someone runs live-headless).
        if telegram_bot is not None:

            async def _send_text_for_bankroll(text: str, silent: bool) -> None:
                assert telegram_bot is not None  # checked above
                await telegram_bot.send_text(text, silent=silent)

            _bankroll_send: Callable[[str, bool], Awaitable[None]] | None = _send_text_for_bankroll
        else:
            _bankroll_send = None

        tasks["bankroll_sync_loop"] = asyncio.create_task(
            _supervised(
                lambda: bankroll_sync_loop(
                    db=db,
                    kalshi=rest_client,
                    poll_interval_sec=300,
                    stop_event=stop_event,
                    send_text=_bankroll_send,
                ),
                "bankroll_sync_loop",
                critical=False,
            )
        )
        tasks["settlement_loop"] = asyncio.create_task(
            _supervised(
                lambda: settlement_loop(
                    db=db,
                    kalshi=rest_client,
                    poll_interval_sec=cfg.notifications.settlement_check_interval_seconds,
                    stop_event=stop_event,
                    notifier=_settlement_notifier,
                ),
                "settlement_loop",
                critical=False,
            )
        )

    log.info("trumpbot_started", task_count=len(tasks))
    exit_code = 0
    try:
        done, _ = await asyncio.wait(
            [*tasks.values(), asyncio.create_task(stop_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                log.error("task_terminated_with_error", error=repr(exc))
                exit_code = 1
    finally:
        log.info("trumpbot_shutting_down")
        discovery.stop()
        ws_feed.stop()
        matcher_worker.stop()
        for task in tasks.values():
            task.cancel()
        for task in tasks.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await rss_poller.stop()
        await twitter_scraper.stop()
        await truth_scraper.stop()
        await rest_client.aclose()
        await telegram.aclose()
        if telegram_bot is not None:
            await telegram_bot.stop()
        await health.stop()
        insert_system_event(
            db,
            event_type="shutdown",
            severity="info",
            component="daemon",
            message="trumpbot daemon stopped",
        )
        db.close()
    return exit_code


async def _supervised(
    coro_factory: Callable[[], Awaitable[None]], name: str, *, critical: bool
) -> None:
    """Run ``coro_factory()`` and surface its outcome via metrics + logs."""
    TASK_HEALTHY.labels(task=name).set(1)
    try:
        await coro_factory()
    except asyncio.CancelledError:
        raise
    except StateError:
        TASK_HEALTHY.labels(task=name).set(0)
        log.error("task_state_error_halt", task=name)
        raise
    except Exception as exc:
        TASK_HEALTHY.labels(task=name).set(0)
        log.error("task_failed", task=name, critical=critical, error=repr(exc))
        if critical:
            raise


def _make_news_metric_handler() -> Callable[[Event], Awaitable[None]]:
    async def handle(event: Event) -> None:
        source = str(event.payload.get("source", "unknown"))
        NEWS_INGESTED.labels(source=source).inc()

    return handle


def _make_market_metric_handler(db: Database) -> Callable[[Event], Awaitable[None]]:
    async def handle(_: Event) -> None:
        ACTIVE_MARKETS.set(len(list_active_markets(db)))

    return handle


def _make_ws_subscribe_handler(
    ws_feed: KalshiWebSocketFeed,
) -> Callable[[Event], Awaitable[None]]:
    """Subscribe the WebSocket feed to any newly-discovered market."""

    async def handle(event: Event) -> None:
        ticker = event.payload.get("ticker")
        if isinstance(ticker, str):
            await ws_feed.subscribe(ticker)

    return handle


def _seed_subjects(db: Database, path: Path) -> None:
    """Idempotently upsert every subject defined in ``path`` (YAML)."""
    if not path.is_file():
        log.info("initial_subjects_not_found", path=str(path))
        return
    import yaml as _yaml

    raw = _yaml.safe_load(path.read_text()) or {}
    rows = raw.get("subjects") or []
    if not isinstance(rows, list):
        log.warning("initial_subjects_invalid_shape", path=str(path))
        return
    seeded = 0
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        subject_key = entry.get("subject_key")
        full_name = entry.get("full_name")
        aliases = entry.get("aliases") or []
        if not isinstance(subject_key, str) or not isinstance(full_name, str):
            continue
        if not isinstance(aliases, list):
            continue
        upsert_subject(
            db,
            SubjectRow(
                subject_key=subject_key,
                full_name=full_name,
                aliases=[str(a) for a in aliases],
                ticker_suffix=None,
                auto_extracted=False,
                llm_enriched=False,
                reviewed=False,
            ),
        )
        seeded += 1
    log.info("initial_subjects_seeded", path=str(path), count=seeded)


def _make_health_check(
    ws_feed: KalshiWebSocketFeed,
    discovery: MarketDiscoveryService,
    matcher_worker: MatcherWorker,
) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return all(
            (
                not ws_feed.stopped(),
                not discovery.stopped(),
                not matcher_worker.stopped(),
            )
        )

    return check


def _load_extractor(cfg: TrumpbotConfig) -> SubjectExtractor:
    if cfg.matcher.subject_aliases_path:
        path = Path(cfg.matcher.subject_aliases_path)
        if path.exists():
            import yaml as _yaml

            data = _yaml.safe_load(path.read_text()) or {}
            if not isinstance(data, dict):
                raise ValueError(f"{path} must be a YAML mapping of subject -> aliases")
            return SubjectExtractor(aliases=data)
    return SubjectExtractor(aliases=DEFAULT_SUBJECT_ALIASES)


# ---------------------------------------------------------------------------
# Background workers (defined here for one-file orchestration clarity)
# ---------------------------------------------------------------------------


class MatcherWorker:
    """Consumes new news events, runs the matcher, writes news_market_matches.

    Phase 4 Part 2.8: when an :class:`LLMClassifier` is injected, every
    Stage-1 ``passed_pre_filter`` row is upgraded by calling the LLM
    against the verbatim contract rules. The keyword row is inserted
    first (so we always have an audit trail) and patched in place with
    ``classifier_type='llm_cascade'`` + the LLM's confidence + the
    ``llm_classification_id`` FK.
    """

    component = "matcher_worker"

    def __init__(
        self,
        *,
        db: Database,
        matcher: NewsMatcher,
        event_bus: EventBus,
        poll_interval_sec: int = 5,
        batch_size: int = 100,
        classifier: LLMClassifier | None = None,
        alert_dispatcher: Any | None = None,
    ) -> None:
        self._db = db
        self._matcher = matcher
        self._bus = event_bus
        self._poll_interval = poll_interval_sec
        self._batch_size = batch_size
        self._classifier = classifier
        self._alerts = alert_dispatcher
        self._stop = asyncio.Event()

    def attach_classifier(
        self,
        *,
        classifier: LLMClassifier,
        alert_dispatcher: Any | None,
    ) -> None:
        """Wire the LLM classifier in after construction.

        The daemon builds the cost guard, the Telegram bot, and the
        alert dispatcher AFTER the matcher worker (the worker doesn't
        depend on them). This setter lets us hand them in once they
        exist, before the worker's task is started."""
        self._classifier = classifier
        self._alerts = alert_dispatcher

    async def run(self) -> None:
        log.info("matcher_worker_started")
        while not self._stop.is_set():
            try:
                processed = await self._process_batch()
                if processed == 0:
                    await self._sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("matcher_worker_error", error=repr(exc))
                insert_system_event(
                    self._db,
                    event_type="matcher_error",
                    severity="error",
                    component=self.component,
                    message=str(exc),
                )
                await self._sleep(self._poll_interval)
        log.info("matcher_worker_stopped")

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            return

    async def _process_batch(self) -> int:
        events = fetch_news_events_without_matches(self._db, limit=self._batch_size)
        if not events:
            return 0
        # Match against ALL markets with subject (not just active) so the
        # observation period captures matches against settled markets too
        # — those are how we calibrate matcher quality. The Phase-2
        # decision engine will filter back to active before any trading.
        markets_rows = list_markets_for_matching(self._db)
        contexts = [
            MarketContext(
                ticker=row["ticker"],
                subject=row["subject"] or "",
                open_ts=row["open_ts"],
                close_ts=row["close_ts"],
            )
            for row in markets_rows
            if row["subject"]
        ]
        if not contexts:
            return len(events)

        # Build a fresh matcher each batch with aliases pulled from the
        # subjects table merged on top of DEFAULT_SUBJECT_ALIASES. Without
        # this, the discovery service's "vladimirputin" subject_keys
        # never resolve to alias lists and every match returns
        # confidence=0 / "unknown_subject". This is the bridge between
        # the Phase-1 discovery-side keys (long form) and the matcher's
        # original short-form keys.
        merged_aliases = self._build_merged_aliases()
        matcher = NewsMatcher(extractor=SubjectExtractor(aliases=merged_aliases))

        # Stage 1 — keyword pre-filter, written to DB. Stage 2 (LLM)
        # patches passed_pre_filter rows in place.
        # For each event with at least one passed_pre_filter result, we
        # do a single LLM call against the union of its subject
        # candidates, then patch every match row for that event.
        bulk_rows: list[NewsMatchRow] = []
        events_needing_llm: list[dict[str, Any]] = []

        for evt in events:
            results = matcher.match(
                headline=evt["headline"],
                body=evt["body_excerpt"],
                markets=contexts,
                article_published_ts=evt["raw_published_ts"],
                source=evt["source"],
            )
            event_passed: list[Any] = []  # list of MatchResult that passed pre-filter
            event_failed: list[Any] = []
            for r in results:
                if r.match_reason == PASSED_REASON:
                    event_passed.append(r)
                else:
                    event_failed.append(r)

            # All FAILED rows go through the bulk insert path (cheap).
            for r in event_failed:
                bulk_rows.append(
                    NewsMatchRow(
                        news_event_id=evt["id"],
                        ticker=r.ticker,
                        confidence=r.confidence,
                        matched_subject=r.matched_subject,
                        matched_keywords=r.matched_keywords or None,
                        match_reason=r.match_reason,
                    )
                )
                MATCHES_WRITTEN.labels(confidence_bucket=_bucket(r.confidence)).inc()

            if not event_passed:
                continue

            # PASSED rows: insert one-by-one to capture row ids for LLM patching.
            inserted: list[tuple[int, Any]] = []
            for r in event_passed:
                row_id = insert_news_match_returning_id(
                    self._db,
                    NewsMatchRow(
                        news_event_id=evt["id"],
                        ticker=r.ticker,
                        confidence=r.confidence,
                        matched_subject=r.matched_subject,
                        matched_keywords=r.matched_keywords or None,
                        match_reason=r.match_reason,
                    ),
                )
                MATCHES_WRITTEN.labels(confidence_bucket=_bucket(r.confidence)).inc()
                inserted.append((row_id, r))

            events_needing_llm.append(
                {
                    "evt": evt,
                    "inserted": inserted,
                    "subject_candidates": {
                        r.matched_subject: merged_aliases.get(r.matched_subject, [])
                        for r in event_passed
                        if r.matched_subject is not None
                    },
                }
            )

        if bulk_rows:
            insert_news_matches(self._db, bulk_rows)

        # Stage 2 — LLM classify each event with at least one passed row.
        if self._classifier is not None and events_needing_llm:
            await self._classify_and_patch(events_needing_llm)

        return len(events)

    async def _classify_and_patch(self, events_needing_llm: list[dict[str, Any]]) -> None:
        """Call the LLM for each event and patch the corresponding match rows.

        Phase 4 Part 2.12 — added a freshness guard: events whose
        ``raw_published_ts`` is older than
        :data:`STALE_ARTICLE_HOURS` (or NULL) skip the LLM call and
        get patched with ``classifier_type='keyword_only'`` +
        ``match_reason='skipped_stale'``. This prevents wasting LLM
        budget on the historical content Google News (and similar
        proxies) periodically surfaces.
        """
        assert self._classifier is not None
        for item in events_needing_llm:
            evt = item["evt"]
            inserted: list[tuple[int, Any]] = item["inserted"]
            candidates: dict[str, list[str]] = item["subject_candidates"]

            # ``evt`` is a sqlite3.Row (no .get); guard for missing
            # column safely via try/except.
            try:
                raw_published_ts = evt["raw_published_ts"]
            except (IndexError, KeyError):
                raw_published_ts = None
            if _article_is_stale(raw_published_ts, STALE_ARTICLE_HOURS):
                log.info(
                    "llm_skipped_stale_article",
                    news_event_id=evt["id"],
                    raw_published_ts=raw_published_ts,
                )
                # Patch every passed row to reflect the skip so the
                # audit trail records why no LLM call happened.
                for row_id, mr in inserted:
                    update_match_with_classification(
                        self._db,
                        match_id=row_id,
                        update=LLMMatchUpdate(
                            classifier_type="keyword_only",
                            confidence=0.0,
                            matched_subject=mr.matched_subject,
                            match_reason="skipped_stale",
                            llm_classification_id=None,
                        ),
                    )
                continue

            try:
                classified = await self._classifier.classify(
                    news_event_id=evt["id"],
                    headline=evt["headline"],
                    body=evt["body_excerpt"],
                    subject_candidates=candidates,
                )
            except ClassifierAuthError as exc:
                log.error("llm_classifier_auth_error", error=repr(exc))
                if self._alerts is not None:
                    with contextlib.suppress(Exception):
                        await self._alerts.send(
                            template_name="alert_critical_anthropic_auth",
                            data={},
                            dedup_key="anthropic_auth",
                            component="news_classifier",
                        )
                # Leave keyword_only rows intact (they're already inserted
                # with confidence=0.0). Continue with the next event.
                continue

            if classified is None:
                # Cap hit, parse failure, or transient error — leave the
                # keyword rows in place. The /spend command + the
                # llm_classifications row record what happened.
                continue

            result, classification_id = classified
            picked_subject = result.subject

            # Patch every passed row for this event. The picked subject
            # becomes the row's matched_subject (the LLM chose one out of
            # the candidate set); rows for OTHER subjects are downgraded
            # back to keyword_only with a "stage_2_picked_other_subject"
            # reason so the audit trail is clear.
            for row_id, mr in inserted:
                if picked_subject is not None and mr.matched_subject == picked_subject:
                    update_match_with_classification(
                        self._db,
                        match_id=row_id,
                        update=LLMMatchUpdate(
                            classifier_type="llm_cascade",
                            confidence=float(result.confidence),
                            matched_subject=picked_subject,
                            match_reason=(
                                f"llm_cascade:interaction={result.interaction_occurred}"
                                f"|tense={result.tense}|negated={result.negated}"
                                f"|indirect={result.indirect_only}"
                            ),
                            llm_classification_id=classification_id,
                        ),
                    )
                else:
                    update_match_with_classification(
                        self._db,
                        match_id=row_id,
                        update=LLMMatchUpdate(
                            classifier_type="llm_cascade",
                            confidence=0.0,
                            matched_subject=mr.matched_subject,
                            match_reason="llm_cascade:not_picked_subject",
                            llm_classification_id=classification_id,
                        ),
                    )

    def _build_merged_aliases(self) -> dict[str, list[str]]:
        """Discovery-service subjects layered on top of DEFAULT_SUBJECT_ALIASES."""
        from trumpbot.db.repositories import subjects_alias_map

        return {**DEFAULT_SUBJECT_ALIASES, **subjects_alias_map(self._db)}


def _bucket(confidence: float) -> str:
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    if confidence > 0:
        return "low"
    return "zero"


# Phase 4 Part 2.10 — ``HeartbeatLogger`` was REMOVED. The DB-only
# periodic liveness logger added log noise without earning its keep:
# the daily digest covers the daily "what's the bot doing?" question,
# /status answers it on demand, and the healthcheck endpoint
# (`/healthz` on port 9090) is the machine-readable liveness probe.


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run() -> None:
    parser = argparse.ArgumentParser(prog="trumpbot")
    parser.add_argument(
        "--config",
        default=os.environ.get("TRUMPBOT_CONFIG", "/etc/trumpbot/config.yaml"),
        help="Path to YAML config (env: TRUMPBOT_CONFIG)",
    )
    args = parser.parse_args()
    config_path = Path(args.config).expanduser()
    if not config_path.is_file():
        print(f"config not found: {config_path}", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_amain(config_path)))


__all__ = ["MatcherWorker", "run"]
