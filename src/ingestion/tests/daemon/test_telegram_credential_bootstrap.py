from __future__ import annotations

import pytest
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.system_users import TELEGRAM_BOT_USER_EMAIL
from ingestion.daemon.telegram_credential_bootstrap import (
    TELEGRAM_BOT_TOKEN_ENV,
    TelegramCredentialBootstrapError,
    bootstrap_telegram_credential,
)

pytestmark = pytest.mark.django_db(transaction=True)


async def test_bootstrap_telegram_credential_upserts_system_owned_encrypted_row(
    fernet_key: str,
) -> None:
    from core.models import PlatformCredential

    cipher = CredentialCipher([fernet_key])

    status = await bootstrap_telegram_credential(
        cipher,
        env={TELEGRAM_BOT_TOKEN_ENV: "123456:prod-bot-token"},
    )

    row = await PlatformCredential.objects.aget(
        user_email=TELEGRAM_BOT_USER_EMAIL,
        platform="telegram",
    )
    assert status == "created"
    assert row.display_name == "Telegram bot"
    assert row.is_expired is False
    assert row.is_paused is False
    assert row.expired_at is None
    assert cipher.decrypt_json(bytes(row.encrypted_payload)) == {
        "bot_token": "123456:prod-bot-token"
    }


async def test_bootstrap_telegram_credential_updates_existing_row(fernet_key: str) -> None:
    cipher = CredentialCipher([fernet_key])

    first = await bootstrap_telegram_credential(
        cipher,
        env={TELEGRAM_BOT_TOKEN_ENV: "123456:old-token"},
    )
    second = await bootstrap_telegram_credential(
        cipher,
        env={TELEGRAM_BOT_TOKEN_ENV: "123456:new-token"},
    )

    from core.models import PlatformCredential

    row = await PlatformCredential.objects.aget(
        user_email=TELEGRAM_BOT_USER_EMAIL,
        platform="telegram",
    )
    assert first == "created"
    assert second == "updated"
    assert cipher.decrypt_json(bytes(row.encrypted_payload)) == {"bot_token": "123456:new-token"}


async def test_bootstrap_telegram_credential_requires_bot_token(fernet_key: str) -> None:
    cipher = CredentialCipher([fernet_key])

    with pytest.raises(TelegramCredentialBootstrapError, match=TELEGRAM_BOT_TOKEN_ENV):
        await bootstrap_telegram_credential(cipher, env={TELEGRAM_BOT_TOKEN_ENV: "   "})
