"""Subprocess wrapper for the ingestion daemon, for use in e2e tests.

The daemon is spawned as ``python -m ingestion.daemon`` and its stdout is
tailed for the uvicorn banner to discover which port uvicorn bound to.
We bind uvicorn on port 0 (OS-assigned) so parallel test runs don't collide.

This wrapper is intentionally thin: the tests own the stub servers and the
Postgres schema. The wrapper only knows how to start, read-port-from-stdout,
healthcheck, and stop the process cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from tests.e2e.config import ADMIN

if TYPE_CHECKING:
    from pytest_django.plugin import _DatabaseBlocker

# Match the uvicorn TCP banner. Accepts IPv4 literal, hostname, or [IPv6] forms.
# We currently force host=127.0.0.1 in _build_env, but keep this permissive in
# case the daemon's config is overridden via env.
_UVICORN_PORT_RE = re.compile(r"Uvicorn running on https?://(?:[^:\s\[]+|\[[^\]]+\]):(\d+)")


def _run_migrate_sync(django_db_blocker: Any) -> None:
    import django
    import django.conf

    if not django.conf.settings.configured:
        django.setup()
    from django.core.management import call_command

    # pytest-django installs a DB access blocker at collection time; e2e tests
    # intentionally bypass it because they need a real committed schema that
    # the daemon subprocess can see.
    with django_db_blocker.unblock():
        call_command("migrate", verbosity=0, interactive=False)


async def apply_django_migrations(django_db_blocker: _DatabaseBlocker) -> None:
    """Apply db_core migrations against the POSTGRES_* env DB.

    Idempotent — safe to call every session. Runs in-process via Django's
    management commands; Django's ORM refuses to run inside an asyncio loop,
    so we hop to a worker thread for the duration of the migrate command.
    """
    await asyncio.get_running_loop().run_in_executor(None, _run_migrate_sync, django_db_blocker)


class DaemonProcess:
    """Spawn + stop the ingestion daemon as a child process.

    Usage:
        daemon = DaemonProcess(
            teams_csa_url=..., teams_graph_url=...,
            moodle_url=..., telegram_url=...,
        )
        await daemon.start()
        # daemon.base_url is ready; hit /api/* with httpx
        await daemon.stop()
    """

    def __init__(
        self,
        *,
        teams_csa_url: str,
        teams_graph_url: str,
        moodle_url: str,
        telegram_url: str,
        boot_timeout_seconds: float = 30.0,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._teams_csa_url = teams_csa_url
        self._teams_graph_url = teams_graph_url
        self._moodle_url = moodle_url
        self._telegram_url = telegram_url
        self._boot_timeout = boot_timeout_seconds
        self._extra_env = dict(extra_env or {})

        self._proc: asyncio.subprocess.Process | None = None
        self._port: int | None = None
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._stdout_lines: deque[str] = deque(maxlen=500)

    @property
    def base_url(self) -> str:
        if self._port is None:
            raise RuntimeError("DaemonProcess.start() has not completed")
        return f"http://127.0.0.1:{self._port}"

    @property
    def ws_base_url(self) -> str:
        if self._port is None:
            raise RuntimeError("DaemonProcess.start() has not completed")
        return f"ws://127.0.0.1:{self._port}"

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("DaemonProcess.start() has not completed")
        return self._port

    def _build_env(self) -> dict[str, str]:
        # Isolate the daemon's SQLite state per-test run.
        assert self._tmpdir is not None
        sqlite_path = str(Path(self._tmpdir.name) / "state.db")

        env: dict[str, str] = {
            **os.environ,
            # Adapter base URLs point at the stub servers.
            "ENDPOINT_TEAMS__CSA_BASE_URL": self._teams_csa_url,
            "ENDPOINT_TEAMS__GRAPH_BASE_URL": self._teams_graph_url,
            "ENDPOINT_MOODLE__BASE_URL": self._moodle_url,
            "ENDPOINT_TELEGRAM__API_BASE_URL": self._telegram_url,
            # Per-process daemon state.
            "ENDPOINT_DAEMON__SQLITE_PATH": sqlite_path,
            # Bind uvicorn on an OS-assigned port; we scrape it from stdout.
            "ENDPOINT_DAEMON__API__HOST": "127.0.0.1",
            "ENDPOINT_DAEMON__API__PORT": "0",
            # Tests don't terminate TLS in front of uvicorn.
            "ENDPOINT_DAEMON__API__HTTPS_TERMINATED_UPSTREAM": "false",
            # Keep the daemon login credential aligned with the client-side
            # e2e fixture instead of whichever password a developer has in
            # their local .env.
            "ENDPOINT_ADMIN_PASSWORD": ADMIN.password,
            # D5: refresh loop disabled in C1 so Chromium never launches.
            # See docs/ingestion/daemon/E2E_TEST_DESIGN.md.
            "ENDPOINT_DAEMON__TEAMS_REFRESH__ENABLED": "false",
            "ENDPOINT_DAEMON__TEAMS_REFRESH__POOL_SIZE": "0",
        }
        env.update(self._extra_env)
        return env

    async def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("DaemonProcess already started")

        self._tmpdir = tempfile.TemporaryDirectory(prefix="endpoint-daemon-e2e-")
        env = self._build_env()

        # ``-u`` forces Python unbuffered stdout so each line reaches us as
        # soon as the daemon emits it — critical for the banner scrape.
        # ``start_new_session=True`` puts the daemon in its own process group
        # so we can signal the whole tree via os.killpg on force-cleanup.
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-m",
            "ingestion.daemon",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )

        deadline = time.monotonic() + self._boot_timeout
        try:
            self._port = await self._scrape_port_from_stdout(deadline)
            await self._wait_for_http_ready(deadline)
        except BaseException:
            await self._force_cleanup()
            raise

    async def _scrape_port_from_stdout(self, deadline: float) -> int:
        """Read daemon stdout until the uvicorn banner appears; return its port."""
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                tail = "\n".join(self._stdout_lines)
                raise RuntimeError(
                    f"daemon did not emit uvicorn banner within boot timeout:\n{tail}"
                )
            try:
                line_bytes = await asyncio.wait_for(stdout.readline(), timeout=remaining)
            except TimeoutError:
                tail = "\n".join(self._stdout_lines)
                raise RuntimeError(
                    f"daemon did not emit uvicorn banner within boot timeout:\n{tail}"
                ) from None
            if not line_bytes:
                # pipe closed — process died before binding
                returncode = self._proc.returncode
                tail = "\n".join(list(self._stdout_lines)[-40:])
                raise RuntimeError(
                    f"daemon exited before binding uvicorn (rc={returncode}):\n{tail}"
                )
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            self._stdout_lines.append(line)
            match = _UVICORN_PORT_RE.search(line)
            if match:
                return int(match.group(1))

    async def _wait_for_http_ready(self, deadline: float) -> None:
        """Poll the daemon until it answers HTTP, even with an auth-gated route."""
        last_exc: BaseException | None = None
        async with httpx.AsyncClient(base_url=self.base_url, timeout=2.0) as client:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    # /api/settings/about returns 200 or 401 once the app is up.
                    resp = await client.get("/api/settings/about")
                except (httpx.HTTPError, OSError) as exc:
                    last_exc = exc
                    await asyncio.sleep(0.1)
                    continue
                if resp.status_code < 500:
                    return
                await asyncio.sleep(0.1)
        raise RuntimeError(
            f"daemon bound port {self._port} but never returned a non-5xx: {last_exc!r}"
        )

    async def stop(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
        await self._force_cleanup()

    async def _force_cleanup(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self._proc.pid, signal.SIGKILL)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except ProcessLookupError:
                pass
        self._proc = None
        self._port = None
        if self._tmpdir is not None:
            with contextlib.suppress(OSError):
                self._tmpdir.cleanup()
            self._tmpdir = None
