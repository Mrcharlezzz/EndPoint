"""Config precedence: defaults < env < SQLite overlay."""

from __future__ import annotations

from pathlib import Path

import pytest
from ingestion.daemon.config import DaemonConfig, load_config
from ingestion.daemon.store.local import open_store


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def test_defaults_apply_when_nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _daemon_env_keys():
        monkeypatch.delenv(key, raising=False)

    config = DaemonConfig()

    assert config.log_level == "INFO"
    assert config.scheduler.global_paused is False
    assert config.platforms.teams_enabled is True


def test_env_beats_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENDPOINT_DAEMON__LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ENDPOINT_DAEMON__SCHEDULER__MAX_CONCURRENT_SYNCS", "32")

    config = DaemonConfig()

    assert config.log_level == "DEBUG"
    assert config.scheduler.max_concurrent_syncs == 32


def test_overlay_beats_env(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_path: Path,
) -> None:
    # Pick env values that differ from defaults so we can prove env was parsed
    # AND that the overlay then flips the result back.
    monkeypatch.setenv("ENDPOINT_DAEMON__SCHEDULER__GLOBAL_PAUSED", "true")
    monkeypatch.setenv("ENDPOINT_DAEMON__PLATFORMS__TEAMS_ENABLED", "false")

    # Env-only config: confirms the env was actually read (defaults are
    # False/True, so seeing True/False proves precedence step 2).
    env_only = load_config(store=None)
    assert env_only.scheduler.global_paused is True
    assert env_only.platforms.teams_enabled is False

    store = open_store(sqlite_path)
    try:
        store.set_overlay("scheduler.global_paused", False)
        store.set_overlay("platforms.teams_enabled", True)

        config = load_config(store)

        assert config.scheduler.global_paused is False
        assert config.platforms.teams_enabled is True
    finally:
        store.close()


def test_platform_pause_defaults_to_false(sqlite_path: Path) -> None:
    """``platforms.paused.<name>`` must default to False across the board
    so a fresh daemon boot doesn't inadvertently pause every platform."""
    config = DaemonConfig()
    assert config.platforms.paused.teams is False
    assert config.platforms.paused.moodle is False
    assert config.platforms.paused.telegram is False


def test_platform_pause_overlay_round_trip(sqlite_path: Path) -> None:
    """Setting the nested overlay key applies the pause to the loaded config."""
    store = open_store(sqlite_path)
    try:
        store.set_overlay("platforms.paused.teams", True)
        config = load_config(store)
        assert config.platforms.paused.teams is True
        # Unrelated platforms still default to False.
        assert config.platforms.paused.moodle is False
    finally:
        store.close()


def test_shadow_mode_defaults_to_false() -> None:
    """Default-off is load-bearing: a daemon that defaulted to shadow mode
    would silently drop content on any fresh install."""
    config = DaemonConfig()
    assert config.ops.shadow_mode is False


def test_shadow_mode_overlay_round_trip(sqlite_path: Path) -> None:
    """``ops.shadow_mode`` flips via the overlay without env vars."""
    store = open_store(sqlite_path)
    try:
        store.set_overlay("ops.shadow_mode", True)
        config = load_config(store)
        assert config.ops.shadow_mode is True
    finally:
        store.close()


def test_overlay_ignores_unknown_keys(sqlite_path: Path) -> None:
    store = open_store(sqlite_path)
    try:
        store.set_overlay("not.a.real.key", "ignored")
        store.set_overlay("scheduler.global_paused", True)

        config = load_config(store)

        assert config.scheduler.global_paused is True
    finally:
        store.close()


def test_load_config_without_store_returns_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENDPOINT_DAEMON__LOG_LEVEL", "WARNING")

    config = load_config(store=None)

    assert config.log_level == "WARNING"


def _daemon_env_keys() -> list[str]:
    return [
        "ENDPOINT_DAEMON__LOG_LEVEL",
        "ENDPOINT_DAEMON__SQLITE_PATH",
        "ENDPOINT_DAEMON__SCHEDULER__GLOBAL_PAUSED",
        "ENDPOINT_DAEMON__SCHEDULER__DRAIN_MODE",
        "ENDPOINT_DAEMON__SCHEDULER__TICK_SECONDS",
        "ENDPOINT_DAEMON__SCHEDULER__MAX_CONCURRENT_SYNCS",
        "ENDPOINT_DAEMON__PLATFORMS__TEAMS_ENABLED",
        "ENDPOINT_DAEMON__PLATFORMS__MOODLE_ENABLED",
        "ENDPOINT_DAEMON__PLATFORMS__TELEGRAM_ENABLED",
        "ENDPOINT_DAEMON__API__HOST",
        "ENDPOINT_DAEMON__API__PORT",
        "ENDPOINT_DAEMON__API__SESSION_TTL_HOURS",
    ]
