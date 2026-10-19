"""Ingestor daemon: long-running service that polls users on a schedule.

Import the daemon package only when the ``daemon`` extra is installed:
``uv sync --extra daemon``. The core ingestion library stays Django-free;
this sub-package brings in db_core (Django ORM) and FastAPI.
"""

from __future__ import annotations

__all__: list[str] = []
