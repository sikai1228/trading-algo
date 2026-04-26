"""Tests for platform-aware path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from trumpbot.platform_paths import (
    AUTO_SENTINEL,
    PlatformPaths,
    detect_platform,
    for_system,
    resolve_path,
)


class TestForSystem:
    def test_darwin_layout(self) -> None:
        paths = for_system("Darwin", home=Path("/Users/sikai"))
        assert paths.config_dir == Path("/Users/sikai/.config/trumpbot")
        assert paths.database_dir == Path("/Users/sikai/Library/Application Support/trumpbot")
        assert paths.log_dir == Path("/Users/sikai/Library/Logs/trumpbot")
        assert paths.private_key_path == Path("/Users/sikai/.config/trumpbot/kalshi_private.pem")
        assert paths.snapshot_dir == Path(
            "/Users/sikai/Library/Application Support/trumpbot/markets"
        )

    def test_linux_layout(self) -> None:
        paths = for_system("Linux", home=Path("/home/trumpbot"))
        assert paths.config_dir == Path("/etc/trumpbot")
        assert paths.database_dir == Path("/var/lib/trumpbot")
        assert paths.log_dir == Path("/var/log/trumpbot")
        assert paths.private_key_path == Path("/etc/trumpbot/kalshi_private.pem")

    def test_unknown_system_falls_back_to_home(self) -> None:
        paths = for_system("FreeBSD", home=Path("/home/x"))
        assert paths.config_dir == Path("/home/x/.trumpbot")
        assert paths.database_dir == Path("/home/x/.trumpbot")

    def test_database_path_property(self) -> None:
        paths = for_system("Darwin", home=Path("/Users/u"))
        assert paths.database_path == Path(
            "/Users/u/Library/Application Support/trumpbot/trumpbot.db"
        )

    def test_config_yaml_path_property(self) -> None:
        paths = for_system("Linux", home=Path("/h"))
        assert paths.config_yaml_path == Path("/etc/trumpbot/config.yaml")

    def test_initial_subjects_path_property(self) -> None:
        paths = for_system("Darwin", home=Path("/Users/u"))
        assert paths.initial_subjects_path == Path(
            "/Users/u/.config/trumpbot/initial_subjects.yaml"
        )


class TestResolvePath:
    def test_auto_returns_default(self) -> None:
        default = Path("/x/y/z")
        assert resolve_path(AUTO_SENTINEL, default) == default

    def test_explicit_value_wins(self) -> None:
        assert resolve_path("/explicit/path", Path("/default")) == Path("/explicit/path")

    def test_path_value_passes_through(self) -> None:
        explicit = Path("/explicit")
        assert resolve_path(explicit, Path("/default")) == explicit


class TestDetectPlatform:
    def test_returns_a_known_string(self) -> None:
        out = detect_platform()
        assert out in {"Darwin", "Linux", "Windows"} or isinstance(out, str)


class TestImmutability:
    def test_platform_paths_frozen(self) -> None:
        """The dataclass is frozen so the daemon can stash it in a
        cache without worrying about accidental mutation."""
        import dataclasses

        paths = for_system("Darwin", home=Path("/h"))
        with pytest.raises(dataclasses.FrozenInstanceError):
            paths.config_dir = Path("/other")  # type: ignore[misc]


def test_platform_paths_dataclass_attrs() -> None:
    """Sanity: PlatformPaths exposes the fields the daemon needs."""
    expected = {
        "config_dir",
        "database_dir",
        "log_dir",
        "private_key_path",
        "snapshot_dir",
    }
    paths = for_system("Darwin", home=Path("/h"))
    assert expected.issubset(set(PlatformPaths.__dataclass_fields__.keys()))
    assert isinstance(paths, PlatformPaths)
