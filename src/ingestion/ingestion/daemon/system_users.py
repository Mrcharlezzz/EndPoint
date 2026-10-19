"""Sentinel user emails that own system-scoped credentials.

Telegram is the first example: one bot fetches the groups it has been added
to, so its ``PlatformCredential`` row is owned by a fixed non-human email
rather than any real user. Per-user endpoints filter this email so the bot
doesn't leak into user-scoped views (Users, Credentials, Next sync).

Extend :data:`SYSTEM_USER_EMAILS` when adding another singleton-style
credential so the per-user filters pick it up automatically.
"""

from __future__ import annotations

TELEGRAM_BOT_USER_EMAIL = "telegram-bot@system.endpoint"

SYSTEM_USER_EMAILS: frozenset[str] = frozenset({TELEGRAM_BOT_USER_EMAIL})
