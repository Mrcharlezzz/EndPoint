"""Slice 12 commit 2: SIGHUP config reload + helper details.

Companion to ``test_lifecycle_sighup.py``: this file digs into the
``_on_sighup`` helper's branches that the basline file leaves
under-covered:

* boot-only field changes log ``runtime.config_reload_partial`` with
  the right ``boot_only`` set;
* multiple schedulers all see ``reset_scope_cleanup`` called;
* a scheduler whose ``reset_scope_cleanup`` raises is logged but
  doesn't break the SIGHUP loop;
* the ``schedulers`` list is mutable on the frozen Runtime;
* ``_diff_config`` recursion bottoms out on scalar values cleanly;
* the SIGHUP handler is installable when ``signal.SIGHUP`` exists
  (POSIX-only test).

Module-level ``Runtime.schedulers`` field is asserted to exist as a
mutable list so ``_on_sighup`` can iterate it.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from ingestion.daemon import runtime as runtime_module
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.runtime import (
    Runtime,
    _diff_config,
    _flat_diff,
    _install_sighup_handler,
    _on_sighup,
)
from ingestion.daemon.store.local import apply_migrations, open_store

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENDPOINT_FERNET_KEY", Fernet.generate_key().decode())


def _make_runtime(tmp_path: Path) -> Runtime:
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


class _RecordingLog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.calls.append(("info", event, dict(kwargs)))

    def warning(self, event: str, **kwargs: object) -> None:
        self.calls.append(("warning", event, dict(kwargs)))

    def exception(self, event: str, **kwargs: object) -> None:
        self.calls.append(("exception", event, dict(kwargs)))

    def events(self) -> list[str]:
        return [event for _level, event, _ in self.calls]


# ---------------------------------------------------------------------------
# Runtime.schedulers field exists as a list-like
# ---------------------------------------------------------------------------


def test_runtime_schedulers_field_is_mutable_list(
    fernet_env: None,
    tmp_path: Path,
) -> None:
    """The frozen dataclass keeps ``schedulers`` as a list so SIGHUP
    can mutate it without falling over the frozen guard."""
    runtime = _make_runtime(tmp_path)
    try:
        assert runtime.schedulers == []
        runtime.schedulers.append(object())
        assert len(runtime.schedulers) == 1
    finally:
        runtime.store.close()


def test_runtime_schedulers_default_is_empty(
    fernet_env: None,
    tmp_path: Path,
) -> None:
    """A fresh Runtime has no schedulers attached."""
    runtime = _make_runtime(tmp_path)
    try:
        assert runtime.schedulers == []
    finally:
        runtime.store.close()


# ---------------------------------------------------------------------------
# _diff_config helper
# ---------------------------------------------------------------------------


def test_diff_config_returns_empty_on_identical_payload() -> None:
    """No changes => empty dict (truthy-empty)."""
    a: dict[str, Any] = {"x": 1, "n": {"y": 2}}
    b: dict[str, Any] = {"x": 1, "n": {"y": 2}}
    assert _diff_config(a, b) == {}


def test_diff_config_with_deeply_nested_path_to_leaf() -> None:
    """Nested 3+ levels: dotted path includes every intermediate."""
    a: dict[str, Any] = {"a": {"b": {"c": 1}}}
    b: dict[str, Any] = {"a": {"b": {"c": 2}}}
    diff = _diff_config(a, b)
    assert "a.b.c" in diff
    assert diff["a.b.c"] == (1, 2)


def test_diff_config_added_key_only_in_new() -> None:
    """A key in new but not in old shows up with old=None."""
    a: dict[str, Any] = {}
    b: dict[str, Any] = {"x": 1}
    diff = _diff_config(a, b)
    assert "x" in diff
    assert diff["x"] == (None, 1)


def test_diff_config_removed_key_only_in_old() -> None:
    a: dict[str, Any] = {"x": 1}
    b: dict[str, Any] = {}
    diff = _diff_config(a, b)
    assert "x" in diff
    assert diff["x"] == (1, None)


def test_flat_diff_scalar_at_root_no_recursion() -> None:
    """Scalar inputs at root don't recurse — they record the diff."""
    out: dict[str, tuple[object, object]] = {}
    _flat_diff(1, 2, "root", out)
    assert out == {"root": (1, 2)}


