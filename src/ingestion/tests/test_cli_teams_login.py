"""Tests for ingestion.cli.auth.teams_login — Teams login logic."""

from __future__ import annotations

import httpx
import respx
from ingestion.cli.auth.teams_login import (
    _discover_teams,
    _print_token_info,
    run_teams_login,
)


class TestDiscoverTeams:
    @respx.mock
    def test_returns_teams(self) -> None:
        respx.get("https://graph.microsoft.com/v1.0/me/joinedTeams").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "team-1", "displayName": "CS101"},
                        {"id": "team-2", "displayName": "MATH201"},
                    ]
                },
            )
        )
        ids, names = _discover_teams("fake_token")
        assert ids == ["team-1", "team-2"]
        assert names == ["CS101", "MATH201"]

    @respx.mock
    def test_empty_teams(self) -> None:
        respx.get("https://graph.microsoft.com/v1.0/me/joinedTeams").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        ids, names = _discover_teams("fake_token")
        assert ids == []
        assert names == []

    def test_network_error(self) -> None:
        # No mock = real request to nowhere → exception → empty
        ids, names = _discover_teams("")
        assert ids == []
        assert names == []


class TestPrintTokenInfo:
    def test_invalid_token(self) -> None:
        _print_token_info("not-a-jwt", "test_token")  # Should not crash

    def test_empty_token(self) -> None:
        _print_token_info("", "test_token")  # Should not crash


class TestRunTeamsLogin:
    def test_playwright_not_installed(self) -> None:
        """When playwright is not installed, should show error and return."""
        import sys

        # Temporarily hide playwright from imports
        saved = sys.modules.get("playwright.sync_api")
        sys.modules["playwright.sync_api"] = None  # type: ignore[assignment]
        try:
            run_teams_login()  # Should print error panel and return
        finally:
            if saved is not None:
                sys.modules["playwright.sync_api"] = saved
            else:
                sys.modules.pop("playwright.sync_api", None)
