"""Per-platform connection management surfacing PlatformCredential rows."""

from __future__ import annotations

from rest_framework.decorators import api_view
from rest_framework.response import Response

from api.models.mirrors import PlatformCredential
from api.services.auth_service import persist_platform_credential
from api.services.credential_payloads import build_moodle_blob, build_teams_blob
from api.services.headless_client import (
    HeadlessError,
    HeadlessNotFound,
    HeadlessUnauthorized,
    HeadlessUnconfigured,
    fetch_payload,
)
from api.services.moodle_client import (
    MoodleAuthError,
    authenticate as moodle_authenticate,
)


_VALID_PLATFORMS = ("teams", "moodle")


def _err(message: str, code: str, status: int) -> Response:
    return Response({"error": {"code": code, "message": message}}, status=status)


def _row_to_dict(row: PlatformCredential) -> dict:
    if row.is_expired:
        status = "expired"
    elif row.is_paused:
        status = "paused"
    else:
        status = "connected"
    return {
        "platform": row.platform,
        "status": status,
        "last_sync_at": row.last_refreshed_at,
        "expired_at": row.expired_at,
    }


@api_view(["GET"])
def connections_list(request):
    """All platform connections owned by the current user."""
    user = request._request.auth_user
    rows = PlatformCredential.objects.filter(user_email=user.email).order_by("platform")
    return Response({"results": [_row_to_dict(row) for row in rows]})


@api_view(["POST"])
def teams_finalize(request):
    """Attach Teams to the current user (used after sign-in via Moodle)."""
    user = request._request.auth_user
    session_id = request.data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return _err("session_id is required", "validation_error", 400)

    try:
        payload = fetch_payload(session_id)
    except HeadlessNotFound:
        return _err("Headless session not found", "not_found", 404)
    except HeadlessUnauthorized:
        return _err("Headless rejected the internal secret", "internal_error", 502)
    except HeadlessUnconfigured:
        return _err("Headless integration is not configured", "internal_error", 503)
    except HeadlessError as exc:
        return _err(f"Headless error: {exc}", "internal_error", 502)

    if payload.get("status") != "captured":
        return _err("Headless session has not captured tokens yet", "conflict", 409)
    tokens = payload.get("tokens") or {}
    if not isinstance(tokens.get("graph_token"), str):
        return _err("Headless payload missing graph_token", "internal_error", 502)

    persist_platform_credential(user=user, platform="teams", payload=build_teams_blob(payload))
    row = PlatformCredential.objects.get(user_email=user.email, platform="teams")
    return Response(_row_to_dict(row), status=200)


@api_view(["POST"])
def moodle_connect(request):
    """Attach Moodle to the current user (used after sign-in via Teams)."""
    user = request._request.auth_user
    username = request.data.get("username")
    password = request.data.get("password")
    if not isinstance(username, str) or not username:
        return _err("username is required", "validation_error", 400)
    if not isinstance(password, str) or not password:
        return _err("password is required", "validation_error", 400)

    try:
        identity = moodle_authenticate(username, password)
    except MoodleAuthError as exc:
        return _err(f"Moodle login failed: {exc}", "authentication_error", 401)

    if identity.email.strip().lower() != user.email.strip().lower():
        return _err(
            "Moodle email does not match signed-in user",
            "validation_error",
            409,
        )

    persist_platform_credential(
        user=user,
        platform="moodle",
        payload=dict(build_moodle_blob(identity.token, identity.user_id)),
    )
    row = PlatformCredential.objects.get(user_email=user.email, platform="moodle")
    return Response(_row_to_dict(row), status=200)


@api_view(["DELETE"])
def disconnect(request, platform: str):
    if platform not in _VALID_PLATFORMS:
        return _err(f"unknown platform: {platform}", "validation_error", 400)
    user = request._request.auth_user
    deleted, _ = PlatformCredential.objects.filter(
        user_email=user.email, platform=platform
    ).delete()
    if deleted == 0:
        return _err("Connection not found", "not_found", 404)
    return Response(status=204)


@api_view(["POST"])
def reconnect(request, platform: str):
    """Mark the credential as paused=False, is_expired=False so the daemon retries.

    Re-providing actual credentials uses the platform-specific connect
    endpoint above; this is the lightweight 'try again' button.
    """
    if platform not in _VALID_PLATFORMS:
        return _err(f"unknown platform: {platform}", "validation_error", 400)
    user = request._request.auth_user
    updated = PlatformCredential.objects.filter(user_email=user.email, platform=platform).update(
        is_paused=False, is_expired=False, expired_at=None
    )
    if updated == 0:
        return _err("Connection not found", "not_found", 404)
    row = PlatformCredential.objects.get(user_email=user.email, platform=platform)
    return Response(_row_to_dict(row), status=200)
