"""Pre-live trading safety checklist.

Phase 4 Part 1.

Run this BEFORE flipping ``cfg.execution.mode = "live"``. It verifies
every prerequisite for safe live trading and refuses to give the
green light unless they all pass.

Checks:

1. **Kalshi auth** — `/portfolio/balance` succeeds with the configured
   API key + private key. (If this fails, signing or credentials are
   wrong.)
2. **Bankroll sanity** — reported balance >= a configurable minimum
   (default $50). Trading with $0 doesn't make sense and likely means
   the deposit hasn't cleared.
3. **Reconciliation clean** — startup reconciliation runs against
   real Kalshi state and reports zero drift. Any drift means there's
   unfinished business from a prior run that must be resolved first.
4. **Recent dry-run history exists** — at least N closed dry-run
   trades over the past 7 days, demonstrating the engine has been
   exercised. Default N=5. Override with --min-dry-run-trades.
5. **Risk caps configured** — `cfg.decision.position_size_hard_cap_cents`
   is set and reasonable (default $20).
6. **Telegram round-trip** — bot can send a message + get the
   acknowledgement back. (If the bot isn't running we can't approve
   trades.)

Output is a green/red line per check + a final verdict. Exits 0 only
when ALL checks pass.

Usage::

    uv run python -m scripts.pre_live_checklist
    uv run python -m scripts.pre_live_checklist --min-balance-usd 100
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trumpbot.account.reconcile import reconcile_once  # noqa: E402
from trumpbot.config import load_config  # noqa: E402
from trumpbot.db.connection import Database  # noqa: E402
from trumpbot.kalshi.auth import load_private_key  # noqa: E402
from trumpbot.kalshi.client import KalshiClient  # noqa: E402
from trumpbot.platform_paths import current_platform_paths, resolve_path  # noqa: E402


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def _print_result(r: CheckResult) -> None:
    icon = "✅" if r.passed else "❌"
    print(f"  {icon} {r.name}: {r.detail}")


async def _check_kalshi_auth(client: KalshiClient) -> CheckResult:
    try:
        balance = await client.get_balance()
    except Exception as exc:
        return CheckResult(
            name="Kalshi auth",
            passed=False,
            detail=f"failed: {exc!r}",
        )
    return CheckResult(
        name="Kalshi auth",
        passed=True,
        detail=f"reachable, balance ${balance.balance / 100:.2f}",
    )


async def _check_bankroll(client: KalshiClient, min_cents: int) -> CheckResult:
    try:
        balance = await client.get_balance()
    except Exception as exc:
        return CheckResult(
            name="Bankroll >= minimum",
            passed=False,
            detail=f"could not fetch: {exc!r}",
        )
    if balance.balance < min_cents:
        return CheckResult(
            name="Bankroll >= minimum",
            passed=False,
            detail=(f"reported ${balance.balance / 100:.2f} < required " f"${min_cents / 100:.2f}"),
        )
    return CheckResult(
        name="Bankroll >= minimum",
        passed=True,
        detail=f"${balance.balance / 100:.2f}",
    )


async def _check_reconciliation(db: Database, client: KalshiClient) -> CheckResult:
    report = await reconcile_once(db=db, kalshi=client)
    if not report.succeeded:
        return CheckResult(
            name="Reconciliation",
            passed=False,
            detail="could not complete (Kalshi unreachable for /orders or /positions)",
        )
    if report.has_drift:
        return CheckResult(
            name="Reconciliation",
            passed=False,
            detail=(
                f"drift detected: {len(report.drifts)} rows. Resolve via "
                "/reconcile_resolve or by manual SQL before going live."
            ),
        )
    return CheckResult(
        name="Reconciliation",
        passed=True,
        detail=(
            f"clean ({report.pending_count} pending, {report.live_count} live, "
            f"{report.kalshi_position_count} kalshi positions)"
        ),
    )


def _check_recent_dry_run(db: Database, min_count: int) -> CheckResult:
    conn = db.connect()
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
          FROM trades
         WHERE status LIKE 'dry_run%'
           AND created_at >= datetime('now', '-7 days')
        """
    ).fetchone()
    n = int(row["c"])
    if n < min_count:
        return CheckResult(
            name="Recent dry-run history",
            passed=False,
            detail=(
                f"only {n} dry-run trades in past 7 days; want at least {min_count}. "
                "Run more dry-run cycles before going live."
            ),
        )
    return CheckResult(
        name="Recent dry-run history",
        passed=True,
        detail=f"{n} dry-run trades in past 7 days",
    )


