"""Silent OIDC token refresh against Microsoft Entra.

Replays an SSO cookie jar (specifically ``ESTSAUTHPERSISTENT`` and
friends, captured during a headed Playwright login) against
``/oauth2/v2.0/authorize?prompt=none`` to obtain a short-lived
authorization code, then exchanges the code for an access token via
``/oauth2/v2.0/token``. This is the same primitive ``msal.js.browser``
uses for silent renewal in the Teams web client; we just speak the
protocol directly instead of wrapping a browser around it.

No browser, no JS execution, no Playwright dependency — just two HTTPS
round-trips. See ``docs/plans/2026-04-25-teams-token-refresh-strategy.md``
for the design rationale.

Public usage:

    config = SilentOIDCConfig(
        client_id="5e3ce6c0-2b1f-4285-8d4b-75ee78787346",
        redirect_uri="https://teams.microsoft.com/v2/authv2",
        scope=[
            "https://graph.microsoft.com/.default",
            "openid",
            "profile",
            "offline_access",
        ],
    )
    bundle = silent_token_refresh(cookies, config)
    # bundle.access_token, bundle.refresh_token, bundle.expires_at, ...

The ``cookies`` argument is a list of Playwright-style cookie dicts
(``[{"name", "value", "domain", "path", ...}, ...]``) — exactly what
``BrowserContext.cookies()`` returns and what we already persist to
``~/.config/ingest/sessions/teams_cookies.json``.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

ENTRA_AUTHORIZE_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
ENTRA_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# Hosts where a 302 from /authorize indicates the SSO session has lapsed
# and the user has to complete interactive login again.
_INTERACTIVE_LOGIN_HOSTS: tuple[str, ...] = (
    "login.microsoftonline.com",
    "login.microsoftonline.us",
    "login.microsoftonline-p.cn",
    "login.microsoftonline.de",
    "login.live.com",
    "login.windows.net",
)

# OAuth error codes that mean "no automated path forward, user action required".
_INTERACTIVE_REQUIRED_ERRORS: frozenset[str] = frozenset(
    {"interaction_required", "login_required", "consent_required"}
)


@dataclass(frozen=True)
class SilentOIDCConfig:
    """Static parameters for one silent-refresh call.

    ``tenant`` defaults to ``common`` because that's what MSAL.js uses
    in the Teams web client's silent-renewal calls (probe M, 2026-04-25);
    Entra resolves the actual tenant from the cookie session. Pass a
    specific tenant id only if you have a reason to override.
    """

    client_id: str
    redirect_uri: str
    scope: list[str]
    tenant: str = "common"
    # Optional hints. When the SSO session has freshness for one
    # resource but not another, Entra rejects the silent flow with
    # ``interaction_required`` unless the request narrows itself to
    # a known principal (login_hint) and / or homerealm (domain_hint).
    login_hint: str | None = None
    domain_hint: str | None = None


@dataclass(frozen=True)
class TokenBundle:
    """Result of a successful ``silent_token_refresh`` call.

    ``raw`` carries the full ``/token`` JSON response, kept for debugging
    and for fields we don't surface explicitly (``client_info``,
    ``ext_expires_in``, etc.). Treat it as opaque.
    """

    access_token: str
    refresh_token: str | None
    id_token: str | None
    expires_at: datetime
    scope: list[str]
    raw: dict[str, Any] = field(default_factory=lambda: {})


class SilentOIDCError(Exception):
    """Base class for silent OIDC failures."""


class InteractionRequiredError(SilentOIDCError):
    """SSO session is no longer valid; user must re-login interactively.

    Raised when Entra rejects the silent flow because the cookie session
    expired, MFA freshness lapsed, conditional access requires user
    interaction, or admin consent is missing for the requested scopes.
    Daemon callers should mark the credential row ``expired`` and stop
    retrying until the user re-runs the headed login.
    """


class TokenExchangeError(SilentOIDCError):
    """Protocol-level failure: malformed response, network, etc."""


def _make_pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for PKCE S256.

    RFC 7636 allows verifiers of 43-128 characters. We use 64.
    """
    verifier = secrets.token_urlsafe(48)[:64]
    challenge_bytes = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _build_authorize_url(
    config: SilentOIDCConfig,
    code_challenge: str,
    nonce: str,
    state: str,
) -> str:
    """Build the GET URL for ``/authorize?prompt=none&...``.

    Uses ``response_mode=query`` so the auth code lands in the redirect
    URL's query string (visible to httpx via the ``Location`` header)
    rather than the fragment (which would only be visible to a real
    browser).
    """
    params: dict[str, str] = {
        "client_id": config.client_id,
        "scope": " ".join(config.scope),
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "response_mode": "query",
        "prompt": "none",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "nonce": nonce,
        "state": state,
        "client-request-id": str(uuid.uuid4()),
        "x-client-SKU": "ingest-silent-oidc",
        "client_info": "1",
    }
    if config.login_hint:
        params["login_hint"] = config.login_hint
    if config.domain_hint:
        params["domain_hint"] = config.domain_hint
    return ENTRA_AUTHORIZE_URL.format(tenant=config.tenant) + "?" + urlencode(params)


