"""Tests for ingestion.cli._inspect_impl — browse results logic."""

from __future__ import annotations

from pathlib import Path

from ingestion.cli._inspect_impl import (
    _build_item_label,
    _resolve_output_file,
    _show_item_detail,
)
from ingestion.cli.app import state


class TestResolveOutputFile:
    def test_no_files_returns_none(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        state.output_dir = tmp_path / "output"
        result = _resolve_output_file(None, tmp_path, tmp_path / "output")
        assert result is None

    def test_latest_file_default(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        f1 = out_dir / "2026-01-01T00-00.json"
        f2 = out_dir / "2026-01-02T00-00.json"
        f1.write_text("{}")
        f2.write_text("{}")

        result = _resolve_output_file(None, tmp_path, out_dir)
        assert result is not None
        assert result.name == "2026-01-02T00-00.json"

    def test_direct_file_path(self, tmp_path: Path) -> None:
        f = tmp_path / "test.json"
        f.write_text("{}")
        result = _resolve_output_file(str(f), tmp_path, tmp_path / "output")
        assert result == f

    def test_timestamp_prefix_match(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        f = out_dir / "2026-03-13T12-00.json"
        f.write_text("{}")

        result = _resolve_output_file("2026-03-13", tmp_path, out_dir)
        assert result is not None
        assert result.name == "2026-03-13T12-00.json"

    def test_index_selector(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        f = out_dir / "2026-01-01T00-00.json"
        f.write_text("{}")

        result = _resolve_output_file("0", tmp_path, out_dir)
        assert result is not None

    def test_invalid_selector_returns_none(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        result = _resolve_output_file("nonexistent", tmp_path, out_dir)
        assert result is None


class TestBuildItemLabel:
    def test_with_title_and_badge(self) -> None:
        item = {"title": "Assignment 1", "_badge": "NEW"}
        label = _build_item_label(item)
        assert "Assignment 1" in label
        assert "NEW" in label

    def test_with_body_fallback(self) -> None:
        item = {"body": "This is a message body", "_badge": "UNCHANGED"}
        label = _build_item_label(item)
        assert "This is a message body" in label
        # UNCHANGED badge should NOT appear
        assert "UNCHANGED" not in label

    def test_with_course_name(self) -> None:
        item = {"body": "Hello", "course_name": "CS101"}
        label = _build_item_label(item)
        assert "CS101" in label

    def test_with_due_date(self) -> None:
        item = {"title": "HW1", "deadline": "2026-03-15T23:59:00"}
        label = _build_item_label(item)
        assert "2026-03-15" in label

    def test_long_body_truncated(self) -> None:
        item = {"body": "x" * 200}
        label = _build_item_label(item)
        assert "..." in label


class TestShowItemDetail:
    def test_renders_without_error(self) -> None:
        item = {
            "title": "Test Item",
            "_badge": "NEW",
            "platform": "moodle",
            "content_type": "assignment",
            "course_name": "CS101",
            "author": "Prof",
            "deadline": "2026-03-15",
            "published_at": "2026-03-01",
            "body": "Assignment description",
            "attachments": [
                {"filename": "hw1.pdf", "size_bytes": 1024000},
            ],
        }
        _show_item_detail(item)  # Should not crash

    def test_minimal_item(self) -> None:
        _show_item_detail({})  # Should not crash

    def test_long_body_truncated(self) -> None:
        item = {"body": "y" * 600}
        _show_item_detail(item)  # Should not crash
