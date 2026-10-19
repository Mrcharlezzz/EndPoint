"""Compose an audit ``actor`` string from the request + session claims.

Every mutation endpoint records who did the deed. With a single-admin model
the username isn't useful on its own, but the request's source IP and the
session cookie's ``iat`` together pin the action to a specific login — which
matters when you're reconstructing "did the operator replay a stolen cookie
10 days later or was this the legitimate admin". Format:
``admin@<source_ip>/<session_iat>``.

Missing pieces fall back to ``"unknown"`` rather than raising so a bug in
the web-server / cookie plumbing can't block audit writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ingestion.daemon.api.auth import COOKIE_NAME

if TYPE_CHECKING:
    from fastapi import Request


def make_actor(request: Request) -> str:
    """Return ``admin@<source_ip>/<session_iat>``.

    Pulls the source IP from ``request.client.host`` and the issued-at
    timestamp from the signed session cookie. Never raises.
    """
    source_ip = request.client.host if request.client else "unknown"
    iat = _iat_from_request(request)
    iat_str = str(iat) if iat is not None else "unknown"
    return f"admin@{source_ip}/{iat_str}"


def _iat_from_request(request: Request) -> int | None:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    app_state = getattr(request.app, "state", None)
    ctx = getattr(app_state, "api_context", None) if app_state is not None else None
    if ctx is None:
        return None
    claims = ctx.auth.settings.verify_session(cookie)
    if not claims:
        return None
    iat = claims.get("iat")
    return int(iat) if isinstance(iat, int) else None
