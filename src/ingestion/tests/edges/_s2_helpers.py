"""Shared test helpers for slice-2 edge tests.

Lives next to the edge file so the helpers don't bloat the test
module's LOC budget. Underscore prefix keeps pytest from collecting
this file as a test module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.ports.credential_port import CredentialPort, CredentialRow
from ingestion.daemon.store.local import open_store


def build_client(
    tmp_path: Path,
    fernet_key: str,
    *,
    credentials: CredentialPort | None = None,
) -> TestClient:
    """Wire up an in-process daemon API. Optionally swap in a fake
    credentials port so router tests don't need a live ORM."""
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    if credentials is not None:
        from dataclasses import replace as dc_replace

        app.state.api_context = dc_replace(app.state.api_context, credentials=credentials)
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    return client


def make_row(
    *,
    user_email: str = "alice@x",
    platform: str = "moodle",
    is_paused: bool = False,
    is_expired: bool = False,
) -> CredentialRow:
    return CredentialRow(
        user_email=user_email,
        platform=platform,
        is_paused=is_paused,
        is_expired=is_expired,
        expired_at=None,
        last_refreshed_at=None,
        encrypted_payload=b"\x00",
    )


class FakeCredPort:
    """In-memory port. Mirrors the production store's surface."""

    def __init__(self, seed: list[CredentialRow] | None = None) -> None:
        self.rows: dict[tuple[str, str], CredentialRow] = {}
        for row in seed or ():
            self.rows[(row.user_email, row.platform)] = row

    async def get(self, email: str, platform: str) -> CredentialRow | None:
        return self.rows.get((email, platform))

    async def list_for_user(self, email: str) -> list[CredentialRow]:
        return [r for (e, _), r in self.rows.items() if e == email]

    async def list_for_platform(self, platform: str) -> list[CredentialRow]:
        return [r for (_, p), r in self.rows.items() if p == platform]

    async def set_paused(self, email: str, platform: str, paused: bool) -> int:
        row = self.rows.get((email, platform))
        if row is None or row.is_paused == paused:
            return 0
        self.rows[(email, platform)] = _replace(row, is_paused=paused)
        return 1

    async def set_paused_bulk_for_user(self, email: str, paused: bool) -> int:
        n = 0
        for key, row in list(self.rows.items()):
            if key[0] != email or row.is_paused == paused:
                continue
            self.rows[key] = _replace(row, is_paused=paused)
            n += 1
        return n

    async def set_expired(self, email: str, platform: str, expired: bool) -> int:
        row = self.rows.get((email, platform))
        if row is None or row.is_expired == expired:
            return 0
        self.rows[(email, platform)] = _replace(row, is_expired=expired)
        return 1


def _replace(row: CredentialRow, **changes: Any) -> CredentialRow:
    base: dict[str, Any] = {
        "user_email": row.user_email,
        "platform": row.platform,
        "is_paused": row.is_paused,
        "is_expired": row.is_expired,
        "expired_at": row.expired_at,
        "last_refreshed_at": row.last_refreshed_at,
        "encrypted_payload": row.encrypted_payload,
    }
    base.update(changes)
    return CredentialRow(**base)


class CapturingWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def write(
        self,
        *,
        action: str,
        target: str | None = None,
        before: Any = None,
        after: Any = None,
        reason: str | None = None,
    ) -> int:
        self.calls.append(
            {
                "action": action,
                "target": target,
                "before": before,
                "after": after,
                "reason": reason,
            }
        )
        return len(self.calls)
