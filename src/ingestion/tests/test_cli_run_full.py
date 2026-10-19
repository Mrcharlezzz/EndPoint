"""Tests for ingestion.cli._run_impl — extended run command tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ingestion.cli._run_impl import (
    _print_dry_run,
    _print_token_expiry,
    execute_run_sync,
    pre_run_check,
)
from ingestion.cli.app import state
from ingestion.cli.state import save_credentials
from ingestion.schemas import (
    ContentType,
    ErrorCode,
    FetchError,
    FetchResult,
    NormalizedItem,
    ScopeStats,
    SyncStats,
)


class TestPreRunCheck:
    def _mock_questionary(self) -> MagicMock:
        mock_q = MagicMock()
        mock_q.Choice = __import__("questionary").Choice
        return mock_q

    def test_user_exits(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        save_credentials("telegram", {"bot_token": "fake"}, tmp_path)
        mock_q = self._mock_questionary()
        mock_q.select.return_value.ask.return_value = "Exit"
        with patch.dict("sys.modules", {"questionary": mock_q}):
            ok, platforms, scopes = pre_run_check([], [])
            assert ok is False

    def test_user_fetches_single_platform(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        save_credentials("telegram", {"bot_token": "fake"}, tmp_path)
        mock_q = self._mock_questionary()
        mock_q.select.return_value.ask.return_value = "Fetch now"
        with patch.dict("sys.modules", {"questionary": mock_q}):
            ok, platforms, scopes = pre_run_check([], [])
            assert ok is True
            assert platforms == ["telegram"]

    def test_no_credentials_user_exits(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        mock_q = self._mock_questionary()
        mock_q.select.return_value.ask.return_value = "Exit"
        with patch.dict("sys.modules", {"questionary": mock_q}):
            ok, _, _ = pre_run_check([], [])
            assert ok is False

    def test_cli_flags_bypass_interactive(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        save_credentials("telegram", {"bot_token": "fake"}, tmp_path)
        mock_q = self._mock_questionary()
        mock_q.select.return_value.ask.return_value = "Fetch now"
        with patch.dict("sys.modules", {"questionary": mock_q}):
            ok, platforms, scopes = pre_run_check(["telegram"], ["messages"])
            assert ok is True
            assert platforms == ["telegram"]
            assert scopes == ["messages"]


class TestPrintTokenExpiry:
    def test_empty_token(self) -> None:
        _print_token_expiry("")  # No crash

    def test_invalid_token(self) -> None:
        _print_token_expiry("not-a-jwt")  # No crash


class TestPrintDryRun:
    def test_prints_plan(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        creds = {"telegram": {"bot_token": "fake"}}
        _print_dry_run(
            ["telegram"],
            creds,
            None,
            False,
            tmp_path,
        )

    def test_with_incremental_cursor(self, tmp_path: Path) -> None:
        from ingestion.cli.state import save_cursor

        state.config_dir = tmp_path
        save_cursor("telegram", {"last_update_id": 42}, tmp_path)
        creds = {"telegram": {"bot_token": "fake"}}
        _print_dry_run(
            ["telegram"],
            creds,
            None,
            True,
            tmp_path,
        )


class TestExecuteRunFull:
    async def test_fetch_with_items(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        state.output_dir = tmp_path / "output"
        state.format = "table"

        save_credentials("telegram", {"bot_token": "fake"}, tmp_path)

        mock_result = FetchResult(
            items=[
                NormalizedItem(
                    platform="telegram",
                    external_id="tg:msg:1",
                    content_type=ContentType.RAW_MESSAGE,
                    title="",
                    body="Hello world",
                ),
            ],
            files=[],
            errors=[],
            cursor={"last_update_id": 1},
            stats=SyncStats(
                duration_seconds=0.5,
                items_fetched=1,
                per_scope_stats={},
            ),
        )

        mock_adapter = AsyncMock()
        mock_adapter.fetch = AsyncMock(return_value=mock_result)
        mock_adapter.test_connection = AsyncMock(return_value=True)

        with (
            patch(
                "ingestion.cli._run_impl._test_connection",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ingestion.cli._run_impl._make_adapter",
                return_value=mock_adapter,
            ),
        ):
            await execute_run_sync(
                platforms=["telegram"],
                scopes=[],
                incremental=False,
                dry_run=False,
            )

        # Check output was saved
        output_files = list((tmp_path / "output").glob("*.json"))
        assert len(output_files) == 1

    async def test_fetch_with_errors(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        state.output_dir = tmp_path / "output"
        state.format = "table"

        save_credentials("telegram", {"bot_token": "fake"}, tmp_path)

        mock_result = FetchResult(
            items=[],
            files=[],
            errors=[
                FetchError(
                    scope="messages",
                    message="Rate limited",
                    code=ErrorCode.RATE_LIMITED,
                )
            ],
            cursor=None,
            stats=SyncStats(
                duration_seconds=0.1,
                items_fetched=0,
                per_scope_stats={},
            ),
        )

        mock_adapter = AsyncMock()
        mock_adapter.fetch = AsyncMock(return_value=mock_result)

        with (
            patch(
                "ingestion.cli._run_impl._test_connection",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ingestion.cli._run_impl._make_adapter",
                return_value=mock_adapter,
            ),
        ):
            await execute_run_sync(
                platforms=["telegram"],
                scopes=[],
                incremental=False,
                dry_run=False,
            )

    async def test_json_output_format(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        state.output_dir = tmp_path / "output"
        state.format = "json"

        save_credentials("telegram", {"bot_token": "fake"}, tmp_path)

        mock_result = FetchResult(
            items=[],
            files=[],
            errors=[],
            cursor=None,
            stats=SyncStats(
                duration_seconds=0.1,
                items_fetched=0,
                per_scope_stats={},
            ),
        )

        mock_adapter = AsyncMock()
        mock_adapter.fetch = AsyncMock(return_value=mock_result)

        with (
            patch(
                "ingestion.cli._run_impl._test_connection",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ingestion.cli._run_impl._make_adapter",
                return_value=mock_adapter,
            ),
        ):
            await execute_run_sync(
                platforms=["telegram"],
                scopes=[],
                incremental=False,
                dry_run=False,
            )

    async def test_adapter_exception(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        state.output_dir = tmp_path / "output"
        state.format = "table"

        save_credentials("telegram", {"bot_token": "fake"}, tmp_path)

        mock_adapter = AsyncMock()
        mock_adapter.fetch = AsyncMock(side_effect=RuntimeError("Connection failed"))

        with (
            patch(
                "ingestion.cli._run_impl._test_connection",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ingestion.cli._run_impl._make_adapter",
                return_value=mock_adapter,
            ),
        ):
            await execute_run_sync(
                platforms=["telegram"],
                scopes=[],
                incremental=False,
                dry_run=False,
            )

    async def test_scope_filtering(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        state.output_dir = tmp_path / "output"
        state.format = "table"

        save_credentials("telegram", {"bot_token": "fake"}, tmp_path)

        mock_result = FetchResult(
            items=[],
            files=[],
            errors=[],
            cursor=None,
            stats=SyncStats(
                duration_seconds=0.1,
                items_fetched=0,
                per_scope_stats={"messages": ScopeStats(items_fetched=0)},
            ),
        )

        mock_adapter = AsyncMock()
        mock_adapter.fetch = AsyncMock(return_value=mock_result)

        with (
            patch(
                "ingestion.cli._run_impl._test_connection",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ingestion.cli._run_impl._make_adapter",
                return_value=mock_adapter,
            ),
        ):
            await execute_run_sync(
                platforms=["telegram"],
                scopes=["messages"],
                incremental=False,
                dry_run=False,
            )

    async def test_profile_includes_benchmark_summary(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        state.output_dir = tmp_path / "output"
        state.format = "table"

        save_credentials("telegram", {"bot_token": "fake"}, tmp_path)

        mock_result = FetchResult(
            items=[
                NormalizedItem(
                    platform="telegram",
                    external_id="tg:msg:1",
                    content_type=ContentType.RAW_MESSAGE,
                    title="",
                    body="Hello world",
                ),
                NormalizedItem(
                    platform="telegram",
                    external_id="tg:msg:2",
                    content_type=ContentType.RAW_MESSAGE,
                    title="",
                    body="Second message",
                ),
            ],
            files=[],
            errors=[],
            cursor=None,
            stats=SyncStats(
                duration_seconds=4.0,
                items_fetched=2,
                errors=0,
                per_scope_stats={},
            ),
        )

        mock_adapter = AsyncMock()
        mock_adapter.fetch = AsyncMock(return_value=mock_result)

        with (
            patch(
                "ingestion.cli._run_impl._test_connection",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ingestion.cli._run_impl._make_adapter",
                return_value=mock_adapter,
            ),
        ):
            await execute_run_sync(
                platforms=["telegram"],
                scopes=[],
                incremental=False,
                dry_run=False,
                profile=True,
            )

        profile_files = list((tmp_path / "output").glob("profile-*.json"))
        assert len(profile_files) == 1
        profile_data = json.loads(profile_files[0].read_text(encoding="utf-8"))
        benchmark = profile_data["profiles"][0]["benchmark"]
        assert benchmark["items_fetched"] == 2
        assert benchmark["errors"] == 0
        assert benchmark["fetch_duration_seconds"] == 4.0
        assert benchmark["fetch_throughput_items_per_second"] == 0.5
        assert benchmark["run_duration_seconds"] == pytest.approx(4.0, abs=0.01)
