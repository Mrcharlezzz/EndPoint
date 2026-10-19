"""Fetch Telegram messages from personal account for LLM cost research.

Usage:
    1. Get api_id + api_hash from https://my.telegram.org
    2. Create .env file in this directory (or export env vars):
         TG_API_ID=12345678
         TG_API_HASH=your-telegram-api-hash
    3. Run:
         cd ingestion/scripts
         uv run python telegram_fetch.py

    First run will ask for your phone number + code (creates a session file).
    Subsequent runs reuse the session automatically.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.tl.types import Channel, Chat, User
except ImportError:
    print("Install telethon first:  uv pip install telethon")
    sys.exit(1)

# --- Config ---
MESSAGES_PER_CHANNEL = 1000
OUTPUT_DIR = Path.home() / ".config" / "ingest" / "output"
SESSION_FILE = Path(__file__).parent / "tg_session"

# Load credentials from env or .env file
def load_credentials() -> tuple[int, str]:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")

    if not api_id or not api_hash:
        print("Missing TG_API_ID or TG_API_HASH.")
        print("Set them in .env or as environment variables.")
        sys.exit(1)

    return int(api_id), api_hash


async def list_groups(client: TelegramClient) -> list[dict]:
    """List all groups/channels the user is in."""
    dialogs = await client.get_dialogs()
    groups = []
    for d in dialogs:
        entity = d.entity
        if isinstance(entity, (Channel, Chat)):
            groups.append({
                "id": d.id,
                "name": d.name or "(unnamed)",
                "type": "channel" if isinstance(entity, Channel) else "group",
                "members": getattr(entity, "participants_count", None),
                "unread": d.unread_count,
            })
    return groups


async def fetch_messages(
    client: TelegramClient,
    channel_id: int,
    channel_name: str,
    limit: int = MESSAGES_PER_CHANNEL,
) -> list[dict]:
    """Fetch messages from a channel, normalized to match ingestion schema."""
    items = []
    async for msg in client.iter_messages(channel_id, limit=limit):
        if msg.text is None and msg.message is None:
            continue  # skip service messages (joins, pins, etc.)

        body = msg.text or msg.message or ""
        if not body.strip():
            continue

        # Build author name
        author = None
        if msg.sender:
            if isinstance(msg.sender, User):
                parts = [msg.sender.first_name or "", msg.sender.last_name or ""]
                author = " ".join(p for p in parts if p) or None
            elif hasattr(msg.sender, "title"):
                author = msg.sender.title

        # Build attachments
        attachments = []
        if msg.document:
            attrs = {type(a).__name__: a for a in (msg.document.attributes or [])}
            filename = None
            if "DocumentAttributeFilename" in attrs:
                filename = attrs["DocumentAttributeFilename"].file_name
            attachments.append({
                "external_id": f"tg_file_{msg.document.id}",
                "filename": filename or f"document_{msg.document.id}",
                "download_url": "",  # would need client.download_media()
                "content_type": msg.document.mime_type,
                "size_bytes": msg.document.size,
                "is_media": (msg.document.mime_type or "").startswith(("video/", "audio/")),
            })
        elif msg.photo:
            attachments.append({
                "external_id": f"tg_photo_{msg.photo.id}",
                "filename": f"photo_{msg.photo.id}.jpg",
                "download_url": "",
                "content_type": "image/jpeg",
                "size_bytes": None,
                "is_media": False,
            })

        # Forward attribution
        fwd_author = None
        if msg.forward:
            if msg.forward.sender:
                if isinstance(msg.forward.sender, User):
                    parts = [msg.forward.sender.first_name or "", msg.forward.sender.last_name or ""]
                    fwd_author = " ".join(p for p in parts if p) or None

        item = {
            "external_id": f"tg_{channel_id}_{msg.id}",
            "platform": "telegram",
            "content_type": "raw_message",
            "title": None,
            "body": body,
            "body_html": None,
            "course_name": channel_name,
            "course_external_id": str(channel_id),
            "author": fwd_author or author,
            "published_at": msg.date.astimezone(UTC).isoformat() if msg.date else None,
            "due_at": None,
            "raw_payload": {
                "schema_version": "2",
                "source": "telegram_message",
                "ingestion_mode": "personal_fetch",
                "fetched_at": datetime.now(UTC).isoformat(),
                "original": {
                    "message_id": msg.id,
                    "chat_id": channel_id,
                    "reply_to": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
                    "views": msg.views,
                    "forwards": msg.forwards,
                    "is_forwarded": msg.forward is not None,
                },
            },
            "attachments": attachments,
            "ingestion_mode": "personal_fetch",
        }
        items.append(item)

    return items


def pick_groups_interactive(groups: list[dict]) -> list[dict]:
    """Multi-round filter-and-pick group selector.

    Loop: type keyword → pick from filtered list → repeat.
    Type 'done' or 'q' to finish and start fetching.
    Shows running selection count between rounds.
    """
    selected_ids: set[int] = set()
    selected_groups: list[dict] = []

    while True:
        if selected_groups:
            print(f"\n  Selected so far ({len(selected_groups)}):")
            for g in selected_groups:
                print(f"    - {g['name']}")
            print()

        query = input(
            "Search groups (keyword), or 'done'/'q' to finish: "
        ).strip()

        if not query:
            continue

        if query.lower() in ("done", "q", "quit", "exit"):
            break

        filtered = [
            g for g in groups
            if query.lower() in g["name"].lower() and g["id"] not in selected_ids
        ]

        if not filtered:
            already = [g for g in groups if query.lower() in g["name"].lower()]
            if already:
                print(f"  All matches for '{query}' already selected.")
            else:
                print(f"  No groups match '{query}'.")
            continue

        # Print numbered list
        print(f"\n  Matches for '{query}':")
        for i, g in enumerate(filtered):
            members = f" ({g['members']} members)" if g["members"] else ""
            print(f"    [{i}] {g['name']}{members}")

        pick = input(
            "\n  Enter numbers to add (e.g. 0,2,3), 'all', or Enter to skip: "
        ).strip()

        if not pick:
            continue

        if pick.lower() == "all":
            to_add = filtered
        else:
            try:
                indices = [int(x.strip()) for x in pick.split(",")]
                to_add = [filtered[i] for i in indices]
            except (ValueError, IndexError):
                print("  Invalid selection.")
                continue

        for g in to_add:
            if g["id"] not in selected_ids:
                selected_ids.add(g["id"])
                selected_groups.append(g)
                print(f"    + {g['name']}")

    return selected_groups


async def main_async() -> None:
    """Single async context — no reconnection issues."""
    api_id, api_hash = load_credentials()
    client = TelegramClient(str(SESSION_FILE), api_id, api_hash)

    await client.start()
    me = await client.get_me()
    print(f"Logged in as: {me.first_name}\n")

    # Fetch group list
    groups = await list_groups(client)
    groups.sort(key=lambda g: g["name"].lower())
    print(f"Found {len(groups)} groups/channels.\n")

    # Interactive picker (plain input() works fine inside asyncio)
    selected = pick_groups_interactive(groups)

    if not selected:
        print("No channels selected.")
        await client.disconnect()
        return

    # Fetch messages
    all_items: list[dict] = []
    for g in selected:
        print(f"\nFetching {MESSAGES_PER_CHANNEL} messages from: {g['name']}...")
        items = await fetch_messages(client, g["id"], g["name"])
        all_items.extend(items)
        print(f"  Got {len(items)} text messages")

    await client.disconnect()

    # Save output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M")
    output_path = OUTPUT_DIR / f"telegram-personal-{timestamp}.json"

    output_data = {
        "items": all_items,
        "files": [],
        "errors": [],
        "stats": {
            "items_fetched": len(all_items),
            "channels_fetched": len(selected),
            "channels": [g["name"] for g in selected],
        },
        "metadata": {
            "platforms": ["telegram"],
            "source": "personal_account",
            "timestamp": datetime.now(UTC).isoformat(),
            "messages_per_channel": MESSAGES_PER_CHANNEL,
        },
    }

    output_path.write_text(json.dumps(output_data, indent=2, ensure_ascii=False))
    print(f"\nSaved {len(all_items)} messages to: {output_path}")
    print(f"File size: {output_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    asyncio.run(main_async())
