"""snapshot_contract.py — fetch the verbatim Kalshi resolution rules.

Used to (re)snapshot ``data/contracts/kxtrumpmeet_rules.txt`` from
the live Kalshi API. Run this on first deployment, and again any
time Kalshi updates the contract text — the LLM cascade hashes the
file content on every classification call and fires
``alert_critical_contract_rules_changed`` when the hash drifts.

Usage:

    uv run python scripts/snapshot_contract.py [--ticker TICKER]
                                                [--out PATH]
                                                [--config PATH]

If no ``--ticker`` is given, picks any open KXTRUMPMEET-* market.
Writes the ``rules_primary`` field verbatim to ``--out`` (default
``data/contracts/kxtrumpmeet_rules.txt``) and prints the SHA-256.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trumpbot.config import load_config  # noqa: E402
from trumpbot.kalshi.auth import load_private_key  # noqa: E402
from trumpbot.kalshi.client import KalshiClient  # noqa: E402

DEFAULT_OUT = _REPO_ROOT / "data" / "contracts" / "kxtrumpmeet_rules.txt"
DEFAULT_CONFIG = Path(
    os.environ.get(
        "TRUMPBOT_CONFIG",
        str(_REPO_ROOT / "config" / "config.example.yaml"),
    )
)


async def _fetch(ticker: str | None, out: Path, config_path: Path) -> int:
    cfg = load_config(config_path)
    private_key = load_private_key(
        Path(cfg.kalshi.private_key_path).expanduser(),
        passphrase=(
            cfg.kalshi.private_key_passphrase.encode()
            if cfg.kalshi.private_key_passphrase
            else None
        ),
    )
    async with KalshiClient(
        api_key_id=cfg.kalshi.api_key_id,
        private_key=private_key,
        base_url=cfg.kalshi.base_url,
    ) as client:
        page = await client.list_markets(series_ticker="KXTRUMPMEET", limit=200, status=None)
        markets = list(page.markets)

    if not markets:
        print("no KXTRUMPMEET markets returned", file=sys.stderr)
        return 1
    if ticker is None:
        chosen = next((m for m in markets if m.status == "active"), markets[0])
    else:
        match = next((m for m in markets if m.ticker == ticker), None)
        if match is None:
            print(f"ticker {ticker!r} not found in KXTRUMPMEET-*", file=sys.stderr)
            return 1
        chosen = match
    rules = chosen.rules_primary or chosen.rules_secondary or ""
    if not rules:
        print(f"market {chosen.ticker} has no rules text", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rules)
    h = hashlib.sha256(rules.encode("utf-8")).hexdigest()
    print(f"wrote {len(rules)} bytes to {out}")
    print(f"sha256: {h}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default=None, help="Specific KXTRUMPMEET-* ticker")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    return asyncio.run(_fetch(args.ticker, args.out, args.config))


if __name__ == "__main__":
    raise SystemExit(main())
