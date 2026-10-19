"""Runtime smoke tests: bootstrap wiring and graceful SIGTERM shutdown.

Two layers of test:
    1. In-process tests that set the ``asyncio.Event`` directly. Fast, no
       subprocess overhead, and exercise the real shutdown path the signal
       handler uses.
    2. One subprocess test that launches ``python -m ingestion.daemon`` with a
       real Fernet key + isolated SQLite path, waits for the ``daemon.started``
       log line, sends SIGTERM, and asserts the process exits cleanly within
       the Phase 0 exit criterion of 5 seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from ingestion.daemon import runtime
from ingestion.daemon.crypto import ENV_FERNET_KEY

# Passthrough env keys the subprocess needs for Django / uv / Postgres. Everything
# else is dropped so the test doesn't inherit a dev secret (e.g. a real Fernet
# key, a POSTGRES_PASSWORD set in .env) into the subprocess and then potentially
# print it via captured stdout on failure.
_INHERITED_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "DJANGO_SETTINGS_MODULE",
    "PYTHONPATH",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "VIRTUAL_ENV",
)


@pytest.fixture
def fernet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FERNET_KEY, Fernet.generate_key().decode())


async def test_shutdown_event_exits_tick_loop(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENDPOINT_DAEMON__SCHEDULER__TICK_SECONDS", "0.05")

    rt = runtime._bootstrap(sqlite_path=tmp_path / "state.db")
    try:
        task = asyncio.create_task(runtime._tick_loop(rt))
        await asyncio.sleep(0.1)
        assert not task.done()
        rt.shutdown.set()
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        rt.store.close()


async def test_shadow_mode_logs_warning_on_startup(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Booting with ``ops.shadow_mode=true`` must surface a WARNING on
    stdout so the first line of the log file tells the operator content
    writes are suppressed. Flipped-and-forgotten is the common footgun."""
    monkeypatch.setenv("ENDPOINT_DAEMON__SCHEDULER__TICK_SECONDS", "0.05")
    monkeypatch.setenv("ENDPOINT_DAEMON__OPS__SHADOW_MODE", "true")

    rt = runtime._bootstrap(sqlite_path=tmp_path / "state.db")
    try:
        assert rt.config.ops.shadow_mode is True

        # Capture the log record through the module logger. structlog's
        # BoundLogger proxies through stdlib logging under the daemon
        # configuration, so a stdlib handler sees the warning.
        captured: list[tuple[str, dict[str, object]]] = []

        class _RecordingLog:
            def info(self, event: str, **kwargs: object) -> None:
                captured.append((event, kwargs))

            def warning(self, event: str, **kwargs: object) -> None:
                captured.append((event, kwargs))

            def debug(self, *_a: object, **_kw: object) -> None:
                return

        monkeypatch.setattr(runtime, "get_logger", lambda _name=None: _RecordingLog())

        task = asyncio.create_task(runtime._tick_loop(rt))
        await asyncio.sleep(0.1)
        rt.shutdown.set()
        await asyncio.wait_for(task, timeout=1.0)

        warnings = [kwargs for event, kwargs in captured if event == "daemon.shadow_mode_active"]
        assert len(warnings) == 1
        assert "suppressed" in str(warnings[0].get("message", ""))
    finally:
        rt.store.close()