def _cookies_to_jar(cookies: list[dict[str, Any]]) -> httpx.Cookies:
    """Convert Playwright-style cookie dicts to an httpx ``Cookies`` jar."""
    jar = httpx.Cookies()
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        domain = c.get("domain", "")
        path = c.get("path", "/")
        jar.set(
            name,
            value,
            domain=domain if isinstance(domain, str) else "",
            path=path if isinstance(path, str) else "/",
        )
    return jar


def _extract_code_from_html_body(body: str, expected_redirect: str) -> str:
    """Pull the auth code out of an HTML auto-redirect page.

    Entra sometimes serves a 200 HTML response instead of a 302 when the
    redirect_uri is registered as a SPA endpoint. The HTML embeds the
    real redirect URL (with the ``code`` and ``state`` in query) inside
    one of: a ``<form action="...">`` for form_post; a
    ``<meta http-equiv="refresh">``; or a ``<script>`` that sets
    ``window.location``. In every case we've seen, the redirect_uri
    appears literally in the HTML with the code in its query string. We
    pattern-match for it.
    """
    import re

    expected_host = urlparse(expected_redirect).netloc
    if not expected_host:
        raise TokenExchangeError("cannot parse HTML body: expected_redirect has no host")

    # Look for any URL on the redirect host that carries a `code=` param.
    # Quoted in either form_action or location.replace style, so we match
    # the URL fragment up to a closing quote / whitespace.
    pattern = re.compile(
        r"""https?://[^"'\s<>]*"""
        + re.escape(expected_host)
        + r"""[^"'\s<>]*[?&]code=[^"'\s<>&]+""",
        re.IGNORECASE,
    )
    match = pattern.search(body)
    if not match:
        # Common error case: Entra error page with `error=` instead of `code=`.
        err_pattern = re.compile(
            r"""https?://[^"'\s<>]*"""
            + re.escape(expected_host)
            + r"""[^"'\s<>]*[?&]error=([^"'\s<>&]+)""",
            re.IGNORECASE,
        )
        err_match = err_pattern.search(body)
        if err_match:
            err_code = err_match.group(1)
            if err_code in _INTERACTIVE_REQUIRED_ERRORS:
                raise InteractionRequiredError(f"/authorize HTML body carried error={err_code}")
            raise TokenExchangeError(f"/authorize HTML body carried error={err_code}")
        raise TokenExchangeError(
            f"/authorize returned 200 HTML but no embedded redirect URL "
            f"was found; body sample: {body[:1500]}"
        )

    return _extract_code_from_location(match.group(0), expected_redirect)


