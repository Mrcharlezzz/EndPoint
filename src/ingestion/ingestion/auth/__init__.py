"""Shared authentication primitives for ingestion components.

Public surface:
    silent_token_refresh: SSO-cookie-driven OIDC silent renewal.
    SilentOIDCConfig, TokenBundle: data shapes for the above.
    SilentOIDCError, InteractionRequiredError, TokenExchangeError: failures.
"""

from ingestion.auth.silent_oidc import (
    InteractionRequiredError,
    SilentOIDCConfig,
    SilentOIDCError,
    TokenBundle,
    TokenExchangeError,
    refresh_with_token,
    silent_token_refresh,
)

__all__ = [
    "InteractionRequiredError",
    "SilentOIDCConfig",
    "SilentOIDCError",
    "TokenBundle",
    "TokenExchangeError",
    "refresh_with_token",
    "silent_token_refresh",
]
