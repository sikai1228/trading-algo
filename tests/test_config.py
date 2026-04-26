"""Tests for YAML config loader and env-var expansion."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from trumpbot.config import load_config

SAMPLE_CONFIG = """
kalshi:
  api_key_id: "${TEST_KEY_ID}"
  private_key_path: "/tmp/key.pem"
  target_series:
    - KXTRUMPCALL
news:
  sources:
    - name: "reuters"
      type: "rss"
      url: "https://example.com/feed.xml"
      poll_interval_sec: 90
      weight: 1.0
      is_kalshi_approved: true
"""


def test_load_and_env_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY_ID", "abc-123")
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE_CONFIG)
    cfg = load_config(p)
    assert cfg.kalshi.api_key_id == "abc-123"
    assert cfg.kalshi.target_series == ["KXTRUMPCALL"]
    assert cfg.news.sources[0].name == "reuters"
    assert cfg.news.sources[0].type == "rss"


def test_missing_env_becomes_empty(tmp_path: Path) -> None:
    os.environ.pop("TEST_KEY_ID", None)
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE_CONFIG)
    cfg = load_config(p)
    assert cfg.kalshi.api_key_id == ""


def test_invalid_config_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("not: valid: yaml: structure: here:")
    with pytest.raises((ValueError, yaml.YAMLError)):
        load_config(p)
