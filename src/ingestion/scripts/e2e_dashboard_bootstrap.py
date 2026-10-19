"""Boot a real ingestion daemon + stub platforms for dashboard e2e.

Two consumers:
- Playwright ``globalSetup`` — spawns this script, parses ``DAEMON_URL=``
  from stdout, runs tests against the real daemon, then ``SIGTERM``s.
- ``pnpm dev:full`` — dev-loop convenience; keeps the daemon alive
  alongside Vite so dashboard work doesn't require a separate terminal.

The heavy lifting (stub servers, daemon subprocess spawn, Postgres
truncate) is done by the existing daemon-API e2e primitives at
``src/ingestion/tests/e2e/``. This script only orchestrates them and
layers a dashboard-shaped seed on top.

See: docs/plans/2026-04-21-dashboard-e2e-real-daemon-design.md
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

# ``tests/`` is a pytest rootdir package, not an installed one — it lives
# at ``<ingestion-root>/tests`` but ``packages = ["ingestion"]`` in
# pyproject.toml means ``uv run`` doesn't put the ingestion root on
# sys.path when invoking a script directly. Prepend it here so
# ``from tests.e2e.*`` resolves without a pytest wrapper.
_INGESTION_ROOT = Path(__file__).resolve().parents[1]
if str(_INGESTION_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGESTION_ROOT))


def _load_env_file(path: Path) -> None:
    """Minimal .env loader — setdefault each KEY=VALUE line.

    Mirrors ``tests/e2e/conftest.py::_load_env_file`` so this script works
    standalone (no pytest) without drifting from the fixture semantics.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)


# Repo root holds POSTGRES_*, PG_*, ENDPOINT_FERNET_KEY, ENDPOINT_SESSION_SECRET.
# Load before importing anything that transitively triggers Django settings
# resolution or daemon config.
_REPO_ROOT = _INGESTION_ROOT.parent.parent
_load_env_file(_REPO_ROOT / ".env")
# Django is not needed by this script's own process, but the task-#3 seed
# step ``exec``s ``scripts/seed_test_errors.py`` which calls ``core.bootstrap``;
# setting the module here means we inherit a consistent default into that child.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Imports below depend on env + sys.path set above.
import psycopg  # noqa: E402
from tests.e2e.config import ADMIN  # noqa: E402
from tests.e2e.daemon_proc import DaemonProcess  # noqa: E402
from tests.e2e.db import truncate_all_except  # noqa: E402
from tests.e2e.stubs.moodle import MoodleStub  # noqa: E402
from tests.e2e.stubs.teams_csa import TeamsCsaStub  # noqa: E402
from tests.e2e.stubs.teams_graph import TeamsGraphStub  # noqa: E402
from tests.e2e.stubs.telegram import TelegramStub  # noqa: E402

if TYPE_CHECKING:
    from tests.e2e.stubs.base import StubServer

# Admin password: supply a strong default for standalone runs where .env
# doesn't carry one. ``setdefault`` so any explicit value wins. Session
# secret is intentionally not defaulted — missing = fail loud at daemon
# boot, which is the correct signal.
os.environ.setdefault("ENDPOINT_ADMIN_PASSWORD", ADMIN.password)


_DEFAULT_SEED = Path("scripts/seed_test_errors.py")


def _pg_conn_kwargs() -> dict[str, Any]:
    """Build psycopg connection kwargs from POSTGRES_* env.

    Mirrors ``tests/e2e/conftest.py::_pg_conn_kwargs`` so bootstrap and the
    pytest harness truncate the same DB on the same wiring.
    """
    return {
        "dbname": os.environ["POSTGRES_DB"],
        "user": os.environ["POSTGRES_USER"],
        "password": os.environ["POSTGRES_PASSWORD"],
        "host": os.environ.get("PG_HOST", "localhost"),
        "port": int(os.environ.get("PG_PORT", "5432")),
    }


def _open_daemon_log() -> TextIO:
    """Open ``<repo>/playwright-report/daemon.log`` in append mode.

    Playwright's default artifact upload grabs the whole ``playwright-report``
    directory on failure, so putting the log there means CI captures it
    without extra wiring.
    """
    log_path = _REPO_ROOT / "playwright-report" / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path.open("a", encoding="utf-8", buffering=1)  # line-buffered


def _configure_stub_logging(log_handle: TextIO) -> logging.Handler:
    """Route the aiohttp stub access logs + other in-process loggers to the file.

    Bootstrap's own stdout is reserved for the ``DAEMON_URL=…`` / ``READY``
    line protocol consumed by Playwright globalSetup — keep library logs off it.
    """
    handler = logging.StreamHandler(log_handle)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return handler


async def _drain_daemon_stdout(daemon: DaemonProcess, log_handle: TextIO) -> None:
    """Forward the daemon subprocess stdout to ``daemon.log`` until it closes.

    Must run — uvicorn + the daemon emit a continuous log stream after the
    banner is scraped; without a reader the 64K pipe buffer fills and the
    daemon blocks on its next write.
    """
    proc = daemon._proc  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    if proc is None or proc.stdout is None:
        return
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            log_handle.write("[daemon] ")
            log_handle.write(line.decode("utf-8", errors="replace"))
    except asyncio.CancelledError:
        # Cancellation on teardown is the expected exit path.
        raise


