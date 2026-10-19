"""Headless auth server — screenshot streaming over WebSocket.

No VNC, no Xvfb.  JPEG frames flow over a single WebSocket alongside
JSON input events.  Works through any reverse proxy / Cloudflare Tunnel.

    POST      /auth/teams/start                          — spawn session
    GET       /auth/teams/{id}                           — poll status only
    DELETE    /auth/teams/{id}                           — stop session
    WEBSOCKET /auth/teams/{id}/ws                        — frame stream + input
    GET       /internal/auth/teams/{id}/payload          — full token payload
                                                          (requires X-Internal-Secret)

The public ``GET /auth/teams/{id}`` deliberately omits captured tokens
and team_ids so the browser session can never read raw Microsoft
bearer tokens. The backend fetches the full payload over the loopback
``/internal`` endpoint with a shared secret, persists it via
``PlatformCredential``, and the daemon's refresh loop takes over from
there.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import queue
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ingestion.headless.session import (
    LoginSession,
    SessionStatus,
    filter_sso_cookies,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

_sessions: dict[str, LoginSession] = {}
STATIC_DIR = Path(__file__).parent / "static"

# Env var the backend uses to authenticate to the loopback ``/internal``
# endpoint. If it is unset on the server, the endpoint refuses every
# request with 503 — never returning 401, so an attacker can't probe
# whether secret-auth is configured.
_INTERNAL_SECRET_ENV = "ENDPOINT_HEADLESS__INTERNAL_SECRET"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    tasks = [s.cleanup() for s in _sessions.values()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _sessions.clear()


app = FastAPI(title="Endpoint Headless Auth", lifespan=lifespan)


# ------------------------------------------------------------------
# Models
# ------------------------------------------------------------------
class StartResponse(BaseModel):
    """POST /auth/teams/start response."""

    session_id: str
    status: str


class SessionResponse(BaseModel):
    """Public GET /auth/teams/{id} response.

    Deliberately omits ``tokens``, ``refresh_token``, ``team_ids``, and
    ``cookies`` — anything sensitive is reachable only via the internal
    endpoint with the shared secret.
    """

    session_id: str
    status: str
    error: str | None = None


class InternalPayloadResponse(BaseModel):
    """GET /internal/auth/teams/{id}/payload response.

    Returned only when the ``X-Internal-Secret`` header matches the
    server's configured secret. Carries the full Teams capture so the
    backend can persist credentials and hand off to the daemon.
    """

    session_id: str
    status: str
    tokens: dict[str, str] = {}
    refresh_token: str | None = None
    team_ids: list[str] = []
    cookies: list[dict[str, Any]] = []
    error: str | None = None


# ------------------------------------------------------------------
# Internal-secret helpers
# ------------------------------------------------------------------
def _check_internal_secret(provided: str | None) -> None:
    """Validate the ``X-Internal-Secret`` header.

    Raises
    ------
    HTTPException 503
        Server has not configured ``ENDPOINT_HEADLESS__INTERNAL_SECRET``.
        Returned ahead of any session lookup so an attacker can't tell
        whether internal auth is enabled.
    HTTPException 401
        Header missing or value does not match. Compared with
        :func:`hmac.compare_digest` to avoid timing leaks.
    """
    expected = os.environ.get(_INTERNAL_SECRET_ENV)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Internal endpoint not configured",
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid internal secret",
        )


# ------------------------------------------------------------------
# REST endpoints
# ------------------------------------------------------------------
@app.post("/auth/teams/start", response_model=StartResponse)
async def start_login() -> StartResponse:
    session = LoginSession()
    _sessions[session.id] = session
    await session.start()

    # Give Playwright a moment to launch
    for _ in range(20):
        if session.status != SessionStatus.STARTING:
            break
        await asyncio.sleep(0.1)

    if session.status == SessionStatus.FAILED:
        _sessions.pop(session.id, None)
        raise HTTPException(status_code=500, detail=session.error or "Failed")

    return StartResponse(session_id=session.id, status=session.status.value)


@app.get("/auth/teams/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str) -> SessionResponse:
    """Public status poll — never includes tokens or team_ids."""
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionResponse(
        session_id=session.id,
        status=session.status.value,
        error=session.error,
    )


@app.delete("/auth/teams/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    session = _sessions.pop(session_id, None)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await session.cleanup()
    return {"status": "closed"}


@app.get(
    "/internal/auth/teams/{session_id}/payload",
    response_model=InternalPayloadResponse,
)
async def get_internal_payload(
    session_id: str,
    x_internal_secret: str | None = Header(default=None, alias="X-Internal-Secret"),
) -> InternalPayloadResponse:
    """Internal: full token payload for backend hand-off.

    The backend calls this after the public poll reports ``captured``.
    Requires ``X-Internal-Secret`` matching ``ENDPOINT_HEADLESS__INTERNAL_SECRET``.

    The 404-on-missing-session check runs *after* the secret check so
    callers without the secret can never enumerate session ids.
    """
    _check_internal_secret(x_internal_secret)
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return InternalPayloadResponse(
        session_id=session.id,
        status=session.status.value,
        tokens=dict(session.tokens),
        refresh_token=session.refresh_token,
        team_ids=list(session.team_ids),
        cookies=filter_sso_cookies(session.cookies),
        error=session.error,
    )


# ------------------------------------------------------------------
# WebSocket — frame streaming + input forwarding
# ------------------------------------------------------------------
@app.websocket("/auth/teams/{session_id}/ws")
async def screen_ws(websocket: WebSocket, session_id: str) -> None:
    """Stream JPEG frames (binary) and receive input events (JSON text)."""
    session = _sessions.get(session_id)
    if not session:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    logger.info("Screen WS connected: session=%s", session_id)

    _terminal = frozenset({SessionStatus.CAPTURED, SessionStatus.FAILED, SessionStatus.CLOSED})

    async def send_frames() -> None:
        while session.status not in _terminal:
            try:
                frame = await asyncio.to_thread(session.frames.get, timeout=0.15)
                await websocket.send_bytes(frame)
            except queue.Empty:
                pass
            except (WebSocketDisconnect, RuntimeError):
                break

    async def recv_input() -> None:
        try:
            while True:
                text = await websocket.receive_text()
                evt: dict[str, object] = json.loads(text)
                session.inputs.put_nowait(evt)  # type: ignore[arg-type]
        except (WebSocketDisconnect, RuntimeError):
            pass

    try:
        _done, pending = await asyncio.wait(
            [asyncio.create_task(send_frames()), asyncio.create_task(recv_input())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    except Exception as exc:
        logger.debug("Screen WS error: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()
        logger.info("Screen WS closed: session=%s", session_id)


# ------------------------------------------------------------------
# Static
# ------------------------------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
