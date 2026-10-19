"""Fetch channel messages from Teams CSA internal API.

Decision #2: CSA is the primary source for messages (frequent fetches).
Decision #7: Per-channel continuationToken cursor + last_sync_at fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ingestion.adapters.teams.endpoints import csa_channel_messages

if TYPE_CHECKING:
    from ingestion.clients.http import AsyncHTTPClient

logger = logging.getLogger(__name__)

_DEFAULT_PAGE_SIZE = 50


async def fetch_channel_messages(
    client: AsyncHTTPClient,
    thread_id: str,
    channel_id: str,
    *,
    page_size: int = _DEFAULT_PAGE_SIZE,
    continuation_token: str | None = None,
    base_url: str = "https://teams.cloud.microsoft/api/csa/emea/api/v2",
) -> tuple[list[dict], str | None]:
    """Fetch all messages in a channel via CSA v2 API.

    Returns:
        (messages, last_continuation_token) — token is None if no more pages.
    """
    url = csa_channel_messages(thread_id, channel_id, base_url=base_url)
    params: dict[str, str] = {
        "filterSystemMessage": "true",
        "pageSize": str(page_size),
    }
    if continuation_token:
        params["continuationToken"] = continuation_token

    all_messages: list[dict] = []
    last_token: str | None = None

    while True:
        data = await client.get(url, params=params)

        for chain in data.get("replyChains", []):
            all_messages.extend(chain.get("messages", []))

        has_more = data.get("hasMore", False)
        new_token = data.get("continuationToken")

        if has_more and new_token:
            last_token = new_token
            params = {
                "filterSystemMessage": "true",
                "pageSize": str(page_size),
                "continuationToken": new_token,
            }
        else:
            last_token = None
            break

    return all_messages, last_token
