"""Tests for ingestion.cli.local_ports — JSON-file port implementations."""

from __future__ import annotations

from pathlib import Path

import pytest
from ingestion.cli.local_ports import (
    JsonFileContentSink,
    JsonFileSyncStateStore,
    JsonFileTokenStore,
    JsonlSyncLogger,
)
from ingestion.schemas import ContentType, NormalizedItem, SyncStats


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "ingest"


def _make_item(external_id: str = "test_1", body: str = "hello") -> NormalizedItem:
    return NormalizedItem(
        external_id=external_id,
        platform="test",
        content_type=ContentType.ASSIGNMENT,
        body=body,
    )


class TestJsonFileTokenStore:
    async def test_get_missing(self, config_dir: Path) -> None:
        store = JsonFileTokenStore(config_dir)
        assert await store.get_credentials("local", "teams") is None

    async def test_save_and_get(self, config_dir: Path) -> None:
        store = JsonFileTokenStore(config_dir)
        await store.update_credentials("local", "moodle", {"token": "abc"})
        creds = await store.get_credentials("local", "moodle")
        assert creds == {"token": "abc"}

    async def test_mark_expired(self, config_dir: Path) -> None:
        store = JsonFileTokenStore(config_dir)
        await store.update_credentials("local", "moodle", {"token": "abc"})
        await store.mark_expired("local", "moodle")
        assert await store.get_credentials("local", "moodle") is None


class TestJsonFileContentSink:
    async def test_exists_false(self, config_dir: Path) -> None:
        sink = JsonFileContentSink(config_dir)
        assert await sink.exists("test", "nonexistent") is False

    async def test_upsert_then_exists(self, config_dir: Path) -> None:
        sink = JsonFileContentSink(config_dir)
        item = _make_item()
        result = await sink.upsert(item)
        assert result == "test_1"
        assert await sink.exists("test", "test_1") is True

    async def test_upsert_batch(self, config_dir: Path) -> None:
        sink = JsonFileContentSink(config_dir)
        items = [_make_item("a"), _make_item("b")]
        ids = await sink.upsert_batch(items)
        assert ids == ["a", "b"]


class TestJsonFileSyncStateStore:
    async def test_get_missing(self, config_dir: Path) -> None:
        store = JsonFileSyncStateStore(config_dir)
        assert await store.get_cursor("local", "teams", "default") is None

    async def test_set_and_get(self, config_dir: Path) -> None:
        store = JsonFileSyncStateStore(config_dir)
        await store.set_cursor("local", "teams", "default", {"last": "2026"})
        cursor = await store.get_cursor("local", "teams", "default")
        assert cursor == {"last": "2026"}


class TestJsonlSyncLogger:
    async def test_log_complete(self, config_dir: Path) -> None:
        logger = JsonlSyncLogger(config_dir)
        sync_id = await logger.log_start("local", "teams")
        assert isinstance(sync_id, str)

        stats = SyncStats(items_fetched=10, items_new=5, errors=0, duration_seconds=1.5)
        await logger.log_complete(sync_id, stats)

        from ingestion.cli.state import load_history

        entries = load_history(config_dir)
        assert len(entries) == 1
        assert entries[0]["status"] == "complete"
        assert entries[0]["items_fetched"] == 10

    async def test_log_failure(self, config_dir: Path) -> None:
        logger = JsonlSyncLogger(config_dir)
        sync_id = await logger.log_start("local", "moodle")
        await logger.log_failure(sync_id, "Token expired")

        from ingestion.cli.state import load_history

        entries = load_history(config_dir)
        assert len(entries) == 1
        assert entries[0]["status"] == "failed"
        assert entries[0]["error"] == "Token expired"
