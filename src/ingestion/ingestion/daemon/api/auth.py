"""Session-cookie auth for the daemon API.

Login: POST ``/api/auth/login`` with ``{"password": "..."}``. The password is
compared to ``ENDPOINT_ADMIN_PASSWORD`` with :func:`secrets.compare_digest`.
On success the server issues a signed cookie that encodes
``{"admin": True, "iat": <unix>}`` and expires after
``api.session_ttl_hours``.

Every other route goes through :func:`require_admin`, which validates the
cookie with the same signer. Missing, tampered, or expired cookies yield
HTTP 401.

The implementation uses :mod:`itsdangerous` (HMAC-SHA256) for signing; no
crypto rolled by hand. ``ENDPOINT_SESSION_SECRET`` is the HMAC key and
must be at least :data:`MIN_SESSION_SECRET_BYTES` long. Multiple secrets
can be supplied comma-separated for rotation: the first is used for
signing, all are accepted for verifying.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from typing import Annotated, Any

from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from pydantic import BaseModel, Field

from ingestion.daemon.logging import get_logger

_log = get_logger(__name__)

COOKIE_NAME = "endpoint_admin_session"

# Short session-secret keys are trivially bruteforced. 32 bytes matches the
# recommended minimum for HMAC-SHA256 (RFC 2104 / RFC 7518 §3.2).
MIN_SESSION_SECRET_BYTES = 32

# Minimum admin password length. Not perfect, but refuses the worst
# "change-me" defaults that end up in public commits.
MIN_ADMIN_PASSWORD_LENGTH = 8


class LoginPayload(BaseModel):
    # Bound the body so a malicious client can't CPU-DoS us with multi-MB
    # constant-time compares. Real admin passwords are well under 256
    # characters.
    password: str = Field(max_length=256)


class AuthSettings:
    """Reads env vars at construction. Fail-closed if missing."""

    def __init__(
        self,
        *,
        admin_password: str | None = None,
        session_secret: str | None = None,
        session_ttl_seconds: int = 24 * 3600,
    ) -> None:
        pw = (
            admin_password
            if admin_password is not None
            else os.environ.get("ENDPOINT_ADMIN_PASSWORD")
        )
        secret = (
            session_secret
            if session_secret is not None
            else os.environ.get("ENDPOINT_SESSION_SECRET")
        )
        if not pw:
            raise RuntimeError("ENDPOINT_ADMIN_PASSWORD is required for the API")
        if not secret:
            raise RuntimeError("ENDPOINT_SESSION_SECRET is required for the API")
        if len(pw) < MIN_ADMIN_PASSWORD_LENGTH or pw.strip().lower() in {"change-me", "admin"}:
            raise RuntimeError(
                "ENDPOINT_ADMIN_PASSWORD is too short or a well-known default; "
                f"use a strong password with at least {MIN_ADMIN_PASSWORD_LENGTH} characters"
            )
        # Accept comma-separated secrets for rotation. First entry signs new
        # cookies; all entries verify existing ones. Trailing-comma / empty
        # slots raise so the operator notices a botched rotation instead of
        # silently invalidating every session.
        raw_secrets = [s.strip() for s in secret.split(",")]
        if any(s == "" for s in raw_secrets):
            raise RuntimeError(
                "ENDPOINT_SESSION_SECRET contains an empty entry; "
                "check for stray commas or missing keys during rotation"
            )
        for s in raw_secrets:
            if len(s.encode()) < MIN_SESSION_SECRET_BYTES:
                raise RuntimeError(
                    "ENDPOINT_SESSION_SECRET entries must be at least "
                    f"{MIN_SESSION_SECRET_BYTES} bytes long"
                )

        self._admin_password = pw
        # HMAC-SHA256 instead of itsdangerous's SHA1 default.
        self._signer = TimestampSigner(raw_secrets[0], digest_method=hashlib.sha256)
        self._verify_signers = tuple(
            TimestampSigner(s, digest_method=hashlib.sha256) for s in raw_secrets
        )
        self.session_ttl_seconds = session_ttl_seconds

    def check_password(self, candidate: str) -> bool:
        return secrets.compare_digest(candidate, self._admin_password)

    def sign_session(self, *, admin: bool = True) -> str:
        """Sign a cookie value: ``"admin:<iat>"``. ``iat`` is the unix time."""
        issued_at = int(time.time())
        payload = f"{int(admin)}:{issued_at}"
        return self._signer.sign(payload).decode()

    def verify_session(self, cookie_value: str) -> dict[str, Any] | None:
        """Return the decoded claims if the cookie is valid + fresh.

        Tries every configured verify signer so a rotation (new signing key
        added as the first entry) doesn't invalidate every existing session.
        """
        for verifier in self._verify_signers:
            try:
                raw = verifier.unsign(cookie_value, max_age=self.session_ttl_seconds)
            except SignatureExpired:
                return None
            except BadSignature:
                continue
            payload = raw.decode()
            parts = payload.split(":", 1)
            if len(parts) != 2:
                return None
            admin_flag, iat = parts
            try:
                return {"admin": bool(int(admin_flag)), "iat": int(iat)}
            except ValueError:
                return None
        return None


def set_session_cookie(
    response: Response,
    token: str,
    ttl_seconds: int,
    *,
    secure: bool,
) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    # Deletion must mirror the original cookie's attributes or the browser
    # ignores it. Keep path + secure + samesite in lockstep with set_.
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure,
    )


class AuthProvider:
    """Exposed via FastAPI dependency. One instance per app."""

    def __init__(self, settings: AuthSettings) -> None:
        self._settings = settings

    @property
    def settings(self) -> AuthSettings:
        return self._settings

    def require(self, cookie: str | None, *, request: Request | None = None) -> dict[str, Any]:
        if not cookie:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthenticated")
        claims = self._settings.verify_session(cookie)
        if claims is None or not claims.get("admin"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session")
        # Defence in depth: admin GET endpoints are read-only, but mutating
        # requests must carry X-Requested-With so a CSRF attacker can't use
        # a top-level navigation to trigger one (SameSite=Lax lets GET
        # through). Cheap double-check that XHR-style fetch() was used.
        if request is not None and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            xrw = request.headers.get("x-requested-with", "").lower()
            if xrw != "xmlhttprequest":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="missing X-Requested-With header",
                )
        return claims


def make_require_admin(provider: AuthProvider) -> Any:
    """Factory for a FastAPI dependency bound to ``provider``."""

    def _dep(
        request: Request,
        session: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
    ) -> dict[str, Any]:
        return provider.require(session, request=request)

    return Depends(_dep)
