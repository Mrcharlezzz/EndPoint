"""Audit log: append-only writes and filtered reads."""

from __future__ import annotations

from pathlib import Path

import pytest
from ingestion.daemon.store.audit import list_entries, write_entry
from ingestion.daemon.store.local import apply_migrations


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


async def test_write_entry_inserts_row(db_path: Path) -> None:
    row_id = await write_entry(
        db_path,
        actor="admin",
        action="user.pause",
        target="alice@example.com",
        before={"is_paused": False},
        after={"is_paused": True},
        reason="excessive failures",
    )
    assert row_id > 0

    entries = await list_entries(db_path)
    assert len(entries) == 1
    assert entries[0]["actor"] == "admin"
    assert entries[0]["action"] == "user.pause"
    assert entries[0]["target"] == "alice@example.com"
    assert entries[0]["before"] == {"is_paused": False}
    assert entries[0]["after"] == {"is_paused": True}
    assert entries[0]["reason"] == "excessive failures"


async def test_write_entry_with_null_fields(db_path: Path) -> None:
    await write_entry(
        db_path,
        action="system.boot",
    )
    entries = await list_entries(db_path)
    assert len(entries) == 1
    assert entries[0]["actor"] is None
    assert entries[0]["before"] is None
    assert entries[0]["after"] is None
    assert entries[0]["reason"] is None


async def test_list_entries_filters_by_action(db_path: Path) -> None:
    await write_entry(db_path, action="user.pause", target="alice@x")
    await write_entry(db_path, action="user.unpause", target="alice@x")
    await write_entry(db_path, action="config.update", target="scheduler.tick_seconds")

    pauses = await list_entries(db_path, action="user.pause")
    assert len(pauses) == 1
    assert pauses[0]["action"] == "user.pause"


async def test_list_entries_filters_by_target(db_path: Path) -> None:
    await write_entry(db_path, action="user.pause", target="alice@x")
    await write_entry(db_path, action="user.pause", target="bob@x")
    await write_entry(db_path, action="sync.force", target="alice@x")

    alice_rows = await list_entries(db_path, target="alice@x")
    assert len(alice_rows) == 2
    assert {r["action"] for r in alice_rows} == {"user.pause", "sync.force"}


async def test_list_entries_orders_by_timestamp_desc(db_path: Path) -> None:
    await write_entry(db_path, action="first", target="x")
    await write_entry(db_path, action="second", target="x")
    await write_entry(db_path, action="third", target="x")

    rows = await list_entries(db_path)
    assert [r["action"] for r in rows] == ["third", "second", "first"]


async def test_list_entries_respects_limit(db_path: Path) -> None:
    for i in range(5):
        await write_entry(db_path, action=f"a{i}")

    rows = await list_entries(db_path, limit=2)
    assert len(rows) == 2
