"""Slice 12 commit 2: scheduler ``_run_scope_cleanup_once`` + integration.

Pinned behaviours that the impl-baseline file does not exhaustively
cover:

* the cleanup pass walks every registered platform — a multi-platform
  scheduler with mixed ``scope_for_platform`` resolutions cleans only
  what the resolver actually advertised;
* a resolver returning ``None`` for one platform skips that platform
  but does NOT abort the pass (other platforms still pruned);
* exceptions inside ``cleanup_stale_cursors`` are caught per platform
  via ``log.exception`` so one platform's failure doesn't poison the
  others;
* ``current_scopes`` always implicitly includes the legacy ``default``
  bucket — a cursor at scope=='default' is never deleted unless the
  resolver explicitly excludes it AND the impl removes the implicit
  union (current behaviour: it's always added);
* ``reset_scope_cleanup`` re-arms cleanly so subsequent ticks re-run
  the pass;
* the warn-once memory in ``_scope_vanish_warned`` is cleared after
  cleanup runs so subsequent drift re-warns.

The scheduler integration uses the same in-memory stub pattern the
implementer's baseline uses; this file pushes harder on edge surfaces.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ingestion.daemon.candidates import InMemoryCandidateSource
from ingestion.daemon.dead_letter import DeadLetterTracker
from ingestion.daemon.lifecycle.cleanup import cleanup_stale_cursors
from ingestion.daemon.scheduler import Scheduler, SchedulerDeps
from ingestion.daemon.store.local import apply_migrations, open_store
from ingestion.daemon.worker import WorkerPool


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "state.db"
    apply_migrations(p)
    return p


def _seed_cursor(
    db_path: Path,
    *,
    user_email: str,
    platform: str,
    scope: str,
) -> None:
    now = datetime.now(tz=UTC).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_email, platform, scope, "{}", now),
        )
        conn.commit()
    finally:
        conn.close()


def _count_cursors(db_path: Path, platform: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sync_cursors WHERE platform = ?",
            (platform,),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


class _ServiceStub:
    """Catch-all stub for the SchedulerDeps protocols. Each method is
    a no-op returning the protocol's default for the slice 12 path."""

    async def list_candidates(self) -> list[Any]:
        return []

    async def get_credentials(self, *_a: Any, **_kw: Any) -> dict[str, Any] | None:
        return None

    async def update_credentials(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def mark_expired(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def upsert(self, *_a: Any, **_kw: Any) -> bool:
        return False

    async def upsert_batch(self, *_a: Any, **_kw: Any) -> int:
        return 0

    async def get_cursor(self, *_a: Any, **_kw: Any) -> dict[str, Any] | None:
        return None

    async def set_cursor(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def log_start(self, *_a: Any, **_kw: Any) -> str:
        return "id"

    async def log_complete(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def log_failure(self, *_a: Any, **_kw: Any) -> None:
        return None


def _build_scheduler(
    db_path: Path,
    *,
    scope_for_platform: Any | None = None,
    event_sink: Any | None = None,
) -> Scheduler:
    pool = WorkerPool(global_limit=2)
    dead_letter = DeadLetterTracker(db_path, threshold=3, cooldown_seconds=60)
    stub = _ServiceStub()
    deps = SchedulerDeps(
        config_store=open_store(db_path),
        candidate_source=InMemoryCandidateSource([]),
        worker_pool=pool,
        dead_letter=dead_letter,
        token_store=stub,  # type: ignore[arg-type]
        content_sink=stub,  # type: ignore[arg-type]
        sync_state=stub,  # type: ignore[arg-type]
        sync_logger=stub,  # type: ignore[arg-type]
        adapter_factory=lambda _p: None,  # type: ignore[arg-type,return-value]
        sqlite_path=db_path,
        scope_for_platform=scope_for_platform,
        event_sink=event_sink,
    )
    return Scheduler(deps)


# ---------------------------------------------------------------------------
# _run_scope_cleanup_once: integration with cleanup_stale_cursors
# ---------------------------------------------------------------------------


async def test_run_scope_cleanup_skipped_when_resolver_is_none(db_path: Path) -> None:
    """Without ``scope_for_platform``, the cleanup pass returns 0
    without touching the DB."""
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="legacy")
    scheduler = _build_scheduler(db_path, scope_for_platform=None)
    deleted = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    assert deleted == 0
    # Cursor untouched.
    assert _count_cursors(db_path, "teams") == 1


async def test_run_scope_cleanup_idempotent_after_first_pass(db_path: Path) -> None:
    """Once the pass runs, ``_scope_cleanup_done`` is True and a
    second call returns 0 without re-running cleanup."""
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="legacy")
    scheduler = _build_scheduler(
        db_path,
        scope_for_platform=lambda p: frozenset({"channels"}) if p == "teams" else None,
    )
    first = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    assert first >= 1
    # Even if we re-seed a stale cursor, the second call no-ops.
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="another_legacy")
    second = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    assert second == 0


async def test_run_scope_cleanup_resolver_returning_none_skips_platform(
    db_path: Path,
) -> None:
    """A resolver that returns None for moodle skips moodle's cleanup
    but still cleans teams."""
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="legacy")
    _seed_cursor(db_path, user_email="u@x", platform="moodle", scope="something")

    def resolver(p: str) -> Any:
        if p == "teams":
            return frozenset({"channels"})
        return None  # opt-out for moodle

    scheduler = _build_scheduler(db_path, scope_for_platform=resolver)
    deleted = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    # Teams cleaned (1 stale row); moodle untouched.
    assert deleted == 1
    assert _count_cursors(db_path, "moodle") == 1


