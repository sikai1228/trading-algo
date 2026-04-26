# trading-algo

Algorithmic trading bot for Kalshi prediction markets focused on "Will Trump talk to/meet/mention X this month?" markets. The system observes markets and news in real time, scores each article against the active markets, and persists everything for later analysis. **Phase 1 is observation only — no orders are placed.**

See `TRADING.md` (kept outside this repo) for the full architectural spec.

## Project layout

```
trumpbot/
  kalshi/          REST client (auth, rate-limit, schemas, client, exceptions)
  market_data/     MarketDataFeed ABC + KalshiWebSocketFeed
  news/            NewsMonitor ABC + RSSPoller + TwitterScraper +
                   TruthSocialScraper + NewsMatcher
  discovery/       MarketDiscoveryService + SubjectExtractor
  decision/        DecisionEngine ABC (Phase 2)
  risk/            RiskManager ABC (Phase 2)
  executor/        Executor + ApprovalGate ABCs (Phase 3)
  db/              Database + migrations runner + repositories
  events/          In-process pub/sub event bus
  health/          Localhost /health + /metrics HTTP server
  utils/           timeutil, url canonicalization, structlog setup
  config.py        Pydantic-validated YAML config loader
  daemon.py        Top-level orchestrator + MatcherWorker
  __main__.py      `python -m trumpbot`

config/
  config.example.yaml      full source list and tuning knobs
  subject_aliases.yaml     subject -> alias dictionary (configurable)
  match_verbs.yaml         documentation of matcher verb lists

migrations/
  001_initial.sql          full Phase 1 + Phase 2/3 table definitions

deploy/
  trumpbot.service         hardened systemd unit
  litestream.yml           continuous SQLite replication to S3/B2
  setup.sh                 idempotent one-shot installer

scripts/
  inspect_data.py          read-only summary of captured data
  replay_news_match.py     re-run matcher against a stored event

tests/
  test_kalshi_*.py         auth, rate-limit, REST client (respx-mocked)
  test_news_matcher.py     ~70 tests covering positives, negation,
                           future tense, indirect language, alias
                           variations, partial-name safety, windows
  test_db.py               schema + repositories + idempotency
  test_rss_poller.py       end-to-end RSS persistence with respx
  test_*.py                config loader, event bus, utils, subjects
```

## Setup

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
# install all pinned deps + dev tools
uv sync

# one-time pre-commit hook install
uv run pre-commit install

# regenerate the detect-secrets baseline (only after auditing real findings)
uv run detect-secrets scan --baseline .secrets.baseline
```

## Environment variables

Production reads secrets from `/etc/trumpbot/secrets.env` (mode 0600, owned by the `trumpbot` service user, sourced by systemd via `EnvironmentFile=`). For local development, copy the template below into a local `.env` (gitignored) and source it before running.

| Variable | Purpose |
| --- | --- |
| `TRUMPBOT_CONFIG` | Path to the YAML config (default `/etc/trumpbot/config.yaml`) |
| `KALSHI_API_KEY_ID` | Kalshi API key identifier |
| `KALSHI_PRIVATE_KEY_PASSPHRASE` | Passphrase for the encrypted RSA private key |
| `TWITTER_BEARER_TOKEN` | Optional. If unset, Twitter ingestion is silently disabled. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | For litestream backups |
| `LITESTREAM_BUCKET`, `LITESTREAM_REGION`, `LITESTREAM_ENDPOINT` | litestream destination |

The Kalshi RSA private key lives at `/etc/trumpbot/kalshi_private.pem` (mode 0600). Encrypt at rest with a passphrase the operator types on every restart.

## Configuration

`config/config.example.yaml` enumerates every news source from the Phase 1 brief with appropriate weights and `is_kalshi_approved` flags. Verify the exact `target_series` strings against the live Kalshi platform before deployment — the spec uses `KXTRUMPCALL`, `KXTRUMPMEET`, `KXTRUMPMENTION` as a starting point, but Kalshi may rename or split series.

## Running migrations

Migrations live in `migrations/` and are applied automatically the first time `Database.connect()` is called. The connection helper tracks applied filenames in a `schema_migrations` table.

```bash
uv run python -c "from trumpbot.db import Database; Database('trumpbot.db').connect()"
```

## Running the daemon

```bash
# Local development with a hand-edited config:
TRUMPBOT_CONFIG=/path/to/config.yaml uv run python -m trumpbot

# Or via the systemd unit on a deployed VPS:
sudo systemctl start trumpbot
sudo journalctl -u trumpbot -f
```

The daemon starts the following concurrent tasks:

1. **MarketDiscoveryService** — polls Kalshi REST every 5 min, upserts target-series markets.
2. **KalshiWebSocketFeed** — live orderbook + trade feed, periodic price snapshots, automatic reconnect.
3. **RSSPoller** — one task per source from the configured list.
4. **TwitterScraper** — one task per handle (requires bearer token; otherwise no-op).
5. **TruthSocialScraper** — polls @realDonaldTrump.
6. **MatcherWorker** — consumes new news events, runs the matcher against active markets.
7. **HealthcheckServer** — `127.0.0.1:9090/health` and `/metrics` (Prometheus).
   (Phase 4 Part 2.10 removed the `HeartbeatLogger`; the healthcheck endpoint
   is the machine-readable liveness probe, the daily digest covers the
   operator-facing "is it alive?" question.)

SIGTERM/SIGINT trigger graceful shutdown.

## Inspecting captured data

```bash
# summary of markets, recent news, high-confidence matches, daily stats
uv run python scripts/inspect_data.py --db /var/lib/trumpbot/trumpbot.db

# re-run the matcher on a specific stored news event for debugging
uv run python scripts/replay_news_match.py 12345 --db /var/lib/trumpbot/trumpbot.db
```

## Tests, lint, type-check

```bash
uv run pytest          # 127 tests, ~1.5s
uv run ruff check .
uv run black --check .
uv run mypy
```

All four are required to pass in CI. The matcher test suite (`tests/test_news_matcher.py`) is the spec for the matcher — when matcher behavior changes, those tests change with it.

## Build order

- **Phase 1 (this scaffold)**: data collection only. Markets, prices, news, matches. No trading.
- **Phase 2**: DecisionEngine + RiskManager + DryRunExecutor. Backtest against Phase 1 data. Tune thresholds.
- **Phase 3**: Telegram approval gate, kill switch, monitoring. Continue dry-run.
- **Phase 4**: Switch to live executor with $500 bankroll and 2% max position size.
- **Phase 5**: Increase bankroll based on observed Sharpe / drawdown. Consider auto-mode after 60+ days of clean human-approved data.
- **Future**: Web observability backend reads from the same SQLite database via `bot.queries`. Daemon does not change.

See `TRADING.md` § Build Order for the full sequence.
