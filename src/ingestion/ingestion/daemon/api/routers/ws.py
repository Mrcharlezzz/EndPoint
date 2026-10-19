"""WebSocket channels: /ws/logs, /ws/syncs, /ws/errors, /ws/alerts, /ws/metrics.

Every channel shares the same pattern:
    1. Verify the signed session cookie manually (FastAPI's ``Depends`` on
       WebSocket routes behaves subtly differently — we read the cookie off
       the handshake ourselves to keep the auth check explicit).
    2. Accept the connection.
    3. Read an optional filter payload (first client message, a JSON object).
       Subsequent client messages are treated as filter updates so the
       dashboard can toggle without reconnecting.
    4. Subscribe to the channel via ``EventBus`` and stream events.
    5. Heartbeat every ``HEARTBEAT_INTERVAL_SECONDS`` when idle so proxies
       (nginx default 60s idle timeout) don't silently cut the connection.
    6. Wrap ``send_json`` in a timeout — a TCP blackhole would otherwise
       hang the handler forever, filling the queue and starving the bus.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, TypedDict

from fastapi import APIRouter, Cookie, WebSocket, WebSocketDisconnect, status
from starlette.websockets import WebSocketState

from ingestion.daemon.api.auth import COOKIE_NAME
from ingestion.daemon.api.deps import get_context
from ingestion.daemon.logging import get_logger


class _SessionClaims(TypedDict):
    """Decoded shape of the admin session cookie. Mirrors the dict
    returned by :meth:`AuthSettings.verify_session`.

    Kept private to this module because the WS layer is the only
    in-tree consumer that needs the literal shape — HTTP routes go
    through ``make_require_admin`` which wraps the same data behind a
    dependency."""

    admin: bool
    iat: int


router = APIRouter(tags=["ws"])

_log = get_logger(__name__)

# How long to wait for an event before sending a ping. Must be well under
# typical proxy idle timeouts (nginx default 60s).
HEARTBEAT_INTERVAL_SECONDS = 20.0

# How long a send_json can block before we assume the client is stuck and
# close the connection. Real clients complete a send in sub-millisecond.
SEND_TIMEOUT_SECONDS = 5.0


def _verify_ws_cookie(websocket: WebSocket, session: str | None) -> _SessionClaims | None:
    """Return the typed session claims if the cookie is valid, else None.

    Narrowed from ``dict[str, Any]`` to a TypedDict so callers see the
    ``admin`` / ``iat`` keys statically. The underlying
    :meth:`AuthSettings.verify_session` is still untyped at the moment
    (returns ``dict[str, Any]``); we re-shape its result here at the
    only WS-side boundary that consumes it.
    """
    ctx = get_context(websocket)
    if not session:
        return None
    claims = ctx.auth.settings.verify_session(session)
    if not claims or not claims.get("admin"):
        return None
    # ``verify_session`` builds the dict with exactly these keys (see
    # ``AuthSettings.verify_session``); re-shape into the TypedDict so
    # downstream readers don't have to .get() with type narrowing.
    return _SessionClaims(admin=bool(claims.get("admin")), iat=int(claims.get("iat", 0)))


@router.websocket("/ws/logs")
async def ws_logs(
    websocket: WebSocket,
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> None:
    """Stream new log records. First message is the filter; subsequent
    messages update it live."""
    await _ws_channel(websocket, session, channel="logs")


@router.websocket("/ws/syncs")
async def ws_syncs(
    websocket: WebSocket,
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> None:
    """Stream scheduler events: sync.started, sync.finished, sync.failed."""
    await _ws_channel(websocket, session, channel="syncs")


@router.websocket("/ws/errors")
async def ws_errors(
    websocket: WebSocket,
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> None:
    """Stream error events (failed syncs, dead-letter rollovers, refresh failures)."""
    await _ws_channel(websocket, session, channel="errors")


@router.websocket("/ws/alerts")
async def ws_alerts(
    websocket: WebSocket,
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> None:
    """Stream alert firings and resolutions."""
    await _ws_channel(websocket, session, channel="alerts")


@router.websocket("/ws/metrics")
async def ws_metrics(
    websocket: WebSocket,
    session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> None:
    """Stream rolling metrics snapshots (queue depth, running sync count, etc.)."""
    await _ws_channel(websocket, session, channel="metrics")


async def _ws_channel(
    websocket: WebSocket,
    session: str | None,
    *,
    channel: str,
) -> None:
    claims = _verify_ws_cookie(websocket, session)
    if claims is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()

    ctx = get_context(websocket)

    # Read the initial filter synchronously so events published between
    # accept() and the first filter don't sneak past it. The client is
    # expected to send one JSON message immediately after handshake; if
    # they don't, the filter stays empty (accept everything).
    filter_config: dict[str, object] = {}
    try:
        initial = await websocket.receive_json()
        if isinstance(initial, dict):
            filter_config = initial
    except WebSocketDisconnect:
        return
    except Exception:
        filter_config = {}

    updates: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=16)

    # Spawn a background reader so subsequent filter updates can land
    # without the client reconnecting. Disconnects also flow through the
    # queue so the main loop can tear down promptly.
    async def _reader() -> None:
        try:
            while True:
                message = await websocket.receive_json()
                if isinstance(message, dict):
                    with contextlib.suppress(asyncio.QueueFull):
                        updates.put_nowait(message)
        except WebSocketDisconnect:
            with contextlib.suppress(asyncio.QueueFull):
                updates.put_nowait({"__closed__": True})
        except Exception:
            _log.exception("ws.reader_errored", channel=channel)
            with contextlib.suppress(asyncio.QueueFull):
                updates.put_nowait({"__closed__": True})

    reader_task = asyncio.create_task(_reader(), name=f"ws:{channel}:reader")

    try:
        async with ctx.events.subscribe(channel) as queue:
            closed = False
            while not closed:
                # Drain any pending filter updates BEFORE the next event read
                # so the filter reflects the most recent client message.
                while not updates.empty():
                    msg = updates.get_nowait()
                    if msg.get("__closed__"):
                        closed = True
                        break
                    filter_config = msg
                if closed:
                    break

                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=HEARTBEAT_INTERVAL_SECONDS,
                    )
                except TimeoutError:
                    # No event for a while — send a ping so the proxy (and
                    # client) know we're alive. If the send hangs, the
                    # timeout below trips and we tear the socket down.
                    if websocket.application_state == WebSocketState.CONNECTED:
                        try:
                            await asyncio.wait_for(
                                websocket.send_json({"type": "ping"}),
                                timeout=SEND_TIMEOUT_SECONDS,
                            )
                        except (TimeoutError, WebSocketDisconnect, RuntimeError):
                            # RuntimeError: uvicorn raises this when the peer
                            # closed between the state check above and the send
                            # — the ASGI state flag lags the TCP close by one
                            # event loop tick.
                            break
                    continue

                if not _matches(event, filter_config):
                    continue
                try:
                    await asyncio.wait_for(
                        websocket.send_json(event),
                        timeout=SEND_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    _log.warning(
                        "ws.send_timeout",
                        channel=channel,
                        claims_iat=claims["iat"],
                    )
                    break
                except (WebSocketDisconnect, RuntimeError):
                    break
    finally:
        reader_task.cancel()
        with contextlib.suppress(BaseException):
            await reader_task
        with contextlib.suppress(Exception):
            if websocket.application_state == WebSocketState.CONNECTED:
                await websocket.close()


def _matches(event: dict[str, object], filter_config: dict[str, object]) -> bool:
    """Apply a simple equality filter. ``None`` is the only wildcard.

    Empty-string and empty-list are treated as explicit filters (which may
    match nothing) so a buggy client doesn't accidentally receive everyone's
    traffic. The previous behaviour of accepting ``""``/``[]`` as wildcards
    was surprising and would mean ``{"levels": []}`` matches every level.
    """
    for key, wanted in filter_config.items():
        if wanted is None:
            continue
        if key.startswith("__"):
            continue
        if event.get(key) != wanted:
            return False
    return True