def test_flat_diff_dict_vs_scalar_treats_as_diff() -> None:
    """A dict on one side and a scalar on the other is a diff at the
    current prefix — no asymmetric recursion attempt."""
    out: dict[str, tuple[object, object]] = {}
    _flat_diff({"a": 1}, "scalar", "root", out)
    assert out == {"root": ({"a": 1}, "scalar")}


def test_flat_diff_equal_scalars_skipped() -> None:
    out: dict[str, tuple[object, object]] = {}
    _flat_diff(7, 7, "x", out)
    assert out == {}


# ---------------------------------------------------------------------------
# _on_sighup — branches
# ---------------------------------------------------------------------------


def test_sighup_boot_only_change_logs_partial_reload(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change to log_level (a boot-only field per impl) triggers
    runtime.config_reload_partial with boot_only={'log_level'} and
    a message about restart."""
    runtime = _make_runtime(tmp_path)
    runtime.store.set_overlay("log_level", "DEBUG")
    log = _RecordingLog()
    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: log)
    try:
        _on_sighup(runtime)
        events = log.events()
        assert "runtime.config_reload_partial" in events
        # The partial-reload log should carry boot_only + message kwargs.
        partial = next(c for c in log.calls if c[1] == "runtime.config_reload_partial")
        kwargs = partial[2]
        assert "log_level" in kwargs.get("boot_only", [])
        assert "restart" in str(kwargs.get("message", ""))
    finally:
        runtime.store.close()


def test_sighup_non_boot_change_does_not_log_partial(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduler.global_paused flip is live-tunable; partial-reload
    must NOT fire."""
    runtime = _make_runtime(tmp_path)
    runtime.store.set_overlay("scheduler.global_paused", True)
    log = _RecordingLog()
    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: log)
    try:
        _on_sighup(runtime)
        events = log.events()
        assert "runtime.config_reload_partial" not in events
        assert "runtime.config_reloaded" in events
    finally:
        runtime.store.close()


def test_sighup_resets_multiple_schedulers(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All registered schedulers see ``reset_scope_cleanup`` called.
    The runtime is supposed to support multiple schedulers via a
    list — we assert each gets the call."""
    runtime = _make_runtime(tmp_path)
    counts = {"a": 0, "b": 0}

    class _Sched:
        def __init__(self, key: str) -> None:
            self._key = key

        def reset_scope_cleanup(self) -> None:
            counts[self._key] += 1

    runtime.schedulers.append(_Sched("a"))
    runtime.schedulers.append(_Sched("b"))
    log = _RecordingLog()
    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: log)
    try:
        _on_sighup(runtime)
        assert counts == {"a": 1, "b": 1}
    finally:
        runtime.store.close()


def test_sighup_scheduler_without_reset_method_skipped_silently(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduler-like object without ``reset_scope_cleanup`` is
    skipped (the impl uses ``getattr`` with a None default)."""
    runtime = _make_runtime(tmp_path)

    class _Bare:
        pass

    runtime.schedulers.append(_Bare())
    log = _RecordingLog()
    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: log)
    try:
        _on_sighup(runtime)  # must not raise
        # SIGHUP log still emitted.
        assert "runtime.config_reloaded" in log.events()
    finally:
        runtime.store.close()


