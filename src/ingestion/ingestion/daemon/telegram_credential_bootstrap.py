"""Bootstrap the singleton Telegram bot credential from production secrets."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ingestion.daemon.system_users import TELEGRAM_BOT_USER_EMAIL

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ingestion.daemon.crypto import CredentialCipher

TELEGRAM_BOT_TOKEN_ENV = "ENDPOINT_TELEGRAM__BOT_TOKEN"
TELEGRAM_PLATFORM = "telegram"
TELEGRAM_DISPLAY_NAME = "Telegram bot"


class TelegramCredentialBootstrapError(RuntimeError):
    """Raised when the Telegram credential bootstrap input is invalid."""


async def bootstrap_telegram_credential(
    cipher: CredentialCipher,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Upsert the system-owned Telegram bot credential.

    Production seed artifacts intentionally exclude ``platform_credentials``.
    This restores the singleton bot credential through the runtime secret
    channel instead, keeping the artifact portable and credential-free.
    """
    source = env if env is not None else os.environ
    bot_token = source.get(TELEGRAM_BOT_TOKEN_ENV, "").strip()
    if not bot_token:
        raise TelegramCredentialBootstrapError(
            f"{TELEGRAM_BOT_TOKEN_ENV} must be set to bootstrap Telegram polling"
        )

    from core.models import PlatformCredential  # noqa: PLC0415

    encrypted = cipher.encrypt_json({"bot_token": bot_token})
    _, created = await PlatformCredential.objects.aupdate_or_create(
        user_email=TELEGRAM_BOT_USER_EMAIL,
        platform=TELEGRAM_PLATFORM,
        defaults={
            "display_name": TELEGRAM_DISPLAY_NAME,
            "encrypted_payload": encrypted,
            "last_refreshed_at": datetime.now(tz=UTC),
            "is_expired": False,
            "is_paused": False,
            "expired_at": None,
        },
    )
    return "created" if created else "updated"
