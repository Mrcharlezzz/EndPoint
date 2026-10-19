"""Telegram login — bot token paste + validation via getMe."""

from __future__ import annotations

import httpx
import questionary
from rich.console import Console

from ingestion.cli.display import SYM_FAIL, SYM_OK, SYM_PENDING, print_status
from ingestion.cli.state import save_credentials
from ingestion.config import TelegramConfig

console = Console(stderr=True)


def run_telegram_login() -> None:
    """Prompt for bot token and validate via getMe."""
    config = TelegramConfig()
    api_base = config.api_base_url.rstrip("/")

    token = questionary.password("Paste your Telegram bot token:").ask()
    if not token:
        return

    token = token.strip()
    print_status(SYM_PENDING, "Validating with Telegram API...")

    bot_info = _validate_token(api_base, token)
    if bot_info is None:
        print_status(SYM_FAIL, "Invalid bot token.")
        return

    bot_username = bot_info.get("username", "unknown")
    bot_id = bot_info.get("id", 0)
    print_status(SYM_OK, f"Bot: @{bot_username} (id: {bot_id})")

    groups = _discover_groups(api_base, token)
    if groups:
        print_status(SYM_OK, f"Found {len(groups)} group chats")
        for g in groups:
            console.print(f"    [dim]•[/dim] {g['title']} (id: {g['id']})")
    else:
        console.print("  [dim]No group chats found (send a message to a group first)[/dim]")

    save_credentials("telegram", {"bot_token": token})
    console.print("  Saved to ~/.config/ingest/credentials.json")


def _validate_token(api_base: str, token: str) -> dict | None:
    """Call getMe to validate the bot token."""
    url = f"{api_base}/bot{token}/getMe"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            data = resp.json()
        if data.get("ok"):
            return data.get("result", {})
    except Exception:
        pass
    return None


def _discover_groups(api_base: str, token: str) -> list[dict]:
    """Fetch recent updates to discover group/supergroup chats."""
    url = f"{api_base}/bot{token}/getUpdates"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url, params={"limit": 100})
            data = resp.json()
    except Exception:
        return []

    if not data.get("ok"):
        return []

    seen_chats: dict[int, dict] = {}
    for update in data.get("result", []):
        for key in ("message", "edited_message"):
            msg = update.get(key)
            if not msg:
                continue
            chat = msg.get("chat", {})
            chat_type = chat.get("type", "")
            if chat_type in ("group", "supergroup"):
                chat_id = chat.get("id", 0)
                if chat_id not in seen_chats:
                    seen_chats[chat_id] = {
                        "id": chat_id,
                        "title": chat.get("title", f"Chat {chat_id}"),
                    }

    return list(seen_chats.values())
