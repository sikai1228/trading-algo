"""End-to-end smoke test for the trumpbot daemon.

Run this before loading the launchd agent. It launches the real
daemon as a subprocess against your real config (and therefore real
credentials, real Kalshi, real news sources, real database) for 60
seconds, then queries the database to confirm the daemon actually
did the work it's supposed to do.

Asserts:
  1. ≥ 1 market discovered in the ``markets`` table
  2. ≥ 1 row in ``price_snapshots``
  3. ≥ 1 row in ``news_events`` (any RSS source polled successfully)
  4. zero ``system_events`` with ``severity = 'critical'``

Exits 0 on success, non-zero on any failed assertion. Prints a
clean summary either way.

Usage:
    uv run python scripts/smoke_test.py
    uv run python scripts/smoke_test.py --config /path/to/config.yaml
    uv run python scripts/smoke_test.py --duration-sec 90
"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow ``python scripts/smoke_test.py`` to import trumpbot regardless of
# the caller's cwd (running scripts from the repo root would normally
# work, but we want the launchd plist's ProgramArguments to be robust).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import yaml  # noqa: E402

from trumpbot.platform_paths import current_platform_paths, resolve_path  # noqa: E402

DEFAULT_DURATION_SEC = 60


@dataclass
class SmokeResult:
    markets_discovered: int
    price_snapshots: int
    news_events: int
    critical_events: list[dict[str, Any]]
    daemon_exit_code: int | None
    new_markets_during_run: int
    new_snapshots_during_run: int
    new_news_during_run: int

    @property
    def passed(self) -> bool:
        return (
            self.new_markets_during_run >= 1
            and self.new_snapshots_during_run >= 1
            and self.new_news_during_run >= 1
            and not self.critical_events
        )


def _resolve_config_path(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value).expanduser()
    env_value = os.environ.get("TRUMPBOT_CONFIG")
    if env_value:
        return Path(env_value).expanduser()
    return current_platform_paths().config_yaml_path


def _resolve_db_path(config_path: Path) -> Path:
    raw = yaml.safe_load(config_path.read_text())
    db_field = (raw or {}).get("database", {}).get("path", "auto")
    paths = current_platform_paths()
    return resolve_path(db_field, paths.database_path)


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0])


def _critical_events_since(conn: sqlite3.Connection, since_iso: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT event_type, component, message, ts
            FROM system_events
            WHERE severity = 'critical' AND ts >= ?
            ORDER BY ts ASC
            """,
            (since_iso,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "event_type": r[0],
            "component": r[1],
            "message": r[2],
            "ts": r[3],
        }
        for r in rows
    ]


def _utcnow_iso() -> str:
    from trumpbot.utils.timeutil import utcnow_iso

    return utcnow_iso()


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    return conn


def _baseline_counts(db_path: Path) -> tuple[int, int, int]:
    if not db_path.exists():
        return (0, 0, 0)
    conn = _open_readonly(db_path)
    try:
        return (
            _table_count(conn, "markets"),
            _table_count(conn, "price_snapshots"),
            _table_count(conn, "news_events"),
        )
    finally:
        conn.close()


@contextmanager
def _launch_daemon(config_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    """Spawn the daemon, attached to its own process group for clean shutdown."""
    cmd = [sys.executable, "-m", "trumpbot", "--config", str(config_path)]
    print(f"[smoke] launching: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                print("[smoke] daemon did not exit on SIGTERM; sending SIGKILL")
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait(timeout=5)


def run_smoke_test(*, config_path: Path, duration_sec: int) -> SmokeResult:
    if not config_path.is_file():
        raise SystemExit(f"config not found at {config_path}")
    db_path = _resolve_db_path(config_path)
    print(f"[smoke] config: {config_path}")
    print(f"[smoke] database: {db_path}")

    started_iso = _utcnow_iso()
    baseline_markets, baseline_snaps, baseline_news = _baseline_counts(db_path)
    print(
        f"[smoke] baseline: markets={baseline_markets} "
        f"snapshots={baseline_snaps} news_events={baseline_news}"
    )

    deadline = time.monotonic() + duration_sec
    daemon_exit_code: int | None = None

    with _launch_daemon(config_path) as proc:
        last_tick = 0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                # Daemon exited early — capture and break out of the wait loop.
                daemon_exit_code = proc.returncode
                print(f"[smoke] daemon exited early with code {daemon_exit_code}")
                break
            elapsed = int(duration_sec - (deadline - time.monotonic()))
            if elapsed and elapsed != last_tick and elapsed % 10 == 0:
                last_tick = elapsed
                print(f"[smoke] {elapsed}s/{duration_sec}s elapsed; daemon pid={proc.pid}")
            time.sleep(1)
        if proc.poll() is None:
            print(f"[smoke] {duration_sec}s elapsed; sending SIGTERM")

    # Now read the database (read-only).
    if not db_path.exists():
        raise SystemExit(f"database was not created at {db_path}")
    conn = _open_readonly(db_path)
    try:
        markets_total = _table_count(conn, "markets")
        snaps_total = _table_count(conn, "price_snapshots")
        news_total = _table_count(conn, "news_events")
        critical = _critical_events_since(conn, started_iso)
    finally:
        conn.close()

    return SmokeResult(
        markets_discovered=markets_total,
        price_snapshots=snaps_total,
        news_events=news_total,
        critical_events=critical,
        daemon_exit_code=daemon_exit_code,
        new_markets_during_run=markets_total - baseline_markets,
        new_snapshots_during_run=snaps_total - baseline_snaps,
        new_news_during_run=news_total - baseline_news,
    )


def _print_summary(result: SmokeResult) -> None:
    line = "─" * 60
    print()
    print(line)
    print(" trumpbot smoke test summary")
    print(line)
    checks = [
        ("≥1 market discovered", result.new_markets_during_run >= 1, result.new_markets_during_run),
        (
            "≥1 price snapshot written",
            result.new_snapshots_during_run >= 1,
            result.new_snapshots_during_run,
        ),
        ("≥1 news event ingested", result.new_news_during_run >= 1, result.new_news_during_run),
        ("0 critical system_events", not result.critical_events, len(result.critical_events)),
    ]
    for label, ok, value in checks:
        marker = "✅" if ok else "❌"
        print(f" {marker} {label:<32} (during run: {value})")
    print(line)
    print(
        f" totals in DB: markets={result.markets_discovered} "
        f"snapshots={result.price_snapshots} news_events={result.news_events}"
    )
    if result.daemon_exit_code is not None:
        print(f" ⚠️  daemon exited early with code {result.daemon_exit_code}")
    if result.critical_events:
        print(" critical system_events captured:")
        for ev in result.critical_events:
            print(f"   - [{ev['ts']}] {ev['component']}/{ev['event_type']}: {ev['message'][:120]}")
    print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.yaml (default: platform default)")
    parser.add_argument(
        "--duration-sec",
        type=int,
        default=DEFAULT_DURATION_SEC,
        help=f"How long to run the daemon (default: {DEFAULT_DURATION_SEC})",
    )
    args = parser.parse_args()

    config_path = _resolve_config_path(args.config)
    result = run_smoke_test(config_path=config_path, duration_sec=args.duration_sec)
    _print_summary(result)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