def test_sighup_scheduler_reset_raises_does_not_break_sighup(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduler whose ``reset_scope_cleanup`` raises gets logged
    via ``log.exception`` but the SIGHUP handler still completes."""
    runtime = _make_runtime(tmp_path)

    class _Raises:
        def reset_scope_cleanup(self) -> None:
            raise RuntimeError("scheduler-side boom")

    runtime.schedulers.append(_Raises())
    log = _RecordingLog()
    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: log)
    try:
        _on_sighup(runtime)  # must not propagate
        assert "runtime.scheduler_reset_errored" in log.events()
        # Reload still landed.
        assert "runtime.config_reloaded" in log.events()
    finally:
        runtime.store.close()


def test_sighup_with_no_diff_still_resets_schedulers(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGHUP without overlay change still re-arms schedulers — operators
    can SIGHUP just to trigger a cleanup pass."""
    runtime = _make_runtime(tmp_path)
    reset_count = 0

    class _Sched:
        def reset_scope_cleanup(self) -> None:
            nonlocal reset_count
            reset_count += 1

    runtime.schedulers.append(_Sched())
    log = _RecordingLog()
    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: log)
    try:
        _on_sighup(runtime)
        assert reset_count == 1
        # changes=0 in the reload log.
        reload_call = next(c for c in log.calls if c[1] == "runtime.config_reloaded")
        assert reload_call[2].get("changes") == 0
    finally:
        runtime.store.close()


def test_sighup_failed_load_does_not_reset_scheduler(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``load_config`` raises, the SIGHUP handler bails BEFORE
    resetting schedulers — the prior config is the safer state."""
    runtime = _make_runtime(tmp_path)
    reset_count = 0

    class _Sched:
        def reset_scope_cleanup(self) -> None:
            nonlocal reset_count
            reset_count += 1

    runtime.schedulers.append(_Sched())
    log = _RecordingLog()
    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: log)

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("overlay parse failed")

    monkeypatch.setattr(runtime_module, "load_config", boom)
    try:
        _on_sighup(runtime)
        assert "runtime.config_reload_failed" in log.events()
        assert reset_count == 0
    finally:
        runtime.store.close()


def test_sighup_swap_uses_object_setattr_for_frozen_dataclass(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frozen Runtime CAN have its config swapped via SIGHUP.
    Confirms the impl uses ``object.__setattr__`` rather than a normal
    assignment (which would raise FrozenInstanceError)."""
    runtime = _make_runtime(tmp_path)
    runtime.store.set_overlay("scheduler.global_paused", True)
    log = _RecordingLog()
    monkeypatch.setattr(runtime_module, "get_logger", lambda _name=None: log)
    try:
        before = runtime.config
        _on_sighup(runtime)
        # The config object reference changed (new snapshot).
        assert runtime.config is not before
        # And the new snapshot has the overlay applied.
        assert runtime.config.scheduler.global_paused is True
    finally:
        runtime.store.close()


# ---------------------------------------------------------------------------
# _install_sighup_handler — POSIX-only path
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="SIGHUP is POSIX-only")
def test_install_sighup_handler_registers_callback(
    fernet_env: None,
    tmp_path: Path,
) -> None:
    """The handler installer wires _on_sighup to SIGHUP. Run inside
    a fresh asyncio loop so the loop has signal-handler support."""
    runtime = _make_runtime(tmp_path)
    try:

        async def _run() -> None:
            loop = asyncio.get_running_loop()
            _install_sighup_handler(loop, runtime)
            # Verify a handler is registered for SIGHUP. We cannot
            # call get_signal_handler on an asyncio loop directly, but
            # add_signal_handler followed by remove_signal_handler is
            # a permissive way to confirm one is set up — remove
            # returns True when a handler was registered.
            sighup = getattr(signal, "SIGHUP")  # noqa: B009
            assert loop.remove_signal_handler(sighup) is True

        asyncio.run(_run())
    finally:
        runtime.store.close()


def test_install_sighup_handler_no_op_when_sighup_missing(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``signal.SIGHUP`` (Windows), the helper returns silently."""
    runtime = _make_runtime(tmp_path)
    try:

        async def _run() -> None:
            loop = asyncio.get_running_loop()
            # Force the SIGHUP-missing branch by hiding the attribute.
            monkeypatch.delattr(runtime_module.signal, "SIGHUP", raising=False)
            _install_sighup_handler(loop, runtime)  # must not raise

        asyncio.run(_run())
    finally:
        runtime.store.close()
