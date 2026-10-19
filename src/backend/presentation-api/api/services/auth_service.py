"""Find-or-create User, persist PlatformCredential, mint Session, set/clear cookie.

Used by both Microsoft (Teams) and Moodle sign-in flows. Keeps the views thin.
"""

from __future__ import annotations

from typing import Any

import httpx
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone

from api.models.identity import Session, User
from api.models.mirrors import PlatformCredential
from api.services.credentials_crypto import encrypt_payload


class GraphValidationError(RuntimeError):
    pass


def validate_graph_token(graph_token: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Call Graph ``/me`` to confirm the token works and pull identity claims."""
    url = f"{settings.MS_GRAPH_BASE_URL.rstrip('/')}/me"
    try:
        resp = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {graph_token}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        raise GraphValidationError(f"Graph unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise GraphValidationError(f"Graph /me returned {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict):
        raise GraphValidationError("Graph /me did not return an object")
    return data


def find_or_create_user(*, email: str, display_name: str) -> User:
    """Idempotent user upsert keyed by email (case-insensitive)."""
    email_norm = email.strip().lower()
    if not email_norm:
        raise ValueError("email is required")
    user, created = User.objects.get_or_create(
        email=email_norm,
        defaults={"display_name": display_name or email_norm.split("@")[0]},
    )
    if not created:
        # Refresh display_name if we now have a better one and the user
        # hasn't customized it (currently we don't expose a customize
        # endpoint, so we always refresh).
        if display_name and user.display_name != display_name:
            user.display_name = display_name
            user.save(update_fields=["display_name", "updated_at"])
        if user.deleted_at is not None:
            # Re-activating a soft-deleted user on sign-in.
            user.deleted_at = None
            user.save(update_fields=["deleted_at", "updated_at"])
    return user


@transaction.atomic
def persist_platform_credential(
    *,
    user: User,
    platform: str,
    payload: dict[str, Any],
) -> PlatformCredential:
    """Encrypt the payload and upsert the PlatformCredential row."""
    encrypted = encrypt_payload(payload)
    cred, _ = PlatformCredential.objects.update_or_create(
        user_email=user.email,
        platform=platform,
        defaults={
            "display_name": user.display_name,
            "encrypted_payload": encrypted,
            "is_paused": False,
            "is_expired": False,
            "expired_at": None,
            "last_refreshed_at": timezone.now(),
        },
    )
    return cred


def mint_session(user: User) -> Session:
    return Session.objects.create(user=user)


def set_session_cookie(response: HttpResponse, session: Session) -> None:
    response.set_cookie(
        settings.SESSION_COOKIE_NAME,
        session.token,
        expires=session.expires_at,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite=settings.SESSION_COOKIE_SAMESITE,
        domain=settings.SESSION_COOKIE_DOMAIN,
        path="/",
    )


def clear_session_cookie(response: HttpResponse) -> None:
    response.delete_cookie(
        settings.SESSION_COOKIE_NAME,
        domain=settings.SESSION_COOKIE_DOMAIN,
        path="/",
    )