def _extract_code_from_location(location: str, expected_redirect: str) -> str:
    """Pull the ``code`` query param from a 302 ``Location`` header.

    Defensive against three failure modes:

    * Server bounces us back to interactive login (cookie session dead) —
      raised as :class:`InteractionRequiredError`.
    * Redirect carries an ``error`` param — raised as
      :class:`InteractionRequiredError` if the error class implies user
      action, otherwise :class:`TokenExchangeError`.
    * Redirect lands somewhere unexpected (neither an interactive login
      page nor the configured redirect_uri) — raised as
      :class:`TokenExchangeError`.
    """
    if not location:
        raise TokenExchangeError("/authorize 302 had empty Location header")
    parsed = urlparse(location)
    expected_host = urlparse(expected_redirect).netloc

    # If we're being bounced to interactive login, the session is dead.
    if parsed.netloc in _INTERACTIVE_LOGIN_HOSTS and parsed.netloc != expected_host:
        raise InteractionRequiredError(
            f"SSO session invalid; /authorize redirected to interactive login at {parsed.netloc}"
        )

    params = parse_qs(parsed.query)
    if "error" in params:
        err = params["error"][0]
        desc = params.get("error_description", ["?"])[0]
        if err in _INTERACTIVE_REQUIRED_ERRORS:
            raise InteractionRequiredError(f"{err}: {desc}")
        raise TokenExchangeError(f"/authorize error: {err}: {desc}")

    if "code" not in params:
        raise TokenExchangeError(
            f"/authorize redirect carried no 'code' parameter: {location[:200]}"
        )
    return params["code"][0]


def _exchange_code_for_token(
    code: str,
    code_verifier: str,
    config: SilentOIDCConfig,
    *,
    client: httpx.Client,
) -> TokenBundle:
    """POST ``/token`` to swap the auth code for an access token.

    Per RFC 6749 §4.1.3 the request is form-encoded. The ``client_id``
    must match the one on the ``/authorize`` call; ``code_verifier`` is
    the PKCE secret whose hash was sent earlier.
    """
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "code_verifier": code_verifier,
    }
    # The Teams Web Client client_id is registered as a Single-Page
    # Application in Azure AD. Entra enforces a cross-origin policy on
    # SPA token redemption (AADSTS9002327): the /token POST must look
    # like a browser fetch from the SPA's origin. The Origin header is
    # what Entra checks; httpx does not set it by default.
    redirect_origin = urlparse(config.redirect_uri)
    origin = f"{redirect_origin.scheme}://{redirect_origin.netloc}"
    resp = client.post(
        ENTRA_TOKEN_URL.format(tenant=config.tenant),
        data=body,
        headers={
            "Accept": "application/json",
            "Origin": origin,
        },
    )
    if resp.status_code != 200:
        err: dict[str, Any]
        try:
            err = dict(resp.json())
        except Exception:
            err = {"error": "non_json", "error_description": resp.text[:300]}
        err_code = str(err.get("error", ""))
        if err_code in _INTERACTIVE_REQUIRED_ERRORS:
            raise InteractionRequiredError(f"/token: {err}")
        raise TokenExchangeError(f"/token returned {resp.status_code}: {err}")

    raw_data: Any = resp.json()
    if not isinstance(raw_data, dict) or "access_token" not in raw_data:
        raise TokenExchangeError(f"/token response missing access_token: {raw_data!r:.200}")
    data = cast("dict[str, Any]", raw_data)

    expires_in = int(data.get("expires_in", 3599))
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    scope_value = data.get("scope", "")
    scope_str = scope_value if isinstance(scope_value, str) else ""

    refresh_value = data.get("refresh_token")
    id_value = data.get("id_token")

    return TokenBundle(
        access_token=str(data["access_token"]),
        refresh_token=str(refresh_value) if isinstance(refresh_value, str) else None,
        id_token=str(id_value) if isinstance(id_value, str) else None,
        expires_at=expires_at,
        scope=scope_str.split(),
        raw=data,
    )