async def test_run_scope_cleanup_default_scope_implicitly_kept(db_path: Path) -> None:
    """The pipeline's "default" bucket is added to ``current_scopes``
    by the scheduler regardless of the resolver, so a cursor at
    scope='default' must NOT be pruned even when the resolver doesn't
    list it."""
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="default")
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="legacy")
    scheduler = _build_scheduler(
        db_path,
        scope_for_platform=lambda _p: frozenset({"channels"}),  # excludes 'default'
    )
    deleted = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    assert deleted == 1  # only 'legacy'
    # 'default' survives.
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT scope FROM sync_cursors WHERE platform=?", ("teams",)
        ).fetchall()
    finally:
        conn.close()
    scopes = {r[0] for r in rows}
    assert "default" in scopes
    assert "legacy" not in scopes


async def test_run_scope_cleanup_multi_platform_walks_all(db_path: Path) -> None:
    """The pass iterates ``iter_platforms()`` so every registered
    platform's cursors get a cleanup pass."""
    # Teams + moodle both have stale rows.
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="legacy_t")
    _seed_cursor(db_path, user_email="u@x", platform="moodle", scope="legacy_m")
    scheduler = _build_scheduler(
        db_path,
        scope_for_platform=lambda p: frozenset({"keep"}) if p in {"teams", "moodle"} else None,
    )
    deleted = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    assert deleted == 2


async def test_run_scope_cleanup_one_platform_failing_does_not_abort_others(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``cleanup_stale_cursors`` raises for one platform, the others
    still get cleaned. The failure is logged via ``log.exception``."""
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="legacy_t")
    _seed_cursor(db_path, user_email="u@x", platform="moodle", scope="legacy_m")
    real = cleanup_stale_cursors

    async def faulty(*args: Any, **kwargs: Any) -> int:
        if kwargs.get("platform") == "teams":
            raise RuntimeError("teams-only cleanup boom")
        return await real(*args, **kwargs)

    # Patch the lazy import path used inside _run_scope_cleanup_once.
    import ingestion.daemon.lifecycle.cleanup as cleanup_module

    monkeypatch.setattr(cleanup_module, "cleanup_stale_cursors", faulty)
    scheduler = _build_scheduler(
        db_path,
        scope_for_platform=lambda p: frozenset({"keep"}) if p in {"teams", "moodle"} else None,
    )
    deleted = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    # Moodle was cleaned (1); teams blew up.
    assert deleted == 1


async def test_reset_re_arms_so_next_call_runs(db_path: Path) -> None:
    """``reset_scope_cleanup`` flips ``_scope_cleanup_done`` so a
    subsequent call re-runs the pass."""
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="legacy")
    scheduler = _build_scheduler(
        db_path,
        scope_for_platform=lambda p: frozenset({"channels"}) if p == "teams" else None,
    )
    await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    # Re-seed and re-arm.
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="another_legacy")
    scheduler.reset_scope_cleanup()
    deleted = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    assert deleted == 1


async def test_run_scope_cleanup_emits_event_per_pruned_scope(db_path: Path) -> None:
    """Each pruned scope produces one event on the lifecycle channel
    via the scheduler's event_sink."""
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="stale_a")
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="stale_b")
    captured: list[tuple[str, dict[str, Any]]] = []

    def sink(channel: str, payload: dict[str, Any]) -> None:
        captured.append((channel, payload))

    scheduler = _build_scheduler(
        db_path,
        scope_for_platform=lambda p: frozenset({"keep"}) if p == "teams" else None,
        event_sink=sink,
    )
    deleted = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    assert deleted == 2
    # Two events on the lifecycle channel for the stale scopes.
    teams_events = [
        p for c, p in captured if c == "lifecycle" and p["event"] == "lifecycle.cursor_pruned"
    ]
    assert len(teams_events) == 2
    scopes = sorted(p["scope"] for p in teams_events)
    assert scopes == ["stale_a", "stale_b"]


async def test_run_scope_cleanup_clears_warn_memory(db_path: Path) -> None:
    """After the cleanup runs, the warn-once memory should be cleared
    so subsequent vanished scopes re-warn (the impl explicitly clears
    ``_scope_vanish_warned`` at the bottom of the pass)."""
    scheduler = _build_scheduler(
        db_path,
        scope_for_platform=lambda p: frozenset({"keep"}) if p == "teams" else None,
    )
    # Pretend we already warned about a couple of scopes.
    scheduler._scope_vanish_warned.add(("u@x", "teams", "stale1"))  # noqa: SLF001
    scheduler._scope_vanish_warned.add(("u@x", "teams", "stale2"))  # noqa: SLF001
    await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    assert scheduler._scope_vanish_warned == set()  # noqa: SLF001


# ---------------------------------------------------------------------------
# cleanup_stale_cursors event sink schema (lifecycle module)
# ---------------------------------------------------------------------------


async def test_cleanup_stale_cursors_event_payload_keys(db_path: Path) -> None:
    """Event payload schema is {event, platform, scope}."""
    _seed_cursor(db_path, user_email="u@x", platform="teams", scope="x")
    captured: list[dict[str, Any]] = []

    def sink(_c: str, p: dict[str, Any]) -> None:
        captured.append(p)

    await cleanup_stale_cursors(
        db_path,
        platform="teams",
        current_scopes=frozenset(),
        event_sink=sink,
    )
    assert len(captured) == 1
    assert set(captured[0].keys()) == {"event", "platform", "scope"}
