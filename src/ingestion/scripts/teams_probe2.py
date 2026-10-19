"""Probe the C++ team specifically to find what hangs.

Usage: .venv/bin/python scripts/teams_probe2.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

CREDS_PATH = Path.home() / ".config" / "ingest" / "credentials.json"
BASE = "https://graph.microsoft.com/v1.0"


def load_creds() -> dict:
    data = json.loads(CREDS_PATH.read_text())
    return data.get("teams", {})


async def get(token: str, url: str, label: str, timeout: float = 15) -> dict | None:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        dt = time.monotonic() - t0
        print(f"  [{r.status_code}] {label} ({dt:.2f}s)")
        if r.status_code == 200:
            return r.json()
        else:
            print(f"        {r.text[:150]}")
            return None
    except Exception as e:
        dt = time.monotonic() - t0
        print(f"  [ERR] {label} ({dt:.2f}s) — {e}")
        return None


async def main() -> None:
    creds = load_creds()
    token = creds["graph_token"]

    # Find the C++ team
    print("Finding C++ team...")
    data = await get(token, f"{BASE}/me/joinedTeams", "joinedTeams")
    if not data:
        return

    cpp_team = None
    for t in data.get("value", []):
        name = t.get("displayName", "")
        if "C++" in name or "C and C++" in name:
            cpp_team = t
            break

    if not cpp_team:
        print("C++ team not found!")
        for t in data.get("value", []):
            print(f"  • {t.get('displayName', '?')}")
        return

    team_id = cpp_team["id"]
    print(f"\nTeam: {cpp_team['displayName']}")
    print(f"  ID: {team_id}")

    # Channels
    print("\n--- Channels ---")
    ch_data = await get(token, f"{BASE}/teams/{team_id}/channels", "channels")
    if ch_data:
        for ch in ch_data.get("value", []):
            print(f"    • {ch.get('displayName', '?')} (id: {ch['id'][:30]}...)")

    # Drives
    print("\n--- Drives ---")
    dr_data = await get(token, f"{BASE}/groups/{team_id}/drives", "drives")
    if dr_data:
        for d in dr_data.get("value", []):
            print(f"    • {d.get('name', '?')} (id: {d['id'][:20]}...)")

            # List root of each drive (just top level)
            drive_id = d["id"]
            root_data = await get(
                token,
                f"{BASE}/drives/{drive_id}/root/children",
                f"  root of '{d.get('name', '?')}'",
            )
            if root_data:
                items = root_data.get("value", [])
                for item in items[:10]:
                    kind = "📁" if "folder" in item else "📄"
                    size = item.get("size", 0)
                    children = item.get("folder", {}).get("childCount", "")
                    children_str = f" ({children} items)" if children else f" ({size} bytes)"
                    print(f"      {kind} {item.get('name', '?')}{children_str}")
                if len(items) > 10:
                    print(f"      ... and {len(items) - 10} more")

    # Graph messages (NOT CSA) — test one channel
    if ch_data:
        channels = ch_data.get("value", [])
        if channels:
            ch = channels[0]
            ch_id = ch["id"]
            print(f"\n--- Graph Messages for '{ch.get('displayName')}' (first 5) ---")
            msg_data = await get(
                token,
                f"{BASE}/teams/{team_id}/channels/{ch_id}/messages?$top=5",
                "graph messages",
            )
            if msg_data:
                for m in msg_data.get("value", []):
                    sender = (m.get("from") or {}).get("user", {}).get("displayName", "?")
                    body = (m.get("body") or {}).get("content", "")[:80]
                    print(f"    • [{sender}]: {body}")

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
