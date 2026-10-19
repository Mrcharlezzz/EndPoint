"""Tests for ingestion.cli._history_impl — history viewing logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ingestion.cli._history_impl import _show_run_details, run_history
from ingestion.cli.app import state
from ingestion.cli.state import append_history


class TestShowRunDetails:
    def test_renders_entry(self) -> None:
        entry = {
            "timestamp": "2026-03-13T12:00:00",
            "platforms": ["teams", "moodle"],
            "items_fetched": 42,
            "errors": 1,
            "duration_seconds": 3.5,
            "output_file": "/tmp/test.json",
        }
        _show_run_details(entry)  # Should not crash

    def test_empty_entry(self) -> None:
        _show_run_details({})  # Should not crash


class TestRunHistory:
    def test_no_history(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        # Should print "No history" and return
        run_history()

    def test_with_history_user_cancels(self, tmp_path: Path) -> None:
        state.config_dir = tmp_path
        append_history(
            {
                "timestamp": "2026-03-13T12:00:00",
                "platforms": ["teams"],
                "items_fetched": 10,
                "errors": 0,
                "duration_seconds": 1.0,
            },
            tmp_path,
        )

        with patch("ingestion.cli._history_impl.questionary") as mock_q:
            mock_q.select.return_value.ask.return_value = None
            mock_q.Choice = __import__("questionary").Choice
            run_history()
