"""Tests for ingestion.cli.app — Typer app entry point."""

from __future__ import annotations

from ingestion.cli.app import app
from typer.testing import CliRunner

runner = CliRunner()


class TestAppCallback:
    def test_version_flag(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "ingest" in result.output

    def test_no_args_shows_help(self) -> None:
        result = runner.invoke(app, [])
        # Typer with no_args_is_help=True exits with code 0 or 2
        assert result.exit_code in (0, 2)

    def test_help_flag(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "ingest" in result.output or "Usage" in result.output

    def test_verbose_flag(self) -> None:
        result = runner.invoke(app, ["--verbose", "--help"])
        assert result.exit_code == 0

    def test_format_json(self) -> None:
        result = runner.invoke(app, ["--format", "json", "--help"])
        assert result.exit_code == 0