async def test_no_shadow_mode_warning_when_disabled(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default boot must NOT emit the shadow warning — otherwise operators
    desensitise to the signal and the footgun survives."""
    monkeypatch.setenv("ENDPOINT_DAEMON__SCHEDULER__TICK_SECONDS", "0.05")

    rt = runtime._bootstrap(sqlite_path=tmp_path / "state.db")
    try:
        assert rt.config.ops.shadow_mode is False

        captured: list[tuple[str, dict[str, object]]] = []

        class _RecordingLog:
            def info(self, event: str, **kwargs: object) -> None:
                captured.append((event, kwargs))

            def warning(self, event: str, **kwargs: object) -> None:
                captured.append((event, kwargs))

            def debug(self, *_a: object, **_kw: object) -> None:
                return

        monkeypatch.setattr(runtime, "get_logger", lambda _name=None: _RecordingLog())

        task = asyncio.create_task(runtime._tick_loop(rt))
        await asyncio.sleep(0.1)
        rt.shutdown.set()
        await asyncio.wait_for(task, timeout=1.0)

        events = [event for event, _ in captured]
        assert "daemon.shadow_mode_active" not in events
    finally:
        rt.store.close()


async def test_run_exits_when_shutdown_is_set(
    fernet_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENDPOINT_DAEMON__SCHEDULER__TICK_SECONDS", "0.05")
    # Stub out every heavy loop so the shutdown-path assertion is the only
    # thing under test. The scheduler would hit pytest-django's DB guard and
    # the API would refuse to start without ENDPOINT_ADMIN_PASSWORD.
    monkeypatch.setenv("ENDPOINT_DAEMON__SCHEDULER__ENABLED", "false")
    monkeypatch.setenv("ENDPOINT_DAEMON__API__ENABLED", "false")
    monkeypatch.setenv("ENDPOINT_DAEMON__TEAMS_REFRESH__ENABLED", "false")
    monkeypatch.setenv("ENDPOINT_DAEMON__OBSERVABILITY__RETENTION_DAYS", "0")
    monkeypatch.setenv("ENDPOINT_DAEMON__OBSERVABILITY__ALERT_TICK_SECONDS", "0")
    sqlite_path = tmp_path / "state.db"

    original_tick = runtime._tick_loop

    async def tick_then_shutdown(rt: runtime.Runtime) -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, rt.shutdown.set)
        await original_tick(rt)

    monkeypatch.setattr(runtime, "_tick_loop", tick_then_shutdown)

    await asyncio.wait_for(runtime.run(sqlite_path=sqlite_path), timeout=2.0)
    assert sqlite_path.exists()


def _subprocess_env(tmp_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _INHERITED_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env.update(
        {
            ENV_FERNET_KEY: Fernet.generate_key().decode(),
            "ENDPOINT_DAEMON__SQLITE_PATH": str(tmp_path / "state.db"),
            "ENDPOINT_DAEMON__SCHEDULER__TICK_SECONDS": "0.1",
            "ENDPOINT_DAEMON__LOG_LEVEL": "INFO",
            # Disable every heavy background task in this scaffolding test —
            # the goal is to prove SIGTERM semantics, not exercise Playwright
            # / Postgres / an actual uvicorn bind.
            "ENDPOINT_DAEMON__TEAMS_REFRESH__ENABLED": "false",
            "ENDPOINT_DAEMON__SCHEDULER__ENABLED": "false",
            "ENDPOINT_DAEMON__API__ENABLED": "false",
            # Even with api.enabled=False the app factory reads these at
            # import time; supply dummies so startup doesn't trip the guard.
            "ENDPOINT_ADMIN_PASSWORD": "test-pw",
            "ENDPOINT_SESSION_SECRET": "test-session-secret",
        }
    )
    return env


def _wait_for_event(
    proc: subprocess.Popen[str],
    event_name: str,
    *,
    deadline: float,
) -> None:
    """Block until the daemon emits ``event_name`` or die with a helpful error.

    Uses ``select`` with a short timeout so we check the deadline and the
    proc's exit status even if the daemon hangs without emitting output.
    """
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            remaining = proc.stdout.read()
            raise AssertionError(f"daemon exited early (rc={proc.returncode}): {remaining}")
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            # Stream closed; will be caught by proc.poll() next tick.
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == event_name:
            return
    raise AssertionError(f"daemon never emitted {event_name} within deadline")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM delivery model differs on Windows")
def test_subprocess_handles_sigterm_cleanly(tmp_path: Path) -> None:
    """End-to-end: spawn the daemon, send SIGTERM, expect clean exit under 5s.

    This is the only test that actually touches the OS signal path. The env
    is built from a minimal allowlist (PATH, HOME, Django, Postgres) plus
    our ENDPOINT_* overrides so an ambient dev secret never reaches the
    subprocess — otherwise an assertion failure would dump its stdout into
    pytest output with secrets in tow.
    """
    env = _subprocess_env(tmp_path)

    proc = subprocess.Popen(
        [sys.executable, "-m", "ingestion.daemon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_event(proc, "daemon.started", deadline=time.monotonic() + 10.0)

        proc.send_signal(signal.SIGTERM)
        exit_start = time.monotonic()
        rc = proc.wait(timeout=5.0)
        elapsed = time.monotonic() - exit_start
        assert rc == 0, f"daemon exited non-zero: {rc}"
        assert elapsed < 5.0, f"daemon took {elapsed:.2f}s to shut down"
    finally:
        if proc.poll() is None:
            proc.kill()
            # If SIGKILL can't reap within 5s, leak the handle rather than
            # hanging the suite; pytest will warn about the zombie.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5.0)
