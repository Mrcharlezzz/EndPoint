"""SQLite-backed :class:`SyncStateStore` against ``sync_cursors``.

Cursors live in the daemon-local SQLite. Key is ``(user_email, platform, scope)``.
The pipeline writes the cursor only on full success, so replays on restart
resume exactly where the last completed sync left off.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from ingestion.daemon.store.async_conn import connect

if TYPE_CHECKING:
    from pathlib import Path


class SqliteSyncStateStore:
    """Read/write the ``sync_cursors`` table."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def get_cursor(
        self,
        user_id: str,
        platform: str,
        scope: str,
    ) -> dict[str, Any] | None:
        async with (
            connect(self._db_path) as conn,
            conn.execute(
                "SELECT cursor_json FROM sync_cursors "
                "WHERE user_email = ? AND platform = ? AND scope = ?",
                (user_id, platform, scope),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            return None
        decoded: Any = json.loads(row[0])
        if not isinstance(decoded, dict):
            raise ValueError(
                f"sync_cursors row for {user_id}/{platform}/{scope} is not a JSON object"
            )
        return cast("dict[str, Any]", decoded)

    async def set_cursor(
        self,
        user_id: str,
        platform: str,
        scope: str,
        cursor: dict[str, Any],
    ) -> None:
        payload = json.dumps(cursor, separators=(",", ":"))
        now = datetime.now(tz=UTC).isoformat()
        async with connect(self._db_path) as conn:
            await conn.execute(
                """
                INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_email, platform, scope) DO UPDATE SET
                    cursor_json = excluded.cursor_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, platform, scope, payload, now),
            )
            await conn.commit()
