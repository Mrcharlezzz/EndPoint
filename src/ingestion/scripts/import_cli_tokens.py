"""Import platform credentials from the CLI's token file into Postgres.

Reads ``~/.config/ingest/credentials.json`` (the file the ingestion CLI
writes at login time), encrypts each platform's payload with the
daemon's Fernet key, and upserts one ``PlatformCredential`` row per
platform under the operator's email.

Dev-only. Useful after a fresh DB when the dashboard needs real tokens
to drive the adapters end-to-end without going through the daemon's
auth flow. Safe to re-run — each call overwrites the row for
``(user_email, platform)``.

Usage::

    # user_email defaults to the logged-in Teams account when the JWT is
    # parseable; override with --user-email for other platforms.
    uv run python scripts/import_cli_tokens.py
    uv run python scripts/import_cli_tokens.py --user-email me@example.com

Requires ``ENDPOINT_FERNET_KEY`` in the environment (loaded from .env
by the standard daemon config path).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import core.bootstrap

core.bootstrap.init()

from core.models import PlatformCredential  # noqa: E402
from ingestion.daemon.crypto import CredentialCipher  # noqa: E402
from ingestion.daemon.system_users import TELEGRAM_BOT_USER_EMAIL  # noqa: E402

DEFAULT_CREDENTIALS_FILE = Path.home() / ".config" / "ingest" / "credentials.json"

ADAPTER_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "teams": ("graph_token", "team_ids"),
    "moodle": ("token", "user_id"),
    "telegram": ("bot_token",),
}

# Platforms that belong to a single global bot, not the logged-in operator.
# Their credentials get written under a sentinel email so per-user dashboard
# surfaces don't show the bot as if it were a real user.
SYSTEM_OWNED_PLATFORMS: dict[str, tuple[str, str]] = {
    "telegram": (TELEGRAM_BOT_USER_EMAIL, "Telegram bot"),
}


def extract_upn_from_graph_token(token: str) -> tuple[str | None, str | None]:
    """Parse the unverified JWT payload for ``upn`` and display name.

    We only use this to pick a sensible default user_email when one isn't
    passed on the CLI. Signature verification doesn't matter here — if
    the token is bogus the daemon's adapter calls will fail loudly.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None, None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None, None
    upn = payload.get("upn") or payload.get("unique_name")
    given = payload.get("given_name") or ""
    family = payload.get("family_name") or ""
    display = f"{given} {family}".strip() or None
    return upn, display


def build_payload(platform: str, raw: dict[str, object]) -> dict[str, object]:
    """Whitelist the keys the adapter actually reads so we don't smuggle
    CLI-local scratch fields (``refresh_url``, etc) into the encrypted row."""
    required = ADAPTER_REQUIRED_KEYS[platform]
    payload: dict[str, object] = {}
    missing: list[str] = []
    for key in required:
        if key in raw:
            payload[key] = raw[key]
        else:
            missing.append(key)
    if missing:
        raise ValueError(f"{platform} credentials missing required keys: {missing}")
    # Optional Teams fields the adapter uses when present.
    if platform == "teams" and "csa_token" in raw:
        payload["csa_token"] = raw["csa_token"]
    return payload


async def upsert(
    cipher: CredentialCipher,
    *,
    user_email: str,
    display_name: str,
    platform: str,
    payload: dict[str, object],
) -> str:
    encrypted = cipher.encrypt_json(payload)
    _, created = await PlatformCredential.objects.aupdate_or_create(
        user_email=user_email,
        platform=platform,
        defaults={
            "display_name": display_name,
            "encrypted_payload": encrypted,
            "is_expired": False,
            "is_paused": False,
            "expired_at": None,
        },
    )
    return "created" if created else "updated"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS_FILE,
        help=f"Path to the CLI credentials.json (default: {DEFAULT_CREDENTIALS_FILE})",
    )
    parser.add_argument(
        "--user-email",
        default=None,
        help="Email to use as the PlatformCredential.user_email key for every platform.",
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="Display name for new rows (ignored on updates).",
    )
    args = parser.parse_args()

    if not args.credentials.exists():
        print(f"credentials file not found: {args.credentials}", file=sys.stderr)
        return 2

    raw = json.loads(args.credentials.read_text())

    user_email = args.user_email
    display_name = args.display_name
    if user_email is None:
        graph_token = raw.get("teams", {}).get("graph_token", "")
        parsed_upn, parsed_name = extract_upn_from_graph_token(graph_token)
        user_email = parsed_upn
        if display_name is None:
            display_name = parsed_name
    if user_email is None:
        print(
            "could not infer --user-email from the Teams JWT; pass --user-email explicitly",
            file=sys.stderr,
        )
        return 2
    display_name = display_name or user_email

    if not os.environ.get("ENDPOINT_FERNET_KEY"):
        print("ENDPOINT_FERNET_KEY must be set in the environment", file=sys.stderr)
        return 2

    cipher = CredentialCipher.from_env()

    imported: list[tuple[str, str, str]] = []
    for platform in ("teams", "moodle", "telegram"):
        if platform not in raw:
            continue
        try:
            payload = build_payload(platform, raw[platform])
        except ValueError as err:
            print(f"skipping {platform}: {err}", file=sys.stderr)
            continue
        # Singleton-bot platforms (Telegram today) get pinned to a sentinel
        # email regardless of --user-email so per-user dashboard surfaces
        # never show the bot as if it were a real user.
        owner_email, owner_name = SYSTEM_OWNED_PLATFORMS.get(platform, (user_email, display_name))
        status = await upsert(
            cipher,
            user_email=owner_email,
            display_name=owner_name,
            platform=platform,
            payload=payload,
        )
        imported.append((platform, status, owner_email))

    for platform, status, owner in imported:
        print(f"{platform}: {status} for {owner}")
    if not imported:
        print("nothing imported", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
