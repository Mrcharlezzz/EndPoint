"""Starlette ASGI middlewares: request-size cap and login rate limit.

Both middlewares are rolled in-process so the daemon stays dependency-light.
The request-size cap uses the ``content-length`` header where present and
otherwise streams the body through a guarded receive callable that counts
bytes as they arrive — this keeps the check honest for chunked / streaming
requests without ever buffering the whole payload.

The login rate limit is a per-source-IP token bucket guarded by a mutex.
In-memory is fine because the daemon is a single process; nothing is shared
across restarts by design (a restart-based bypass is not a threat model
worth complicating this for).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ingestion.daemon.logging import get_logger
from ingestion.daemon.store.audit import write_entry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_log = get_logger(__name__)

# Login's payload is ``{"password": "..."}`` with the password capped at 256
# characters. 256KB leaves headroom for proxies that pad headers or for
# pathological unicode but still refuses multi-megabyte bodies on the one
# endpoint that would never need them.
#
# Slice 5: the canonical login URL is ``/v1/auth/login``; ``/api/auth/login``
# stays live as the deprecated alias for one cycle. Both share the same
# rate-limit bucket and size cap so an attacker can't double the budget by
# alternating between the two prefixes. ``LOGIN_PATH`` keeps its old name
# for backwards-compatibility (callers that pinned it from earlier slices
# read the canonical value); ``LOGIN_PATHS`` is the recognised set.
LOGIN_PATH = "/v1/auth/login"
LOGIN_PATHS: frozenset[str] = frozenset({"/v1/auth/login", "/api/auth/login"})
LOGIN_MAX_BYTES = 256 * 1024

_TOO_LARGE_BODY = b'{"detail":"request body too large"}'
_RATE_LIMITED_BODY = b'{"detail":"too many login attempts"}'


async def _send_plain_json(
    send: Send,
    status_code: int,
    body: bytes,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """Send a fixed JSON error response without touching the app."""
    all_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if headers:
        all_headers.extend(headers)
    await send({"type": "http.response.start", "status": status_code, "headers": all_headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


class RequestSizeLimitMiddleware:
    """Reject HTTP requests whose body exceeds a configured cap.

    Applies the tighter ``login_max_bytes`` to the login endpoint and
    ``max_bytes`` to everything else. WebSocket handshakes are passed
    through unchanged: they carry no body, so the limit doesn't apply.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        login_max_bytes: int = LOGIN_MAX_BYTES,
        login_path: str | frozenset[str] | None = None,
    ) -> None:
        self._app = app
        self._max_bytes = max_bytes
        self._login_max_bytes = login_max_bytes
        # Accept either a single string (legacy callers) or a set of
        # paths (slice 5+: matches both the /v1/ canonical and the /api/
        # legacy alias). Falls back to the module default LOGIN_PATHS
        # when no override is supplied.
        if login_path is None:
            self._login_paths: frozenset[str] = LOGIN_PATHS
        elif isinstance(login_path, str):
            self._login_paths = frozenset({login_path})
        else:
            self._login_paths = login_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        limit = self._login_max_bytes if scope.get("path") in self._login_paths else self._max_bytes

        # content-length is advisory (clients can lie or omit it). Treat
        # an oversized advertised length as a short-circuit 413, but still
        # meter the actual stream below because a missing or wrong header
        # must not let an attacker bypass the cap.
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value.decode("latin-1"))
                except ValueError:
                    continue
                if declared > limit:
                    await _send_plain_json(send, 413, _TOO_LARGE_BODY)
                    return
                break

        received = 0
        rejected = False

        async def guarded_receive() -> Message:
            nonlocal received, rejected
            message = await receive()
            if message["type"] != "http.request":
                return message
            body = message.get("body", b"") or b""
            received += len(body)
            if received > limit:
                rejected = True
                # Drain until more_body=False so the client's sender can
                # finish cleanly; otherwise some servers log a disconnect
                # error that clutters the audit trail. The drain is bounded
                # by the client's actual upload rate.
                if message.get("more_body"):
                    while True:
                        follow = await receive()
                        if follow["type"] != "http.request":
                            break
                        if not follow.get("more_body"):
                            break
                return {"type": "http.disconnect"}
            return message

        async def sentinel_send(message: Message) -> None:
            # If guarded_receive tripped the limit, suppress whatever the
            # app was trying to say and reply 413 instead.
            if rejected:
                return
            await send(message)

        await self._app(scope, guarded_receive, sentinel_send)
        if rejected:
            await _send_plain_json(send, 413, _TOO_LARGE_BODY)


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class _TokenBucketRegistry:
    """Per-key token buckets. Thread-safe via a single mutex.

    Each bucket starts full with ``capacity`` tokens and refills at
    ``capacity / window_seconds`` tokens per second. Bursts up to the full
    capacity are allowed, which matches most operators' intuition for
    ``N attempts per minute``.
    """

    def __init__(self, *, capacity: int, window_seconds: float) -> None:
        self._capacity = float(capacity)
        self._window = float(window_seconds)
        self._refill_per_second = self._capacity / self._window if self._window > 0 else 0.0
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def consume(self, key: str, *, now: float | None = None) -> tuple[bool, float]:
        """Try to take one token for ``key``.

        Returns ``(allowed, retry_after_seconds)``. ``retry_after_seconds`` is
        the minimum wait until one token is available when the call is denied;
        zero when the call is allowed.
        """
        current = now if now is not None else time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, updated_at=current)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, current - bucket.updated_at)
                bucket.tokens = min(
                    self._capacity,
                    bucket.tokens + elapsed * self._refill_per_second,
                )
                bucket.updated_at = current
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            if self._refill_per_second <= 0.0:
                return False, self._window
            needed = 1.0 - bucket.tokens
            return False, needed / self._refill_per_second


