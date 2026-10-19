"""Probe M — OIDC / OAuth anatomy from a real login.

Captures everything the Teams Web Client + its MSAL stack do against
``login.microsoftonline.com`` during a headed session, so we can pick
between two cookie-only refresh strategies:

  * **Option A (silent OIDC)** — replay
    ``/oauth2/v2.0/authorize?prompt=none`` with stored SSO cookies.
    Needs the exact ``client_id``, ``scope``, ``redirect_uri``,
    ``response_type``, ``response_mode`` Teams uses.
  * **Option C (refresh_token grant)** — grab a ``refresh_token`` once
    and POST to ``/token`` from now on. Needs to know whether MSAL
    surfaces ``refresh_token`` in the ``/token`` response body or
    stashes it in ``sessionStorage`` / ``localStorage``.

The probe is HEADED — we already proved headless doesn't load Teams' UI,
and we need MSAL.js to actually run so we can observe what it calls.
SSO cookies from disk are reused, so no manual login required: MSAL
silent-renews against the existing session.

Output (per session):
  * Every ``/oauth2/*``, ``/devicecode``, ``/.well-known/*`` request,
    grouped by path.
  * ``/authorize`` URLs decoded into ``client_id`` / ``scope`` /
    ``redirect_uri`` / ``response_type`` / ``response_mode`` / ``prompt``.
  * ``/token`` request bodies + response JSON keys, flagging
    ``refresh_token`` presence.
  * ``sessionStorage`` + ``localStorage`` keys mentioning msal/token/auth.

Usage: cd src/ingestion && uv run python scripts/probes/probe_m_oidc_anatomy.py
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))

from _probe_lib import (  # noqa: E402
    TEAMS_URL,
    filter_sso,
    load_cookies,
    load_creds,
)

OAUTH_HOST_MARKERS: tuple[str, ...] = (
    "login.microsoftonline.com",
    "login.microsoftonline.us",
    "login.live.com",
    "login.windows.net",
)

OAUTH_PATH_MARKERS: tuple[str, ...] = (
    "/oauth2/",
    "/common/oauth2/",
    "/devicecode",
    "/.well-known/openid-configuration",
)

INTERESTING_AUTHORIZE_PARAMS: tuple[str, ...] = (
    "client_id",
    "scope",
    "redirect_uri",
    "response_type",
    "response_mode",
    "prompt",
    "domain_hint",
    "login_hint",
    "code_challenge",
    "code_challenge_method",
    "nonce",
    "state",
    "client-request-id",
    "x-client-SKU",
    "x-client-Ver",
    "client_info",
    "resource",
)


def _looks_like_oauth(url: str) -> bool:
    parsed = urlparse(url)
    if not any(host in parsed.netloc for host in OAUTH_HOST_MARKERS):
        return False
    return any(marker in parsed.path for marker in OAUTH_PATH_MARKERS)


def _short_path(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}"


def run(cookies: list[dict[str, Any]], start_url: str, duration: int = 35) -> int:
    from playwright.sync_api import sync_playwright

    requests_log: list[dict[str, Any]] = []
    responses_log: dict[str, dict[str, Any]] = {}
    storage: dict[str, dict[str, str]] = {"sessionStorage": {}, "localStorage": {}}
    t0 = 0.0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            with contextlib.suppress(Exception):
                context.add_cookies(cookies)
            page = context.new_page()

            def on_request(request: Any) -> None:
                url = str(request.url)
                if not _looks_like_oauth(url):
                    return
                t_ms = int((time.monotonic() - t0) * 1000)
                post_data = ""
                try:
                    raw = request.post_data
                    if raw is not None:
                        post_data = str(raw)[:2000]
                except Exception:
                    post_data = ""
                requests_log.append(
                    {
                        "t_ms": t_ms,
                        "method": request.method,
                        "url": url,
                        "short": _short_path(url),
                        "post_data": post_data,
                    }
                )

            def on_response(response: Any) -> None:
                url = str(response.url)
                if not _looks_like_oauth(url):
                    return
                entry: dict[str, Any] = {"status": response.status}
                body = ""
                try:
                    body = response.text()
                except Exception:
                    body = ""
                entry["body"] = body[:3000]
                if body and body.lstrip().startswith("{"):
                    with contextlib.suppress(Exception):
                        data = json.loads(body)
                        if isinstance(data, dict):
                            entry["json_keys"] = sorted(data.keys())
                            entry["has_refresh_token"] = "refresh_token" in data
                            entry["has_access_token"] = "access_token" in data
                            entry["has_id_token"] = "id_token" in data
                            if "scope" in data:
                                entry["scope"] = str(data["scope"])
                responses_log[url] = entry

            page.on("request", on_request)
            page.on("response", on_response)

            t0 = time.monotonic()
            print(f"[t=0ms] goto {start_url}")
            with contextlib.suppress(Exception):
                page.goto(start_url, timeout=30_000)

            print(f"[t={int((time.monotonic() - t0) * 1000)}ms] settle {duration}s")
            for _ in range(duration):
                page.wait_for_timeout(1000)
                if not browser.is_connected():
                    break

            print("\n[storage] sampling sessionStorage + localStorage")
            try:
                ss = page.evaluate(
                    """() => {
                        const out = {};
                        for (let i = 0; i < sessionStorage.length; i++) {
                            const k = sessionStorage.key(i);
                            out[k] = sessionStorage.getItem(k);
                        }
                        return out;
                    }"""
                )
                if isinstance(ss, dict):
                    storage["sessionStorage"] = {str(k): str(v) for k, v in ss.items()}
            except Exception as exc:
                print(f"  sessionStorage read failed: {exc!s:.120}")

            try:
                ls = page.evaluate(
                    """() => {
                        const out = {};
                        for (let i = 0; i < localStorage.length; i++) {
                            const k = localStorage.key(i);
                            out[k] = localStorage.getItem(k);
                        }
                        return out;
                    }"""
                )
                if isinstance(ls, dict):
                    storage["localStorage"] = {str(k): str(v) for k, v in ls.items()}
            except Exception as exc:
                print(f"  localStorage read failed: {exc!s:.120}")
        finally:
            browser.close()

    print()
    print("=" * 72)
    print(f"[oauth_requests] total={len(requests_log)}")

    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in requests_log:
        by_path[r["short"]].append(r)
    for path in sorted(by_path):
        rs = by_path[path]
        print(f"\n  {path}  ({len(rs)} hits)")
        for r in rs[:8]:
            print(f"    t={r['t_ms']:>6}ms  {r['method']}")
        if len(rs) > 8:
            print(f"    ... +{len(rs) - 8} more")

    print("\n" + "=" * 72)
    print("[/authorize URLs — decoded]")
    authorize_count = 0
    for r in requests_log:
        if "/authorize" not in r["url"]:
            continue
        authorize_count += 1
        parsed = urlparse(r["url"])
        params = parse_qs(parsed.query)
        print(f"\n  t={r['t_ms']}ms  {r['method']}  {parsed.netloc}{parsed.path}")
        for k in INTERESTING_AUTHORIZE_PARAMS:
            if k not in params:
                continue
            v = params[k][0] if params[k] else ""
            if k == "scope":
                print(f"    {k}:")
                for s in v.split():
                    print(f"      {s}")
            else:
                print(f"    {k}: {v[:150]}")
        extras = sorted(set(params.keys()) - set(INTERESTING_AUTHORIZE_PARAMS))
        if extras:
            print(f"    other: {extras}")
    if authorize_count == 0:
        print("  (none captured)")

    print("\n" + "=" * 72)
    print("[/token requests — body + response]")
    token_count = 0
    has_any_refresh = False
    for r in requests_log:
        if not r["url"].endswith("/token"):
            continue
        token_count += 1
        print(f"\n  t={r['t_ms']}ms  {r['method']}  {r['short']}")
        if r["post_data"]:
            print(f"    request body: {r['post_data'][:800]}")
        resp = responses_log.get(r["url"], {})
        if resp:
            print(f"    status: {resp.get('status')}")
            if "json_keys" in resp:
                print(f"    json keys: {resp['json_keys']}")
            print(f"    has_access_token : {resp.get('has_access_token')}")
            print(f"    has_refresh_token: {resp.get('has_refresh_token')}")
            print(f"    has_id_token     : {resp.get('has_id_token')}")
            if resp.get("has_refresh_token"):
                has_any_refresh = True
            if resp.get("scope"):
                print(f"    scope: {resp['scope'][:200]}")
            print(f"    body sample: {resp.get('body', '')[:400]}")
    if token_count == 0:
        print("  (none captured)")

    print("\n" + "=" * 72)
    print("[storage analysis]")
    for store_name in ("sessionStorage", "localStorage"):
        items = storage.get(store_name, {})
        print(f"\n  {store_name}: {len(items)} keys total")
        interesting = [
            (k, v) for k, v in items.items()
            if any(n in k.lower() for n in ("msal", "token", "refresh", "auth"))
        ]
        if not interesting:
            print("    (no msal/token/refresh/auth keys)")
            continue
        for k, v in interesting[:25]:
            print(f"    {k[:90]}")
            print(f"      value sample ({len(v)}b): {v[:140]}")
        if len(interesting) > 25:
            print(f"    ... +{len(interesting) - 25} more")

    print("\n" + "=" * 72)
    print("[summary]")
    print(f"  authorize_requests : {authorize_count}")
    print(f"  token_requests     : {token_count}")
    print(f"  refresh_token_seen : {has_any_refresh}")
    storage_refresh_keys = [
        k for store in storage.values() for k in store
        if "refresh" in k.lower()
    ]
    print(f"  storage_refresh_keys: {len(storage_refresh_keys)} -> {storage_refresh_keys[:6]}")
    print()
    print("Decision guide:")
    print("  - If /authorize captured with full params -> Option A (silent OIDC) is buildable.")
    print("  - If refresh_token_seen in /token response or storage -> Option C is buildable.")

    return 0


def main() -> int:
    teams_creds = load_creds()
    _ = teams_creds
    start_url = TEAMS_URL
    cookies = filter_sso(load_cookies())
    print(f"[setup] sso_cookies={len(cookies)} start_url={start_url}")
    print("[note] HEADED browser will open for ~35s. Don't close it.")
    print()
    return run(cookies, start_url, duration=35)


if __name__ == "__main__":
    sys.exit(main())
