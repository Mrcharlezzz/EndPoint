"""Coverage for the audit-writer DI seam.

* :class:`AuditWriter` is a Protocol; any class with the matching
  ``write`` coroutine satisfies it.
* :class:`SqliteAuditWriter` binds the db path + actor at construction
  and forwards everything else to ``audit_store.write_entry``. Pin that
  the binding holds and that the row lands in the audit table with the
  right shape.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from ingestion.daemon.services.audit_writer import AuditWriter, SqliteAuditWriter
from ingestion.daemon.store.local import apply_migrations


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


class _CapturingWriter:
    """Hand-rolled writer that satisfies :class:`AuditWriter` purely by
    having a ``write`` coroutine with the matching signature.

    Used both as a regression check on the Protocol surface AND as a
    test helper the service-layer tests reuse.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._next_id = 1

    async def write(
        self,
        *,
        action: str,
        target: str | None = None,
        before: object = None,
        after: object = None,
        reason: str | None = None,
    ) -> int:
        self.calls.append(
            {
                "action": action,
                "target": target,
                "before": before,
                "after": after,
                "reason": reason,
            }
        )
        row_id = self._next_id
        self._next_id += 1
        return row_id


def test_capturing_writer_is_structural_match_for_protocol() -> None:
    """A duck-typed writer satisfies the Protocol. Pin the Protocol's
    surface — adding a method here without updating the in-memory
    fakes is a contract break."""
    writer: AuditWriter = _CapturingWriter()
    assert callable(writer.write)


def test_sqlite_writer_is_structural_match_for_protocol() -> None:
    """Production writer satisfies the Protocol structurally."""
    writer: AuditWriter = SqliteAuditWriter(Path("/tmp/dummy.db"), "actor")
    assert callable(writer.write)


# ---------------------------------------------------------------------------
# Sqlite writer behaviour
# ---------------------------------------------------------------------------


async def test_sqlite_writer_inserts_one_row(db_path: Path) -> None:
    writer = SqliteAuditWriter(db_path, actor="admin@127.0.0.1/123")
    row_id = await writer.write(
        action="user.pause",
        target="alice@x",
        before={"is_paused": False},
        after={"is_paused": True, "rows_updated": 2},
        reason="triage",
    )
    assert row_id > 0

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT actor, action, target, before_json, after_json, reason "
            "FROM audit_log WHERE id = ?",
            (row_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    actor, action, target, before_json, after_json, reason = row
    assert actor == "admin@127.0.0.1/123"
    assert action == "user.pause"
    assert target == "alice@x"
    assert before_json == '{"is_paused":false}'
    assert after_json == '{"is_paused":true,"rows_updated":2}'
    assert reason == "triage"


async def test_sqlite_writer_binds_actor_at_construction(db_path: Path) -> None:
    """Two writers with different actors must produce rows with each
    actor — pins that no global state leaks between writer instances."""
    a = SqliteAuditWriter(db_path, actor="alice")
    b = SqliteAuditWriter(db_path, actor="bob")
    await a.write(action="x")
    await b.write(action="y")

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT action, actor FROM audit_log ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [("x", "alice"), ("y", "bob")]


async def test_sqlite_writer_accepts_optional_args_as_none(db_path: Path) -> None:
    """Bare ``write(action=...)`` succeeds — every other arg defaults
    to None and lands as NULL in the row."""
    writer = SqliteAuditWriter(db_path, actor="sys")
    row_id = await writer.write(action="bare")

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT target, before_json, after_json, reason FROM audit_log WHERE id = ?",
            (row_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row == (None, None, None, None)


async def test_sqlite_writer_returns_distinct_ids_per_call(db_path: Path) -> None:
    """The store helper returns the inserted rowid; pin that ``write``
    forwards it. Useful when callers want to backref the audit row."""
    writer = SqliteAuditWriter(db_path, actor="sys")
    a = await writer.write(action="a")
    b = await writer.write(action="b")
    assert a != b
    assert b > a


async def test_sqlite_writer_serialises_complex_payloads(db_path: Path) -> None:
    """Lists and nested dicts inside before/after round-trip through the
    audit row; pins that the JSON encoder isn't a regression away from
    coercing them to ``str(obj)``."""
    writer = SqliteAuditWriter(db_path, actor="sys")
    payload = {"platforms": [{"platform": "moodle", "is_paused": False}]}
    row_id = await writer.write(action="user.pause", target="alice@x", before=payload)

    conn = sqlite3.connect(str(db_path))
    try:
        before_json = conn.execute(
            "SELECT before_json FROM audit_log WHERE id = ?", (row_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    # Compact form (no spaces); matches the audit_store's separators.
    assert before_json == '{"platforms":[{"platform":"moodle","is_paused":false}]}'
