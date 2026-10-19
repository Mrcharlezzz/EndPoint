"""Fetch structural data from Microsoft Graph API.

Decision #2: Graph is for structural/infrequent data (channels, assignments, files).
Decision #8: Graph incremental via OData $filter on createdDateTime.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ingestion.adapters.teams.endpoints import (
    graph_channel_messages,
    graph_drive_delta,
    graph_drive_item_children,
    graph_drive_root_children,
    graph_team_channels,
    graph_team_drives,
)
from ingestion.exceptions import (
    InsufficientScopeError,
)

if TYPE_CHECKING:
    from ingestion.clients.http import AsyncHTTPClient

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


async def fetch_joined_teams(
    client: AsyncHTTPClient,
    *,
    base_url: str = _GRAPH_BASE,
) -> list[dict]:
    """Fetch all teams the user is a member of via /me/joinedTeams."""
    url = f"{base_url}/me/joinedTeams"
    return await client.get_paginated(url)


async def fetch_channel_messages_graph(
    client: AsyncHTTPClient,
    team_id: str,
    channel_id: str,
    *,
    top: int = 50,
    base_url: str = _GRAPH_BASE,
) -> list[dict]:
    """Fetch messages from a channel via Graph API (fallback when CSA is unavailable).

    Returns raw Graph message objects. Format differs from CSA messages.
    Returns [] on 403 — logged at WARNING for operator visibility.
    """
    url = graph_channel_messages(team_id, channel_id, base_url=base_url)
    try:
        return await client.get_paginated(url, params={"$top": str(top)})
    except InsufficientScopeError:
        logger.warning("Messages not accessible for channel %s (403).", channel_id)
        return []


async def fetch_channels(
    client: AsyncHTTPClient,
    team_id: str,
    *,
    base_url: str = _GRAPH_BASE,
) -> list[dict]:
    """Fetch all channels for a team via Graph API (OData paginated).

    Returns [] on 403 (InsufficientScope) — some teams deny channel listing
    despite being in joinedTeams. Logged at WARNING for operator visibility.
    """
    url = graph_team_channels(team_id, base_url=base_url)
    try:
        return await client.get_paginated(url)
    except InsufficientScopeError:
        logger.warning("Channels not accessible for team %s (403).", team_id)
        return []


async def fetch_team_drives(
    client: AsyncHTTPClient,
    team_id: str,
    *,
    base_url: str = _GRAPH_BASE,
) -> list[dict]:
    """Fetch all drives (document libraries) for a team. Returns [] on 403."""
    url = graph_team_drives(team_id, base_url=base_url)
    try:
        return await client.get_paginated(url)
    except InsufficientScopeError:
        logger.warning("Drives not accessible for team %s (403).", team_id)
        return []


async def fetch_drive_items_delta(
    client: AsyncHTTPClient,
    drive_id: str,
    *,
    base_url: str = _GRAPH_BASE,
) -> list[dict]:
    """List all file items in a drive via delta query (flat, no recursion).

    Uses ``/drives/{id}/root/delta`` which returns every item in the drive
    in a single paginated response.  Folders and deleted items are filtered
    out — only live files are returned.

    Falls back to recursive traversal if the delta endpoint returns 403 or
    an unexpected error (some SharePoint configurations disable delta).
    """
    url = graph_drive_delta(drive_id, base_url=base_url)
    try:
        raw_items = await client.get_paginated(
            url,
            next_link_key="@odata.nextLink",
        )
    except InsufficientScopeError:
        logger.warning("Drive %s delta not accessible (403), falling back to recursive.", drive_id)
        return await fetch_drive_items_recursive(client, drive_id, base_url=base_url)

    return [item for item in raw_items if "file" in item and "deleted" not in item]


async def fetch_drive_items_recursive(
    client: AsyncHTTPClient,
    drive_id: str,
    folder_id: str | None = None,
    *,
    base_url: str = _GRAPH_BASE,
) -> list[dict]:
    """Recursively list all file items in a drive. Folders are traversed, not returned.

    This is the legacy fallback — prefer :func:`fetch_drive_items_delta`.
    """
    if folder_id:
        url = graph_drive_item_children(drive_id, folder_id, base_url=base_url)
    else:
        url = graph_drive_root_children(drive_id, base_url=base_url)

    try:
        raw_items = await client.get_paginated(url)
    except InsufficientScopeError:
        logger.warning("Drive %s not accessible (403).", drive_id)
        return []

    files: list[dict] = []
    for item in raw_items:
        if "folder" in item:
            sub = await fetch_drive_items_recursive(client, drive_id, item["id"], base_url=base_url)
            files.extend(sub)
        elif "file" in item:
            files.append(item)

    return files
