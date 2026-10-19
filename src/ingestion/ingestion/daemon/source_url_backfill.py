"""Source URL repair helpers for ingested rows."""

from __future__ import annotations

import json
import re
from urllib.parse import quote, urlencode

TEAMS_CSA_EXTERNAL_ID_PREFIX = "teams_csa_"
TEAMS_MESSAGE_BASE_URL = "https://teams.microsoft.com/l/message"
TEAMS_CHAT_CONTEXT = {"contextType": "chat"}
TELEGRAM_EXTERNAL_ID_RE = re.compile(r"^tg_(-?\d+)_(\d+)$")
TELEGRAM_MESSAGE_BASE_URL = "https://t.me/c"


def teams_csa_message_id(external_id: str) -> str | None:
    """Return the CSA message id encoded in a Teams external id."""
    if not external_id.startswith(TEAMS_CSA_EXTERNAL_ID_PREFIX):
        return None
    message_id = external_id.removeprefix(TEAMS_CSA_EXTERNAL_ID_PREFIX).strip()
    return message_id or None


def build_teams_csa_source_url(external_id: str, chat_id: str | None) -> str | None:
    """Build a Teams deep link for a CSA chat message row.

    CSA rows store the Teams message id in ``external_id`` and the Teams
    conversation id in ``course_external_id``. The link format follows the
    Teams chat-message deep link contract.
    """
    message_id = teams_csa_message_id(external_id)
    if message_id is None:
        return None
    if chat_id is None or not chat_id.strip():
        return None

    query = urlencode(
        {"context": json.dumps(TEAMS_CHAT_CONTEXT, separators=(",", ":"))},
        quote_via=quote,
    )
    return (
        f"{TEAMS_MESSAGE_BASE_URL}/{quote(chat_id.strip(), safe='')}/"
        f"{quote(message_id, safe='')}?{query}"
    )


def telegram_message_ids(external_id: str) -> tuple[str, str] | None:
    """Return the Telegram chat id and message id encoded in an external id."""
    match = TELEGRAM_EXTERNAL_ID_RE.fullmatch(external_id)
    if match is None:
        return None
    return match.group(1), match.group(2)


def build_telegram_source_url(external_id: str) -> str | None:
    """Build a Telegram message URL from a deterministic Telegram external id."""
    parsed = telegram_message_ids(external_id)
    if parsed is None:
        return None

    chat_id, message_id = parsed
    internal_chat_id = chat_id[4:] if chat_id.startswith("-100") else chat_id.lstrip("-")
    if not internal_chat_id:
        return None
    return f"{TELEGRAM_MESSAGE_BASE_URL}/{internal_chat_id}/{message_id}"
