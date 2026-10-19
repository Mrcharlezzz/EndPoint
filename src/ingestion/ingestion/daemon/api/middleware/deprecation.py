"""Deprecation header middleware for the ``/api/`` URL alias.

Slice 5 introduces a canonical ``/v1/`` URL prefix and keeps the legacy
``/api/`` paths working as an alias. Clients hitting the legacy paths
must learn (a) the path is deprecated and (b) when it goes away. RFC
8594 standardises both signals:

* ``Deprecation: true`` — the URL is deprecated as of now. The header
  may also carry a date, but ``true`` is the spec-blessed form for
  "currently deprecated".
* ``Sunset: <IMF-fixdate>`` — the date past which the URL is expected
  to disappear. RFC 7231's IMF-fixdate (``Sun, 06 Nov 1994 08:49:37
  GMT``) is the only format the spec admits.

The middleware only rewrites response headers when the inbound request
landed on a path under ``/api/`` (excluding ``/api/openapi.json`` and
``/api/docs`` which are not user-callable endpoints). All other paths
pass through unchanged. The check is path-prefix based — the
middleware does not need to enumerate every route, so adding a new
endpoint under ``/api/...`` automatically picks up the headers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


# Default sunset date for the ``/api/`` alias. Operators can override
# this when constructing the middleware (mostly useful in tests). The
# value is an RFC 7231 IMF-fixdate string and stays stable in env so
# every response under ``/api/...`` carries the same target date.
DEFAULT_SUNSET_DATE = "Mon, 26 Oct 2026 00:00:00 GMT"

# Path prefixes that should NOT receive the deprecation headers even
# when they sit under ``/api/``. ``/api/openapi.json`` and ``/api/docs``
# are FastAPI's auto-generated documentation routes; they're served
# from the canonical doc URL and confusing scrapers with sunset
# headers there has no operator value.
_EXEMPT_PATHS = frozenset(
    {
        "/api/openapi.json",
        "/api/docs",
        "/api/redoc",
    }
)

_DEPRECATION_HEADER = b"deprecation"
_SUNSET_HEADER = b"sunset"
_LINK_HEADER = b"link"


class DeprecationHeaderMiddleware:
    """Add ``Deprecation: true`` + ``Sunset: <date>`` headers on ``/api/`` responses.

    Mounted as a Starlette ASGI middleware. Wraps the outbound
    ``http.response.start`` event to inject two headers when the
    inbound request path begins with ``/api/`` and is not in the
    exempt set. The injection is additive — handler-set values are
    preserved (a handler that wants to set its own ``Sunset`` for a
    specific endpoint wins over the middleware default).

    The ``Link`` header points at the canonical ``/v1/...`` equivalent
    so a curious client can follow the suggested replacement URL via
    standard HTTP chasing — implementing
    ``rel=successor-version`` from RFC 5829 / 8631.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        sunset_date: str = DEFAULT_SUNSET_DATE,
    ) -> None:
        self._app = app
        self._sunset_value = sunset_date.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not _is_legacy_api_path(path):
            await self._app(scope, receive, send)
            return

        await self._app(
            scope,
            receive,
            _wrap_send_with_deprecation(
                send,
                sunset_value=self._sunset_value,
                successor_path=_canonical_successor(path),
            ),
        )


def _is_legacy_api_path(path: str) -> bool:
    """Return True when ``path`` is a deprecated ``/api/...`` URL.

    The check is intentionally simple: prefix match on ``/api/`` and an
    exemption for the FastAPI doc routes. The ``/v1/...`` paths are
    canonical and never match, so they pass through untouched.
    """
    if path in _EXEMPT_PATHS:
        return False
    return path.startswith("/api/") or path == "/api"


def _canonical_successor(legacy_path: str) -> bytes:
    """Map ``/api/...`` to its ``/v1/...`` equivalent for the Link header.

    Bare ``/api`` (no trailing slash) maps to ``/v1`` for completeness.
    The result is encoded to ASCII bytes once so the per-response code
    path doesn't repeat the encode.
    """
    # ``legacy_path`` is either bare ``/api`` or starts with ``/api/`` so
    # slicing off the four leading characters and prepending ``/v1`` is
    # safe in both branches.
    successor = "/v1" if legacy_path == "/api" else "/v1" + legacy_path[len("/api") :]
    return f'<{successor}>; rel="successor-version"'.encode("ascii")


def _wrap_send_with_deprecation(
    send: Send,
    *,
    sunset_value: bytes,
    successor_path: bytes,
) -> Send:
    """Return a ``send`` that injects the deprecation headers on response start.

    Headers are appended to the existing list rather than replacing
    handler-set values — that keeps handler-specific overrides intact
    (e.g. a per-route Sunset that aliases a tighter timeline) while
    still guaranteeing the default trio shows up on every legacy
    response. Duplicates are tolerated: if a handler already set its
    own ``Deprecation`` we don't double-add ours; we only fall back to
    the default when the handler didn't set one.
    """

    async def _send(message: Message) -> None:
        if message.get("type") == "http.response.start":
            raw_headers = list(message.get("headers") or [])
            seen_deprecation = any(name == _DEPRECATION_HEADER for name, _ in raw_headers)
            seen_sunset = any(name == _SUNSET_HEADER for name, _ in raw_headers)
            seen_link = any(name == _LINK_HEADER for name, _ in raw_headers)
            if not seen_deprecation:
                raw_headers.append((_DEPRECATION_HEADER, b"true"))
            if not seen_sunset:
                raw_headers.append((_SUNSET_HEADER, sunset_value))
            if not seen_link:
                raw_headers.append((_LINK_HEADER, successor_path))
            message = {**message, "headers": raw_headers}
        await send(message)

    return _send
