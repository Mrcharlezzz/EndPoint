"""JSON-file implementations of the 4 pipeline ports for CLI usage.

These are concrete implementations that persist to ~/.config/ingest/
and are used when running the pipeline locally (not via Django).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — used at runtime in default args
from typing import TYPE_CHECKING

from ingestion.cli.state import (
    DEFAULT_CONFIG_DIR,
    append_history,
    compute_item_hash,
    load_cursor,
    load_seen_ids,
    save_cursor,
    save_seen_ids,
    update_seen_id,
)

if TYPE_CHECKING:
    from ingestion.schemas import NormalizedItem, SyncStats


class JsonFileTokenStore:
    """TokenStore backed by credentials.json.

    Note: The CLI manages credentials via auth flows (login commands),
    so this is mostly used by the pipeline for mark_expired().
    """

    def __init__(self, config_dir: Path = DEFAULT_CONFIG_DIR) -> None:
        self._config_dir = config_dir

    async def get_credentials(self, user_id: str, platform: str) -> dict | None:
        from ingestion.cli.state import load_credentials

        return load_credentials(platform, self._config_dir)

    async def update_credentials(self, user_id: str, platform: str, credentials: dict) -> None:
        from ingestion.cli.state import save_credentials

        save_credentials(platform, credentials, self._config_dir)

    async def mark_expired(self, user_id: str, platform: str) -> None:
        """Mark a platform's credentials as expired (delete them)."""
        from ingestion.cli.state import clear_credentials

        clear_credentials(platform, self._config_dir)


class JsonFileContentSink:
    """ContentSink backed by seen_ids.json for dedup tracking."""

    def __init__(self, config_dir: Path = DEFAULT_CONFIG_DIR) -> None:
        self._config_dir = config_dir
        self._seen_ids: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._seen_ids is None:
            self._seen_ids = load_seen_ids(self._config_dir)
        return self._seen_ids

    async def exists(self, platform: str, external_id: str) -> bool:
        return external_id in self._load()

    async def upsert(self, item: NormalizedItem) -> str:
        seen = self._load()
        content_hash_raw = item.raw_payload.get("content_hash")
        content_hash = (
            content_hash_raw
            if isinstance(content_hash_raw, str) and content_hash_raw
            else compute_item_hash(item.model_dump(mode="json"))
        )
        self._seen_ids = update_seen_id(item.external_id, content_hash, seen)
        save_seen_ids(self._seen_ids, self._config_dir)
        return item.external_id

    async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]:
        ids = []
        for item in items:
            item_id = await self.upsert(item)
            ids.append(item_id)
        return ids


class JsonFileSyncStateStore:
    """SyncStateStore backed by cursors/{platform}.json."""

    def __init__(self, config_dir: Path = DEFAULT_CONFIG_DIR) -> None:
        self._config_dir = config_dir

    async def get_cursor(self, user_id: str, platform: str, scope: str) -> dict | None:
        return load_cursor(platform, self._config_dir)

    async def set_cursor(self, user_id: str, platform: str, scope: str, cursor: dict) -> None:
        save_cursor(platform, cursor, self._config_dir)


class JsonlSyncLogger:
    """SyncLogger backed by history.jsonl."""

    def __init__(self, config_dir: Path = DEFAULT_CONFIG_DIR) -> None:
        self._config_dir = config_dir
        self._runs: dict[str, dict] = {}

    async def log_start(self, user_id: str, platform: str) -> str:
        sync_id = str(uuid.uuid4())[:8]
        self._runs[sync_id] = {
            "sync_id": sync_id,
            "platform": platform,
            "started_at": datetime.now(tz=UTC).isoformat(),
            "status": "running",
        }
        return sync_id

    async def log_complete(self, sync_id: str, stats: SyncStats) -> None:
        entry = self._runs.pop(sync_id, {"sync_id": sync_id})
        entry.update(
            {
                "status": "complete",
                "completed_at": datetime.now(tz=UTC).isoformat(),
                "items_fetched": stats.items_fetched,
                "items_new": stats.items_new,
                "items_updated": stats.items_updated,
                "errors": stats.errors,
                "duration_seconds": round(stats.duration_seconds, 2),
            }
        )
        append_history(entry, self._config_dir)

    async def log_failure(self, sync_id: str, error: str) -> None:
        entry = self._runs.pop(sync_id, {"sync_id": sync_id})
        entry.update(
            {
                "status": "failed",
                "failed_at": datetime.now(tz=UTC).isoformat(),
                "error": error,
            }
        )
        append_history(entry, self._config_dir)
