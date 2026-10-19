"""Probe N — End-to-end smoke test for silent OIDC refresh.

Validates ``ingestion.auth.silent_oidc.silent_token_refresh`` against
the saved SSO cookie jar in two configurations:

  * **skype**: same scope set MSAL.js used in probe M (Skype audience),
    serves as a sanity check that our flow matches what the Teams web
    client does internally.
  * **graph**: ``https://graph.microsoft.com/.default``, our actual
    target. Success here means the daemon refresh path can drop
    Playwright entirely.

For each, decodes the returned JWT and asserts:

  * ``aud`` matches the requested resource.
  * ``appid == 5e3ce6c0-2b1f-4285-8d4b-75ee78787346`` (Microsoft Teams
    Web Client). This is the discriminator that distinguishes our
    target token from the Assignments UI fallback the headless daemon
    currently captures.
  * Scope set is non-trivial. For graph specifically, looks for
    ``Mail.``, ``Calendars.``, or ``ChatMessage.Send`` markers — these
    are present on the Teams Web Client appid but absent on Assignments
    UI, so they uniquely identify a successful target capture.

Usage: cd src/ingestion && uv run python scripts/probes/probe_n_silent_oidc.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _probe_lib import (  # noqa: E402
    decode_jwt,
    filter_sso,
    load_cookies,
    token_audience,
    token_fingerprint,
    token_scopes,
)
from ingestion.auth.silent_oidc import (  # noqa: E402
    InteractionRequiredError,
    SilentOIDCConfig,
    SilentOIDCError,
    TokenBundle,
    TokenExchangeError,
    refresh_with_token,
    silent_token_refresh,
)

TEAMS_WEB_CLIENT_ID = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"
TEAMS_REDIRECT_URI = "https://teams.microsoft.com/v2/authv2"

# Scope markers that uniquely belong to the Teams Web Client appid (30-scope
# superset) and never appear on the Assignments UI appid (12-scope subset).
DISCRIMINATOR_SCOPES: tuple[str, ...] = (
    "Mail.Read",
    "Mail.ReadWrite",
    "Calendars.Read",
    "Calendars.ReadWrite",
    "ChatMessage.Send",
)

CONFIGS: dict[str, tuple[SilentOIDCConfig, str]] = {
    "skype": (
        SilentOIDCConfig(
            client_id=TEAMS_WEB_CLIENT_ID,
            redirect_uri=TEAMS_REDIRECT_URI,
            scope=[
                "https://api.spaces.skype.com/.default",
                "openid",
                "profile",
                "offline_access",
            ],
        ),
        "https://api.spaces.skype.com",
    ),
    "graph": (
        SilentOIDCConfig(
            client_id=TEAMS_WEB_CLIENT_ID,
            redirect_uri=TEAMS_REDIRECT_URI,
            scope=[
                "https://graph.microsoft.com/.default",
                "openid",
                "profile",
                "offline_access",
            ],
        ),
        "https://graph.microsoft.com",
    ),
}


def _summarize_bundle(bundle: TokenBundle, expected_aud: str) -> tuple[bool, list[str]]:
    """Return (passed, failure_reasons) for one bundle."""
    failures: list[str] = []
    claims = decode_jwt(bundle.access_token) or {}
    appid = str(claims.get("appid", ""))
    aud = token_audience(bundle.access_token)
    scopes = token_scopes(bundle.access_token)

    print(f"    expires_at  : {bundle.expires_at.isoformat()}")
    print(f"    refresh_tok : {'yes' if bundle.refresh_token else 'no'}")
    print(f"    id_tok      : {'yes' if bundle.id_token else 'no'}")
    print(f"    fingerprint : {token_fingerprint(bundle.access_token)}")
    print(f"    aud         : {aud}")
    print(f"    appid       : {appid}")
    print(f"    appname     : {claims.get('app_displayname', '?')}")
    print(f"    upn         : {claims.get('upn', '?')}")
    head = bundle.scope[:6]
    suffix = "..." if len(bundle.scope) > 6 else ""
    print(f"    granted_scope({len(bundle.scope)}): {head}{suffix}")
    print(f"    token_scp   ({len(scopes)}):")
    for s in scopes:
        marker = "  <-- discriminator" if s in DISCRIMINATOR_SCOPES else ""
        print(f"      {s}{marker}")

    if aud != expected_aud and not aud.startswith(expected_aud):
        failures.append(f"aud={aud!r} does not match expected {expected_aud!r}")
    if appid != TEAMS_WEB_CLIENT_ID:
        failures.append(f"appid={appid!r} != target {TEAMS_WEB_CLIENT_ID}")
    if expected_aud == "https://graph.microsoft.com" and not any(
        s in DISCRIMINATOR_SCOPES for s in scopes
    ):
        failures.append(
            "no discriminator scopes present (Mail.*, Calendars.*, "
            "ChatMessage.Send) - looks like Assignments-UI-flavored token"
        )
    return (not failures, failures)


def _try(name: str, config: SilentOIDCConfig, expected_aud: str, cookies: list[dict]) -> int:
    print(f"\n{'=' * 72}")
    print(f"[{name}] scope = {' '.join(config.scope)}")
    t0 = time.monotonic()
    try:
        bundle = silent_token_refresh(cookies, config)
    except InteractionRequiredError as exc:
        print(f"  FAIL (InteractionRequired): {exc}")
        return 2
    except TokenExchangeError as exc:
        print(f"  FAIL (TokenExchange): {exc}")
        return 1
    except SilentOIDCError as exc:
        print(f"  FAIL (other): {exc}")
        return 1
    elapsed = (time.monotonic() - t0) * 1000
    print(f"  OK in {elapsed:.0f}ms")

    passed, failures = _summarize_bundle(bundle, expected_aud)
    if passed:
        print("  -> PASS")
        return 0
    print("  -> assertion failures:")
    for f in failures:
        print(f"     - {f}")
    return 1


def main() -> int:
    cookies = filter_sso(load_cookies())
    print(f"[setup] sso_cookies={len(cookies)} target_appid={TEAMS_WEB_CLIENT_ID}")

    # Run Skype first; if it succeeds, harvest upn from its access_token
    # and re-build the Graph config with login_hint set. Without
    # login_hint the Graph silent flow returns interaction_required on
    # this tenant (observed 2026-04-25).
    skype_config, skype_aud = CONFIGS["skype"]
    rc = _try("skype", skype_config, skype_aud, cookies)

    login_hint: str | None = None
    if rc == 0:
        try:
            bundle = silent_token_refresh(cookies, skype_config)
            claims = decode_jwt(bundle.access_token) or {}
            upn = claims.get("upn") or claims.get("preferred_username")
            if isinstance(upn, str) and upn:
                login_hint = upn
                print(f"\n[hint] using login_hint={login_hint!r} for graph attempt")
        except Exception as exc:
            print(f"\n[hint] could not derive login_hint: {exc!s:.200}")

    graph_base, graph_aud = CONFIGS["graph"]
    graph_config = SilentOIDCConfig(
        client_id=graph_base.client_id,
        redirect_uri=graph_base.redirect_uri,
        scope=graph_base.scope,
        tenant=graph_base.tenant,
        login_hint=login_hint,
    )
    direct_graph_rc = _try("graph (direct silent OIDC)", graph_config, graph_aud, cookies)
    if direct_graph_rc != 0:
        print(
            "  note: direct silent OIDC for Graph is expected to fail on tenants "
            "where the cookie session lacks Graph freshness. Chained path below "
            "is the production strategy."
        )

    # Chained path: bootstrap a refresh_token via Skype silent OIDC,
    # then exchange it for a Graph token via grant_type=refresh_token.
    # This is what MSAL.js does internally for cross-resource silent
    # renewal once it has any refresh_token in cache.
    print(f"\n{'=' * 72}")
    print("[graph (refresh_token chain)] bootstrap via Skype, then exchange")
    chain_rc = 0
    try:
        skype_bundle = silent_token_refresh(cookies, skype_config)
    except SilentOIDCError as exc:
        print(f"  FAIL bootstrapping Skype: {exc}")
        return max(rc, 1)
    if not skype_bundle.refresh_token:
        print("  FAIL: Skype call did not return a refresh_token")
        return max(rc, 1)
    print(f"  bootstrap Skype OK; refresh_token len={len(skype_bundle.refresh_token)}")
    t0 = time.monotonic()
    try:
        chained = refresh_with_token(skype_bundle.refresh_token, graph_config)
    except InteractionRequiredError as exc:
        print(f"  FAIL (InteractionRequired): {exc}")
        chain_rc = 2
    except TokenExchangeError as exc:
        print(f"  FAIL (TokenExchange): {exc}")
        chain_rc = 1
    else:
        print(f"  exchange OK in {(time.monotonic() - t0) * 1000:.0f}ms")
        passed, failures = _summarize_bundle(chained, graph_aud)
        if passed:
            print("  -> PASS")
        else:
            chain_rc = 1
            print("  -> assertion failures:")
            for f in failures:
                print(f"     - {f}")

    # Production path = Skype bootstrap + chained Graph. Direct-Graph is
    # informational only; its failure does not invalidate the strategy.
    rc = max(rc, chain_rc)

    print(f"\n{'=' * 72}")
    if rc == 0:
        print("RESULT: silent OIDC mints the target appid for both audiences.")
        print("Ready to wire HttpxTokenRefresher into ingestion/daemon/auth/refresh.py.")
    elif rc == 2:
        print("RESULT: SSO session has lapsed — re-run `ingest login teams` and retry.")
    else:
        print(f"RESULT: at least one config failed (rc={rc}).")
    return rc


if __name__ == "__main__":
    sys.exit(main())
