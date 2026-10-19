"""``endpoint-admin`` command — operator CLI for the ingestion daemon.

Wraps the daemon's HTTP control-panel API (``/api/actions/*``,
``/api/health``, ``/api/metrics``, ``/api/config/overlay/*``) so on-call
operators can pause a user, force a sync, clear dead-letter, dump state,
or read metrics from a shell — no dashboard required.

Authentication mirrors the dashboard's session-cookie scheme: the CLI
either accepts a pre-issued cookie via ``--session-cookie`` (env:
``ENDPOINT_DAEMON_SESSION_COOKIE``) or performs a one-shot login with
the admin password (env: ``ENDPOINT_ADMIN_PASSWORD`` or
``--password-file``). The password is never accepted as a bare CLI
argument — that would leak it into shell history.

Output:

* Default: structured JSON on stdout (one document per command).
* ``--pretty``: human-friendly indentation.
* Warnings / progress / errors: stderr.

Exit codes (consistent with the spec's edge brainstorm):

* ``0``  success
* ``1``  daemon returned an error envelope (4xx / 5xx)
* ``2``  authentication failure (cookie expired or password wrong)
* ``3``  daemon unreachable / network error
* ``4``  CLI input validation failure (bad URL shape, missing args)

All write subcommands go through ``/api/actions/*``; this module never
talks to the daemon's SQLite directly. The single read-only escape is
the ``debug lockfile`` subcommand defined in
:mod:`ingestion.cli.admin_debug`, which inspects the lockfile without
needing a running daemon.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from ingestion import __version__

# Suspicious config keys: setting these via the CLI is almost always a bug
# (rotating SECRET_KEY / admin password belongs in env / secrets manager,
# not in the live SQLite overlay). Trip a stderr warning so the operator
# at least sees the foot-gun before pulling the trigger.
_SUSPICIOUS_CONFIG_KEYS = (
    "secret",
    "password",
    "session_secret",
    "fernet",
    "api_key",
    "token",
)

# Cookie name must match :data:`ingestion.daemon.api.auth.COOKIE_NAME`.
# Hard-coded here (instead of imported) so the CLI can run without
# ``daemon`` extras installed (``itsdangerous``, ``fastapi``, etc.).
_SESSION_COOKIE_NAME = "endpoint_admin_session"

# Network timeout for every daemon call. Operations are short (the daemon
# does the heavy lifting); a tight ceiling means a wedged daemon shows up
# as exit 3 in seconds rather than minutes.
_HTTP_TIMEOUT_SECONDS = 15.0

# Exit codes — referenced from edge-case tests by name.
EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_AUTH_ERROR = 2
EXIT_NETWORK_ERROR = 3
EXIT_VALIDATION_ERROR = 4


def _err(message: str) -> None:
    """Write a single line to stderr.

    Stdout is reserved for the JSON output blob so callers can pipe
    ``endpoint-admin status | jq`` cleanly. Anything informational —
    progress, warnings, error envelopes — flows through here.
    """
    print(message, file=sys.stderr)


def _print_json(payload: Any, *, pretty: bool) -> None:
    """Serialise ``payload`` to stdout. Pretty mode uses 2-space indent."""
    if pretty:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
    else:
        json.dump(payload, sys.stdout, default=str)
    print()  # trailing newline so terminal prompts land on the next line


def _validate_base_url(url: str) -> str:
    """Reject URLs without a scheme — ``localhost:8080`` parses ambiguously
    in httpx (treats ``localhost`` as the scheme) and yields confusing
    failure modes.
    """
    if not re.match(r"^https?://", url):
        _err(f"--base-url {url!r} must include http:// or https://")
        raise typer.Exit(code=EXIT_VALIDATION_ERROR)
    return url.rstrip("/")


def _read_password(password_file: Path | None) -> str | None:
    """Resolve the admin password from a file or env var, in that order.

    The CLI never accepts the password as a positional/argv flag — that
    would leak it into shell history. ``--password-file`` lets operators
    point at a 0600-mode file; absent that, the password comes from
    ``ENDPOINT_ADMIN_PASSWORD`` (env vars are inherited by subshells but
    don't end up in history).
    """
    if password_file is not None:
        try:
            return password_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            _err(f"could not read --password-file {password_file}: {exc}")
            raise typer.Exit(code=EXIT_VALIDATION_ERROR) from exc
    return os.environ.get("ENDPOINT_ADMIN_PASSWORD") or None


@dataclass(frozen=True)
class ClientOptions:
    """Immutable bundle of resolved options for one daemon session.

    Frozen so accidentally mutating it inside a subcommand yields a
    typed error rather than corrupting a sibling call's settings (the
    Typer callback installs one instance on a context bag).
    """

    base_url: str
    session_cookie: str | None
    password: str | None
    pretty: bool
    # Optional explicit Idempotency-Key. When None the CLI auto-
    # generates a fresh uuid4().hex per mutating request; when set,
    # every mutating subcommand reuses this exact value. Operators
    # scripting controlled retries (e.g. a chaos-test harness that
    # wants to verify replay behaviour) pin their own key here.
    # Default ``None`` keeps backwards-compatibility with existing
    # callers that construct ClientOptions positionally without the
    # new field (slice 9 tests, the edges/s9 harness).
    idempotency_key: str | None = None


class _DaemonClient:
    """Thin httpx wrapper that handles auth + canonical error mapping.

    One instance per command invocation. The underlying ``httpx.Client``
    is constructed lazily on the first ``request()`` and reused for
    every subsequent call in this invocation — that's the connection-
    reuse contract operators rely on when scripting bursts (a
    ``dlq replay`` chain reuses one TCP socket across login, the
    initial 404, the clear, and the force_sync, instead of opening
    four separate connections).

    Auth flows:

    1. Caller supplies a session cookie -> attached to the client jar at
       construction so every request carries it automatically.
    2. Caller supplies a password -> POST ``/api/auth/login`` once,
       capture the ``Set-Cookie`` value, then mirror it onto the shared
       jar so subsequent requests on the same client carry it.
    3. Neither -> exit 2 immediately with a clear hint.

    Use this class as a context manager (or call :meth:`close`) to
    tear the HTTP client down deterministically.
    """

    def __init__(self, opts: ClientOptions) -> None:
        self._opts = opts
        self._cookie: str | None = opts.session_cookie
        self._http: httpx.Client | None = None

    def __enter__(self) -> _DaemonClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying httpx.Client if it was created."""
        if self._http is not None:
            self._http.close()
            self._http = None

    def _ensure_cookie(self, http: httpx.Client) -> str:
        """Return the session cookie value, performing login if needed.

        Memoised on ``self`` so a multi-call subcommand (status hits two
        endpoints) only logs in once. After a successful login the
        cookie is mirrored onto ``http.cookies`` so subsequent requests
        on this client send it automatically without a per-request
        ``cookies=`` argument (which httpx 0.28 deprecated).
        """
        if self._cookie:
            return self._cookie
        if not self._opts.password:
            _err(
                "authentication required: set ENDPOINT_DAEMON_SESSION_COOKIE, "
                "ENDPOINT_ADMIN_PASSWORD, or pass --password-file"
            )
            raise typer.Exit(code=EXIT_AUTH_ERROR)
        try:
            response = http.post(
                "/api/auth/login",
                json={"password": self._opts.password},
            )
        except httpx.HTTPError as exc:
            _err(f"daemon unreachable at {self._opts.base_url} ({exc})")
            raise typer.Exit(code=EXIT_NETWORK_ERROR) from exc
        if response.status_code in (401, 403):
            _err("authentication failed (wrong password)")
            raise typer.Exit(code=EXIT_AUTH_ERROR)
        if response.status_code >= 400:
            _err(self._format_envelope(response, default="login failed"))
            raise typer.Exit(code=EXIT_API_ERROR)
        cookie = response.cookies.get(_SESSION_COOKIE_NAME)
        if not cookie:
            _err("login succeeded but no session cookie was returned")
            raise typer.Exit(code=EXIT_AUTH_ERROR)
        self._cookie = cookie
        # Mirror the cookie onto the shared client's jar so subsequent
        # requests carry it without per-request cookies= arguments.
        http.cookies.set(_SESSION_COOKIE_NAME, cookie)
        return cookie

    def _format_envelope(self, response: httpx.Response, *, default: str) -> str:
        """Pull a readable error message out of the daemon's envelope.

        The daemon wraps every 4xx / 5xx body in
        ``{"detail": {"code": ..., "message": ..., "request_id": ...}}``
        (see :mod:`ingestion.daemon.api.error_envelope`). Fall back to
        the raw body when the shape doesn't match — some upstream proxies
        intercept errors before they reach the daemon.
        """
        try:
            body = response.json()
        except ValueError:
            return f"{default}: {response.text or response.reason_phrase}"
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            code = detail.get("code") or "error"
            message = detail.get("message") or default
            req = detail.get("request_id")
            tail = f" [request_id={req}]" if req else ""
            return f"[{code}] {message}{tail}"
        return f"{default}: {body!r}"

    def _client(self) -> httpx.Client:
        """Return the cached httpx client, building it lazily on first use.

        One client is constructed per ``_DaemonClient`` instance and
        reused across every ``request()`` call in this CLI invocation,
        so a multi-call subcommand (``dlq replay``: login + 404 + clear
        + force_sync) reuses one TCP connection instead of building
        four. Cookies are seeded from ``self._cookie`` at construction
        so the client's jar carries them on every outbound request —
        the per-request ``cookies=`` argument was deprecated in httpx
        0.28. Tests can monkeypatch this method to inject a
        ``MockTransport``.
        """
        if self._http is None:
            cookies: dict[str, str] = {}
            if self._cookie:
                cookies[_SESSION_COOKIE_NAME] = self._cookie
            self._http = httpx.Client(
                base_url=self._opts.base_url,
                timeout=_HTTP_TIMEOUT_SECONDS,
                follow_redirects=False,
                cookies=cookies,
            )
        return self._http

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        require_auth: bool = True,
        idempotency_key: str | None = None,
    ) -> Any:
        """Perform one daemon request and return parsed JSON.

        ``require_auth=False`` skips cookie-handling entirely (used for
        public probes — ``/api/health``, ``/api/metrics``, ``/api/ready``).
        Mutating verbs always carry the X-Requested-With CSRF header
        and an auto-generated ``Idempotency-Key`` (slice 5 part C) so a
        flaky retry from the operator's terminal can't double-execute
        a destructive action.

        The auto-generated key is a fresh ``uuid4().hex``; if the
        caller passes ``idempotency_key`` explicitly (or sets the
        ``ENDPOINT_IDEMPOTENCY_KEY`` environment variable on the
        Typer callback), the explicit value takes precedence.
        """
        http = self._client()
        headers: dict[str, str] = {}
        if require_auth:
            self._ensure_cookie(http)
        upper_method = method.upper()
        if upper_method in {"POST", "PUT", "PATCH", "DELETE"}:
            headers["X-Requested-With"] = "XMLHttpRequest"
            # Slice 5 part C: auto-attach Idempotency-Key on mutating
            # verbs so a network-blip retry on a CLI subcommand
            # (pause, force-sync, purge, dlq replay) is replayed
            # against the cache rather than re-executed against the
            # source of truth. Manual override wins — operators
            # scripting controlled retries can pin their own key.
            resolved_key = idempotency_key or self._opts.idempotency_key
            if resolved_key is None:
                resolved_key = uuid.uuid4().hex
            headers["Idempotency-Key"] = resolved_key
        try:
            response = http.request(
                method,
                path,
                json=json_body,
                params=params,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            _err(f"daemon unreachable at {self._opts.base_url} ({exc})")
            raise typer.Exit(code=EXIT_NETWORK_ERROR) from exc
        if response.status_code in (401, 403):
            _err(self._format_envelope(response, default="authentication failed"))
            raise typer.Exit(code=EXIT_AUTH_ERROR)
        if response.status_code >= 400:
            _err(self._format_envelope(response, default=f"HTTP {response.status_code}"))
            raise typer.Exit(code=EXIT_API_ERROR)
        content_type = response.headers.get("content-type", "")
        if not response.content:
            return None
        if "application/json" in content_type:
            return response.json()
        return response.text


# ---------------------------------------------------------------------------
# Typer app + global options
# ---------------------------------------------------------------------------


app = typer.Typer(
    name="endpoint-admin",
    help="Operator CLI for the ingestion daemon (pause / force / DLQ / status).",
    no_args_is_help=True,
    add_completion=False,
)


_OPTS_STATE_KEY = "client_options"


def _resolve_options(
    *,
    base_url: str,
    session_cookie: str | None,
    password_file: Path | None,
    pretty: bool,
    idempotency_key: str | None,
) -> ClientOptions:
    """Resolve and validate top-level options into one immutable bundle.

    Returns a frozen :class:`ClientOptions`. Cookie precedence (strict,
    highest first):

    1. ``--session-cookie`` flag (when explicitly passed)
    2. ``ENDPOINT_DAEMON_SESSION_COOKIE`` environment variable
    3. Password-derived (login at request time using
       ``ENDPOINT_ADMIN_PASSWORD`` / ``--password-file``)

    Typer reads the env var into ``session_cookie`` automatically (via
    the ``envvar=`` declaration on the option), so by the time we get
    here the flag-then-env precedence is already encoded in the
    parameter — no second os.environ lookup needed. Empty / whitespace-
    only strings are treated as "not set" so an accidentally-blank
    value falls through to the password path instead of impersonating
    a real cookie.
    """
    base_url_resolved = _validate_base_url(base_url)
    cookie = session_cookie.strip() if session_cookie else None
    if not cookie:
        cookie = None
    password = _read_password(password_file)
    # Same empty-string handling as the cookie path so an accidentally-
    # blank flag falls through to the auto-generation default.
    resolved_key = idempotency_key.strip() if idempotency_key else None
    if not resolved_key:
        resolved_key = None
    return ClientOptions(
        base_url=base_url_resolved,
        session_cookie=cookie,
        password=password,
        pretty=pretty,
        idempotency_key=resolved_key,
    )


@app.callback(invoke_without_command=True)
def _root(  # pyright: ignore[reportUnusedFunction]
    ctx: typer.Context,
    base_url: Annotated[
        str,
        typer.Option(
            "--base-url",
            envvar="ENDPOINT_DAEMON_BASE_URL",
            help="Daemon HTTP base URL (default: http://localhost:8765)",
        ),
    ] = "http://localhost:8765",
    session_cookie: Annotated[
        str | None,
        typer.Option(
            "--session-cookie",
            envvar="ENDPOINT_DAEMON_SESSION_COOKIE",
            help="Pre-issued endpoint_admin_session cookie value",
        ),
    ] = None,
    password_file: Annotated[
        Path | None,
        typer.Option(
            "--password-file",
            help="Read ENDPOINT_ADMIN_PASSWORD from this file (mode 0600)",
            exists=False,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    pretty: Annotated[
        bool,
        typer.Option("--pretty", help="Pretty-print JSON output (default: compact)"),
    ] = False,
    idempotency_key: Annotated[
        str | None,
        typer.Option(
            "--idempotency-key",
            envvar="ENDPOINT_IDEMPOTENCY_KEY",
            help=(
                "Pin a specific Idempotency-Key for every mutating request. "
                "Default: a fresh uuid4().hex per request. Used by chaos-test "
                "harnesses that need replay determinism."
            ),
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option("--version", help="Show CLI version and exit", is_eager=True),
    ] = False,
) -> None:
    """Top-level callback. Resolves shared options, prints help when bare."""
    if version:
        print(f"endpoint-admin {__version__}")
        raise typer.Exit()
    ctx.ensure_object(dict)
    ctx.obj[_OPTS_STATE_KEY] = _resolve_options(
        base_url=base_url,
        session_cookie=session_cookie,
        password_file=password_file,
        pretty=pretty,
        idempotency_key=idempotency_key,
    )
    if ctx.invoked_subcommand is None:
        # Typer renders rich help; print and exit cleanly.
        print(ctx.get_help())
        raise typer.Exit()


def get_client(ctx: typer.Context) -> _DaemonClient:
    """Pull the resolved :class:`ClientOptions` out of the Typer context
    and wrap it in a :class:`_DaemonClient` for the subcommand.

    Public (no underscore) so :mod:`ingestion.cli.admin_debug` can call
    it from its subcommand handlers without tripping pyright's
    reportPrivateUsage rule.
    """
    opts = ctx.obj[_OPTS_STATE_KEY]
    assert isinstance(opts, ClientOptions)
    return _DaemonClient(opts)


def get_options(ctx: typer.Context) -> ClientOptions:
    """Return the resolved :class:`ClientOptions` from the Typer context.

    Public for the same cross-module reason as :func:`get_client`.
    """
    opts = ctx.obj[_OPTS_STATE_KEY]
    assert isinstance(opts, ClientOptions)
    return opts


# ---------------------------------------------------------------------------
# Pause / unpause
# ---------------------------------------------------------------------------


@app.command("pause")
def pause(
    ctx: typer.Context,
    email: Annotated[str, typer.Argument(help="User email to pause")],
    platform: Annotated[
        str | None,
        typer.Option("--platform", help="One platform (teams|moodle|telegram); omit for all"),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Audit reason (recommended for incident postmortems)"),
    ] = None,
) -> None:
    """Pause syncs for a user. Idempotent.

    Examples:
        endpoint-admin pause alice@example.com --platform teams
        endpoint-admin pause alice@example.com --reason "rotation"
    """
    client = get_client(ctx)
    body = {"reason": reason} if reason else {}
    if platform:
        path = f"/api/actions/users/{email}/{platform}/pause"
    else:
        path = f"/api/actions/users/{email}/pause"
    payload = client.request("POST", path, json_body=body)
    _print_json(payload, pretty=get_options(ctx).pretty)


@app.command("unpause")
def unpause(
    ctx: typer.Context,
    email: Annotated[str, typer.Argument(help="User email to unpause")],
    platform: Annotated[
        str | None,
        typer.Option("--platform", help="One platform (teams|moodle|telegram); omit for all"),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Audit reason"),
    ] = None,
) -> None:
    """Unpause syncs for a user. Idempotent.

    Example:
        endpoint-admin unpause alice@example.com --platform teams
    """
    client = get_client(ctx)
    body = {"reason": reason} if reason else {}
    if platform:
        path = f"/api/actions/users/{email}/{platform}/unpause"
    else:
        path = f"/api/actions/users/{email}/unpause"
    payload = client.request("POST", path, json_body=body)
    _print_json(payload, pretty=get_options(ctx).pretty)


# ---------------------------------------------------------------------------
# Force-sync
# ---------------------------------------------------------------------------


@app.command("force-sync")
def force_sync(
    ctx: typer.Context,
    email: Annotated[str, typer.Argument(help="User email to force-sync")],
    platform: Annotated[str, typer.Option("--platform", help="Platform (required)")],
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Audit reason"),
    ] = None,
) -> None:
    """Queue an immediate sync for (email, platform).

    Example:
        endpoint-admin force-sync alice@example.com --platform teams
    """
    client = get_client(ctx)
    body = {"reason": reason} if reason else {}
    path = f"/api/actions/users/{email}/{platform}/force_sync"
    payload = client.request("POST", path, json_body=body)
    _print_json(payload, pretty=get_options(ctx).pretty)


# ---------------------------------------------------------------------------
# Dead-letter queue (clear / replay)
# ---------------------------------------------------------------------------

dlq_app = typer.Typer(name="dlq", help="Dead-letter queue operations", no_args_is_help=True)
app.add_typer(dlq_app)


@dlq_app.command("clear")
def dlq_clear(
    ctx: typer.Context,
    email: Annotated[str, typer.Argument(help="User email")],
    platform: Annotated[str, typer.Option("--platform", help="Platform (required)")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Clear dead-letter suppression for (email, platform).

    Example:
        endpoint-admin dlq clear alice@example.com --platform moodle
    """
    client = get_client(ctx)
    body = {"reason": reason} if reason else {}
    path = f"/api/actions/dead_letter/{email}/{platform}/clear"
    payload = client.request("POST", path, json_body=body)
    _print_json(payload, pretty=get_options(ctx).pretty)


@dlq_app.command("replay")
def dlq_replay(
    ctx: typer.Context,
    email: Annotated[str, typer.Argument(help="User email")],
    platform: Annotated[str, typer.Option("--platform", help="Platform (required)")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Re-queue a dead-letter (email, platform) pair for one more attempt.

    Note: depends on slice 7's replay endpoint. Falls back to a clear +
    force-sync sequence when the dedicated endpoint is missing (404).
    """
    client = get_client(ctx)
    body = {"reason": reason} if reason else {}
    primary_path = f"/api/actions/dead_letter/{email}/{platform}/replay"
    try:
        payload = client.request("POST", primary_path, json_body=body)
    except typer.Exit as exc:
        if exc.exit_code != EXIT_API_ERROR:
            raise
        # Fall back: clear DLQ then force-sync. Two writes are visible
        # in the audit log so the op trail still tells the story.
        _err("replay endpoint unavailable; falling back to clear + force-sync")
        client.request(
            "POST",
            f"/api/actions/dead_letter/{email}/{platform}/clear",
            json_body=body,
        )
        payload = client.request(
            "POST",
            f"/api/actions/users/{email}/{platform}/force_sync",
            json_body=body,
        )
    _print_json(payload, pretty=get_options(ctx).pretty)


# ---------------------------------------------------------------------------
# Status / metrics / syncs
# ---------------------------------------------------------------------------


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Daemon health + readiness summary.

    Combines ``/api/health``, ``/api/ready``, ``/api/about`` (slice 14),
    and the CLI version. Returns exit 0 when the daemon answers
    ready=200; exit 1 when ready returns 503 (so this is k8s/CI-friendly
    as a one-line gate).

    The ``/api/about`` payload is fetched separately so an older daemon
    that pre-dates slice 14 (no ``/api/about`` route) still produces a
    useful status — the field is reported as ``None`` and the CLI does
    not exit non-zero on its absence. This keeps the rolling-deploy
    case working: a CLI built from a newer commit can still report
    against an older daemon during a window where one has been deployed
    but the other hasn't.

    Example:
        endpoint-admin status --pretty
    """
    client = get_client(ctx)
    health = client.request("GET", "/api/health", require_auth=False)
    try:
        ready = client.request("GET", "/api/ready", require_auth=False)
        ready_ok = True
    except typer.Exit as exc:
        # /ready intentionally answers 503 when degraded — preserve the
        # exit code but still emit the body so the operator sees what
        # failed instead of a blank failure.
        if exc.exit_code != EXIT_API_ERROR:
            raise
        ready = {"status": "unhealthy"}
        ready_ok = False
    # /api/about is best-effort: if the daemon is older than slice 14
    # the request returns 404 (mapped to EXIT_API_ERROR by request()).
    # Catch that single exit code and surface ``None`` so the rest of
    # the status payload is still useful. Any other exit (network
    # error, auth failure) propagates normally.
    about: Any | None
    try:
        about = client.request("GET", "/api/about", require_auth=False)
    except typer.Exit as exc:
        if exc.exit_code != EXIT_API_ERROR:
            raise
        about = None
    payload: dict[str, Any] = {
        "cli_version": __version__,
        "health": health,
        "ready": ready,
        "about": about,
    }
    _print_json(payload, pretty=get_options(ctx).pretty)
    if not ready_ok:
        raise typer.Exit(code=1)


@app.command("metrics")
def metrics(ctx: typer.Context) -> None:
    """Fetch ``/api/metrics`` and surface the key gauges as JSON.

    The endpoint emits Prometheus text exposition; we parse the small
    set of ``endpoint_daemon_*`` metrics into a flat dict so operators
    can pipe to ``jq`` without a Prometheus client.
    """
    client = get_client(ctx)
    body = client.request("GET", "/api/metrics", require_auth=False)
    raw = body if isinstance(body, str) else str(body)
    parsed = parse_prometheus(raw)
    _print_json(parsed, pretty=get_options(ctx).pretty)


def parse_prometheus(text: str) -> dict[str, float | str]:
    """Tiny Prometheus text-format parser.

    Drops ``# HELP`` / ``# TYPE`` lines; keeps each ``name value`` pair
    as a flat key. Quantile-tagged samples (``foo{quantile="0.5"}``)
    flatten to ``foo_q050`` so the output stays JSON-friendly.
    """
    out: dict[str, float | str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        name, value = parts
        # Flatten ``foo{quantile="0.5"}`` -> ``foo_q050``.
        match = re.match(r"^(\w+)\{quantile=\"([^\"]+)\"\}$", name)
        if match:
            base, qstr = match.group(1), match.group(2)
            try:
                key = f"{base}_q{int(float(qstr) * 100):03d}"
            except ValueError:
                key = f"{base}_q_{qstr}"
        else:
            key = name
        try:
            out[key] = float(value)
        except ValueError:
            out[key] = value
    return out


syncs_app = typer.Typer(name="syncs", help="Sync history queries", no_args_is_help=True)
app.add_typer(syncs_app)


@syncs_app.command("running")
def syncs_running(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(help="Max rows (1..500)")] = 200,
) -> None:
    """List in-flight syncs (state=running).

    Example:
        endpoint-admin syncs running --limit 50
    """
    client = get_client(ctx)
    payload = client.request(
        "GET",
        "/api/syncs",
        params={"state": "running", "limit": limit},
    )
    _print_json(payload, pretty=get_options(ctx).pretty)


# ---------------------------------------------------------------------------
# Config get / set / reset
# ---------------------------------------------------------------------------


config_app = typer.Typer(name="config", help="Daemon config overlay ops", no_args_is_help=True)
app.add_typer(config_app)


def _warn_if_suspicious_key(key: str) -> None:
    """Print a stderr warning when ``key`` looks like a secret/credential.

    Setting SECRET_KEY-like values via the overlay is almost always a
    mistake (those belong in env / a secrets manager); the warning gives
    the operator a chance to abort before pulling the trigger.
    """
    lowered = key.lower()
    if any(token in lowered for token in _SUSPICIOUS_CONFIG_KEYS):
        _err(
            f"warning: key {key!r} looks like a secret; the config overlay "
            "is the wrong place for credentials — prefer env vars"
        )


@config_app.command("get")
def config_get(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Dotted overlay key (e.g. scheduler.global_paused)")],
) -> None:
    """Read a single overlay key, or 'unset' if absent.

    Example:
        endpoint-admin config get scheduler.global_paused
    """
    client = get_client(ctx)
    overlay = client.request("GET", "/api/config/overlay")
    if not isinstance(overlay, dict):
        _err("unexpected response shape for overlay")
        raise typer.Exit(code=EXIT_API_ERROR)
    value = overlay.get(key)
    _print_json({"key": key, "value": value, "set": key in overlay}, pretty=get_options(ctx).pretty)


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Dotted overlay key")],
    value: Annotated[
        str,
        typer.Argument(help="Value (string|int|float|bool|JSON literal)"),
    ],
) -> None:
    """Set an overlay key. Values are coerced from string when obvious.

    Example:
        endpoint-admin config set scheduler.global_paused true
        endpoint-admin config set scheduler.tick_seconds 0.5
    """
    _warn_if_suspicious_key(key)
    client = get_client(ctx)
    coerced = _coerce_value(value)
    payload = client.request(
        "PUT",
        f"/api/config/overlay/{key}",
        json_body={"value": coerced},
    )
    _print_json(payload, pretty=get_options(ctx).pretty)


@config_app.command("reset")
def config_reset(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Dotted overlay key to clear")],
) -> None:
    """Clear an overlay row so the value falls back to env / defaults.

    Example:
        endpoint-admin config reset scheduler.global_paused
    """
    client = get_client(ctx)
    payload = client.request("DELETE", f"/api/config/overlay/{key}")
    _print_json(payload, pretty=get_options(ctx).pretty)


def _coerce_value(raw: str) -> Any:
    """Best-effort JSON-literal coercion: ``true`` / ``false`` / numbers /
    JSON objects parse; everything else is treated as a bare string.
    """
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


# ---------------------------------------------------------------------------
# Debug subcommand group (defined in admin_debug.py)
# ---------------------------------------------------------------------------

# The debug surface lives in :mod:`ingestion.cli.admin_debug`. It's wired
# in here at the bottom so the file load order is admin -> admin_debug,
# avoiding the cycle that would arise if admin_debug imported helpers
# from this module at module level. The import is optional — if the
# module isn't installed (older deployment), the CLI still works
# without ``endpoint-admin debug``.
try:
    # ``reportMissingImports``: admin_debug.py is shipped in the same
    # release as this module but the import is wrapped in try/except so
    # we can deploy admin.py first (slice 9 part B) and admin_debug.py
    # immediately after (slice 9 part C) without an intermediate broken
    # state. The pyright suppression matches the runtime intent.
    from ingestion.cli.admin_debug import debug_app  # pyright: ignore[reportMissingImports]

    app.add_typer(debug_app, name="debug")
except ImportError:
    # admin_debug.py is optional at import time. When missing, the
    # rest of the CLI continues to work and `--help` simply omits the
    # ``debug`` subcommand.
    pass


def main() -> None:
    """Console-script entry point. Defined as a function so pyproject's
    ``[project.scripts]`` table can point at it without dragging Typer's
    decorators into the module's import-time side effects."""
    app()


if __name__ == "__main__":
    main()
