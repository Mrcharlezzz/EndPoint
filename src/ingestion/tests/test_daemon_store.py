"""SQLite migration runner and overlay round-trip."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ingestion.daemon.store.local import apply_migrations, open_store


def test_migrations_are_applied_once(tmp_path: Path) -> None:
    db = tmp_path / "state.db"

    first = apply_migrations(db)
    second = apply_migrations(db)

    assert "0001_initial.sql" in first
    assert first == sorted(first)  # applied in lexical order
    assert second == []  # idempotent


def test_sync_history_has_sync_kind_column_with_content_sync_default(tmp_path: Path) -> None:
    """0008 adds the sync_kind discriminator; legacy rows default to content_sync."""
    db = tmp_path / "state.db"
    apply_migrations(db)

    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(sync_history)")}
        assert "sync_kind" in cols
        # dflt_value is column index 4 in PRAGMA table_info; SQLite quotes string
        # defaults, e.g. "'content_sync'".
        assert cols["sync_kind"][4] == "'content_sync'"

        # Inserting without the new column should fall back to the default.
        conn.execute(
            """
            INSERT INTO sync_history (id, user_email, platform, started_at, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("legacy-row", "alice@x.test", "teams", "2026-04-20T00:00:00+00:00", "complete"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT sync_kind FROM sync_history WHERE id = ?",
            ("legacy-row",),
        ).fetchone()
        assert row is not None
        assert row[0] == "content_sync"

        index_names = {row[1] for row in conn.execute("PRAGMA index_list(sync_history)")}
        assert "ix_sync_history_kind_time" in index_names
    finally:
        conn.close()


def test_overlay_round_trip(tmp_path: Path) -> None:
    store = open_store(tmp_path / "state.db")
    try:
        assert store.get_overlay("missing") is None

        store.set_overlay("scheduler.global_paused", True)
        store.set_overlay("platforms.teams_enabled", False)

        assert store.get_overlay("scheduler.global_paused") is True
        assert store.get_overlay("platforms.teams_enabled") is False
        assert store.all_overlay() == {
            "scheduler.global_paused": True,
            "platforms.teams_enabled": False,
        }

        store.set_overlay("scheduler.global_paused", False)
        store.delete_overlay("platforms.teams_enabled")

        assert store.all_overlay() == {"scheduler.global_paused": False}
    finally:
        store.close()


def test_open_store_runs_migrations(tmp_path: Path) -> None:
    store = open_store(tmp_path / "state.db")
    try:
        # If migrations ran, the table exists and the overlay helpers work.
        store.set_overlay("k", {"nested": [1, 2, 3]})
        assert store.get_overlay("k") == {"nested": [1, 2, 3]}
    finally:
        store.close()


def test_migrations_skip_non_conforming_filenames(tmp_path: Path) -> None:
    # A stray "fix.sql" (no NNNN_ prefix) would sort after 0004_* and run
    # out of order if we didn't filter. Expect the runner to skip it.
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_first.sql").write_text("CREATE TABLE a (id INTEGER);")
    (mig_dir / "fix.sql").write_text("CREATE TABLE wrong (id INTEGER);")
    (mig_dir / "NOT_A_MIGRATION.sql").write_text("oops")

    db = tmp_path / "state.db"
    applied = apply_migrations(db, migrations_dir=mig_dir)

    assert applied == ["0001_first.sql"]


def test_migrations_handle_bom_prefixed_files(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_bom.sql").write_bytes(b"\xef\xbb\xbfCREATE TABLE t (id INTEGER);")

    db = tmp_path / "state.db"
    applied = apply_migrations(db, migrations_dir=mig_dir)

    assert applied == ["0001_bom.sql"]
