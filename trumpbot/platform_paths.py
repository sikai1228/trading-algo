"""Platform-aware filesystem path defaults.

The daemon detects ``platform.system()`` at startup and uses the
filesystem layout conventional for that OS:

- **Darwin (macOS)** — Apple's per-user layout:

  =====================  ====================================================
  Config                 ``~/.config/trumpbot/``
  Database               ``~/Library/Application Support/trumpbot/``
  Logs                   ``~/Library/Logs/trumpbot/``
  Private key            ``~/.config/trumpbot/kalshi_private.pem``
  =====================  ====================================================

- **Linux** — FHS system-wide layout used by the systemd unit:

  =====================  ============================
  Config                 ``/etc/trumpbot/``
  Database               ``/var/lib/trumpbot/``
  Logs                   ``/var/log/trumpbot/``
  Private key            ``/etc/trumpbot/kalshi_private.pem``
  =====================  ============================

- **Other** — falls back to ``$HOME/.trumpbot/`` (testing / unknown OS).

Config files written for the daemon may use the literal string
``"auto"`` for any path field; ``resolve_path`` substitutes the
platform default. This means the same example config works on macOS
and Linux without editing.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

AUTO_SENTINEL = "auto"


@dataclass(frozen=True)
class PlatformPaths:
    """Where the daemon's files live on this OS."""

    config_dir: Path
    database_dir: Path
    log_dir: Path
    private_key_path: Path
    snapshot_dir: Path

    @property
    def database_path(self) -> Path:
        return self.database_dir / "trumpbot.db"

    @property
    def config_yaml_path(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def secrets_env_path(self) -> Path:
        return self.config_dir / "secrets.env"

    @property
    def initial_subjects_path(self) -> Path:
        return self.config_dir / "initial_subjects.yaml"


def detect_platform() -> str:
    """Return ``"Darwin"``, ``"Linux"``, or whatever ``platform.system()`` says."""
    return platform.system()


def for_system(system: str, *, home: Path | None = None) -> PlatformPaths:
    """Return :class:`PlatformPaths` for an explicit OS name."""
    home_dir = home or Path.home()
    if system == "Darwin":
        cfg = home_dir / ".config" / "trumpbot"
        return PlatformPaths(
            config_dir=cfg,
            database_dir=home_dir / "Library" / "Application Support" / "trumpbot",
            log_dir=home_dir / "Library" / "Logs" / "trumpbot",
            private_key_path=cfg / "kalshi_private.pem",
            snapshot_dir=home_dir / "Library" / "Application Support" / "trumpbot" / "markets",
        )
    if system == "Linux":
        return PlatformPaths(
            config_dir=Path("/etc/trumpbot"),
            database_dir=Path("/var/lib/trumpbot"),
            log_dir=Path("/var/log/trumpbot"),
            private_key_path=Path("/etc/trumpbot/kalshi_private.pem"),
            snapshot_dir=Path("/var/lib/trumpbot/markets"),
        )
    # Fallback: per-user dot-directory under HOME. Used by tests and
    # any unrecognized system (BSD, Windows, etc.).
    base = home_dir / ".trumpbot"
    return PlatformPaths(
        config_dir=base,
        database_dir=base,
        log_dir=base / "logs",
        private_key_path=base / "kalshi_private.pem",
        snapshot_dir=base / "markets",
    )


def current_platform_paths(*, home: Path | None = None) -> PlatformPaths:
    """:class:`PlatformPaths` for the OS we're running on now."""
    return for_system(detect_platform(), home=home)


def resolve_path(value: str | Path, default: Path) -> Path:
    """If ``value`` is the ``"auto"`` sentinel, return ``default``; else ``Path(value)``.

    Used by the daemon to expand ``database.path: "auto"`` etc. in the
    YAML config to a platform-appropriate concrete path at startup.
    """
    if isinstance(value, str) and value == AUTO_SENTINEL:
        return default
    return Path(value)
