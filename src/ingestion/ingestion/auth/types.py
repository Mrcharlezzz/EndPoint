"""Shape declarations for credential blobs that move through the auth layer.

The :class:`~ingestion.ports.TokenStore` protocol uses ``dict[str, Any]``
on its read/write methods because it is platform-agnostic. The TypedDicts
in this module declare what the dict actually carries for each platform,
so callers and reviewers have one place to look for "what fields can I
expect / am I allowed to set."

These are documentation, not runtime checks. Mismatches surface at
type-check time only.
"""

from __future__ import annotations

from typing import Any, TypedDict


class TeamsCredentialBlob(TypedDict, total=False):
    """Persisted shape of the Teams credential blob.

    Stored encrypted (Fernet) in
    ``core.models.PlatformCredential.encrypted_payload``. Read and
    written via :class:`ingestion.daemon.ports.token_store.PostgresTokenStore`.

    All fields are optional (``total=False``) because the blob evolves
    over time: an old row may predate ``refresh_token``, a Skype-only
    bootstrap may not carry ``graph_token``, etc. The refresh loop
    tolerates partial blobs and fills in what it can.

    Field rules:

    * ``graph_token`` — short-lived (~60 min) Microsoft Graph access
      token, audience ``https://graph.microsoft.com``. The platform
      adapter consumes this for Calendar, Mail, Teams metadata, etc.
    * ``csa_token`` — short-lived Chat Service Aggregator access token,
      audience ``https://chatsvcagg.teams.microsoft.com``. Used by the
      chat / channel message adapter.
    * ``refresh_token`` — long-lived (sliding 90-day window by default,
      tenant-tunable) OAuth2 refresh token. Issued by Microsoft Entra
      because the silent OIDC flow asks for ``offline_access``. Once
      present, the refresh loop prefers ``grant_type=refresh_token``
      over the cookie bootstrap and only falls back when the
      refresh_token has been revoked or rolled past its window.
    * ``cookies`` — Playwright-style cookie jar. Filtered to the SSO
      domains (``*.login.microsoftonline.com`` / ``login.live.com``)
      before being replayed; the Teams app cookies on ``*.microsoft.com``
      are deliberately stripped on every refresh because replaying them
      lets Teams JS short-circuit silent renewal with stale tokens.
    * ``team_ids`` — discovered Microsoft 365 Group object ids the user
      is a member of. Cached at login time so the platform adapter can
      avoid a Graph round-trip for "what should I sync."
    * ``refresh_url`` — pre-built channel deeplink that the
      Playwright-driven refresher used as a homepage. Kept for
      backwards compatibility while the daemon migrates to
      :class:`ingestion.daemon.auth.refresh.HttpxTokenRefresher`, which
      ignores this field.
    """

    graph_token: str
    csa_token: str
    refresh_token: str
    cookies: list[dict[str, Any]]
    team_ids: list[str]
    refresh_url: str
