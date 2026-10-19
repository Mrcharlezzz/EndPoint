from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from ingestion.daemon.production_seed import (
    DAEMON_TABLES,
    POSTGRES_EXCLUDED_TABLES,
    POSTGRES_TABLES,
    SeedValidationError,
    _count_postgres_table,
    export_sqlite_seed_rows,
    import_sqlite_seed_rows,
    prepare_postgres_import,
    sanitize_postgres_row,
    validate_artifact,
)
from ingestion.daemon.store.local import apply_migrations


def _artifact() -> dict[str, object]:
    return {
        "format_version": 1,
        "created_at": "2026-05-04T10:00:00Z",
        "manifest": {
            "postgres": {table: 0 for table in POSTGRES_TABLES},
            "daemon": {table: 0 for table in DAEMON_TABLES},
        },
        "postgres": {table: [] for table in POSTGRES_TABLES},
        "daemon": {table: [] for table in DAEMON_TABLES},
    }


def test_sanitize_ingested_item_clears_user_and_raw_payload() -> None:
    row = {
        "external_id": "teams_graph_1",
        "fetched_by_user_email": "alice@example.com",
        "payload": {"title": "Visible payload"},
        "raw_payload": {"graph_token": "secret"},
    }

    sanitized = sanitize_postgres_row("ingested_items", row)

    assert sanitized["fetched_by_user_email"] is None
    assert sanitized["raw_payload"] == {}
    assert row["fetched_by_user_email"] == "alice@example.com"
    assert row["raw_payload"] == {"graph_token": "secret"}


def test_excluded_tables_do_not_include_dropped_user_telegram_groups() -> None:
    assert "presentation_api_user_telegram_groups" not in POSTGRES_EXCLUDED_TABLES


def test_validate_artifact_rejects_missing_preserved_table() -> None:
    artifact = _artifact()
    postgres = artifact["postgres"]
    assert isinstance(postgres, dict)
    postgres.pop("attachments")

    with pytest.raises(SeedValidationError, match="attachments"):
        validate_artifact(artifact)


def test_prepare_postgres_import_defers_base_to_canonical_linking() -> None:
    artifact = _artifact()
    postgres = artifact["postgres"]
    assert isinstance(postgres, dict)
    postgres["courses"] = [{"id": "course-1", "name": "Algorithms"}]
    postgres["base_classified_items"] = [
        {
            "id": "item-1",
            "title": "Exam update",
            "canonical_entity_id": "canonical-1",
        }
    ]
    postgres["assignments"] = [{"baseclassifieditem_ptr_id": "item-1", "deadline": None}]
    postgres["canonical_entities"] = [
        {
            "id": "canonical-1",
            "primary_item_id": "item-1",
            "course_id": "course-1",
        }
    ]

    batches, backfills = prepare_postgres_import(postgres)

    assert [batch.table for batch in batches] == [
        "courses",
        "base_classified_items",
        "assignments",
        "canonical_entities",
    ]
    assert batches[1].rows == [
        {
            "id": "item-1",
            "title": "Exam update",
            "canonical_entity_id": None,
        }
    ]
    assert backfills == [("item-1", "canonical-1")]


def test_export_sqlite_seed_rows_returns_ordered_sync_cursors(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    apply_migrations(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("bob@example.com", "teams", "channels", '{"cursor":2}', "2026-05-04T10:05:00Z"),
        )
        conn.execute(
            """
            INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("alice@example.com", "moodle", "default", '{"cursor":1}', "2026-05-04T10:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    rows = export_sqlite_seed_rows(db_path)

    assert rows == [
        {
            "user_email": "alice@example.com",
            "platform": "moodle",
            "scope": "default",
            "cursor_json": '{"cursor":1}',
            "updated_at": "2026-05-04T10:00:00Z",
        },
        {
            "user_email": "bob@example.com",
            "platform": "teams",
            "scope": "channels",
            "cursor_json": '{"cursor":2}',
            "updated_at": "2026-05-04T10:05:00Z",
        },
    ]


def test_count_postgres_table_accepts_mapping_rows() -> None:
    class FakeCursor:
        def __init__(self, row: object) -> None:
            self.row = row
            self.query: object | None = None

        def execute(self, query: object) -> None:
            self.query = query

        def fetchone(self) -> object:
            return self.row

    cur = FakeCursor({"count": 3})

    assert _count_postgres_table(cur, "courses") == 3
    assert cur.query is not None


def test_import_sqlite_seed_rows_preserves_config_overlay(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    apply_migrations(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO config_overlay (key, value_json, updated_at)
            VALUES (?, ?, ?)
            """,
            ("scheduler.global_paused", "true", "2026-05-04T10:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    import_sqlite_seed_rows(
        db_path,
        [
            {
                "user_email": "alice@example.com",
                "platform": "moodle",
                "scope": "default",
                "cursor_json": '{"cursor":1}',
                "updated_at": "2026-05-04T10:00:00Z",
            }
        ],
    )

    conn = sqlite3.connect(str(db_path))
    try:
        overlay = conn.execute("SELECT key, value_json FROM config_overlay").fetchall()
        cursors = conn.execute(
            """
            SELECT user_email, platform, scope, cursor_json, updated_at
            FROM sync_cursors
            """
        ).fetchall()
    finally:
        conn.close()

    assert overlay == [("scheduler.global_paused", "true")]
    assert cursors == [
        (
            "alice@example.com",
            "moodle",
            "default",
            '{"cursor":1}',
            "2026-05-04T10:00:00Z",
        )
    ]


def test_import_sqlite_seed_rows_rejects_nonempty_runtime_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    apply_migrations(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO sync_history (id, user_email, platform, started_at, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("sync-1", "alice@example.com", "moodle", "2026-05-04T10:00:00Z", "running"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SeedValidationError, match="sync_history"):
        import_sqlite_seed_rows(db_path, [])
