"""Tests for CLI auth flows — moodle, telegram, teams login logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
from ingestion.cli.auth.moodle_login import (
    AuthError,
    _acquire_token,
    _fetch_user_info,
    run_moodle_login,
)
from ingestion.cli.auth.telegram_login import (
    _discover_groups,
    _validate_token,
    run_telegram_login,
)


class TestMoodleAcquireToken:
    @respx.mock
    def test_success(self) -> None:
        respx.post("https://elearning.constructor.university/login/token.php").mock(
            return_value=httpx.Response(200, json={"token": "abc123"})
        )
        token = _acquire_token("https://elearning.constructor.university", "user", "pass")
        assert token == "abc123"

    @respx.mock
    def test_auth_error(self) -> None:
        respx.post("https://elearning.constructor.university/login/token.php").mock(
            return_value=httpx.Response(200, json={"error": "Invalid login"})
        )
        with pytest.raises(AuthError, match="Invalid login"):
            _acquire_token("https://elearning.constructor.university", "user", "wrong")


class TestMoodleFetchUserInfo:
    @respx.mock
    def test_returns_user_info(self) -> None:
        base = "https://elearning.constructor.university"
        endpoint = f"{base}/webservice/rest/server.php"
        respx.post(url__eq=endpoint).mock(
            side_effect=[
                httpx.Response(200, json={"userid": 42, "sitename": "CU Moodle"}),
                httpx.Response(
                    200,
                    json=[
                        {"fullname": "CS101"},
                        {"fullname": "MATH201"},
                    ],
                ),
            ]
        )
        user_id, site_name, courses = _fetch_user_info(base, "token123")
        assert user_id == 42
        assert site_name == "CU Moodle"
        assert courses == ["CS101", "MATH201"]


class TestRunMoodleLogin:
    @respx.mock
    def test_user_cancels_username(self) -> None:
        with patch("ingestion.cli.auth.moodle_login.questionary") as mock_q:
            mock_q.text.return_value.ask.return_value = None
            run_moodle_login()

    @respx.mock
    def test_successful_login(self, tmp_path: Path) -> None:
        base = "https://elearning.constructor.university"
        respx.post(f"{base}/login/token.php").mock(
            return_value=httpx.Response(200, json={"token": "abc12345"})
        )
        respx.post(f"{base}/webservice/rest/server.php").mock(
            side_effect=[
                httpx.Response(200, json={"userid": 1, "sitename": "Test"}),
                httpx.Response(200, json=[]),
            ]
        )

        with (
            patch("ingestion.cli.auth.moodle_login.questionary") as mock_q,
            patch("ingestion.cli.auth.moodle_login.save_credentials") as mock_save,
        ):
            mock_q.text.return_value.ask.return_value = "user"
            mock_q.password.return_value.ask.return_value = "pass"
            run_moodle_login()
            mock_save.assert_called_once()


class TestTelegramValidateToken:
    @respx.mock
    def test_valid_token(self) -> None:
        respx.get("https://api.telegram.org/botfake123/getMe").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"username": "testbot", "id": 123}}
            )
        )
        result = _validate_token("https://api.telegram.org", "fake123")
        assert result is not None
        assert result["username"] == "testbot"

    @respx.mock
    def test_invalid_token(self) -> None:
        respx.get("https://api.telegram.org/botbad/getMe").mock(
            return_value=httpx.Response(200, json={"ok": False})
        )
        result = _validate_token("https://api.telegram.org", "bad")
        assert result is None

    def test_network_error(self) -> None:
        result = _validate_token("http://localhost:1", "fake")
        assert result is None


class TestTelegramDiscoverGroups:
    @respx.mock
    def test_finds_groups(self) -> None:
        respx.get("https://api.telegram.org/botfake/getUpdates").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "chat": {
                                    "id": -100,
                                    "type": "supergroup",
                                    "title": "Test Group",
                                }
                            },
                        },
                        {
                            "update_id": 2,
                            "message": {
                                "chat": {
                                    "id": -200,
                                    "type": "private",
                                    "first_name": "User",
                                }
                            },
                        },
                    ],
                },
            )
        )
        groups = _discover_groups("https://api.telegram.org", "fake")
        assert len(groups) == 1
        assert groups[0]["title"] == "Test Group"

    @respx.mock
    def test_no_groups(self) -> None:
        respx.get("https://api.telegram.org/botfake/getUpdates").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": []})
        )
        groups = _discover_groups("https://api.telegram.org", "fake")
        assert groups == []

    @respx.mock
    def test_deduplicates(self) -> None:
        chat = {"id": -100, "type": "group", "title": "Same Group"}
        respx.get("https://api.telegram.org/botfake/getUpdates").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"update_id": 1, "message": {"chat": chat}},
                        {"update_id": 2, "message": {"chat": chat}},
                    ],
                },
            )
        )
        groups = _discover_groups("https://api.telegram.org", "fake")
        assert len(groups) == 1


class TestRunTelegramLogin:
    def test_user_cancels(self) -> None:
        with patch("ingestion.cli.auth.telegram_login.questionary") as mock_q:
            mock_q.password.return_value.ask.return_value = None
            run_telegram_login()

    @respx.mock
    def test_invalid_token(self) -> None:
        respx.get("https://api.telegram.org/botbad/getMe").mock(
            return_value=httpx.Response(200, json={"ok": False})
        )
        with patch("ingestion.cli.auth.telegram_login.questionary") as mock_q:
            mock_q.password.return_value.ask.return_value = "bad"
            run_telegram_login()
