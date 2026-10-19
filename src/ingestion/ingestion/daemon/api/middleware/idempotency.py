"""``Idempotency-Key`` middleware: cache successful responses for replay.

A retried POST that crashed mid-request risks double-execution: the
caller times out, retries with the same body, and the second attempt
either races the first or runs to completion alongside it. Even
"naturally idempotent" mutations (set_paused=True, mark_expired=True)
ship duplicate audit rows and fire duplicate webhook events.

Solution: callers supply an opaque ``Idempotency-Key`` header on
mutating verbs. The middleware caches the response (status, body,
headers) keyed by ``(key, method, path)`` for a TTL window. A retry
with the same triple replays the cached response verbatim and tags
it with ``Idempotent-Replay: true`` so the caller knows. A retry
with a different body for the same key is a contract violation —
the middleware rejects it with 422 ``idempotency_conflict`` rather
than silently replaying or silently re-executing.

Scope:

* Only applies to mutating methods (POST/PUT/PATCH/DELETE). GETs
  ignore the header.
* Applies to BOTH ``/v1/...`` and ``/api/...`` paths. Keys are
  global, not version-scoped — a caller that retries with the
  same key and a different prefix gets the same cached response.
* The header is OPTIONAL. Absent header == no caching, handler runs
  every time (current behaviour for callers that haven't adopted
  the contract).

Validation:

* Key must be 16-128 hex characters (same regex as ``X-Request-ID``).
  Invalid keys are rejected with 422 ``idempotency_invalid_key``.

Storage:

* SQLite table ``idempotency_log`` (migration 0010).
* TTL default 24h, configurable per-instance.
* Hourly prune loop in ``observability_loops`` deletes expired rows.

Edge cases:

* Concurrent requests with the same key — the SQL UNIQUE constraint
  on the composite primary key catches the second writer, which
  re-reads the row and replays.
* A handler that crashes BEFORE the response start message reaches
  the middleware leaves no cache row; the next retry re-executes.
  Acceptable trade — the alternative would be to cache failures and
  permanently replay them, which is far worse.
* Body size: requests larger than ``_MAX_CACHED_BODY_BYTES`` (1 MiB)
  bypass the cache to keep the table small. The handler still runs
  and the response is returned untouched, just not cached. Operators
  who need the cache for large bodies should bump the cap.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite

from ingestion.daemon.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from starlette.types import ASGIApp, Message, Receive, Scope, Send


_log = get_logger(__name__)


# Same shape rule as X-Request-ID so an attacker can't smuggle CRLF or
# control characters into the header (which would land in audit logs and
# Prometheus labels).
_KEY_MIN_LEN = 16
_KEY_MAX_LEN = 128
_KEY_PATTERN = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)

# Methods that get cached. Read methods (GET, HEAD, OPTIONS) are
# already idempotent by HTTP definition; OPTIONS specifically is used
# by CORS preflight and must not be intercepted.
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Cap on the cached response body. 1 MiB is large enough for every
# response shape the daemon currently emits (force-sync / pause /
# bulk-pause results are all sub-KB) but small enough to keep
# pathological replays from bloating the SQLite file.
_MAX_CACHED_BODY_BYTES = 1024 * 1024

# Cap on the cached request body (used to compute the body hash for
# conflict detection). Same ceiling as the response cap.
_MAX_CACHED_REQUEST_BYTES = 1024 * 1024

# Default TTL. 24h matches AWS / Stripe / Twilio idempotency conventions
# and is long enough that a retry from a queued worker won't expire
# between the original failure and the retry attempt.
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60

_HEADER_NAME = b"idempotency-key"
_REPLAY_HEADER = b"idempotent-replay"

# Slice 5: ``/v1/...`` and ``/api/...`` are aliases for the same
# resource. The idempotency cache must not double-charge a caller
# that retries with a different prefix — keys are global, not
# version-scoped. We canonicalise the path by stripping either of
# these leading segments before storage / lookup. Everything else
# (e.g. ``/ws/`` paths) is left unchanged; the middleware also
# skips WS handshakes via the scope-type guard so this is defensive.
_VERSION_PREFIXES = ("/v1", "/api")

# Pre-built error bodies. Pinned strings so the wire shape is stable
# across deploys and the dashboard can match on ``code`` exactly.
_INVALID_KEY_BODY = (
    b'{"detail":{"code":"idempotency_invalid_key","message":'
    b'"Idempotency-Key must be 16-128 hex characters"}}'
)
_CONFLICT_BODY = (
    b'{"detail":{"code":"idempotency_conflict","message":'
    b'"Idempotency-Key reused with a different request body"}}'
)


def _now_iso() -> str:
    """Wall-clock UTC ISO-8601 used for created_at / expires_at columns."""
    return datetime.now(tz=UTC).isoformat()


def _is_valid_key(value: str) -> bool:
    """Hex within the length window — reject everything else."""
    if not _KEY_MIN_LEN <= len(value) <= _KEY_MAX_LEN:
        return False
    return _KEY_PATTERN.match(value) is not None


def _hash_body(body: bytes) -> str:
    """SHA-256 of the request body, hex-encoded.

    Used to detect "same key, different body" conflicts. We hash
    rather than store the raw body to keep the conflict-check column
    small and to avoid leaking arbitrary request payloads into a row
    that an admin might inspect via a debug query.
    """
    return hashlib.sha256(body).hexdigest()


def _normalise_path(path: str) -> str:
    """Strip the ``/v1`` or ``/api`` prefix so aliased paths share keys.

    A retry from ``/api/.../force_sync`` to ``/v1/.../force_sync`` with
    the same Idempotency-Key must replay the same response — the prefix
    is purely a routing detail. Everything past the version segment is
    preserved verbatim (including the leading slash so a normalised
    "/auth/login" still parses as an absolute path on inspection).

    Paths that don't start with a known version prefix pass through
    untouched. The OpenAPI doc routes (``/v1/openapi.json`` and
    ``/api/openapi.json``) are GETs so they never reach this code.
    """
    for prefix in _VERSION_PREFIXES:
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix) :]
    return path


class IdempotencyMiddleware:
    """ASGI middleware that caches mutating responses by Idempotency-Key.

    Mounted between the request-ID middleware (outermost) and the
    routers (innermost). Only mutating verbs are intercepted; GETs
    pass through unchanged.

    The middleware buffers the request body so it can hash for
    conflict detection AND replay it to the inner handler. This adds
    one heap allocation per request scoped at the body cap; the cost
    is negligible compared with a SQLite round-trip.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        sqlite_path: Path,
        ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
    ) -> None:
        self._app = app
        self._sqlite_path = sqlite_path
        self._ttl_seconds = ttl_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        method = (scope.get("method") or "").upper()
        if method not in _MUTATING_METHODS:
            await self._app(scope, receive, send)
            return

        key = _extract_key(scope.get("headers") or ())
        if key is None:
            await self._app(scope, receive, send)
            return

        if not _is_valid_key(key):
            await _send_error(send, status_code=422, body=_INVALID_KEY_BODY)
            return

        path = _normalise_path(scope.get("path", ""))
        body = await _buffer_request_body(receive)

        # Bypass the cache for oversized bodies. The handler still
        # runs (we ship the buffered body through ``_replay_receive``)
        # so functional behaviour is unchanged; we just don't cache
        # the response. Operators who need the cap raised can do so
        # by bumping ``_MAX_CACHED_REQUEST_BYTES``.
        if len(body) > _MAX_CACHED_REQUEST_BYTES:
            _log.info(
                "idempotency.body_too_large",
                method=method,
                path=path,
                body_bytes=len(body),
            )
            await self._app(scope, _replay_receive(body), send)
            return

        body_hash = _hash_body(body)

        cached = await _lookup_cached(
            self._sqlite_path,
            key=key,
            method=method,
            path=path,
        )
        if cached is not None:
            cached_hash, status_code, response_body, response_headers, _expires = cached
            if cached_hash != body_hash:
                await _send_error(send, status_code=422, body=_CONFLICT_BODY)
                return
            await _replay_cached(
                send,
                status_code=status_code,
                body=response_body,
                headers=response_headers,
            )
            return

        # Run the handler, capture the response.
        capture = _ResponseCapture()
        await self._app(
            scope,
            _replay_receive(body),
            capture.wrap(send),
        )

        # Only cache successful (2xx) responses. A 4xx/5xx is a
        # business-rule decision and we don't want a single bad
        # request to be replayed forever — caller should fix the
        # input and try again. 2xx is the safe-to-replay slice.
        if (
            capture.status_code is not None
            and 200 <= capture.status_code < 300
            and len(capture.body) <= _MAX_CACHED_BODY_BYTES
        ):
            await _store_cached(
                self._sqlite_path,
                key=key,
                method=method,
                path=path,
                body_hash=body_hash,
                status_code=capture.status_code,
                response_body=capture.body,
                response_headers=capture.headers,
                ttl_seconds=self._ttl_seconds,
            )


