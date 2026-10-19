"""/api/auth/login, /api/auth/logout, /api/auth/whoami.

``router`` is public (login + logout read their state from the cookie).
``whoami_router`` carries the single authenticated endpoint and MUST be
mounted behind ``require_admin`` by the app factory, otherwise any
unauthenticated caller gets ``{"admin": true}`` and bypasses the login
page in the dashboard.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict

from ingestion.daemon.api.auth import (
    COOKIE_NAME,
    LoginPayload,
    clear_session_cookie,
    set_session_cookie,
)
from ingestion.daemon.api.deps import ContextDep
from ingestion.daemon.api.error_envelope import ErrorCode, raise_http_exception
from ingestion.daemon.api.schemas import OkResponse
from ingestion.daemon.store.audit import write_entry

router = APIRouter(prefix="/auth", tags=["auth"])
whoami_router = APIRouter(prefix="/auth", tags=["auth"])


class WhoamiResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    admin: bool
    cookie_name: str


# Small constant wait on failed login so response time doesn't leak
# DB-hit-vs-miss to an attacker. Intentionally short (not a rate limit in
# itself) — rate limits should live in a reverse proxy.
_FAILED_LOGIN_DELAY_SECONDS = 0.5


@router.post("/login", response_model=OkResponse)
async def login(
    payload: LoginPayload,
    response: Response,
    request: Request,
    ctx: ContextDep,
) -> OkResponse:
    source_ip = request.client.host if request.client else None
    if not ctx.auth.settings.check_password(payload.password):
        # Audit the failure with the source IP so brute-force attempts
        # leave a trail the admin can inspect. Sleep briefly to equalise
        # response time with the success path's audit write.
        await write_entry(
            ctx.config.sqlite_path,
            action="auth.login_failed",
            actor=source_ip or "unknown",
        )
        await asyncio.sleep(_FAILED_LOGIN_DELAY_SECONDS)
        raise_http_exception(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.UNAUTHORIZED,
            "invalid password",
        )
    token = ctx.auth.settings.sign_session()
    set_session_cookie(
        response,
        token,
        ctx.auth.settings.session_ttl_seconds,
        secure=ctx.config.api.https_terminated_upstream,
    )
    await write_entry(
        ctx.config.sqlite_path,
        action="auth.login",
        actor=source_ip or "admin",
    )
    return OkResponse(ok=True)


@router.post("/logout", response_model=OkResponse)
async def logout(response: Response, request: Request, ctx: ContextDep) -> OkResponse:
    source_ip = request.client.host if request.client else None
    clear_session_cookie(response, secure=ctx.config.api.https_terminated_upstream)
    await write_entry(ctx.config.sqlite_path, action="auth.logout", actor=source_ip or "admin")
    return OkResponse(ok=True)


@whoami_router.get("/whoami", response_model=WhoamiResponse)
async def whoami(ctx: ContextDep) -> WhoamiResponse:
    _ = ctx
    return WhoamiResponse(admin=True, cookie_name=COOKIE_NAME)
