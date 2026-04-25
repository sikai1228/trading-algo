"""Tests for the daemon's startup subjects-seed step.

The daemon imports ``_seed_subjects`` from ``trumpbot.daemon`` and
calls it on every startup against the YAML at
``cfg.discovery.initial_subjects_path``. This file exercises that
helper directly with a tmp DB and tmp YAML.
"""

from __future__ import annotations

from pathlib import Path

from trumpbot.daemon import _seed_subjects
from trumpbot.db.connection import Database
from trumpbot.db.repositories import get_subject

SAMPLE_YAML = """
subjects:
  - subject_key: vladimirputin
    full_name: Vladimir Putin
    aliases: [Vladimir Putin, Putin]
  - subject_key: xijinping
    full_name: Xi Jinping
    aliases: [Xi Jinping, Xi]
"""


def _seed_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "initial_subjects.yaml"
    p.write_text(body)
    return p


def test_seed_inserts_each_subject(tmp_path: Path) -> None:
    db = Database(tmp_path / "seed.db")
    db.connect()
    yaml_path = _seed_yaml(tmp_path, SAMPLE_YAML)
    _seed_subjects(db, yaml_path)
    putin = get_subject(db, "vladimirputin")
    xi = get_subject(db, "xijinping")
    assert putin is not None
    assert putin["full_name"] == "Vladimir Putin"
    assert xi is not None
    assert xi["full_name"] == "Xi Jinping"
    db.close()


def test_seed_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "idem.db")
    db.connect()
    yaml_path = _seed_yaml(tmp_path, SAMPLE_YAML)
    _seed_subjects(db, yaml_path)
    _seed_subjects(db, yaml_path)
    _seed_subjects(db, yaml_path)
    rows = list(db.connect().execute("SELECT subject_key FROM subjects ORDER BY subject_key"))
    assert [r["subject_key"] for r in rows] == ["vladimirputin", "xijinping"]
    db.close()


def test_seed_missing_file_no_op(tmp_path: Path) -> None:
    db = Database(tmp_path / "missing.db")
    db.connect()
    _seed_subjects(db, tmp_path / "does_not_exist.yaml")
    rows = list(db.connect().execute("SELECT * FROM subjects"))
    assert rows == []
    db.close()


def test_seed_invalid_shape_logs_no_crash(tmp_path: Path) -> None:
    db = Database(tmp_path / "bad.db")
    db.connect()
    yaml_path = _seed_yaml(tmp_path, "subjects: not-a-list\n")
    # Should not raise; entries are simply skipped.
    _seed_subjects(db, yaml_path)
    rows = list(db.connect().execute("SELECT * FROM subjects"))
    assert rows == []
    db.close()


def test_seed_skips_invalid_entries(tmp_path: Path) -> None:
    yaml_body = """
subjects:
  - subject_key: ok
    full_name: OK Name
    aliases: [OK Name]
  - subject_key: 123  # not a string
    full_name: Bad
    aliases: [Bad]
  - {"not": "a-mapping-with-required-keys"}
  - subject_key: also_ok
    full_name: Also OK
    aliases: [Also OK, OK]
"""
    db = Database(tmp_path / "skip.db")
    db.connect()
    yaml_path = _seed_yaml(tmp_path, yaml_body)
    _seed_subjects(db, yaml_path)
    keys = sorted(
        r["subject_key"] for r in db.connect().execute("SELECT subject_key FROM subjects")
    )
    assert keys == ["also_ok", "ok"]
    db.close()


def test_initial_subjects_yaml_loads(tmp_path: Path) -> None:
    """The shipped initial_subjects.yaml must parse + seed every entry."""
    repo_root = Path(__file__).resolve().parent.parent
    yaml_path = repo_root / "config" / "initial_subjects.yaml"
    assert yaml_path.exists(), "config/initial_subjects.yaml is missing"

    db = Database(tmp_path / "shipped.db")
    db.connect()
    _seed_subjects(db, yaml_path)
    count = db.connect().execute("SELECT COUNT(*) FROM subjects").fetchone()[0]
    # The brief lists 22 confirmed subjects for KXTRUMPMEET-26APR.
    assert count == 22
    db.close()
