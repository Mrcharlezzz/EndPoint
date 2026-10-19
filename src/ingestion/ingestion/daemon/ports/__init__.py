"""Concrete port implementations used by the daemon runtime.

Postgres-backed ports (:mod:`.content_sink`, :mod:`.token_store`) sit on top
of ``db_core`` Django models. SQLite-backed ports (:mod:`.sync_state`,
:mod:`.sync_logger`) sit on top of the container-local SQLite file that also
holds the config overlay.

Import the specific submodule you need rather than the package itself — the
Postgres-backed submodules require ``core.bootstrap.init()`` first, while the
SQLite-backed ones do not.
"""

from __future__ import annotations

__all__: list[str] = []
