"""Pydantic response models shared across routers.

Models that only one router emits live next to their route. Anything the
OpenAPI schema references from two or more places lives here so the
generated TypeScript client has a single stable name for the shape.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class OkResponse(BaseModel):
    """Trivial ``{"ok": true}`` body returned by mutation endpoints that
    have no other payload to surface."""

    model_config = ConfigDict(extra="forbid")

    ok: bool


class AuditEntry(BaseModel):
    """Shape of one audit_log row as it comes back from the API.

    ``before`` / ``after`` are free-form JSON snapshots so they keep
    ``extra="allow"`` semantics on the payload objects themselves; the
    entry envelope is still strict.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    timestamp: str
    actor: str | None
    action: str
    target: str | None
    before: Any = None
    after: Any = None
    reason: str | None
