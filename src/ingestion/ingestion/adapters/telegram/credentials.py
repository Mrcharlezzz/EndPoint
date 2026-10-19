"""Typed credentials for the Telegram adapter."""

from __future__ import annotations

from typing import TypedDict


class TelegramCredentials(TypedDict):
    bot_token: str
