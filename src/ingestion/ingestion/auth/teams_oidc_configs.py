"""Public SilentOIDCConfig builders for the Teams Web Client.

The CLI's ``try_silent_refresh``, the daemon's ``HttpxTokenRefresher``,
and the headless auth server all need the same three configs (Graph,
Skype bootstrap, CSA). This module is the single source of truth.

The IDs and redirect URI are public values for the Microsoft Teams Web
Client — see ``docs/plans/2026-04-25-teams-token-refresh-strategy.md``.

Functions are zero-arg builders rather than module-level constants so
the import cost of :class:`SilentOIDCConfig` is paid lazily — relevant
to CLI startup latency.
"""

from __future__ import annotations

from ingestion.auth.silent_oidc import SilentOIDCConfig

TEAMS_WEB_CLIENT_ID = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"
TEAMS_WEB_REDIRECT_URI = "https://teams.microsoft.com/v2/authv2"


def graph_config() -> SilentOIDCConfig:
    """Config for minting a Graph access_token (``graph.microsoft.com``)."""
    return SilentOIDCConfig(
        client_id=TEAMS_WEB_CLIENT_ID,
        redirect_uri=TEAMS_WEB_REDIRECT_URI,
        scope=[
            "https://graph.microsoft.com/.default",
            "openid",
            "profile",
            "offline_access",
        ],
    )


def bootstrap_config() -> SilentOIDCConfig:
    """Config for the Skype bootstrap call.

    Skype is the only audience the SSO cookie session has freshness for
    on the Constructor U tenant; the resulting ``refresh_token`` is
    chained for every subsequent resource via ``refresh_with_token``.
    """
    return SilentOIDCConfig(
        client_id=TEAMS_WEB_CLIENT_ID,
        redirect_uri=TEAMS_WEB_REDIRECT_URI,
        scope=[
            "https://api.spaces.skype.com/.default",
            "openid",
            "profile",
            "offline_access",
        ],
    )


def csa_config() -> SilentOIDCConfig:
    """Config for minting a CSA (chatsvcagg) access_token."""
    return SilentOIDCConfig(
        client_id=TEAMS_WEB_CLIENT_ID,
        redirect_uri=TEAMS_WEB_REDIRECT_URI,
        scope=[
            "https://chatsvcagg.teams.microsoft.com/.default",
            "openid",
            "profile",
            "offline_access",
        ],
    )
