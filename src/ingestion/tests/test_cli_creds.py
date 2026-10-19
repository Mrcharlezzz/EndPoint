"""Tests for ingestion.cli._creds_impl — credential manager logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ingestion.cli._creds_impl import (
    _get_cred_status,
    _show_moodle_details,
    _show_teams_details,
    _show_telegram_details,
    _view_all,
    run_creds_menu,
)
from ingestion.cli.app import state
from ingestion.cli.jwt_utils import decode_unverified, get_expiry_remaining_minutes
from ingestion.cli.state import save_credentials


class TestGetCredStatus:
    def test_teams_with_token(self) -> None:
        status = _get_cred_status("teams", {"graph_token": "fake"})
        assert "valid" in status

    def test_moodle_with_token(self) -> None:
        status = _get_cred_status("moodle", {"token": "abcdefghijklmnop"})
        assert "valid" in status
        assert "wstoken" in status

    def test_telegram(self) -> None:
        status = _get_cred_status("telegram", {"bot_token": "fake"})
        assert "valid" in status

    def test_unknown_platform(self) -> None:
        status = _get_cred_status("discord", {})
        assert status == "unknown"


class TestViewAll:
    def test_shows_all_platforms(self) -> None:
        creds = {
            "teams": {"graph_token": "fake"},
            "moodle": {"token": "fake"},
        }
        _view_all(creds)  # Should not crash

    def test_empty_creds(self) -> None:
        _view_all({})  # Should not crash


class TestShowDetails:
    def test_teams_details(self) -> None:
        creds = {
            "graph_token": "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.sig",
            "csa_token": "csa_token_value_here",
            "team_ids": ["id1", "id2"],
        }
        _show_teams_details(creds)

    def test_moodle_details(self) -> None:
        _show_moodle_details({"token": "abcdefgh12345", "user_id": 42})

    def test_telegram_details(self) -> None:
        _show_telegram_details({"bot_token": "123456:ABCdef"})


class TestJwtUtils:
    def test_empty_token_expiry(self) -> None:
        assert get_expiry_remaining_minutes("") is None

    def test_invalid_token_expiry(self) -> None:
        assert get_expiry_remaining_minutes("not-a-jwt") is None

    def test_invalid_token_decode(self) -> None:
        assert decode_unverified("not-a-jwt") is None

    def test_empty_token_decode(self) -> None:
        assert decode_unverified("") is None


class TestRunCredsMenu:
    def test_user_cancels(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        with patch("ingestion.cli._creds_impl.questionary") as mock_q:
            mock_q.select.return_value.ask.return_value = None
            run_creds_menu()

    def test_view_all_action(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        save_credentials("telegram", {"bot_token": "fake"}, tmp_path)

        with patch("ingestion.cli._creds_impl.questionary") as mock_q:
            mock_q.select.return_value.ask.return_value = "View all credentials"
            run_creds_menu()

    def test_clear_creds_no_creds(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        with patch("ingestion.cli._creds_impl.questionary") as mock_q:
            mock_q.select.return_value.ask.return_value = "Clear credentials"
            run_creds_menu()