def _install_signal_shutdown(shutdown: asyncio.Event) -> None:
    """Wire SIGTERM + SIGINT to set the shutdown event.

    Using ``loop.add_signal_handler`` instead of a plain ``signal.signal``
    means the handler runs inside the asyncio loop — ``event.set()`` is
    safe, and the pending ``event.wait()`` resumes cleanly so the existing
    try/finally drives teardown.
    """
    loop = asyncio.get_running_loop()
    # SIGHUP handled too so that parent-shell exit (e.g. ``uv run`` wrapper
    # dying, a tmux pane closing) triggers cleanup instead of leaving the
    # daemon subprocess listening on its port.
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown.set)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Boot the ingestion daemon + platform stubs against a truncated "
            "Postgres + seeded SQLite, then block until SIGTERM."
        ),
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help=(
            "Block until SIGTERM/SIGINT instead of exiting after READY. "
            "Playwright globalSetup and `pnpm dev:full` always pass this; "
            "omit only for short-lived orchestrators."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help=(
            "Daemon port. Default 0 = OS-assigned (used by Playwright, "
            "avoids collisions). Pass 8080 for dev-loop use."
        ),
    )
    parser.add_argument(
        "--seed",
        type=Path,
        default=_DEFAULT_SEED,
        help=f"Seed script to run after truncate. Default: {_DEFAULT_SEED}.",
    )
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="Skip the seed step (for specs that need an empty state).",
    )
    return parser


async def _main_async(args: argparse.Namespace) -> None:
    """Boot stubs + daemon, truncate Postgres, seed, print addresses + READY.

    Teardown runs in reverse start order under a single try/finally so every
    exit path (success, exception, SIGTERM/SIGINT) reclaims resources.
    """
    shutdown = asyncio.Event()
    _install_signal_shutdown(shutdown)

    log_handle = _open_daemon_log()
    log_handler = _configure_stub_logging(log_handle)

    stubs: list[StubServer] = [
        TeamsCsaStub(),
        TeamsGraphStub(),
        MoodleStub(),
        TelegramStub(),
    ]
    started: list[StubServer] = []
    daemon: DaemonProcess | None = None
    stdout_drain: asyncio.Task[None] | None = None
    # Pre-allocate the SQLite path so the daemon subprocess and the seed
    # subprocess write to the same file. DaemonProcess would otherwise
    # create a throwaway tempdir; ``extra_env`` below overrides it.
    sqlite_tmpdir = tempfile.TemporaryDirectory(prefix="endpoint-dashboard-e2e-")
    try:
        sqlite_path = Path(sqlite_tmpdir.name) / "state.db"

        for stub in stubs:
            await stub.start()
            started.append(stub)

        # When --port is non-zero, pin uvicorn. Default 0 lets the OS pick
        # (matching the pytest fixture) and DaemonProcess scrapes the banner.
        extra_env: dict[str, str] = {
            "ENDPOINT_DAEMON__SQLITE_PATH": str(sqlite_path),
        }
        if args.port != 0:
            extra_env["ENDPOINT_DAEMON__API__HOST"] = "127.0.0.1"
            extra_env["ENDPOINT_DAEMON__API__PORT"] = str(args.port)

        daemon = DaemonProcess(
            teams_csa_url=stubs[0].base_url,
            teams_graph_url=stubs[1].base_url,
            moodle_url=stubs[2].base_url,
            telegram_url=stubs[3].base_url,
            extra_env=extra_env,
        )
        await daemon.start()
        stdout_drain = asyncio.create_task(_drain_daemon_stdout(daemon, log_handle))

        # Known-empty slate before seed. Matches the pytest harness's
        # per-test discipline (tests/e2e/conftest.py::_clean_postgres) but
        # at session granularity since Playwright tests share the DB.
        with psycopg.connect(**_pg_conn_kwargs()) as conn:
            truncate_all_except(conn)

        if not args.skip_seed:
            seed_path = args.seed
            if not seed_path.is_absolute():
                seed_path = _INGESTION_ROOT / seed_path
            seed_env = {**os.environ, "ENDPOINT_DAEMON__SQLITE_PATH": str(sqlite_path)}
            result = subprocess.run(
                [sys.executable, str(seed_path)],
                env=seed_env,
                cwd=str(_INGESTION_ROOT),
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"seed script {seed_path} exited with rc={result.returncode}")

        # Addresses printed AFTER seed so the stdout stream Playwright
        # globalSetup consumes has the 5 lines contiguous before READY.
        print(f"DAEMON_URL={daemon.base_url}", flush=True)
        print(f"STUB_TEAMS_CSA={stubs[0].base_url}", flush=True)
        print(f"STUB_TEAMS_GRAPH={stubs[1].base_url}", flush=True)
        print(f"STUB_MOODLE={stubs[2].base_url}", flush=True)
        print(f"STUB_TELEGRAM={stubs[3].base_url}", flush=True)
        print("READY", flush=True)

        if args.keep_alive:
            # Block until SIGTERM/SIGINT flips the event.
            await shutdown.wait()
    finally:
        if stdout_drain is not None:
            stdout_drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stdout_drain
        if daemon is not None:
            await daemon.stop()
        for stub in reversed(started):
            await stub.stop()
        sqlite_tmpdir.cleanup()
        logging.getLogger().removeHandler(log_handler)
        log_handle.close()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        # Expected during manual verification / Ctrl-C in --keep-alive mode;
        # asyncio.run already drove the finally block for teardown.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
