"""Daemon auth: Teams token refresh via a headless Playwright pool.

The silent-refresh flow already exists in :mod:`ingestion.cli.auth.teams_login`
for the CLI; this package ports it to async, drives it from a bounded pool,
and persists results via :class:`PostgresTokenStore`. Phase 3 of the ingestor
daemon build plan.
"""

from __future__ import annotations

__all__: list[str] = []
