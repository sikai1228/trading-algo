"""Backtest CLI — replay historical news_market_matches through the
production DecisionEngine + RiskManager and print a summary.

Usage:
    uv run python -m scripts.backtest --start 2026-04-01 --end 2026-04-30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python scripts/backtest.py` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trumpbot.backtest.replay import Backtester  # noqa: E402
from trumpbot.platform_paths import current_platform_paths  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="ISO date or timestamp")
    parser.add_argument("--end", required=True, help="ISO date or timestamp")
    parser.add_argument(
        "--db",
        default=None,
        help="Path to trumpbot.db (default: platform default)",
    )
    parser.add_argument("--bankroll-usd", type=float, default=500.0)
    parser.add_argument(
        "--csv",
        default=None,
        help="Optional path to write a per-trade CSV (default: data/backtest_results/<ts>.csv)",
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else current_platform_paths().database_path
    if not db_path.exists():
        print(f"database not found at {db_path}", file=sys.stderr)
        return 2

    bt = Backtester(db_path=db_path, starting_bankroll_usd=args.bankroll_usd)
    start = args.start if "T" in args.start else f"{args.start}T00:00:00Z"
    end = args.end if "T" in args.end else f"{args.end}T23:59:59Z"
    result = bt.run(start_ts=start, end_ts=end)

    print()
    line = "─" * 60
    print(line)
    print(f" backtest {start} -> {end}")
    print(line)
    print(f"  total trades:        {result.total_trades}")
    print(f"  winning trades:      {result.winning_trades}")
    print(f"  losing trades:       {result.losing_trades}")
    print(f"  win rate:            {result.win_rate:.2%}")
    print(f"  realized P&L:        ${result.total_realized_pnl_usd_cents/100:+.2f}")
    print(f"  unrealized P&L:      ${result.total_unrealized_pnl_usd_cents/100:+.2f}")
    print(f"  avg entry price:     {result.average_entry_price_cents}c")
    print(f"  avg exit price:      {result.average_exit_price_cents}c")
    print(f"  avg hold time:       {result.average_hold_time_hours:.1f}h")
    print(f"  Sharpe (annualized): {result.sharpe_ratio:.2f}")
    print(f"  max drawdown:        ${result.max_drawdown_usd_cents/100:.2f}")
    print(f"  risk rejections:     {result.risk_rejections}")
    print()
    # Phase 3 Part 1 v2 metrics: fee + slippage cost on the strategy's edge.
    print(f"  schema version:      v{result.schema_version}")
    print(f"  total entry fees:    ${result.total_entry_fees_cents/100:.2f}")
    print(f"  total exit fees:     ${result.total_exit_fees_cents/100:.2f}")
    print(f"  total slippage:      ${result.total_slippage_cents/100:.2f}")
    delta = result.ideal_realized_pnl_usd_cents - result.total_realized_pnl_usd_cents
    print(
        f"  ideal P&L (no fees): ${result.ideal_realized_pnl_usd_cents/100:+.2f}  "
        f"(delta vs realized: ${delta/100:+.2f})"
    )
    if result.by_source:
        print("  by source:")
        for source, agg in sorted(result.by_source.items()):
            print(
                f"    {source:<25s} trades={agg['trades']:>3} "
                f"P&L=${agg['realized_pnl_usd_cents']/100:+.2f}"
            )
    print(line)

    if args.csv:
        csv_path = Path(args.csv)
    else:
        from datetime import UTC, datetime

        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        csv_path = _REPO_ROOT / "data" / "backtest_results" / f"{ts}.csv"
    bt.write_csv(result, csv_path)
    print(f" trade log written to: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