class LoginRateLimitMiddleware:
    """Cap ``POST /api/auth/login`` requests per source IP.

    Denials return ``429 Too Many Requests`` with ``Retry-After`` set to
    the whole-second count of how long the caller must wait for a fresh
    token, and write an ``auth.login_rate_limited`` audit entry so
    operators see brute-force attempts in the audit feed.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        sqlite_path: Path,
        per_minute: int,
        login_path: str | frozenset[str] | None = None,
        audit_writer: Callable[..., Awaitable[int]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._app = app
        self._sqlite_path = sqlite_path
        # Slice 5: a single bucket guards both the /v1/ and /api/ login
        # paths. Without that, an attacker could double their rate budget
        # by alternating prefixes. ``login_path=None`` picks up the
        # default LOGIN_PATHS set; a string narrows to one path (legacy
        # callers); a frozenset matches several.
        if login_path is None:
            self._login_paths: frozenset[str] = LOGIN_PATHS
        elif isinstance(login_path, str):
            self._login_paths = frozenset({login_path})
        else:
            self._login_paths = login_path
        self._registry = _TokenBucketRegistry(capacity=per_minute, window_seconds=60.0)
        self._audit_writer = audit_writer or write_entry
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in self._login_paths
        ):
            await self._app(scope, receive, send)
            return

        client = scope.get("client")
        # ``client`` is a (host, port) tuple or None (e.g. unix sockets).
        # All IP-less callers share one bucket keyed by "unknown" so we
        # don't accidentally make a trivially-bypassed limiter.
        source_ip = client[0] if client else "unknown"
        now = self._clock() if self._clock is not None else None
        allowed, retry_after = self._registry.consume(source_ip, now=now)
        if allowed:
            await self._app(scope, receive, send)
            return

        retry_seconds = max(1, int(retry_after + 0.999))
        try:
            await self._audit_writer(
                self._sqlite_path,
                action="auth.login_rate_limited",
                actor=source_ip,
            )
        except Exception:
            _log.exception("api.login_rate_limit.audit_failed")
        await _send_plain_json(
            send,
            429,
            _RATE_LIMITED_BODY,
            headers=[(b"retry-after", str(retry_seconds).encode("ascii"))],
        )
