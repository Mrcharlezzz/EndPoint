"""Tests for ingestion.cli.state — file-based state management."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ingestion.cli.state import (
    append_history,
    classify_item,
    clear_all_credentials,
    clear_credentials,
    compute_item_hash,
    enforce_output_retention,
    generate_output_filename,
    list_output_files,
    load_all_credentials,
    load_credentials,
    load_cursor,
    load_history,
    load_seen_ids,
    save_credentials,
    save_cursor,
    save_output,
    save_seen_ids,
    update_seen_id,
)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "ingest"


class TestCredentials:
    def test_save_and_load(self, config_dir: Path) -> None:
        save_credentials("moodle", {"token": "abc", "user_id": 123}, config_dir)
        creds = load_credentials("moodle", config_dir)
        assert creds == {"token": "abc", "user_id": 123}

    def test_load_missing(self, config_dir: Path) -> None:
        assert load_credentials("teams", config_dir) is None

    def test_load_all(self, config_dir: Path) -> None:
        save_credentials("moodle", {"token": "abc"}, config_dir)
        save_credentials("telegram", {"bot_token": "xyz"}, config_dir)
        all_creds = load_all_credentials(config_dir)
        assert "moodle" in all_creds
        assert "telegram" in all_creds

    def test_clear(self, config_dir: Path) -> None:
        save_credentials("moodle", {"token": "abc"}, config_dir)
        assert clear_credentials("moodle", config_dir) is True
        assert load_credentials("moodle", config_dir) is None

    def test_clear_missing(self, config_dir: Path) -> None:
        assert clear_credentials("moodle", config_dir) is False

    def test_clear_all(self, config_dir: Path) -> None:
        save_credentials("moodle", {"token": "abc"}, config_dir)
        save_credentials("telegram", {"bot_token": "xyz"}, config_dir)
        clear_all_credentials(config_dir)
        assert load_all_credentials(config_dir) == {}

    def test_overwrite(self, config_dir: Path) -> None:
        save_credentials("moodle", {"token": "old"}, config_dir)
        save_credentials("moodle", {"token": "new"}, config_dir)
        creds = load_credentials("moodle", config_dir)
        assert creds["token"] == "new"

    def test_chmod_600(self, config_dir: Path) -> None:
        save_credentials("moodle", {"token": "abc"}, config_dir)
        path = config_dir / "credentials.json"
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600


class TestSeenIds:
    def test_save_and_load(self, config_dir: Path) -> None:
        data = {"id1": {"hash": "sha256:abc", "last_seen": "2026-01-01"}}
        save_seen_ids(data, config_dir)
        loaded = load_seen_ids(config_dir)
        assert loaded == data

    def test_classify_new(self) -> None:
        assert classify_item("new_id", "hash1", {}) == "NEW"

    def test_classify_updated(self) -> None:
        seen = {"id1": {"hash": "old_hash", "last_seen": "2026-01-01"}}
        assert classify_item("id1", "new_hash", seen) == "UPDATED"

    def test_classify_unchanged(self) -> None:
        seen = {"id1": {"hash": "same", "last_seen": "2026-01-01"}}
        assert classify_item("id1", "same", seen) == "UNCHANGED"

    def test_classify_none_hash_new(self) -> None:
        assert classify_item("new_id", None, {}) == "NEW"

    def test_classify_none_hash_existing(self) -> None:
        seen = {"id1": {"hash": "old", "last_seen": "2026-01-01"}}
        assert classify_item("id1", None, seen) == "NEW"

    def test_update_immutable(self) -> None:
        original = {"id1": {"hash": "h1", "last_seen": "2026-01-01"}}
        updated = update_seen_id("id2", "h2", original)
        assert "id2" in updated
        assert "id1" in updated
        # Original not mutated
        assert "id2" not in original

    def test_eviction(self, config_dir: Path) -> None:
        # Create more than cap
        data = {f"id_{i}": {"hash": f"h{i}", "last_seen": f"2026-01-{i:02d}"} for i in range(1, 12)}
        from ingestion.cli import state as state_mod

        old_cap = state_mod.SEEN_IDS_CAP
        state_mod.SEEN_IDS_CAP = 10
        try:
            save_seen_ids(data, config_dir)
            loaded = load_seen_ids(config_dir)
            # Should have evicted ~20% = 2 entries
            assert len(loaded) <= 10
        finally:
            state_mod.SEEN_IDS_CAP = old_cap

    def test_compute_hash(self) -> None:
        h = compute_item_hash({"title": "Test", "body": "Hello"})
        assert h.startswith("sha256:")
        # Deterministic
        h2 = compute_item_hash({"title": "Test", "body": "Hello"})
        assert h == h2


class TestCursors:
    def test_save_and_load(self, config_dir: Path) -> None:
        save_cursor("teams", {"last_sync_at": "2026-01-01"}, config_dir)
        cursor = load_cursor("teams", config_dir)
        assert cursor == {"last_sync_at": "2026-01-01"}

    def test_load_missing(self, config_dir: Path) -> None:
        assert load_cursor("teams", config_dir) is None


class TestHistory:
    def test_append_and_load(self, config_dir: Path) -> None:
        append_history({"platform": "teams", "items": 10}, config_dir)
        append_history({"platform": "moodle", "items": 5}, config_dir)
        entries = load_history(config_dir)
        # Most recent first
        assert len(entries) == 2
        assert entries[0]["platform"] == "moodle"
        assert entries[1]["platform"] == "teams"

    def test_load_empty(self, config_dir: Path) -> None:
        assert load_history(config_dir) == []


class TestOutput:
    def test_save_output(self, config_dir: Path) -> None:
        data = {"items": [], "stats": {}}
        path = save_output(data, config_dir)
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["items"] == []

    def test_list_output_files(self, config_dir: Path) -> None:
        out_dir = config_dir / "output"
        out_dir.mkdir(parents=True)
        (out_dir / "2026-03-01.json").write_text("{}")
        (out_dir / "2026-03-02.json").write_text("{}")
        files = list_output_files(config_dir)
        assert len(files) == 2
        # Newest first
        assert files[0].name == "2026-03-02.json"

    def test_retention(self, config_dir: Path) -> None:
        out_dir = config_dir / "output"
        out_dir.mkdir(parents=True)
        for i in range(25):
            (out_dir / f"2026-03-{i:02d}.json").write_text("{}")
        enforce_output_retention(out_dir, max_files=20)
        remaining = list(out_dir.glob("*.json"))
        assert len(remaining) == 20

    def test_generate_filename(self) -> None:
        name = generate_output_filename()
        assert name.endswith(".json")
        assert "T" in name
