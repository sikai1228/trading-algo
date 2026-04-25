"""Snapshot writers: persist discovered events to ``data/markets/`` for
human review and historical record.

Two artifacts per event:

- ``data/markets/<event_ticker>.json`` — verbatim API response
  (formatted with ``json.dumps(..., indent=2, sort_keys=True)``).
- ``data/markets/<event_ticker>_summary.md`` — Markdown summary with
  the verbatim resolution rules (Markets resolve based on this exact
  text — never paraphrase) and a per-market table.

These files go in git so each new month produces a permanent record
of what markets existed at discovery time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trumpbot.utils.timeutil import utcnow_iso


@dataclass(frozen=True)
class MarketSummaryRow:
    """One row in the snapshot's Markets table."""

    ticker: str
    subject_full_name: str
    title: str


def write_snapshots(
    *,
    snapshot_dir: Path,
    event_ticker: str,
    raw_response: dict[str, Any],
    resolution_rules: str,
    markets: list[MarketSummaryRow],
) -> tuple[Path, Path]:
    """Write the JSON + Markdown snapshot pair. Returns ``(json_path, md_path)``.

    Both files are overwritten on every call (the JSON is the most
    recent API response; the Markdown summary is regenerated to match).
    The discovery service only invokes this when it has a fresh
    response with at least one market — empty events are skipped.
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    json_path = snapshot_dir / f"{event_ticker}.json"
    md_path = snapshot_dir / f"{_md_basename(event_ticker)}_summary.md"

    json_path.write_text(json.dumps(raw_response, indent=2, sort_keys=True))
    md_path.write_text(_render_markdown(event_ticker, resolution_rules, markets))
    return json_path, md_path


def _md_basename(event_ticker: str) -> str:
    """Lowercase, underscore-separated form of the event ticker for the md filename."""
    return event_ticker.lower().replace("-", "_")


def _render_markdown(
    event_ticker: str, resolution_rules: str, markets: list[MarketSummaryRow]
) -> str:
    captured = utcnow_iso()
    lines = [
        f"# {event_ticker} Market Discovery",
        f"Captured: {captured}",
        f"Total markets: {len(markets)}",
        "",
        "## Verbatim resolution rules",
        "",
        resolution_rules.strip(),
        "",
        "## Markets",
        "",
        "| Ticker | Subject | Title |",
        "|---|---|---|",
    ]
    for m in sorted(markets, key=lambda r: r.ticker):
        # Escape pipes in fields so the Markdown table doesn't break.
        title = m.title.replace("|", "\\|")
        subject = m.subject_full_name.replace("|", "\\|")
        lines.append(f"| {m.ticker} | {subject} | {title} |")
    lines.append("")
    return "\n".join(lines)
