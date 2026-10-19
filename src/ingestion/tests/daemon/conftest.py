"""Daemon-integration test fixtures. Sets up Django via db_core settings.

Tests in this directory that hit Postgres must carry ``@pytest.mark.django_db``.
Pure-SQLite tests (cursors, sync history) do not need the marker.

If Postgres isn't reachable (common in CI preflight or local-without-docker),
every ``django_db``-marked test is auto-skipped with a clear reason. The
SQLite-only tests stay green. This mirrors ``db_core/tests/conftest.py``.
"""

from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("POSTGRES_DB", "shared_db")
os.environ.setdefault("POSTGRES_USER", "postgres")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres")
os.environ.setdefault("PG_HOST", "localhost")
os.environ.setdefault("PG_PORT", "5432")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("ENDPOINT_FERNET_KEY", Fernet.generate_key().decode())


def _postgres_available() -> bool:
    try:
        import psycopg2

        conn = psycopg2.connect(
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            host=os.environ["PG_HOST"],
            port=int(os.environ["PG_PORT"]),
            connect_timeout=2,
        )
        conn.close()
    except Exception as exc:
        print(f"\n[daemon-conftest] Postgres unavailable: {exc}")
        return False
    return True


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if _postgres_available():
        return
    skip = pytest.mark.skip(reason="Postgres is not running; skipping DB-backed daemon tests")
    for item in items:
        if item.get_closest_marker("django_db"):
            item.add_marker(skip)


@pytest.fixture
def fernet_key() -> str:
    """Return the active Fernet key; regenerate for strict isolation if needed."""
    return os.environ["ENDPOINT_FERNET_KEY"]
