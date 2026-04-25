"""Thin SQLite connection helper with migration runner and transaction context manager."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


class Database:
    """SQLite connection wrapper that runs migrations on startup."""

    def __init__(self, path: Path | str, migrations_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.migrations_dir = migrations_dir or MIGRATIONS_DIR
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        """Open the connection, configure pragmas, and run pending migrations."""
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        self._conn = conn
        self._run_migrations(conn)
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  filename TEXT PRIMARY KEY,"
            "  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            ")"
        )
        applied = {
            row["filename"] for row in conn.execute("SELECT filename FROM schema_migrations")
        }
        if not self.migrations_dir.is_dir():
            return
        for migration in sorted(self.migrations_dir.glob("*.sql")):
            if migration.name in applied:
                continue
            # Each migration .sql file must wrap its own statements in
            # BEGIN/COMMIT; sqlite3.Connection.executescript issues an
            # implicit COMMIT first, so we cannot wrap it in a Python-level
            # transaction context.
            sql = migration.read_text()
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)",
                (migration.name,),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Context manager that wraps a SQLite transaction with commit/rollback."""
        conn = self._conn if self._conn is not None else self.connect()
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


@contextmanager
def transaction(db: Database) -> Iterator[sqlite3.Connection]:
    """Module-level convenience wrapper around ``Database.transaction``."""
    with db.transaction() as conn:
        yield conn
