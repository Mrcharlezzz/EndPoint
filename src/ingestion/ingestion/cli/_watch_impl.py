"""Telegram watch command implementation — live message streaming dashboard."""

from __future__ import annotations

import asyncio
import json
import signal
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — used at runtime

from rich.console import Console

from ingestion.cli.app import state
from ingestion.cli.display import SYM_OK, SYM_PENDING, print_status
from ingestion.cli.state import (
    load_credentials,
    load_cursor,
    save_cursor,
)
from ingestion.config import TelegramConfig

console = Console(stderr=True)


async def run_watch() -> None:
    """Live message streaming dashboard for Telegram."""
    config_dir = state.config_dir
    config = TelegramConfig()

    creds = load_credentials("telegram", config_dir)
    if not creds or not creds.get("bot_token"):
        console.print(
            "  [red]No Telegram credentials. Run [bold]ingest login telegram[/bold][/red]"
        )
        return

    bot_token = creds["bot_token"]
    api_base = config.api_base_url.rstrip("/")

    # Load cursor for resuming
    cursor = load_cursor("telegram", config_dir)
    last_update_id = (cursor or {}).get("last_update_id", 0)

    # Prepare output file
    output_dir = state.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    output_path = output_dir / f"telegram-watch-{today}.json"

    console.print()
    print_status(SYM_PENDING, "Watching groups... (Ctrl+C to stop)")
    console.print()

    total_messages = 0
    running = True

    def handle_sigint(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_sigint)

    import httpx

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(35)) as client:
            while running:
                offset = last_update_id + 1 if last_update_id else 0
                params: dict[str, int] = {"timeout": 30, "limit": 100}
                if offset:
                    params["offset"] = offset

                try:
                    resp = await client.get(
                        f"{api_base}/bot{bot_token}/getUpdates",
                        params=params,
                    )
                    data = resp.json()
                except Exception as exc:
                    console.print(f"  [red]Poll error: {exc}[/red]")
                    await asyncio.sleep(5)
                    continue

                if not data.get("ok"):
                    console.print(f"  [red]API error: {data.get('description', 'unknown')}[/red]")
                    await asyncio.sleep(5)
                    continue

                updates = data.get("result", [])
                batch_messages: list[dict] = []

                for update in updates:
                    update_id = update.get("update_id", 0)
                    if update_id > last_update_id:
                        last_update_id = update_id

                    msg = update.get("message") or update.get("edited_message")
                    if not msg:
                        continue

                    chat = msg.get("chat", {})
                    chat_type = chat.get("type", "")
                    if chat_type not in ("group", "supergroup"):
                        continue

                    chat_title = chat.get("title", f"Chat {chat.get('id', '')}")
                    sender = msg.get("from", {})
                    username = sender.get("username", sender.get("first_name", "unknown"))
                    text = msg.get("text", "(media)")
                    ts = msg.get("date", 0)
                    time_str = (
                        datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M") if ts else "??:??"
                    )

                    console.print(f"  [bold][{chat_title}][/bold]")
                    console.print(f"  {time_str}  @{username}: {text}")
                    console.print()

                    batch_messages.append(msg)
                    total_messages += 1

                # Append to output file in real-time
                if batch_messages:
                    _append_messages(output_path, batch_messages)
                    console.print(
                        f"  [dim]--- {total_messages} messages saved | watching ---[/dim]"
                    )

    except asyncio.CancelledError:
        pass

    console.print()
    print_status(SYM_PENDING, "Stopping watcher...")
    print_status(SYM_OK, f"{total_messages} messages saved")

    # Save cursor
    if last_update_id:
        save_cursor("telegram", {"last_update_id": last_update_id}, config_dir)
        print_status(SYM_OK, f"Cursor saved (will resume from update_id: {last_update_id})")

    console.print(f"  Output: {output_path}")


def _append_messages(path: Path, messages: list[dict]) -> None:
    """Append messages to the output file."""
    existing: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []

    existing.extend(messages)
    path.write_text(
        json.dumps(existing, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