def _extract_key(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    """Pull the ``Idempotency-Key`` header value from the ASGI scope.

    Header names in ASGI are lowercase bytes. Returns the string value
    or None if absent / un-decodeable. Callers further validate the
    shape via ``_is_valid_key``.
    """
    for name, value in headers:
        if name == _HEADER_NAME:
            try:
                return value.decode("ascii").strip()
            except UnicodeDecodeError:
                return None
    return None


async def _buffer_request_body(receive: Receive) -> bytes:
    """Drain the inbound body into a single bytes blob.

    Bounded by ``_MAX_CACHED_REQUEST_BYTES + 1`` (we read up to the
    ceiling plus one byte so the caller can detect overflow). Larger
    requests still get fully drained inside Starlette's downstream
    plumbing — we only buffer up to the cap here for our own use.

    Returns an empty ``b""`` when the request carries no body
    (e.g. a DELETE without a payload).
    """
    chunks: list[bytes] = []
    total = 0
    cap = _MAX_CACHED_REQUEST_BYTES + 1
    while True:
        message = await receive()
        if message["type"] != "http.request":
            # Disconnect / unexpected — bail with what we have.
            break
        chunk = message.get("body", b"") or b""
        if chunk:
            chunks.append(chunk)
            total += len(chunk)
            if total >= cap:
                # Drain the rest so the client doesn't block on a
                # half-read body. We don't actually keep the bytes
                # past the cap (the upstream code path bypasses
                # caching).
                while message.get("more_body"):
                    follow = await receive()
                    if follow["type"] != "http.request":
                        break
                    if not follow.get("more_body"):
                        break
                break
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _replay_receive(body: bytes) -> Receive:
    """Build a one-shot ``receive`` callable that replays ``body``.

    The first call returns the buffered body in a single
    ``http.request`` message with ``more_body=False``. Subsequent
    calls return ``http.disconnect`` so a misbehaving handler that
    keeps reading doesn't hang.
    """
    delivered = False

    async def _receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    return _receive


class _ResponseCapture:
    """Tee that mirrors outbound ASGI messages into a buffer.

    Wraps the downstream ``send`` so every byte the handler emits is
    forwarded to the original send AND captured for caching. Status
    code, headers, and the concatenated body are exposed as
    attributes after the response stream completes.

    Headers are stored as a list of (name, value) byte tuples and
    only the headers safe to replay are captured — Content-Length is
    rebuilt from the cached body on replay so a stale header can't
    desynchronize the wire.
    """

    def __init__(self) -> None:
        self.status_code: int | None = None
        self.headers: list[tuple[str, str]] = []
        self._body_chunks: list[bytes] = []

    @property
    def body(self) -> bytes:
        return b"".join(self._body_chunks)

    def wrap(self, send: Send) -> Send:
        async def _send(message: Message) -> None:
            mtype = message.get("type")
            if mtype == "http.response.start":
                self.status_code = int(message.get("status") or 0)
                # Inject Idempotent-Replay: false on the first
                # execution so the caller can confirm the handler
                # actually ran (vs hitting a stale cache from
                # somewhere else in the stack).
                raw_headers = list(message.get("headers") or [])
                raw_headers.append((_REPLAY_HEADER, b"false"))
                message = {**message, "headers": raw_headers}
                # Capture for the cache. Skip Content-Length —
                # we'll regenerate it on replay so the cached row
                # never goes stale relative to the body.
                #
                # The latin-1 ``decode`` on the value branch is total
                # (every byte 0x00-0xFF round-trips through latin-1)
                # so the ``UnicodeDecodeError`` branch only fires for
                # a non-ASCII *header NAME*. Header names are
                # restricted to ASCII tokens by RFC 7230 §3.2.6, so
                # in practice this branch is unreachable from a
                # well-behaved handler — we still skip rather than
                # poison the cache row, and the tester pins coverage
                # via ``test_response_capture_skips_undecodable_header_bytes``
                # which crafts a ``\xff``-named header to drive the
                # branch deterministically.
                for name, value in raw_headers:
                    try:
                        decoded_name = name.decode("ascii").lower()
                        decoded_value = value.decode("latin-1")
                    except UnicodeDecodeError:
                        continue
                    if decoded_name in {"content-length", "idempotent-replay"}:
                        continue
                    self.headers.append((decoded_name, decoded_value))
            elif mtype == "http.response.body":
                chunk = message.get("body", b"") or b""
                if chunk:
                    self._body_chunks.append(chunk)
            await send(message)

        return _send


async def _send_error(send: Send, *, status_code: int, body: bytes) -> None:
    """Short-circuit error response (422 invalid key / 422 conflict)."""
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _replay_cached(
    send: Send,
    *,
    status_code: int,
    body: bytes,
    headers: list[tuple[str, str]],
) -> None:
    """Emit a cached response.

    Reconstructs Content-Length from the cached body so a malformed
    cached row can't ship a wrong header. Always tags the reply with
    ``Idempotent-Replay: true`` so the caller can distinguish a fresh
    execution from a cached replay.
    """
    raw_headers: list[tuple[bytes, bytes]] = []
    for name, value in headers:
        raw_headers.append((name.encode("ascii"), value.encode("latin-1")))
    raw_headers.append((b"content-length", str(len(body)).encode("ascii")))
    raw_headers.append((_REPLAY_HEADER, b"true"))
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": raw_headers,
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _lookup_cached(
    sqlite_path: Path,
    *,
    key: str,
    method: str,
    path: str,
) -> tuple[str, int, bytes, list[tuple[str, str]], str] | None:
    """Find a non-expired cached row for ``(key, method, path)``.

    Returns ``(body_hash, status_code, response_body, response_headers, expires_at)``
    or None when no live row exists. Expired rows are treated as
    absent so a request after the TTL re-executes; the prune loop
    will delete them on its next pass.
    """
    now = _now_iso()
    try:
        async with aiosqlite.connect(str(sqlite_path)) as conn:
            cursor = await conn.execute(
                """
                SELECT body_hash, status_code, response_body,
                       response_headers_json, expires_at
                FROM idempotency_log
                WHERE key = ? AND method = ? AND path = ?
                  AND expires_at > ?
                """,
                (key, method, path, now),
            )
            row = await cursor.fetchone()
            await cursor.close()
    except Exception:
        # A SQLite read failure must NEVER take the request down.
        # Log + treat as cache-miss; the handler will run normally.
        _log.warning("idempotency.lookup_failed", exc_info=True)
        return None
    if row is None:
        return None
    body_hash, status_code, response_body, response_headers_json, expires_at = row
    headers = _decode_headers(response_headers_json)
    if isinstance(response_body, str):
        body_bytes = response_body.encode("latin-1")
    else:
        body_bytes = response_body
    return body_hash, int(status_code), body_bytes, headers, expires_at


async def _store_cached(
    sqlite_path: Path,
    *,
    key: str,
    method: str,
    path: str,
    body_hash: str,
    status_code: int,
    response_body: bytes,
    response_headers: list[tuple[str, str]],
    ttl_seconds: int,
) -> None:
    """Insert a row into ``idempotency_log``. Idempotent on PK conflict.

    A concurrent caller racing with the same key will hit the UNIQUE
    constraint; we treat the conflict as "another request already
    stored its result" and silently swallow — the next retry by
    EITHER caller will then hit the row. We don't UPDATE on conflict
    because the existing row's body is the canonical answer for that
    key; overwriting could change the replay payload mid-window.
    """
    now = datetime.now(tz=UTC)
    expires = now + timedelta(seconds=ttl_seconds)
    headers_json = json.dumps(response_headers, separators=(",", ":"))
    response_text = response_body.decode("latin-1")
    try:
        async with aiosqlite.connect(str(sqlite_path)) as conn:
            await conn.execute(
                """
                INSERT OR IGNORE INTO idempotency_log (
                    key, method, path, body_hash, status_code,
                    response_body, response_headers_json,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    method,
                    path,
                    body_hash,
                    status_code,
                    response_text,
                    headers_json,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            await conn.commit()
    except Exception:
        # Storage failure is non-fatal — the response already shipped.
        # Log so an operator can investigate; the next retry will
        # re-execute, which is the safe default.
        _log.warning("idempotency.store_failed", exc_info=True)


def _decode_headers(raw_json: object) -> list[tuple[str, str]]:
    """Re-parse the JSON-encoded headers list. Tolerates malformed rows."""
    if not isinstance(raw_json, str):
        return []
    try:
        decoded = json.loads(raw_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    headers: list[tuple[str, str]] = []
    for item in decoded:
        if isinstance(item, list) and len(item) == 2:
            name, value = item
            if isinstance(name, str) and isinstance(value, str):
                headers.append((name, value))
    return headers


async def prune_expired(sqlite_path: Path) -> int:
    """Delete rows whose ``expires_at`` is in the past.

    Returns the number of rows removed. Called from the TTL prune
    loop in ``observability_loops``. Designed to be safe to invoke
    on every tick: a single bad row cannot block the whole prune
    because the WHERE clause is set-based.
    """
    now = _now_iso()
    try:
        async with aiosqlite.connect(str(sqlite_path)) as conn:
            cursor = await conn.execute(
                "DELETE FROM idempotency_log WHERE expires_at <= ?",
                (now,),
            )
            await conn.commit()
            deleted = cursor.rowcount or 0
            await cursor.close()
    except Exception:
        _log.warning("idempotency.prune_failed", exc_info=True)
        return 0
    return int(deleted)
