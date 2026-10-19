"""Auth endpoints: Teams (via headless), Moodle (username/password), logout.

Both sign-in paths produce two side effects:

1. find-or-create the Endpoint :class:`User`
2. upsert a :class:`PlatformCredential` row so the daemon's refresh loop
   can take over ingestion immediately

Then a :class:`Session` is minted and set as an httpOnly cookie.
"""

from __future__ import annotations

import logging

from django.db import DatabaseError, IntegrityError, transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response

from api.services.auth_service import (
    GraphValidationError,
    clear_session_cookie,
    find_or_create_user,
    mint_session,
    persist_platform_credential,
    set_session_cookie,
    validate_graph_token,
)
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


logger = logging.getLogger(__name__)


def _err(message: str, code: str, status: int) -> Response:
    return Response({"error": {"code": code, "message": message}}, status=status)


def _establish_session(*, email, display_name, platform, payload):
    """Find-or-create user, persist credential, mint session, atomically.

    Wraps the three writes in a single transaction so a failed credential
    insert doesn't leave a half-finalized User row behind. Any database
    error gets logged and re-raised as a typed exception the view can
    convert to a clean 502 instead of a generic 500.
    """
    try:
        with transaction.atomic():
            user = find_or_create_user(email=email, display_name=display_name)
            persist_platform_credential(user=user, platform=platform, payload=payload)
            session = mint_session(user)
        return user, session
    except IntegrityError as exc:
        logger.warning(
            "auth.persist.integrity_error", extra={"platform": platform, "error": str(exc)}
        )
        raise _SessionEstablishError(
            "Couldn't save your sign-in. A duplicate record was rejected, "
            "this is usually safe to retry.",
            code="conflict",
            status=409,
        ) from exc
    except DatabaseError as exc:
        logger.error("auth.persist.db_error", extra={"platform": platform, "error": str(exc)})
        raise _SessionEstablishError(
            "We couldn't write your sign-in to the database. Try again in a moment.",
            code="internal_error",
            status=502,
        ) from exc


class _SessionEstablishError(Exception):
    def __init__(self, message: str, *, code: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _user_payload(user) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
    }


def _session_response(user, session) -> Response:
    payload = {
        "user": _user_payload(user),
        "expires_at": session.expires_at,
    }
    response = Response(payload, status=200)
    set_session_cookie(response, session)
    return response


@api_view(["POST"])
def teams_finalize(request):
    """Promote a captured headless session into an Endpoint session.

    Body: ``{"session_id": "<headless session id>"}``
    """
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
    graph_token = tokens.get("graph_token")
    if not isinstance(graph_token, str) or not graph_token:
        return _err("Headless payload missing graph_token", "internal_error", 502)

    try:
        me = validate_graph_token(graph_token)
    except GraphValidationError as exc:
        return _err(f"Graph validation failed: {exc}", "internal_error", 502)

    email = (me.get("mail") or me.get("userPrincipalName") or "").strip().lower()
    if not email:
        return _err("Microsoft profile has no email", "internal_error", 502)
    display_name = (me.get("displayName") or "").strip()

    try:
        user, session = _establish_session(
            email=email,
            display_name=display_name,
            platform="teams",
            payload=build_teams_blob(payload),
        )
    except _SessionEstablishError as exc:
        return _err(str(exc), exc.code, exc.status)
    return _session_response(user, session)


@api_view(["POST"])
def moodle_login(request):
    """Authenticate against Moodle, find-or-create user, mint session.

    Body: ``{"username": "...", "password": "..."}``
    """
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

    try:
        user, session = _establish_session(
            email=identity.email,
            display_name=identity.full_name,
            platform="moodle",
            payload=dict(build_moodle_blob(identity.token, identity.user_id)),
        )
    except _SessionEstablishError as exc:
        return _err(str(exc), exc.code, exc.status)
    return _session_response(user, session)


@api_view(["POST"])
def logout(request):
    """Invalidate the current session (delete row + clear cookie).

    The route is public so a stale-cookie user can still clear their
    cookie. We try to delete the matching Session row if the cookie is
    present and valid; on any failure we still return 204 + clear-cookie
    so the client never gets stuck.
    """
    from django.conf import settings

    from api.models.identity import Session

    token = request.COOKIES.get(settings.SESSION_COOKIE_NAME)
    if token:
        try:
            Session.objects.select_related("user").get(token=token).delete()
        except Session.DoesNotExist:
            pass
    response = Response(status=204)
    clear_session_cookie(response)
    return response
