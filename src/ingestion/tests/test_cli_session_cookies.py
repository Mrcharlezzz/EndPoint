"""Tests for session cookie persistence and token expiry checking."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from ingestion.cli.state import (
    is_token_expired,
    load_session_cookies,
    save_session_cookies,
)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "ingest"


class TestSessionCookies:
    def test_save_and_load(self, config_dir: Path) -> None:
        cookies = [
            {
                "name": "ESTSAUTHPERSISTENT",
                "value": "abc123",
                "domain": ".login.microsoftonline.com",
            },
            {
                "name": "ESTSAUTH",
                "value": "xyz789",
                "domain": ".login.microsoftonline.com",
            },
        ]
        save_session_cookies("teams", cookies, config_dir)
        loaded = load_session_cookies("teams", config_dir)
        assert len(loaded) == 2
        assert loaded[0]["name"] == "ESTSAUTHPERSISTENT"

    def test_load_missing(self, config_dir: Path) -> None:
        loaded = load_session_cookies("teams", config_dir)
        assert loaded == []

    def test_overwrite(self, config_dir: Path) -> None:
        save_session_cookies("teams", [{"name": "old"}], config_dir)
        save_session_cookies("teams", [{"name": "new"}], config_dir)
        loaded = load_session_cookies("teams", config_dir)
        assert len(loaded) == 1
        assert loaded[0]["name"] == "new"

    def test_chmod_600(self, config_dir: Path) -> None:
        save_session_cookies("teams", [{"name": "test"}], config_dir)
        path = config_dir / "sessions" / "teams_cookies.json"
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_separate_platforms(self, config_dir: Path) -> None:
        save_session_cookies("teams", [{"name": "teams_cookie"}], config_dir)
        save_session_cookies("moodle", [{"name": "moodle_cookie"}], config_dir)
        assert load_session_cookies("teams", config_dir)[0]["name"] == "teams_cookie"
        assert load_session_cookies("moodle", config_dir)[0]["name"] == "moodle_cookie"

    def test_load_corrupt_json(self, config_dir: Path) -> None:
        path = config_dir / "sessions" / "teams_cookies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json{{{")
        assert load_session_cookies("teams", config_dir) == []

    def test_load_dict_instead_of_list(self, config_dir: Path) -> None:
        """If file contains a dict instead of list, return empty list."""
        path = config_dir / "sessions" / "teams_cookies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"wrong": "format"}))
        assert load_session_cookies("teams", config_dir) == []


class TestIsTokenExpired:
    def _make_jwt(self, exp: int) -> str:
        """Create a minimal unsigned JWT with given exp claim."""
        import base64

        header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
        payload_data = json.dumps({"exp": exp}).encode()
        payload = base64.urlsafe_b64encode(payload_data).rstrip(b"=").decode()
        return f"{header}.{payload}."

    def test_expired_token(self) -> None:
        # Token expired 1 hour ago
        expired_exp = int(time.time()) - 3600
        token = self._make_jwt(expired_exp)
        assert is_token_expired(token) is True

    def test_valid_token(self) -> None:
        # Token expires in 1 hour
        future_exp = int(time.time()) + 3600
        token = self._make_jwt(future_exp)
        assert is_token_expired(token) is False

    def test_expiring_within_buffer(self) -> None:
        # Token expires in 3 minutes (within default 5-minute buffer)
        soon_exp = int(time.time()) + 180
        token = self._make_jwt(soon_exp)
        assert is_token_expired(token, buffer_minutes=5) is True

    def test_expiring_outside_buffer(self) -> None:
        # Token expires in 10 minutes (outside 5-minute buffer)
        token_exp = int(time.time()) + 600
        token = self._make_jwt(token_exp)
        assert is_token_expired(token, buffer_minutes=5) is False

    def test_garbage_token(self) -> None:
        assert is_token_expired("not.a.jwt") is True

    def test_empty_token(self) -> None:
        assert is_token_expired("") is True

    def test_no_exp_claim(self) -> None:
        import base64

        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(b'{"sub":"user"}').rstrip(b"=").decode()
        token = f"{header}.{payload}."
        assert is_token_expired(token) is True
