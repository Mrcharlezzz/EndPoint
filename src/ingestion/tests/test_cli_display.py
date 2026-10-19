"""Tests for ingestion.cli.display — shared display utilities."""

from __future__ import annotations

from ingestion.cli.display import (
    format_badge,
    format_duration,
    format_token_redacted,
)


class TestFormatDuration:
    def test_milliseconds(self) -> None:
        assert format_duration(0.5) == "500ms"

    def test_seconds(self) -> None:
        assert format_duration(3.14) == "3.1s"

    def test_minutes(self) -> None:
        result = format_duration(125)
        assert result == "2m5s"


class TestFormatTokenRedacted:
    def test_short_token(self) -> None:
        # Short tokens show fewer visible chars for security
        result = format_token_redacted("abc")
        assert result == "abc...****"

    def test_long_token(self) -> None:
        # 16-char token: effective_visible = min(8, max(4, 4)) = 4
        result = format_token_redacted("abcdefghijklmnop")
        assert result == "abcd...****"


class TestFormatBadge:
    def test_new(self) -> None:
        badge = format_badge("NEW")
        assert "NEW" in badge.plain

    def test_updated(self) -> None:
        badge = format_badge("UPDATED")
        assert "UPD" in badge.plain

    def test_unchanged(self) -> None:
        badge = format_badge("UNCHANGED")
        assert "---" in badge.plain
