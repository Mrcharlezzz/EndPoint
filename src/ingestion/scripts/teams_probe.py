"""Quick probe script to test Teams API endpoints and find where things hang.

Usage: .venv/bin/python scripts/teams_probe.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

# Load credentials
CREDS_PATH = Path.home() / ".config" / "ingest" / "credentials.json"


def load_creds() -> dict:
    if not CREDS_PATH.exists():
        print(f"No credentials at {CREDS_PATH}")
        sys.exit(1)
    data = json.loads(CREDS_PATH.read_text())
    teams = data.get("teams", {})
    if not teams.get("graph_token"):
        print("No graph_token in teams credentials")
        sys.exit(1)
    return teams


async def probe(url: str, token: str, label: str, timeout: float = 10) -> dict | None:
    """Hit a single URL and report timing + status."""
    print(f"\n--- {label} ---")
    print(f"  URL: {url[:100]}")
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"}
            )
        elapsed = time.monotonic() - start
        print(f"  Status: {resp.status_code} ({elapsed:.2f}s)")

        if resp.status_code == 200:
            data = resp.json()
            if "value" in data:
                print(f"  Items: {len(data['value'])}")
                if data.get("@odata.nextLink"):
                    print(f"  Has next page: yes")
                for item in data["value"][:3]:
                    name = item.get("displayName") or item.get("name") or item.get("id", "?")
                    print(f"    • {name}")
                if len(data["value"]) > 3:
                    print(f"    ... and {len(data['value']) - 3} more")
            elif "replyChains" in data:
                chains = data.get("replyChains", [])
                msg_count = sum(len(c.get("messages", [])) for c in chains)
                print(f"  Reply chains: {len(chains)}, Messages: {msg_count}")
                print(f"  hasMore: {data.get('hasMore')}")
                print(f"  continuationToken: {'yes' if data.get('continuationToken') else 'no'}")
            else:
                # Single object (like /me)
                name = data.get("displayName") or data.get("userPrincipalName") or "?"
                print(f"  Result: {name}")
            return data
        else:
            print(f"  Body: {resp.text[:200]}")
            return None
    except httpx.TimeoutException:
        elapsed = time.monotonic() - start
        print(f"  TIMEOUT after {elapsed:.2f}s")
        return None
    except Exception as e:
        elapsed = time.monotonic() - start
        print(f"  ERROR: {e} ({elapsed:.2f}s)")
        return None


async def main() -> None:
    creds = load_creds()
    graph_token = creds["graph_token"]
    csa_token = creds.get("csa_token")
    team_ids = creds.get("team_ids", [])

    BASE = "https://graph.microsoft.com/v1.0"
    CSA_BASE = "https://teams.cloud.microsoft/api/csa/emea/api/v2"

    # 1. Test /me
    await probe(f"{BASE}/me", graph_token, "/me")

    # 2. Test /me/joinedTeams
    teams_data = await probe(f"{BASE}/me/joinedTeams", graph_token, "/me/joinedTeams")

    if not team_ids and teams_data:
        team_ids = [t["id"] for t in teams_data.get("value", []) if t.get("id")]

    if not team_ids:
        print("\nNo team IDs available. Exiting.")
        return

    # 3. Test channels for first team
    team_id = team_ids[0]
    print(f"\n=== Testing team: {team_id} ===")

    channels_data = await probe(
        f"{BASE}/teams/{team_id}/channels", graph_token, f"channels for team {team_id[:8]}..."
    )

    # 4. Test CSA messages for first channel (if we have csa_token)
    if channels_data and csa_token:
        channels = channels_data.get("value", [])
        if channels:
            ch = channels[0]
            ch_id = ch["id"]
            ch_name = ch.get("displayName", "?")
            print(f"\n=== Testing CSA for channel: {ch_name} ===")
            await probe(
                f"{CSA_BASE}/teams/{ch_id}/channels/{ch_id}?filterSystemMessage=true&pageSize=5",
                csa_token,
                f"CSA messages for {ch_name}",
            )
    elif not csa_token:
        print("\n  [no csa_token — skipping CSA test]")

    # 5. Test drives
    await probe(
        f"{BASE}/groups/{team_id}/drives", graph_token, f"drives for team {team_id[:8]}..."
    )

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
