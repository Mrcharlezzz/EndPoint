"""FastAPI surface for the ingestor daemon.

Import :func:`create_app` to get a configured :class:`fastapi.FastAPI`. The
app reads ``ENDPOINT_ADMIN_PASSWORD`` and ``ENDPOINT_SESSION_SECRET`` at
startup; every route except ``/api/auth/login`` and ``/api/openapi.json``
requires a signed session cookie.
"""

from __future__ import annotations

from ingestion.daemon.api.app import create_app

__all__ = ["create_app"]
