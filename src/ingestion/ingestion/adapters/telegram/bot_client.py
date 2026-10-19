"""Telegram Bot API client — GET-based with token-in-URL-path authentication.

Inherits retry/backoff/rate-limiting from BaseHTTPClient.
Overrides _parse_response to unwrap Telegram's {ok, result} envelope.
Overrides _parse_retry_after to read retry_after from JSON body.

The Telegram Bot API uses a different pattern from standard REST APIs:
- Base URL: https://api.telegram.org/bot{token}/{method}
- All requests are GET (for read operations)
- Token is embedded in the URL path, not in headers
- Responses wrap data in {"ok": bool, "result": ...}
- File downloads use a separate URL pattern: /file/bot{token}/{file_path}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from ingestion.clients.base import BaseHTTPClient
from ingestion.exceptions import PlatformUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChatMeta:
    username: str | None
    type: str


class TelegramBotClient(BaseHTTPClient):
    """Async HTTP client for the Telegram Bot API.

    Handles GET-based calls, token-in-URL injection, and Telegram-specific error detection.
    """

    def __init__(
        self,
        bot_token: str,
        api_base_url: str = "https://api.telegram.org",
        max_retries: int = 3,
        timeout: int = 30,
        requests_per_second: float = 1.0,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._api_base_url = api_base_url.rstrip("/")
        self._bot_url = f"{self._api_base_url}/bot{bot_token}"
        self._chat_meta: dict[int, ChatMeta] = {}

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        )

        super().__init__(
            max_retries=max_retries,
            timeout=timeout,
            requests_per_second=requests_per_second,
            sleep_func=sleep_func,
            client=client,
        )

    @property
    def _platform_name(self) -> str:
        return "telegram"

    def _parse_response(self, response: httpx.Response, *, url: str) -> Any:
        """Unwrap Telegram's {ok: true, result: ...} envelope."""
        data = response.json()
        if data.get("ok") is True:
            return data["result"]

        description = data.get("description", "Unknown error")
        error_code = data.get("error_code", 0)
        raise PlatformUnavailableError(
            platform="telegram",
            message=f"Telegram API error on {url} ({error_code}): {description}",
        )

    def _parse_retry_after(self, response: httpx.Response) -> float:
        """Parse retry_after from Telegram 429 response body or headers."""
        try:
            data = response.json()
            parameters = data.get("parameters", {})
            if "retry_after" in parameters:
                return float(parameters["retry_after"])
        except Exception:
            pass

        raw = response.headers.get("Retry-After", "")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        return 5.0

    async def _call(self, method: str, params: dict | None = None) -> Any:
        """Call a Telegram Bot API method."""
        url = f"{self._bot_url}/{method}"
        return await self._request("GET", url, params=params)

    async def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 0,
        limit: int = 100,
    ) -> list[dict]:
        """Poll for new updates via getUpdates."""
        params: dict[str, int] = {"timeout": timeout, "limit": limit}
        if offset is not None:
            params["offset"] = offset
        return await self._call("getUpdates", params)

    async def get_me(self) -> dict:
        """Get bot info via getMe. Used for test_connection."""
        return await self._call("getMe")

    async def get_chat_meta(self, chat_id: int) -> ChatMeta:
        """Return cached username/type metadata for one chat."""
        cached = self._chat_meta.get(chat_id)
        if cached is not None:
            return cached

        result = await self._call("getChat", {"chat_id": chat_id})
        meta = ChatMeta(
            username=str(result.get("username") or "") or None,
            type=str(result.get("type", "")),
        )
        self._chat_meta[chat_id] = meta
        return meta

    def build_file_url(self, file_path: str) -> str:
        """Build the download URL for a file obtained via getFile."""
        return f"{self._api_base_url}/file/bot{self._bot_token}/{file_path}"
