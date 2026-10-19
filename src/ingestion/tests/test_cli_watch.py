"""Tests for ingestion.cli._watch_impl — Telegram watch logic."""

from __future__ import annotations

import json
from pathlib import Path

from ingestion.cli._watch_impl import _append_messages


class TestAppendMessages:
    def test_creates_new_file(self, tmp_path: Path) -> None:
        path = tmp_path / "watch.json"
        messages = [{"text": "hello", "date": 1}]
        _append_messages(path, messages)

        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["text"] == "hello"

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "watch.json"
        path.write_text(json.dumps([{"text": "first"}]))

        _append_messages(path, [{"text": "second"}])

        data = json.loads(path.read_text())
        assert len(data) == 2
        assert data[0]["text"] == "first"
        assert data[1]["text"] == "second"

    def test_handles_corrupt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "watch.json"
        path.write_text("not json")

        _append_messages(path, [{"text": "new"}])

        data = json.loads(path.read_text())
        assert len(data) == 1

    def test_empty_messages(self, tmp_path: Path) -> None:
        path = tmp_path / "watch.json"
        _append_messages(path, [])

        data = json.loads(path.read_text())
        assert data == []

    def test_multiple_batches(self, tmp_path: Path) -> None:
        path = tmp_path / "watch.json"
        _append_messages(path, [{"id": 1}])
        _append_messages(path, [{"id": 2}, {"id": 3}])

        data = json.loads(path.read_text())
        assert len(data) == 3
