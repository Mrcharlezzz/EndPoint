import os
import pytest


def _postgres_available() -> bool:
    try:
        import psycopg2

        conn = psycopg2.connect(
            dbname=os.environ.get("POSTGRES_DB", "shared_db"),
            user=os.environ.get("POSTGRES_USER", "postgres"),
            password=os.environ.get("POSTGRES_PASSWORD", "postgres"),
            host=os.environ.get("PG_HOST", "localhost"),
            port=int(os.environ.get("PG_PORT", "5432")),
            connect_timeout=2,
        )
        conn.close()
        return True
    except Exception as e:
        print(f"\n[conftest] Postgres unavailable: {e}")
        return False


def pytest_collection_modifyitems(items):
    if _postgres_available():
        return
    skip = pytest.mark.skip(reason="Postgres is not running — skipping DB tests")
    for item in items:
        if item.get_closest_marker("django_db"):
            item.add_marker(skip)
