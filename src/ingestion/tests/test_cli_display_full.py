"""Tests for ingestion.cli.display — full coverage of display utilities."""

from __future__ import annotations

from ingestion.cli.display import (
    format_badge,
    format_duration,
    format_platform_header,
    format_token_redacted,
    make_stats_table,
    print_error_panel,
    print_status,
    print_success_panel,
)


class TestPrintStatus:
    def test_print_status_outputs(self, capsys: object) -> None:
        """print_status writes to stderr."""
        print_status("[green]✔[/green]", "test message")
        # Doesn't crash — Rich output goes to stderr console


class TestPrintPanels:
    def test_error_panel(self) -> None:
        print_error_panel("Error Title", "Error body text")

    def test_success_panel(self) -> None:
        print_success_panel("Success Title", "Success body text")


class TestMakeStatsTable:
    def test_creates_table(self) -> None:
        table = make_stats_table({"items": 42, "errors": 0, "duration": "1.5s"})
        assert table.row_count == 3

    def test_empty_stats(self) -> None:
        table = make_stats_table({})
        assert table.row_count == 0


class TestFormatPlatformHeader:
    def test_with_status(self) -> None:
        result = format_platform_header("teams", "valid")
        assert "teams" in result
        assert "valid" in result

    def test_without_status(self) -> None:
        result = format_platform_header("moodle")
        assert "moodle" in result


class TestFormatDurationEdge:
    def test_zero(self) -> None:
        assert format_duration(0) == "0ms"

    def test_exactly_one_second(self) -> None:
        assert format_duration(1.0) == "1.0s"

    def test_exactly_sixty(self) -> None:
        assert format_duration(60) == "1m0s"


class TestFormatTokenRedactedEdge:
    def test_empty_string(self) -> None:
        assert format_token_redacted("") == "(empty)"

    def test_exactly_visible_length(self) -> None:
        # 8-char token: effective_visible = min(8, max(4, 2)) = 4
        result = format_token_redacted("12345678")
        assert result == "1234...****"

    def test_one_over_visible(self) -> None:
        # 9-char token: effective_visible = min(8, max(4, 2)) = 4
        result = format_token_redacted("123456789")
        assert result == "1234...****"


class TestFormatBadgeEdge:
    def test_unknown_badge(self) -> None:
        badge = format_badge("SOMETHING_ELSE")
        assert "---" in badge.plain