def refresh_with_token(
    refresh_token: str,
    config: SilentOIDCConfig,
    *,
    timeout: float = 15.0,
) -> TokenBundle:
    """Exchange a refresh_token for a fresh access_token at the requested scope.

    This is the second half of the standard OAuth2 dance: once we have
    a refresh_token from any silent OIDC call (regardless of which
    resource it was issued for), we can ask Entra for tokens against
    any resource the original ``offline_access`` consent covers. Same
    client_id, just different scope.

    Practical use: do one ``silent_token_refresh`` against the
    easiest-to-mint resource (Skype, in our case) to bootstrap a
    refresh_token, then use this function for every subsequent token
    on any resource. After bootstrap, this needs no cookies.
    """
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config.client_id,
        "scope": " ".join(config.scope),
    }
    redirect_origin = urlparse(config.redirect_uri)
    origin = f"{redirect_origin.scheme}://{redirect_origin.netloc}"
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            ENTRA_TOKEN_URL.format(tenant=config.tenant),
            data=body,
            headers={
                "Accept": "application/json",
                "Origin": origin,
            },
        )
    if resp.status_code != 200:
        err: dict[str, Any]
        try:
            err = dict(resp.json())
        except Exception:
            err = {"error": "non_json", "error_description": resp.text[:300]}
        err_code = str(err.get("error", ""))
        if err_code in _INTERACTIVE_REQUIRED_ERRORS or err_code == "invalid_grant":
            raise InteractionRequiredError(f"refresh_token exchange: {err}")
        raise TokenExchangeError(f"refresh_token /token returned {resp.status_code}: {err}")

    raw_data: Any = resp.json()
    if not isinstance(raw_data, dict) or "access_token" not in raw_data:
        raise TokenExchangeError(
            f"refresh_token /token response missing access_token: {raw_data!r:.200}"
        )
    data = cast("dict[str, Any]", raw_data)
    expires_in = int(data.get("expires_in", 3599))
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    scope_value = data.get("scope", "")
    scope_str = scope_value if isinstance(scope_value, str) else ""
    refresh_value = data.get("refresh_token")
    id_value = data.get("id_token")
    return TokenBundle(
        access_token=str(data["access_token"]),
        refresh_token=str(refresh_value) if isinstance(refresh_value, str) else None,
        id_token=str(id_value) if isinstance(id_value, str) else None,
        expires_at=expires_at,
        scope=scope_str.split(),
        raw=data,
    )


def silent_token_refresh(
    cookies: list[dict[str, Any]],
    config: SilentOIDCConfig,
    *,
    timeout: float = 15.0,
) -> TokenBundle:
    """Run one silent OIDC refresh: cookies in, fresh access token out.

    Two HTTPS round-trips:

    1. ``GET /oauth2/v2.0/authorize?prompt=none&...`` with the cookie
       jar — Entra validates ``ESTSAUTHPERSISTENT``, returns 302 with
       a ``code`` in the redirect URL's query string.
    2. ``POST /oauth2/v2.0/token`` with the code + PKCE verifier —
       Entra returns the JSON token bundle.

    Raises
    ------
    InteractionRequiredError
        SSO session lapsed, MFA freshness expired, conditional access
        wants the user, or consent missing. Caller should stop the
        refresh loop and trigger interactive re-login.
    TokenExchangeError
        Protocol or network anomaly. Caller may retry with backoff.
    """
    code_verifier, code_challenge = _make_pkce_pair()
    nonce = str(uuid.uuid4())
    state = secrets.token_urlsafe(16)
    authorize_url = _build_authorize_url(config, code_challenge, nonce, state)
    jar = _cookies_to_jar(cookies)

    with httpx.Client(
        cookies=jar,
        follow_redirects=False,
        timeout=timeout,
    ) as client:
        resp = client.get(authorize_url)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            code = _extract_code_from_location(location, config.redirect_uri)
        elif resp.status_code == 200:
            # Entra occasionally serves an HTML page that JS-redirects
            # to the redirect_uri (this happens with SPA registrations
            # under some response_mode combinations). Try to extract the
            # code by parsing the form action / meta refresh / JS
            # location-set in the HTML body before giving up.
            code = _extract_code_from_html_body(resp.text, config.redirect_uri)
        else:
            raise TokenExchangeError(f"/authorize returned {resp.status_code}: {resp.text[:1500]}")

        return _exchange_code_for_token(code, code_verifier, config, client=client)
