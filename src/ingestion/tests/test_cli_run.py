"""Tests for ingestion.cli._run_impl — run command implementation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from ingestion.cli._run_impl import (
    _make_adapter,
    _resolve_platform_scopes,
    execute_run_sync,
)
from ingestion.config import IngestionConfig
from ingestion.schemas import FetchScope


class TestResolvePlatformScopes:
    def test_none_returns_none(self) -> None:
        assert _resolve_platform_scopes("teams", None) is None

    def test_filter_to_platform(self) -> None:
        scope_filter = frozenset({FetchScope.MESSAGES, FetchScope.FORUMS})
        result = _resolve_platform_scopes("teams", scope_filter)
        # Teams only supports MESSAGES (not FORUMS)
        assert result == frozenset({FetchScope.MESSAGES})

    def test_no_overlap_returns_none(self) -> None:
        scope_filter = frozenset({FetchScope.FORUMS})
        result = _resolve_platform_scopes("telegram", scope_filter)
        assert result is None


class TestMakeAdapter:
    def test_teams(self) -> None:
        config = IngestionConfig()
        adapter = _make_adapter("teams", config)
        assert adapter.platform == "teams"

    def test_moodle(self) -> None:
        config = IngestionConfig()
        adapter = _make_adapter("moodle", config)
        assert adapter.platform == "moodle"

    def test_telegram(self) -> None:
        config = IngestionConfig()
        adapter = _make_adapter("telegram", config)
        assert adapter.platform == "telegram"

    def test_unknown(self) -> None:
        config = IngestionConfig()
        with pytest.raises(ValueError, match="Unknown platform"):
            _make_adapter("discord", config)


class TestExecuteRunDryRun:
    async def test_dry_run_no_creds(self, tmp_path: Path) -> None:
        """Dry run with no credentials should print and return."""
        from ingestion.cli.app import state

        state.config_dir = tmp_path
        state.output_dir = tmp_path / "output"
        state.format = "table"

        # No credentials file = no creds
        await execute_run_sync(
            platforms=[],
            scopes=[],
            incremental=False,
            dry_run=True,
        )
        # Should not crash — just prints "no valid credentials"

    async def test_dry_run_with_creds(self, tmp_path: Path) -> None:
        """Dry run with mock credentials should print plan."""
        from ingestion.cli.app import state
        from ingestion.cli.state import save_credentials

        state.config_dir = tmp_path
        state.output_dir = tmp_path / "output"
        state.format = "table"

        save_credentials("telegram", {"bot_token": "fake"}, tmp_path)

        with patch(
            "ingestion.cli._run_impl._test_connection",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await execute_run_sync(
                platforms=["telegram"],
                scopes=[],
                incremental=False,
                dry_run=True,
            )
        # Should not crash — prints dry run plan