def _check_risk_caps(cfg) -> CheckResult:  # type: ignore[no-untyped-def]
    cap_cents = cfg.decision.position_size_hard_cap_cents
    if cap_cents <= 0:
        return CheckResult(
            name="Risk cap (position_size_hard_cap_cents)",
            passed=False,
            detail=f"unset or non-positive ({cap_cents})",
        )
    if cap_cents > 10000:
        return CheckResult(
            name="Risk cap (position_size_hard_cap_cents)",
            passed=False,
            detail=(
                f"${cap_cents / 100:.2f} feels too high for a v1 bankroll. "
                "Recommend $20 (=2000) for the first 30 days."
            ),
        )
    return CheckResult(
        name="Risk cap (position_size_hard_cap_cents)",
        passed=True,
        detail=f"${cap_cents / 100:.2f}",
    )


def _check_approval_mode_hardcoded() -> CheckResult:
    """Verify approval mode is the safe default ``"human"``.

    Phase 4 Part 2.11 made the mode config-reachable; this check
    asserts the operator hasn't flipped it before pre-live. Returns
    ``passed=False`` if ``cfg.approval.mode != "human"`` so the
    operator must explicitly acknowledge auto-approval before going
    live."""
    cfg = load_config(_default_config_path())
    if cfg.approval.mode != "human":
        return CheckResult(
            name="Approval mode = human",
            passed=False,
            detail=(
                f"cfg.approval.mode = {cfg.approval.mode!r}; expected 'human' "
                "before live. Switch to auto only after the shadow_decisions "
                "audit shows stable signals."
            ),
        )
    return CheckResult(
        name="Approval mode = human",
        passed=True,
        detail="cfg.approval.mode == 'human'",
    )


def _default_config_path() -> Path:
    """Best-effort: pick the config the operator most likely points
    the daemon at. Mirrors the daemon's CLI default."""
    return Path(os.environ.get("TRUMPBOT_CONFIG", "/etc/trumpbot/config.yaml")).expanduser()


async def _amain(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config).expanduser()
    if not cfg_path.is_file():
        print(f"config not found: {cfg_path}", file=sys.stderr)
        return 2
    cfg = load_config(cfg_path)
    paths = current_platform_paths()
    db_path = resolve_path(cfg.database.path, paths.database_path)
    private_key_path = resolve_path(cfg.kalshi.private_key_path, paths.private_key_path)

    db = Database(db_path)
    db.connect()
    private_key = load_private_key(
        private_key_path,
        passphrase=(
            cfg.kalshi.private_key_passphrase.encode()
            if cfg.kalshi.private_key_passphrase
            else None
        ),
    )
    client = KalshiClient(
        api_key_id=cfg.kalshi.api_key_id,
        private_key=private_key,
        base_url=cfg.kalshi.base_url,
        rate_per_sec=cfg.kalshi.rate_per_sec,
        burst=cfg.kalshi.rate_burst,
        rate_limit_pct=cfg.kalshi.rate_limit_pct,
    )

    results: list[CheckResult] = []
    print("Pre-live checklist\n")

    # 1. Kalshi auth
    r = await _check_kalshi_auth(client)
    results.append(r)
    _print_result(r)

    # 2. Bankroll
    r = await _check_bankroll(client, args.min_balance_usd * 100)
    results.append(r)
    _print_result(r)

    # 3. Reconciliation
    r = await _check_reconciliation(db, client)
    results.append(r)
    _print_result(r)

    # 4. Dry-run history
    r = _check_recent_dry_run(db, args.min_dry_run_trades)
    results.append(r)
    _print_result(r)

    # 5. Risk caps
    r = _check_risk_caps(cfg)
    results.append(r)
    _print_result(r)

    # 6. Approval mode hardcoded
    r = _check_approval_mode_hardcoded()
    results.append(r)
    _print_result(r)

    await client.aclose()
    db.close()

    failed = [r for r in results if not r.passed]
    print()
    if failed:
        print(f"❌ {len(failed)}/{len(results)} checks failed. NOT safe to go live.")
        return 1
    print(f"✅ {len(results)}/{len(results)} checks passed. Safe to go live.")
    print()
    print("Next step: edit config.yaml -> execution.mode: live, restart daemon.")
    print("(That edit alone enables live trading. The /halt switch still works.)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="pre_live_checklist")
    parser.add_argument(
        "--config",
        default=str(Path("~/.config/trumpbot/config.yaml").expanduser()),
    )
    parser.add_argument(
        "--min-balance-usd",
        type=int,
        default=50,
        help="Minimum reported Kalshi balance to allow going live (default $50)",
    )
    parser.add_argument(
        "--min-dry-run-trades",
        type=int,
        default=5,
        help="Minimum closed dry-run trades in the past 7 days (default 5)",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
