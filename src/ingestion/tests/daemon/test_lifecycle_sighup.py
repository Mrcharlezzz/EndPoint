"""SIGHUP config reload + scheduler scope cleanup wiring (slice 12 part B).

Pinned behaviours:

* SIGHUP re-reads the overlay and updates ``runtime.config`` in
  place via ``object.__setattr__`` (the dataclass stays frozen
  for accidental mutation safety, but the SIGHUP path is the one
  deliberate swap);
* SIGHUP re-arms the scheduler's one-shot scope cleanup so
  vanished cursors are deleted on the next tick;
* SIGHUP during shutdown is ignored — re-reading state would
  race the lifecycle close order;
* an overlay validation failure logs ``runtime.config_reload_failed``
  but keeps the daemon running with the prior config;
* scheduler ``_run_scope_cleanup_once`` walks every registered
  platform exactly once until ``reset_scope_cleanup`` re-arms it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ingestion.daemon import runtime as runtime_module
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.runtime import (
    Runtime,
    _diff_config,
    _flat_diff,
    _on_sighup,
)
from ingestion.daemon.store.local import apply_migrations, open_store


@pytest.fixture
def fernet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("ENDPOINT_FERNET_KEY", Fernet.generate_key().decode())


# ---------------------------------------------------------------------------
# _diff_config / _flat_diff
# ---------------------------------------------------------------------------


def test_diff_config_returns_empty_for_equal_dumps() -> None:
    a = {"x": 1, "nested": {"y": 2}}
    b = {"x": 1, "nested": {"y": 2}}
    assert _diff_config(a, b) == {}


def test_diff_config_flattens_nested_keys_to_dotted_paths() -> None:
    a = {"x": 1, "nested": {"y": 2, "z": 3}}
    b = {"x": 2, "nested": {"y": 2, "z": 4}}
    diff = _diff_config(a, b)
    # x changed at root, nested.z changed at depth 1.
    assert "x" in diff
    assert "nested.z" in diff
    assert "nested.y" not in diff


def test_flat_diff_handles_missing_keys_in_either_side() -> None:
    out: dict[str, tuple[object, object]] = {}
    _flat_diff({"a": 1}, {"b": 2}, "", out)
    assert "a" in out
    assert "b" in out


# ---------------------------------------------------------------------------
# _on_sighup
# ---------------------------------------------------------------------------


def _build_runtime(tmp_path: Path) -> Runtime:
    apply_migrations(tmp_path / "state.db")
    cipher = CredentialCipher.from_env()
    store = open_store(tmp_path / "state.db")
    config = DaemonConfig(sqlite_path=tmp_path / "state.db")
    return Runtime(
        config=config,
        store=store,
        cipher=cipher,
        shutdown=asyncio.Event(),
        lock_path=None,
    )


def test_sighup_no_changes_logs_zero_diff(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGHUP without any overlay changes still logs ``runtime.config_reloaded``
    but with ``changes=0``."""
    runtime = _build_runtime(tmp_path)
    captured: list[tuple[str, dict[str, Any]]] = []

    class _RecordingLog:
        def info(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

        def warning(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

        def exception(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: _RecordingLog())

    try:
        _on_sighup(runtime)
        events = [e for e, _ in captured]
        assert "runtime.config_reloaded" in events
        kw = next(kw for e, kw in captured if e == "runtime.config_reloaded")
        assert kw.get("changes") == 0
    finally:
        runtime.store.close()


def test_sighup_picks_up_overlay_change(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An overlay change between init and SIGHUP shows up as a non-empty
    diff and updates ``runtime.config``."""
    runtime = _build_runtime(tmp_path)
    # Flip an overlay key after the runtime was constructed.
    runtime.store.set_overlay("scheduler.global_paused", True)

    captured: list[tuple[str, dict[str, Any]]] = []

    class _RecordingLog:
        def info(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

        def warning(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

        def exception(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: _RecordingLog())

    try:
        assert runtime.config.scheduler.global_paused is False
        _on_sighup(runtime)
        # Config snapshot was swapped in place.
        assert runtime.config.scheduler.global_paused is True
        # Diff was logged with at least one key.
        kw = next(kw for e, kw in captured if e == "runtime.config_reloaded")
        assert kw.get("changes", 0) >= 1
        keys = kw.get("keys") or []
        assert "scheduler.global_paused" in keys
    finally:
        runtime.store.close()


def test_sighup_during_shutdown_is_ignored(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGHUP that arrives after shutdown.set() is dropped — re-reading
    state would race the close order."""
    runtime = _build_runtime(tmp_path)
    runtime.shutdown.set()

    captured: list[tuple[str, dict[str, Any]]] = []

    class _RecordingLog:
        def info(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

        def warning(self, event: str, **kwargs: object) -> None:
            pass

        def exception(self, event: str, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: _RecordingLog())

    try:
        _on_sighup(runtime)
        events = [e for e, _ in captured]
        assert "runtime.sighup_ignored_during_shutdown" in events
        # No config_reloaded log emitted.
        assert "runtime.config_reloaded" not in events
    finally:
        runtime.store.close()


def test_sighup_with_failing_load_keeps_old_config(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If load_config raises (e.g. validation breaks mid-call), the old
    config snapshot stays and the failure is logged."""
    runtime = _build_runtime(tmp_path)
    old_config = runtime.config

    captured: list[tuple[str, dict[str, Any]]] = []

    class _RecordingLog:
        def info(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

        def warning(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

        def exception(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: _RecordingLog())

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("simulated overlay failure")

    monkeypatch.setattr(runtime_module, "load_config", boom)

    try:
        _on_sighup(runtime)
        events = [e for e, _ in captured]
        assert "runtime.config_reload_failed" in events
        # Config snapshot unchanged.
        assert runtime.config is old_config
    finally:
        runtime.store.close()


def test_sighup_re_arms_scheduler_scope_cleanup(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler calls ``reset_scope_cleanup`` on every registered
    scheduler so the next tick re-runs the scope prune."""
    runtime = _build_runtime(tmp_path)
    reset_called: list[bool] = []

    class _FakeScheduler:
        def reset_scope_cleanup(self) -> None:
            reset_called.append(True)

    runtime.schedulers.append(_FakeScheduler())

    class _RecordingLog:
        def info(self, *_a: Any, **_kw: Any) -> None:
            return

        def warning(self, *_a: Any, **_kw: Any) -> None:
            return

        def exception(self, *_a: Any, **_kw: Any) -> None:
            return

    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: _RecordingLog())

    try:
        _on_sighup(runtime)
        assert reset_called == [True]
    finally:
        runtime.store.close()


def test_sighup_no_scheduler_registered_is_safe(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If no Scheduler ever registered (e.g. scheduler disabled), SIGHUP
    still completes — empty list iteration is the no-op path."""
    runtime = _build_runtime(tmp_path)
    assert runtime.schedulers == []

    class _RecordingLog:
        def info(self, *_a: Any, **_kw: Any) -> None:
            return

        def warning(self, *_a: Any, **_kw: Any) -> None:
            return

        def exception(self, *_a: Any, **_kw: Any) -> None:
            return

    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: _RecordingLog())

    try:
        _on_sighup(runtime)  # must not raise
    finally:
        runtime.store.close()


# ---------------------------------------------------------------------------
# Scheduler scope cleanup pass
# ---------------------------------------------------------------------------


async def test_scheduler_scope_cleanup_deletes_cursor_for_removed_scope(
    tmp_path: Path,
) -> None:
    """When ``scope_for_platform`` returns a frozenset that omits a
    stored scope, the scheduler's cleanup pass deletes the cursor."""
    db_path = tmp_path / "state.db"
    apply_migrations(db_path)
    # Seed a stale cursor on teams.
    import sqlite3

    now = datetime.now(tz=UTC).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("alice@x", "teams", "stale_legacy", "{}", now),
        )
        conn.execute(
            "INSERT INTO sync_cursors (user_email, platform, scope, cursor_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("alice@x", "teams", "channels", "{}", now),
        )
        conn.commit()
    finally:
        conn.close()

    # Build a Scheduler with a real scope_for_platform resolver.
    from ingestion.daemon.candidates import InMemoryCandidateSource
    from ingestion.daemon.dead_letter import DeadLetterTracker
    from ingestion.daemon.scheduler import Scheduler, SchedulerDeps
    from ingestion.daemon.worker import WorkerPool

    pool = WorkerPool(global_limit=2)
    dead_letter = DeadLetterTracker(db_path, threshold=3, cooldown_seconds=60)

    class _Stub:
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
            return "x"

        async def log_complete(self, *_a: Any, **_kw: Any) -> None:
            return None

        async def log_failure(self, *_a: Any, **_kw: Any) -> None:
            return None

    stub = _Stub()
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
        scope_for_platform=lambda p: frozenset({"channels"}) if p == "teams" else None,  # type: ignore[arg-type,return-value]
    )
    scheduler = Scheduler(deps)
    deleted = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    assert deleted == 1

    # Stale scope is gone.
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT scope FROM sync_cursors WHERE platform=?", ("teams",)
        ).fetchall()
    finally:
        conn.close()
    scopes = {row[0] for row in rows}
    assert "stale_legacy" not in scopes
    assert "channels" in scopes
    # Idempotent: second call is a no-op (already done).
    second = await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    assert second == 0


async def test_scheduler_reset_scope_cleanup_lets_next_pass_run(tmp_path: Path) -> None:
    """``reset_scope_cleanup`` re-arms the one-shot so a subsequent
    cleanup pass actually runs."""
    db_path = tmp_path / "state.db"
    apply_migrations(db_path)

    from ingestion.daemon.candidates import InMemoryCandidateSource
    from ingestion.daemon.dead_letter import DeadLetterTracker
    from ingestion.daemon.scheduler import Scheduler, SchedulerDeps
    from ingestion.daemon.worker import WorkerPool

    pool = WorkerPool(global_limit=2)
    dead_letter = DeadLetterTracker(db_path, threshold=3, cooldown_seconds=60)

    class _Stub:
        async def list_candidates(self) -> list[Any]:
            return []

    deps = SchedulerDeps(
        config_store=open_store(db_path),
        candidate_source=InMemoryCandidateSource([]),
        worker_pool=pool,
        dead_letter=dead_letter,
        token_store=_Stub(),  # type: ignore[arg-type]
        content_sink=_Stub(),  # type: ignore[arg-type]
        sync_state=_Stub(),  # type: ignore[arg-type]
        sync_logger=_Stub(),  # type: ignore[arg-type]
        adapter_factory=lambda _p: None,  # type: ignore[arg-type,return-value]
        sqlite_path=db_path,
        scope_for_platform=lambda _p: frozenset({"default"}),
    )
    scheduler = Scheduler(deps)
    # First call sets the flag.
    await scheduler._run_scope_cleanup_once()  # noqa: SLF001
    assert scheduler._scope_cleanup_done is True  # noqa: SLF001
    # Reset re-arms.
    scheduler.reset_scope_cleanup()
    assert scheduler._scope_cleanup_done is False  # noqa: SLF001
