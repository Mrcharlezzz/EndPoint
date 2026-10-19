"""Local SQLite store for daemon-only state (cursors, history, config overlay, logs, audit).

Shared Postgres holds the schema of record (``db_core``). Everything else that
is local to a single daemon instance lives here.
"""

from __future__ import annotations

from ingestion.daemon.store.local import LocalStore, apply_migrations, open_store

__all__ = ["LocalStore", "apply_migrations", "open_store"]
