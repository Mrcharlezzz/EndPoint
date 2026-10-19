"""Tests for ingestion.cli._config_impl — config editor logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from ingestion.cli._config_impl import (
    _get_current_value,
    _get_source,
    _save_config_value,
)
from ingestion.cli.app import state
from ingestion.config import IngestionConfig


class TestGetCurrentValue:
    def test_nested_key(self) -> None:
        config = IngestionConfig()
        val = _get_current_value(config, "teams.max_retries")
        assert isinstance(val, int)

    def test_top_level(self) -> None:
        config = IngestionConfig()
        # teams is a sub-config object
        val = _get_current_value(config, "teams")
        assert val is not None

    def test_nonexistent_key(self) -> None:
        config = IngestionConfig()
        val = _get_current_value(config, "nonexistent.key")
        assert val is None


def test_ingestion_config_tolerates_runtime_telegram_bot_token_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENDPOINT_TELEGRAM__BOT_TOKEN", "123456:prod-bot-token")

    config = IngestionConfig(_env_file=None)  # type: ignore[call-arg]

    assert config.telegram.api_base_url == "https://api.telegram.org"
    assert not hasattr(config.telegram, "bot_token")


class TestGetSource:
    def test_default_source(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        entry = {"key": "teams.max_retries", "env": "ENDPOINT_TEAMS__MAX_RETRIES"}
        with patch.dict("os.environ", {}, clear=False):
            source = _get_source(entry)
        assert source == "default"

    def test_env_var_source(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        entry = {"key": "teams.max_retries", "env": "ENDPOINT_TEAMS__MAX_RETRIES"}
        with patch.dict("os.environ", {"ENDPOINT_TEAMS__MAX_RETRIES": "5"}):
            source = _get_source(entry)
        assert source == "env var"


class TestSaveConfigValue:
    def test_save_int(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        _save_config_value("teams.max_retries", "5", "int")
        config_path = tmp_path / "config.yaml"
        assert config_path.exists()

    def test_save_float(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        _save_config_value("teams.requests_per_second", "20.0", "float")

    def test_save_str(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        _save_config_value("moodle.base_url", "https://example.com", "str")

    def test_save_date(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        _save_config_value("teams.semester_start", "2026-01-15", "date")

    def test_invalid_int_raises(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        with pytest.raises(ValueError):
            _save_config_value("teams.max_retries", "not_a_number", "int")

    def test_overwrite_existing(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        _save_config_value("teams.max_retries", "3", "int")
        _save_config_value("teams.max_retries", "5", "int")
        # Should not crash and should overwrite
