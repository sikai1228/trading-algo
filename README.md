# trading-algo

Algorithmic trading bot for Kalshi prediction markets. Single-server, single-process Python 3.11+ system, human-in-the-loop via Telegram. See `TRADING.md` (kept outside this repo) for the full architectural spec.

This is the v1 scaffold. No business logic is implemented yet; only abstract base classes for the five core modules, the SQLite schema, and the systemd unit file.

## Project layout

```
trumpbot/
  market_data/   MarketDataFeed ABC
  news/          NewsMonitor ABC
  decision/      DecisionEngine ABC
  risk/          RiskManager ABC
  executor/      Executor + ApprovalGate ABCs
  db/            SQLite connection + migration runner
migrations/
  001_initial.sql   full schema (markets, price_snapshots, news_events,
                    trades, trade_news_links, risk_decisions, system_events)
deploy/
  trumpbot.service  systemd unit
tests/             pytest suite (empty)
```

## Setup

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
# install dependencies (creates .venv, installs everything pinned in pyproject.toml)
uv sync

# install pre-commit hooks
uv run pre-commit install

# initialize the detect-secrets baseline (one-time)
uv run detect-secrets scan > .secrets.baseline
```

## Environment variables

Production reads secrets from `/etc/trumpbot/secrets.env` (mode 0600, owned by the `trumpbot` service user). For local development, copy the template below into a local `.env` (gitignored) and source it before running.

| Variable | Purpose |
| --- | --- |
| `TRUMPBOT_DB_PATH` | Path to the SQLite database file (default: `./trumpbot.db`) |
| `KALSHI_API_KEY_ID` | Kalshi API key identifier |
| `KALSHI_PRIVATE_KEY_PATH` | Path to the encrypted RSA private key (mode 0600) |
| `KALSHI_PRIVATE_KEY_PASSPHRASE` | Passphrase entered manually at bot startup |
| `KALSHI_ENV` | `demo` or `prod` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID_ALLOWLIST` | Comma-separated list of allowed chat ids (typically one) |
| `EXECUTOR_MODE` | `dry_run`, `paper`, or `live` (default: `dry_run`) |
| `APPROVAL_MODE` | `human` or `auto` (default: `human`) |

None of these are read by code yet; they are documented here so future sessions wire them in consistently.

## Running migrations

Migrations live in `migrations/` and are applied automatically the first time `trumpbot.db.Database.connect()` is called. The connection helper tracks applied filenames in a `schema_migrations` table.

To apply migrations from a fresh shell:

```bash
uv run python -c "from trumpbot.db import Database; Database('trumpbot.db').connect()"
```

## Tests

```bash
uv run pytest
```

The suite is empty in this scaffold; the command exits successfully with no tests collected.

## Lint and type-check

```bash
uv run ruff check .
uv run black --check .
uv run mypy
```

All three are required to pass in CI before any change is merged.

## Build order

Phase 1 (the next milestone) is read-only data collection: Kalshi REST client, market discovery, price snapshot collector, RSS news monitor, keyword matcher. No decision engine, no executor, no real money. See `TRADING.md` § Build Order for the full sequence.
