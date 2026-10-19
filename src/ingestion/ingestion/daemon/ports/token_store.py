"""Postgres-backed :class:`TokenStore` against ``PlatformCredential``.

The Fernet-encrypted ``encrypted_payload`` column is the sole home for
platform credentials. Plaintext tokens never touch the row. ``user_email`` is
the grouping key — the pipeline talks in "user_id" but the value is always an
email at the port boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.models import PlatformCredential

if TYPE_CHECKING:
    from ingestion.daemon.auth.notifier import Notifier
    from ingestion.daemon.crypto import CredentialCipher


class PostgresTokenStore:
    """Read and write encrypted credentials, plus mark rows expired.

    The ``notifier`` (if provided) fires on the valid-to-expired transition.
    Callers that don't care (Phase 1 pipeline) pass nothing and mark_expired
    is a straight write.
    """

    def __init__(self, cipher: CredentialCipher, notifier: Notifier | None = None) -> None:
        self._cipher = cipher
        self._notifier = notifier

    async def get_credentials(self, user_id: str, platform: str) -> dict[str, Any] | None:
        try:
            row = await PlatformCredential.objects.aget(
                user_email=user_id,
                platform=platform,
            )
        except PlatformCredential.DoesNotExist:
            return None
        return self._cipher.decrypt_json(bytes(row.encrypted_payload))

    async def update_credentials(
        self,
        user_id: str,
        platform: str,
        credentials: dict[str, Any],
    ) -> None:
        """Upsert encrypted credentials for ``(user_id, platform)``.

        Every call resets ``is_expired=False`` and clears ``expired_at``
        because the only callers today (login, refresh) write a fresh
        payload on success. If a future caller needs to push credentials
        without declaring them fresh, add an explicit ``mark_fresh`` kwarg
        rather than changing this default in place.

        ``display_name`` and ``is_paused`` are not in ``defaults``: the
        control panel owns those fields. A fresh row uses model defaults
        (empty display_name, not paused); updates leave them alone.
        """
        encrypted = self._cipher.encrypt_json(credentials)
        await PlatformCredential.objects.aupdate_or_create(
            user_email=user_id,
            platform=platform,
            defaults={
                "encrypted_payload": encrypted,
                "last_refreshed_at": datetime.now(tz=UTC),
                "is_expired": False,
                "expired_at": None,
            },
        )

    async def mark_expired(self, user_id: str, platform: str) -> None:
        # Atomic compare-and-set: only rows currently ``is_expired=False``
        # match the filter, so the rowcount tells us whether this call is
        # the valid-to-expired transition. Concurrent callers racing the
        # same row still fire the notifier exactly once because only one
        # UPDATE matches a row whose ``is_expired`` is still False.
        transitioned = await PlatformCredential.objects.filter(
            user_email=user_id,
            platform=platform,
            is_expired=False,
        ).aupdate(is_expired=True, expired_at=datetime.now(tz=UTC))
        if transitioned and self._notifier is not None:
            await self._notifier.user_expired(user_id, platform)
