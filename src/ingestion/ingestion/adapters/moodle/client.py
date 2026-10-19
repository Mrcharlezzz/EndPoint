"""Moodle REST API client — POST-based with token-in-body authentication.

Inherits retry/backoff/rate-limiting from BaseHTTPClient.
Overrides _parse_response to detect Moodle's error-in-200-body pattern.

Moodle's Web Services API differs from typical REST APIs:
- All requests are POST to /webservice/rest/server.php
- Auth token is a POST body parameter, not a Bearer header
- Errors return HTTP 200 with error fields in JSON body
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from ingestion.clients.base import BaseHTTPClient
from ingestion.exceptions import (
    InsufficientScopeError,
    PlatformUnavailableError,
    TokenExpiredError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_MOODLE_TOKEN_ERROR_CODES = frozenset(
    {
        "invalidtoken",
        "accessexception",
        "invalidlogin",
        "sitemaintenance",
    }
)

_MOODLE_PERMISSION_ERROR_CODES = frozenset(
    {
        "requireloginerror",
        "nopermissions",
        "webservicenotavailable",
        "servicenotavailable",
        "accessdenied",
    }
)


class MoodleRestClient(BaseHTTPClient):
    """Async HTTP client for the Moodle Web Services REST API.

    Handles POST-based calls, token injection, and Moodle-specific error detection.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        max_retries: int = 3,
        timeout: int = 30,
        requests_per_second: float = 10.0,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._endpoint = f"{self._base_url}/webservice/rest/server.php"

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
        return "moodle"

    def _parse_response(self, response: httpx.Response, *, url: str) -> Any:
        """Detect Moodle error responses embedded in 200 OK bodies."""
        data = response.json()
        if isinstance(data, dict):
            error_code = data.get("errorcode", "")
            if error_code:
                message = data.get("message", str(data.get("exception", "")))
                if error_code in _MOODLE_TOKEN_ERROR_CODES:
                    raise TokenExpiredError(f"Moodle token error ({error_code}): {message}")
                if error_code in _MOODLE_PERMISSION_ERROR_CODES:
                    raise InsufficientScopeError(
                        resource=url,
                        message=f"Moodle permission error ({error_code}): {message}",
                    )
                raise PlatformUnavailableError(
                    platform="moodle",
                    message=f"Moodle API error ({error_code}): {message}",
                )
        return data

    async def call(self, wsfunction: str, **params: object) -> dict | list:
        """Call a Moodle Web Service function.

        Args:
            wsfunction: The function name (e.g. 'core_enrol_get_users_courses').
            **params: Additional POST parameters.

        Returns:
            Parsed JSON response (dict or list).
        """
        form_data: dict[str, object] = {
            "wstoken": self._token,
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
            **params,
        }
        return await self._request("POST", self._endpoint, data=form_data)
