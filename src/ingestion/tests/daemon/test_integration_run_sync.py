"""End-to-end: seed creds, mock Moodle, run_sync, assert DB + cursor state.

Proves the Phase 1 exit criterion: ``ingestion.pipeline.run_sync`` works
against the real daemon ports (Postgres + SQLite) with only HTTP mocked.

Skips automatically when Postgres isn't reachable from the host (see
``conftest.py``). CI's Postgres service container always satisfies this.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from ingestion.adapters.moodle.adapter import MoodleAdapter
from ingestion.config import MoodleConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.ports.content_sink import PostgresContentSink
from ingestion.daemon.ports.sync_logger import STATE_COMPLETE, SqliteSyncLogger
from ingestion.daemon.ports.sync_state import SqliteSyncStateStore
from ingestion.daemon.ports.token_store import PostgresTokenStore
from ingestion.daemon.store.local import apply_migrations
from ingestion.pipeline import run_sync
from ingestion.schemas import PAYLOAD_SCHEMA_VERSION, FetchScope

pytestmark = pytest.mark.django_db(transaction=True)

MOODLE_BASE = "https://moodle.test"
MOODLE_ENDPOINT = f"{MOODLE_BASE}/webservice/rest/server.php"
USER_EMAIL = "alice@example.edu"


async def _noop_sleep(_seconds: float) -> None:
    pass


def _mock_courses() -> httpx.Response:
    return httpx.Response(
        200,
        json=[{"id": 9273, "fullname": "DSCA Tutorial", "visible": 1}],
    )


def _mock_assignments() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "courses": [
                {
                    "id": 9273,
                    "fullname": "DSCA Tutorial",
                    "assignments": [
                        {
                            "id": 5897,
                            "name": "Homework Test",
                            "intro": "<p>Test assignment</p>",
                            "introformat": 1,
                            "duedate": 1770937140,
                            "grade": 10,
                            "timemodified": 1770992339,
                        }
                    ],
                }
            ],
            "warnings": [],
        },
    )


@respx.mock
async def test_run_sync_end_to_end_persists_items_cursor_and_history(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    from core.models import IngestedItem, PlatformCredential

    respx.post(url__eq=MOODLE_ENDPOINT).mock(side_effect=[_mock_courses(), _mock_assignments()])

    sqlite_path = tmp_path / "state.db"
    apply_migrations(sqlite_path)

    cipher = CredentialCipher([fernet_key])
    token_store = PostgresTokenStore(cipher)
    await token_store.update_credentials(
        USER_EMAIL,
        "moodle",
        {"token": "moodle-tok-123", "user_id": 12059},
    )

    # Sanity: the row we expect is wired correctly and decrypts back.
    seeded = await PlatformCredential.objects.aget(
        user_email=USER_EMAIL,
        platform="moodle",
    )
    assert seeded.is_expired is False
    assert cipher.decrypt_json(bytes(seeded.encrypted_payload)) == {
        "token": "moodle-tok-123",
        "user_id": 12059,
    }

    adapter = MoodleAdapter(
        MoodleConfig(base_url=MOODLE_BASE, max_retries=0, requests_per_second=0),
        sleep_func=_noop_sleep,
    )
    content_sink = PostgresContentSink()
    sync_state = SqliteSyncStateStore(sqlite_path)
    sync_logger = SqliteSyncLogger(sqlite_path)

    credentials = await token_store.get_credentials(USER_EMAIL, "moodle")
    assert credentials is not None

    stats, sync_id = await run_sync(
        adapter=adapter,
        credentials=credentials,
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id=USER_EMAIL,
        scope=frozenset({FetchScope.ASSIGNMENTS}),
        token_store=token_store,
    )

    assert isinstance(sync_id, str) and sync_id
    assert stats.errors == 0
    assert stats.items_fetched == 1
    assert stats.items_new == 1

    rows = [row async for row in IngestedItem.objects.filter(platform="moodle").all()]
    assert len(rows) == 1
    row = rows[0]
    assert row.external_id == "moodle_assign_5897"
    assert row.payload["content_type"] == "assignment"
    assert row.schema_version == PAYLOAD_SCHEMA_VERSION

    cursor = await sync_state.get_cursor(USER_EMAIL, "moodle", "default")
    assert cursor is not None

    import sqlite3

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        history = conn.execute(
            "SELECT state, user_email, platform FROM sync_history WHERE user_email = ?",
            (USER_EMAIL,),
        ).fetchall()
    finally:
        conn.close()
    assert len(history) == 1
    assert history[0]["state"] == STATE_COMPLETE
    assert history[0]["platform"] == "moodle"
