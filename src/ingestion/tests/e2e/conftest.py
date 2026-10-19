"""Session-wide env + fixtures for e2e tests.

Boots the daemon subprocess and all stub servers exactly once per pytest
session, then hands out a fresh ``httpx.AsyncClient`` per test. Stub state
is cleared between tests so recorded requests / queued responses don't
bleed across cases.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import psycopg
import pytest
import pytest_asyncio


def _load_env_file(path: Path) -> None:
    """Minimal .env loader — setdefault each KEY=VALUE line."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
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


# The .env at repo root holds POSTGRES_*, PG_*, ENDPOINT_FERNET_KEY,
# ENDPOINT_SESSION_SECRET. Loaded once at collection time, before any
# test import triggers Django settings resolution.
_repo_root = Path(__file__).resolve().parents[4]
_load_env_file(_repo_root / ".env")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


# Imports below depend on the env being loaded above.
from tests.e2e.daemon_proc import DaemonProcess, apply_django_migrations  # noqa: E402
from tests.e2e.db import truncate_all_except  # noqa: E402
from tests.e2e.stubs.base import StubServer  # noqa: E402
from tests.e2e.stubs.moodle import MoodleStub  # noqa: E402
from tests.e2e.stubs.teams_csa import TeamsCsaStub  # noqa: E402
from tests.e2e.stubs.teams_graph import TeamsGraphStub  # noqa: E402
from tests.e2e.stubs.telegram import TelegramStub  # noqa: E402

if TYPE_CHECKING:
    from pytest_django.plugin import _DatabaseBlocker


def pytest_configure(config: pytest.Config) -> None:
    """Fail loudly if pytest-xdist is active for the e2e suite.

    The shared Postgres + per-test TRUNCATE design assumes serial execution;
    parallel workers would clobber each other. This guard is the single place
    to remove when per-worker DB isolation lands.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        # Only fail if the user is actually collecting e2e tests.
        marker_expr = config.getoption("-m", "") or ""
        if "e2e" in marker_expr:
            pytest.exit(
                "tests/e2e/ does not support pytest-xdist — run with -p no:xdist",
                returncode=2,
            )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-apply @pytest.mark.asyncio(loop_scope="session") to every async test
    under tests/e2e/. Prevents the "loop is closed" footgun when a test forgets
    the decorator and runs on a function-scoped loop that mismatches the
    session-scoped daemon / stub fixtures.

    In ``asyncio_mode=auto`` pytest-asyncio auto-applies an ``asyncio`` marker
    with ``loop_scope=<asyncio_default_test_loop_scope>`` (function by default).
    We want session loop scope under ``tests/e2e/`` without forcing every test
    author to spell it out, so we prepend our own session-scoped marker via
    ``append=False`` — ``get_closest_marker`` then returns the session-scoped
    one and pytest-asyncio honours it over the auto-applied function-scoped
    marker. Tests that explicitly set ``loop_scope="session"`` themselves are
    left alone.
    """
    for item in items:
        # Only touch tests under this conftest's dir.
        if "tests/e2e/" not in str(item.path):
            continue
        # Only async test functions — sync tests don't need the marker.
        if not hasattr(item, "obj") or not inspect.iscoroutinefunction(item.obj):
            continue
        # Preserve explicit session-scoped markers — nothing to add.
        existing = item.get_closest_marker("asyncio")
        if existing is not None and existing.kwargs.get("loop_scope") == "session":
            continue
        item.add_marker(pytest.mark.asyncio(loop_scope="session"), append=False)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def stub_servers() -> AsyncIterator[dict[str, StubServer]]:
    """Boot all 4 platform stubs once per session.

    Yields a dict keyed by platform name. Stops stubs in reverse start
    order on teardown.
    """
    teams_csa = TeamsCsaStub()
    teams_graph = TeamsGraphStub()
    moodle = MoodleStub()
    telegram = TelegramStub()
    started: list[StubServer] = []
    try:
        for stub in (teams_csa, teams_graph, moodle, telegram):
            await stub.start()
            started.append(stub)
        yield {
            "teams_csa": teams_csa,
            "teams_graph": teams_graph,
            "moodle": moodle,
            "telegram": telegram,
        }
    finally:
        for stub in reversed(started):
            await stub.stop()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def daemon(
    stub_servers: dict[str, StubServer],
    django_db_blocker: _DatabaseBlocker,
) -> AsyncIterator[DaemonProcess]:
    """Boot the ingestion daemon subprocess once per session."""
    await apply_django_migrations(django_db_blocker)

    proc = DaemonProcess(
        teams_csa_url=stub_servers["teams_csa"].base_url,
        teams_graph_url=stub_servers["teams_graph"].base_url,
        moodle_url=stub_servers["moodle"].base_url,
        telegram_url=stub_servers["telegram"].base_url,
    )
    await proc.start()
    try:
        yield proc
    finally:
        await proc.stop()


@pytest_asyncio.fixture(loop_scope="session")
async def http_client(daemon: DaemonProcess) -> AsyncIterator[httpx.AsyncClient]:
    """Fresh httpx AsyncClient bound to the daemon's base URL per test."""
    async with httpx.AsyncClient(base_url=daemon.base_url, timeout=5.0) as client:
        yield client


@pytest_asyncio.fixture(loop_scope="session")
async def admin_session(http_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Log in with the admin password; return a client carrying the cookie.

    Uses the ADMIN credentials from ``tests.e2e.config``. The underlying
    ``http_client`` is function-scoped, so each test gets a fresh authed
    client without cookie bleed between tests.
    """
    from tests.e2e.config import ADMIN

    resp = await http_client.post(
        "/api/auth/login",
        json={"email": ADMIN.email, "password": ADMIN.password},
    )
    assert resp.status_code == 200, f"login failed: {resp.status_code} {resp.text}"
    return http_client


@pytest.fixture(autouse=True)
def _reset_stub_state(request: pytest.FixtureRequest) -> None:
    """Clear each stub's queued responses + recorded requests before every test.

    Only activates for tests that actually use the session stubs — otherwise
    we'd force every non-e2e test to stand up the stub servers.
    """
    if "stub_servers" not in request.fixturenames and "daemon" not in request.fixturenames:
        return
    stubs: dict[str, StubServer] = request.getfixturevalue("stub_servers")
    for stub in stubs.values():
        stub.reset()


def _pg_conn_kwargs() -> dict[str, str | int]:
    """Build psycopg connection kwargs from POSTGRES_* env vars.

    These are set by ``_load_env_file`` at module import time, and the
    daemon subprocess reads the same vars through Django's settings.
    """
    return {
        "dbname": os.environ["POSTGRES_DB"],
        "user": os.environ["POSTGRES_USER"],
        "password": os.environ["POSTGRES_PASSWORD"],
        "host": os.environ.get("PG_HOST", "localhost"),
        "port": int(os.environ.get("PG_PORT", "5432")),
    }


@pytest.fixture(autouse=True)
def _clean_postgres(request: pytest.FixtureRequest) -> None:
    """Truncate mutable Postgres tables before each e2e test.

    Gated on the @pytest.mark.e2e marker (applied by pytest_collection_modifyitems
    in this conftest and explicitly on test functions). This is more durable
    than sniffing request.fixturenames — any future e2e fixture is automatically
    covered once the test carries the marker.
    """
    if not request.node.get_closest_marker("e2e"):
        return
    with psycopg.connect(**_pg_conn_kwargs()) as conn:
        truncate_all_except(conn)
