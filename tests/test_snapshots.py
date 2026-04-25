"""Tests for snapshot file writers."""

from __future__ import annotations

import json
from pathlib import Path

from trumpbot.discovery.snapshots import MarketSummaryRow, write_snapshots


def test_writes_json_and_markdown(tmp_path: Path) -> None:
    raw = {"event": {"event_ticker": "KXTRUMPMEET-26APR"}, "markets": []}
    rows = [
        MarketSummaryRow(
            ticker="KXTRUMPMEET-26APR-VPUT",
            subject_full_name="Vladimir Putin",
            title="Donald Trump and Vladimir Putin meet before May 1, 2026?",
        ),
        MarketSummaryRow(
            ticker="KXTRUMPMEET-26APR-MCM",
            subject_full_name="María Corina Machado",
            title="Donald Trump and María Corina Machado meet before May 1, 2026?",
        ),
    ]
    json_path, md_path = write_snapshots(
        snapshot_dir=tmp_path,
        event_ticker="KXTRUMPMEET-26APR",
        raw_response=raw,
        resolution_rules="If Donald Trump and X meet, resolves YES.",
        markets=rows,
    )
    assert json_path == tmp_path / "KXTRUMPMEET-26APR.json"
    assert md_path == tmp_path / "kxtrumpmeet_26apr_summary.md"

    parsed = json.loads(json_path.read_text())
    assert parsed["event"]["event_ticker"] == "KXTRUMPMEET-26APR"

    md = md_path.read_text()
    assert md.startswith("# KXTRUMPMEET-26APR Market Discovery")
    assert "Total markets: 2" in md
    assert "## Verbatim resolution rules" in md
    assert "If Donald Trump and X meet" in md
    assert "| KXTRUMPMEET-26APR-VPUT | Vladimir Putin |" in md
    assert "María Corina Machado" in md


def test_pipe_in_field_is_escaped(tmp_path: Path) -> None:
    rows = [
        MarketSummaryRow(
            ticker="X-1",
            subject_full_name="A | B",
            title="title with | pipe",
        )
    ]
    _, md_path = write_snapshots(
        snapshot_dir=tmp_path,
        event_ticker="X",
        raw_response={},
        resolution_rules="rules",
        markets=rows,
    )
    md = md_path.read_text()
    assert "A \\| B" in md
    assert "title with \\| pipe" in md


def test_creates_directory(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested"
    write_snapshots(
        snapshot_dir=target,
        event_ticker="X",
        raw_response={},
        resolution_rules="r",
        markets=[],
    )
    assert (target / "X.json").exists()
