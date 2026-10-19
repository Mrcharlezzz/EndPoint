"""Request-ID middleware: thread a correlation ID through every log line.

Reads the inbound ``X-Request-ID`` header (if present and well-formed),
otherwise mints a fresh ``uuid4().hex``. The chosen value is stored in
a :class:`contextvars.ContextVar` so any code path that calls
:mod:`structlog`'s contextvars-bound logger picks it up automatically.
The same value is echoed back on the response under ``X-Request-ID`` so
clients (and proxies) can stitch end-to-end traces.

Inbound IDs are validated against :data:`_INBOUND_PATTERN` to prevent
header-based log injection: an attacker who controls the proxy could
otherwise send a multi-line value that splits the JSON log line. We
reject anything that isn't pure hex within a sane length window and
fall back to a generated ID.

WebSocket handshakes get an ID too, bound at connect time. The cookie
auth used by the daemon doesn't carry a request scope through the WS
lifetime so the ID is per-connection rather than per-message; that's
fine for the audit trail (every WS message implicitly inherits the
connection's correlation ID via the contextvar binding).

Background tasks spawned with :func:`asyncio.create_task` inherit the
contextvar automatically (Python 3.7+ asyncio behaviour). Workers
launched via :func:`asyncio.to_thread` ALSO inherit the contextvar in
Python 3.12+ — ``asyncio.to_thread`` copies the calling
:class:`contextvars.Context` into the worker thread before invoking
the callable. This module exports :data:`request_id_var` for callers
that schedule work onto a raw :class:`concurrent.futures` pool (no
auto-context-copy there) or that need to re-bind the ID at a
different boundary (e.g. consumer of a shared queue). Code that
genuinely needs thread-isolated bindings should wrap the worker call
with ``contextvars.copy_context().run(...)`` and unset the var inside
the copy before invoking the worker.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING

from structlog.contextvars import bind_contextvars, unbind_contextvars

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


# Header name pinned to the conventional spelling. Starlette normalises
# inbound names to lowercase so the comparison is case-insensitive.
_HEADER_NAME = "x-request-id"
_HEADER_NAME_BYTES = _HEADER_NAME.encode("ascii")

# Inbound IDs must be hex-only (case-insensitive) within a sane length
# window. Tight enough to reject log-injection payloads (newlines,
# semicolons, quotes) and loose enough to accept tracer formats like
# OpenTelemetry's 32-char hex trace IDs. uuid4().hex is 32 chars; we
# allow 16-128 so callers that prefix with a service name still fit.
_MIN_LEN = 16
_MAX_LEN = 128
_INBOUND_PATTERN = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)

# Module-level contextvar exported for background-task propagation.
# Default is empty string so a structlog binding under the var renders
# as ``request_id=""`` outside of a request scope (rather than raising
# LookupError, which would surface as a noisy log warning).
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class RequestIdMiddleware:
    """ASGI middleware: bind a request ID to every HTTP and WS scope.

    Installs FIRST in the middleware stack so every later layer (rate
    limit, size limit, CORS, route handler, structlog calls inside the
    handler) sees the bound ID. The bind is undone in the ``finally``
    so contextvars don't leak across requests on the same task.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        # The lifespan protocol fires once per process on startup and
        # shutdown; it has no per-request semantics so binding an ID
        # there would be misleading. Pass through untouched.
        if scope_type not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return

        request_id = _resolve_request_id(scope)
        token = request_id_var.set(request_id)
        bind_contextvars(request_id=request_id)
        try:
            if scope_type == "websocket":
                # Starlette doesn't surface a clean response phase for
                # WS handshakes; the upgrade headers ship inside the
                # handler's accept() call. Skip the response-rewrite
                # path; the contextvar binding is enough for log
                # correlation.
                await self._app(scope, receive, send)
                return

            await self._app(
                scope,
                receive,
                _wrap_send_with_header(send, request_id=request_id),
            )
        finally:
            unbind_contextvars("request_id")
            request_id_var.reset(token)


def _resolve_request_id(scope: Scope) -> str:
    """Pick the ID for this request: inbound header (if valid) or a fresh hex.

    Validation prevents log-injection: a header containing a newline
    would otherwise break the JSON log format and corrupt downstream
    aggregators. ``uuid4().hex`` (no dashes) plays well with Prometheus
    label filters and Grafana queries that don't escape ``-``.
    """
    headers = scope.get("headers") or ()
    for name, value in headers:
        if name != _HEADER_NAME_BYTES:
            continue
        try:
            decoded = value.decode("ascii")
        except UnicodeDecodeError:
            break
        if _is_valid_inbound_id(decoded):
            return decoded
        break
    return uuid.uuid4().hex


def _is_valid_inbound_id(candidate: str) -> bool:
    """Inbound IDs must be hex within the length window. Anything else
    (newlines, control chars, dashed UUIDs from older clients) is
    rejected and the middleware mints a fresh ID instead."""
    if not _MIN_LEN <= len(candidate) <= _MAX_LEN:
        return False
    return _INBOUND_PATTERN.match(candidate) is not None


def _wrap_send_with_header(send: Send, *, request_id: str) -> Send:
    """Return a ``send`` callable that injects ``X-Request-ID`` into
    the outbound response start message.

    Only the ``http.response.start`` event carries headers in ASGI;
    every later message (body chunks, trailers) passes through
    untouched. A handler is allowed to set its own ``X-Request-ID``
    (e.g. a downstream service that wants its own ID echoed), but the
    value is re-validated against the same hex-only / 16-128 length
    rule used for inbound IDs. An invalid handler-set value is dropped
    and replaced with the middleware-generated ID. This closes a
    response-header-injection vector: a future handler that reflects
    user input into ``X-Request-ID`` would otherwise let an attacker
    smuggle CRLF / control chars into the response, splitting the
    header block (CWE-113) and poisoning downstream log aggregators.
    """
    header_lower = _HEADER_NAME.encode("ascii")
    header_value = request_id.encode("ascii")

    async def _send(message: Message) -> None:
        if message.get("type") == "http.response.start":
            raw_headers = list(message.get("headers") or [])
            kept: list[tuple[bytes, bytes]] = []
            handler_value: bytes | None = None
            for name, value in raw_headers:
                if name == header_lower:
                    # Capture the first handler-set value; drop any
                    # duplicates to avoid emitting the header twice.
                    if handler_value is None:
                        handler_value = value
                    continue
                kept.append((name, value))
            if handler_value is not None and _is_safe_response_id(handler_value):
                kept.append((header_lower, handler_value))
            else:
                kept.append((header_lower, header_value))
            message = {**message, "headers": kept}
        await send(message)

    return _send


def _is_safe_response_id(value: bytes) -> bool:
    """Return True if a handler-set ``X-Request-ID`` byte value is safe
    to echo verbatim. Same hex-only / length window as the inbound
    validator so a handler can't accidentally relax the contract."""
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError:
        return False
    return _is_valid_inbound_id(decoded)
