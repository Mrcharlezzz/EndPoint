"""Shared FastAPI dependencies.

The :class:`ApiContext` is attached to ``app.state`` at startup and then
passed into each route handler via the :func:`context` dependency. This
keeps route handlers decoupled from global singletons.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request

from ingestion.daemon.api.auth import AuthProvider
from ingestion.daemon.api.cancel_registry import CancelRegistry
from ingestion.daemon.api.pubsub import EventBus
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.ports.credential_port import CredentialPort
from ingestion.daemon.ports.credential_store import DjangoCredentialStore
from ingestion.daemon.store.local import LocalStore


@dataclass(frozen=True)
class ApiContext:
    config: DaemonConfig
    store: LocalStore
    cipher: CredentialCipher
    auth: AuthProvider
    events: EventBus
    # Wall-clock start time of the app process, captured once at context
    # construction. Powers the topbar uptime display. Wall-clock (not
    # monotonic) so it survives serialisation as an ISO string to the
    # dashboard, which computes elapsed client-side.
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    # Per-user lock dict shared with the refresh loop so the on-demand
    # action and the bulk cycle can't run two refreshes for the same user
    # at once. A lock per email is created lazily; locks are cheap and
    # never freed (one per known user, bounded by the credential count).
    user_refresh_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Per-sync cancellation signals. The ``sync cancel`` action flips an
    # Event here so a pipeline checkpoint can observe it; independent of
    # that, the action always persists a terminal ``cancelled`` row in
    # sync_history, so the operator's intent is durable.
    cancels: CancelRegistry = field(default_factory=CancelRegistry)
    # Typed credential port. Routers/services depend on the protocol; the
    # default factory wires the Django-ORM-backed implementation. Tests
    # can substitute an in-memory fake by passing ``credentials=...`` to
    # ``ApiContext`` (or building a fresh one for ``app.state``).
    credentials: CredentialPort = field(default_factory=DjangoCredentialStore)


def get_context(request: Request) -> ApiContext:
    ctx = getattr(request.app.state, "api_context", None)
    if ctx is None:
        raise RuntimeError("ApiContext not installed on app.state")
    return ctx


ContextDep = Annotated[ApiContext, Depends(get_context)]
