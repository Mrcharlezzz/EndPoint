"""Bootstrap the production Telegram bot credential from environment secrets."""

from __future__ import annotations

import asyncio
import sys

import core.bootstrap

core.bootstrap.init()

from ingestion.daemon.crypto import CredentialCipher, CryptoConfigError  # noqa: E402
from ingestion.daemon.telegram_credential_bootstrap import (  # noqa: E402
    TELEGRAM_BOT_TOKEN_ENV,
    TelegramCredentialBootstrapError,
    bootstrap_telegram_credential,
)


async def _run() -> int:
    try:
        cipher = CredentialCipher.from_env()
        status = await bootstrap_telegram_credential(cipher)
    except (CryptoConfigError, TelegramCredentialBootstrapError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"telegram: {status} for telegram-bot@system.endpoint")
    print(f"telegram: token source {TELEGRAM_BOT_TOKEN_ENV}")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
