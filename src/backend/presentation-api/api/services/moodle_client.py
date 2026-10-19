"""Moodle credential validation against ``/login/token.php`` and ``core_webservice_get_site_info``.

Mirrors the logic in ``ingestion/cli/auth/moodle_login.py`` but is a
standalone implementation so the presentation-api does not depend on
the CLI module.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from django.conf import settings


class MoodleAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class MoodleIdentity:
    token: str
    user_id: int
    email: str
    full_name: str
    site_name: str


def authenticate(username: str, password: str, *, timeout: float = 30.0) -> MoodleIdentity:
    """Authenticate against the configured Moodle host. Raises on failure."""
    base = settings.MOODLE_BASE_URL.rstrip("/")
    if not base:
        raise MoodleAuthError("MOODLE_BASE_URL is not set")

    with httpx.Client(timeout=timeout) as client:
        token = _acquire_token(client, base, username, password)
        identity = _site_info(client, base, token)
        if not identity.email:
            # Constructor University's Moodle redacts `useremail` from
            # site_info but exposes it via core_user_get_users_by_field.
            email = _lookup_email(client, base, token, identity.user_id)
            identity = MoodleIdentity(
                token=identity.token,
                user_id=identity.user_id,
                email=email,
                full_name=identity.full_name or email.split("@")[0],
                site_name=identity.site_name,
            )

    if not identity.email:
        raise MoodleAuthError("Moodle account has no email; cannot link to Endpoint user")
    return identity


def _acquire_token(client: httpx.Client, base: str, username: str, password: str) -> str:
    resp = client.post(
        f"{base}/login/token.php",
        data={
            "username": username,
            "password": password,
            "service": "moodle_mobile_app",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if "token" in data:
        return str(data["token"])
    err = data.get("error") or data.get("errorcode") or "unknown error"
    raise MoodleAuthError(str(err))


def _site_info(client: httpx.Client, base: str, token: str) -> MoodleIdentity:
    resp = client.post(
        f"{base}/webservice/rest/server.php",
        data={
            "wstoken": token,
            "wsfunction": "core_webservice_get_site_info",
            "moodlewsrestformat": "json",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or "userid" not in data:
        raise MoodleAuthError("site_info response missing userid")
    user_id = int(data["userid"])
    email = str(data.get("useremail") or "").strip().lower()
    fullname = str(data.get("fullname") or "").strip()
    return MoodleIdentity(
        token=token,
        user_id=user_id,
        email=email,
        full_name=fullname or (email.split("@")[0] if email else ""),
        site_name=str(data.get("sitename") or "").strip(),
    )


def _lookup_email(client: httpx.Client, base: str, token: str, user_id: int) -> str:
    """Fetch the user's email via core_user_get_users_by_field.

    Used when site_info redacts useremail (e.g. Constructor University's
    Moodle). Requires moodle/user:viewdetails on self, which is the
    default for authenticated users.
    """
    resp = client.post(
        f"{base}/webservice/rest/server.php",
        data={
            "wstoken": token,
            "wsfunction": "core_user_get_users_by_field",
            "moodlewsrestformat": "json",
            "field": "id",
            "values[0]": str(user_id),
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or not data:
        return ""
    record = data[0]
    if not isinstance(record, dict):
        return ""
    return str(record.get("email") or "").strip().lower()
