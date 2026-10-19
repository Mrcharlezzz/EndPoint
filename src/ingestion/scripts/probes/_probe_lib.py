"""Shared helpers for Teams silent-refresh probes.

Standalone — does NOT import ingestion.cli or ingestion.daemon so probes
stay isolated from the production refresh path and can be iterated on
without risk to the live system.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_CREDENTIALS = Path.home() / ".config" / "ingest" / "credentials.json"
DEFAULT_COOKIES = Path.home() / ".config" / "ingest" / "sessions" / "teams_cookies.json"

TEAMS_URL = "https://teams.microsoft.com"
REQUIRED_GRAPH_SCOPE = "ChannelMessage.Read.All"

# Mirrors ingestion.daemon.auth.refresh._SSO_DOMAIN_SUFFIXES — keep in sync.
SSO_DOMAIN_SUFFIXES: tuple[str, ...] = (
    ".login.microsoftonline.com",
    ".login.microsoftonline.us",
    ".login.microsoftonline-p.cn",
    ".login.microsoftonline.de",
    ".login.live.com",
    "login.microsoftonline.com",
    "login.microsoftonline.us",
    "login.microsoftonline-p.cn",
    "login.microsoftonline.de",
    "login.live.com",
)

LOGIN_REDIRECT_HOSTS: tuple[str, ...] = (
    "login.microsoftonline.com",
    "login.microsoftonline.us",
    "login.microsoftonline-p.cn",
    "login.live.com",
)


def load_creds() -> dict[str, Any]:
    """Load and return the ``teams`` block from CLI credentials.json."""
    if not DEFAULT_CREDENTIALS.exists():
        print(f"error: credentials.json not found at {DEFAULT_CREDENTIALS}", file=sys.stderr)
        sys.exit(2)
    return json.loads(DEFAULT_CREDENTIALS.read_text()).get("teams", {})


def load_cookies() -> list[dict[str, Any]]:
    """Load the saved cookie jar."""
    if not DEFAULT_COOKIES.exists():
        print(f"error: cookies file not found at {DEFAULT_COOKIES}", file=sys.stderr)
        sys.exit(2)
    raw: Any = json.loads(DEFAULT_COOKIES.read_text())
    if not isinstance(raw, list):
        print("error: cookies file is not a list", file=sys.stderr)
        sys.exit(2)
    result: list[dict[str, Any]] = []
    for entry in raw:  # type: ignore[reportUnknownVariableType]
        if isinstance(entry, dict):
            result.append(entry)
    return result


def is_sso_cookie(cookie: dict[str, Any]) -> bool:
    domain = cookie.get("domain", "")
    if not isinstance(domain, str):
        return False
    return any(domain.endswith(suffix) for suffix in SSO_DOMAIN_SUFFIXES)


def filter_sso(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in cookies if is_sso_cookie(c)]


def group_by_domain(cookies: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for c in cookies:
        domain = str(c.get("domain", ""))
        out.setdefault(domain, []).append(c)
    return out


def decode_jwt(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        import jwt  # PyJWT

        return jwt.decode(token, options={"verify_signature": False})
    except Exception:
        return None


def token_scopes(token: str) -> list[str]:
    decoded = decode_jwt(token) or {}
    return str(decoded.get("scp", "")).split()


def token_has_scope(token: str, scope: str) -> bool:
    return scope in token_scopes(token)


def token_audience(token: str) -> str:
    decoded = decode_jwt(token) or {}
    return str(decoded.get("aud", ""))


def token_exp(token: str) -> int:
    decoded = decode_jwt(token) or {}
    return int(decoded.get("exp", 0))


def exp_remaining_minutes(token: str) -> int | None:
    exp = token_exp(token)
    if exp == 0:
        return None
    return int((datetime.fromtimestamp(exp, tz=UTC) - datetime.now(tz=UTC)).total_seconds() / 60)


def token_fingerprint(token: str) -> str:
    """Short stable identifier for a token (first 12 hex chars of sha256)."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def short_url(url: str, max_path: int = 60) -> str:
    """Host + truncated path + whether a query existed (queries elided)."""
    try:
        parsed = urlparse(url)
        path = parsed.path
        if len(path) > max_path:
            path = path[:max_path] + "..."
        q_marker = "?..." if parsed.query else ""
        return f"{parsed.netloc}{path}{q_marker}"
    except Exception:
        return url[:max_path]


def format_cookie_row(cookie: dict[str, Any]) -> str:
    name = str(cookie.get("name", ""))[:30]
    domain = str(cookie.get("domain", ""))[:32]
    same_site = str(cookie.get("sameSite", ""))[:6]
    secure = "S" if cookie.get("secure") else "-"
    http_only = "H" if cookie.get("httpOnly") else "-"
    exp = cookie.get("expires")
    if isinstance(exp, int | float) and exp > 0:
        try:
            exp_str = datetime.fromtimestamp(exp, tz=UTC).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            exp_str = "?"
    else:
        exp_str = "session"
    size = len(str(cookie.get("value", "")))
    return f"  {domain:<34} {name:<32} {secure}{http_only} {same_site:<6} {exp_str:<12} {size}b"
