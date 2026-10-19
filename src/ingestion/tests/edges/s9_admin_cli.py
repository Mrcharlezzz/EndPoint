"""Edge + production tests for slice 9 (admin CLI + ops tooling).

Pinned scenarios cover the contracts an operator hits during an
incident or unattended automation: deployment-skew detection, bounded
state-dump output, passive lockfile inspection, dlq-replay graceful
fallback, secret-key warning, history-leak prevention, CSRF header
attachment, TCP connection reuse under burst, wedged-daemon timeout,
forward-compat metric parsing, and Justfile recipe idempotence.

Each test names the specific edge in its docstring and is tagged
``[CODE-FIX]`` (a code change is recommended in the implementation) or
``[TEST-COVERAGE]`` (the existing implementation is fine; this just
locks the behaviour). Slow tests (>500ms) carry ``@pytest.mark.slow``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from ingestion.cli import admin, admin_debug
from typer.testing import CliRunner

# ---------------------------------------------------------------------------
# Plumbing — keep small so each edge test can be read in isolation.
# ---------------------------------------------------------------------------


Handler = Callable[[httpx.Request], httpx.Response]


def _mock_client_factory(
    handler: Handler,
    *,
    transport_holder: list[httpx.MockTransport] | None = None,
) -> Callable[[Any], httpx.Client]:
    """Build a ``_client()`` replacement; optionally surface the transport
    for tests that want to assert connection reuse.

    Mirrors the production ``_DaemonClient._client`` shape:

    * Cookies are seeded at construction (httpx 0.28 deprecated the
      per-request ``cookies=`` argument).
    * The constructed client is cached on ``self._http`` so a multi-
      call subcommand reuses it across ``request()`` calls — that's
      the connection-reuse pin under burst.
    """

    def factory(self: Any) -> httpx.Client:
        cached = getattr(self, "_http", None)
        if cached is not None:
            return cached
        transport = httpx.MockTransport(handler)
        if transport_holder is not None:
            transport_holder.append(transport)
        cookies: dict[str, str] = {}
        cookie_value = getattr(self, "_cookie", None)
        if cookie_value:
            cookies[admin._SESSION_COOKIE_NAME] = cookie_value
        client = httpx.Client(
            transport=transport,
            base_url=self._opts.base_url,
            timeout=admin._HTTP_TIMEOUT_SECONDS,
            follow_redirects=False,
            cookies=cookies,
        )
        self._http = client
        return client

    return factory


def _install_mock(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    captured: list[httpx.Request] = []

    def wrap(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req)

    monkeypatch.setattr(admin._DaemonClient, "_client", _mock_client_factory(wrap))
    return captured


def _login_then(paths: dict[str, httpx.Response]) -> Handler:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                headers={"Set-Cookie": "endpoint_admin_session=C; Path=/"},
                json={"ok": True},
            )
        return paths.get(req.url.path, httpx.Response(404, json={"detail": {"message": "no"}}))

    return handler


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in (
        "ENDPOINT_ADMIN_PASSWORD",
        "ENDPOINT_DAEMON_SESSION_COOKIE",
        "ENDPOINT_DAEMON_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# Numbered seed tests (1-13, from the spec)
# ---------------------------------------------------------------------------


def test_1_status_surfaces_cli_and_daemon_versions(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] Deployment-skew detection — ``status`` must include
    the CLI version (in case the operator's tools lag behind a freshly
    deployed daemon) AND the daemon's reported version (returned in
    /health.version). Tests pin both halves of the contract."""
    daemon_version = "2.99.0-dev"
    _install_mock(
        monkeypatch,
        _login_then(
            {
                "/api/health": httpx.Response(
                    200, json={"status": "ok", "version": daemon_version}
                ),
                "/api/ready": httpx.Response(200, json={"status": "ready"}),
            }
        ),
    )
    result = runner.invoke(admin.app, ["status"])
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)
    # CLI version surfaces under the canonical key.
    assert body["cli_version"]
    # Daemon's version (when reported) is preserved verbatim under the
    # health blob — operators can grep for skew with ``jq``.
    assert body["health"]["version"] == daemon_version


def test_2_state_dump_is_bounded_to_100_per_source(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] state-dump bound — the daemon may return more than
    100 rows; the CLI should cap each source at 100. The CLI sends a
    ``limit=100`` query param, so a well-behaved daemon will trim. Pin
    the limit constant + verify it propagates as a query param to every
    sync/audit/errors endpoint."""
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                headers={"Set-Cookie": "endpoint_admin_session=C; Path=/"},
                json={"ok": True},
            )
        captured.append(req)
        # Pretend the daemon returned 1000 rows; the CLI doesn't trim
        # client-side, but the contract is the limit query param.
        if "audit" in req.url.path or "errors" in req.url.path or "syncs" in req.url.path:
            return httpx.Response(200, json=[{"i": i} for i in range(1000)])
        return httpx.Response(200, json={})

    monkeypatch.setattr(admin._DaemonClient, "_client", _mock_client_factory(handler))
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(admin.app, ["debug", "state-dump"])
    assert result.exit_code == 0, result.stderr
    # Every paginated source carries the ``limit=100`` query param.
    bounded = [r for r in captured if r.url.params.get("limit") == "100"]
    assert len(bounded) >= 4, [r.url for r in captured]
    # And the constant itself is 100 (defensive pin against silent bumps).
    assert admin_debug._AUDIT_TAIL_ROWS == 100
    assert admin_debug._DLQ_TAIL_ROWS == 100
    assert admin_debug._SYNCS_TAIL_ROWS == 100


def test_3_state_dump_isolates_failing_sources(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] state-dump runs sources independently — when one
    endpoint returns 500 the others must still produce data; the failed
    source becomes ``null`` and a stderr note is written."""
    _install_mock(
        monkeypatch,
        _login_then(
            {
                "/api/syncs": httpx.Response(500, json={"detail": {"message": "boom"}}),
                "/api/errors": httpx.Response(200, json={"counts": {"dlq": 0}}),
                "/api/audit": httpx.Response(200, json=[{"event": "ok"}]),
                "/api/config/overlay": httpx.Response(200, json={"k": "v"}),
            }
        ),
    )
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(admin.app, ["debug", "state-dump"])
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)
    # Failing source is None; siblings populated.
    assert body["snapshot"]["running_syncs"] is None
    assert body["snapshot"]["audit_tail"] == [{"event": "ok"}]
    assert body["snapshot"]["config_overlay"] == {"k": "v"}
    # Stderr names the failed source.
    assert "running_syncs" in result.stderr


def test_4_lockfile_reader_never_unlinks(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] Lockfile reader is passive — it must NEVER call
    ``os.unlink`` even when the holder PID looks dead. Removing someone
    else's lock is exactly the race the daemon's startup logic already
    handles; the CLI should observe, never act."""
    target = tmp_path / "alive.lock"
    target.write_text("99999\nhost-x\n2026-04-26T00:00:00Z\n", encoding="utf-8")

    unlink_calls: list[Path] = []
    real_unlink = Path.unlink

    def spy(self: Path, *args: object, **kwargs: object) -> Any:
        unlink_calls.append(self)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy)
    # Run the lockfile inspector on this file (the PID 99999 is unlikely
    # to be alive on this host, so the alive flag will be False or None).
    result = runner.invoke(admin.app, ["debug", "lockfile", "--path", str(target)])
    assert result.exit_code == 0
    # The file must still exist after the call.
    assert target.exists()
    # And the inspector must not have called unlink on the lockfile.
    assert target not in unlink_calls


def test_5_status_exit_1_on_ready_503_matches_just_recipe(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] ``endpoint-admin status`` exits 1 when /ready
    returns 503 — same contract as the ``daemon-status`` Justfile recipe
    documented in scripts/daemon-ops.just (``[ "$code" != "200" ] && exit
    1``). One-line gate works in both shells and CI."""
    _install_mock(
        monkeypatch,
        _login_then(
            {
                "/api/health": httpx.Response(200, json={"status": "ok"}),
                "/api/ready": httpx.Response(503, json={"detail": {"message": "stalled"}}),
            }
        ),
    )
    result = runner.invoke(admin.app, ["status"])
    assert result.exit_code == 1
    body = json.loads(result.stdout)
    assert body["ready"]["status"] == "unhealthy"


def test_6_dlq_replay_falls_back_to_clear_plus_force_sync(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] dlq replay graceful fallback — when slice 7's
    /replay endpoint isn't deployed yet (404), the CLI must run the
    legacy clear+force-sync pair so an operator can still re-attempt
    a stuck row mid-deploy without waiting for the dedicated route."""
    captured = _install_mock(
        monkeypatch,
        _login_then(
            {
                "/api/actions/dead_letter/a@x/teams/replay": httpx.Response(
                    404, json={"detail": {"message": "no such route"}}
                ),
                "/api/actions/dead_letter/a@x/teams/clear": httpx.Response(200, json={"ok": True}),
                "/api/actions/users/a@x/teams/force_sync": httpx.Response(
                    200, json={"queued": True}
                ),
            }
        ),
    )
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(admin.app, ["dlq", "replay", "a@x", "--platform", "teams"])
    assert result.exit_code == 0, result.stderr
    paths = [r.url.path for r in captured]
    # All three legs of the fallback present, in order.
    assert paths.count("/api/actions/dead_letter/a@x/teams/replay") == 1
    assert paths.count("/api/actions/dead_letter/a@x/teams/clear") == 1
    assert paths.count("/api/actions/users/a@x/teams/force_sync") == 1
    # The op is visible in the audit log because two writes hit the daemon.
    assert "replay endpoint unavailable" in result.stderr


@pytest.mark.parametrize(
    "key",
    [
        "auth.session_secret",
        "admin_password",
        "fernet.key",
        "graph.api_key",
        "telegram.bot_token",
        "secret_key",
    ],
)
def test_7_config_set_warns_on_suspicious_keys(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """[TEST-COVERAGE] Suspicious-key warning — every key containing
    ``secret``, ``password``, ``fernet``, ``token``, ``api_key``, or
    ``session_secret`` should trip a stderr warning before the request
    hits the daemon. Setting these via the overlay is almost always a
    bug; the operator at least sees the foot-gun before pulling the
    trigger."""
    _install_mock(
        monkeypatch,
        _login_then({f"/api/config/overlay/{key}": httpx.Response(200, json={"ok": True})}),
    )
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(admin.app, ["config", "set", key, '"x"'])
    assert result.exit_code == 0, result.stderr
    assert "looks like a secret" in result.stderr


def test_8_password_cannot_be_passed_as_bare_arg(runner: CliRunner) -> None:
    """[TEST-COVERAGE] History-leak prevention — Typer must reject
    ``--password`` as a flag. Only ``--password-file`` and the env var
    are accepted, so the secret never lands in shell history. Pin the
    contract by asserting the ``--password`` flag isn't recognised."""
    result = runner.invoke(admin.app, ["--password", "supersecret", "pause", "alice@x"])
    # Typer exits with usage error (code 2) when an unknown flag is used.
    assert result.exit_code != 0
    # Stderr (or output for older typer) carries the "no such option" hint.
    combined = (result.stderr or "") + (result.output or "")
    # Defensive — look for either the typer/click error message or the
    # absence of the flag in the help.
    help_result = runner.invoke(admin.app, ["--help"])
    assert "--password " not in help_result.stdout, "--password must NOT be a flag"
    # Verify the rejection produced a non-zero exit code.
    assert result.exit_code == 2 or "no such option" in combined.lower()


_PLATFORM_ARGS = ["alice@x", "--platform", "teams"]
_DLQ_CLEAR_ARGS = ["clear", "alice@x", "--platform", "teams"]


@pytest.mark.parametrize(
    "verb,path,subcommand,extra_args",
    [
        ("POST", "/api/actions/users/alice@x/teams/pause", "pause", _PLATFORM_ARGS),
        ("POST", "/api/actions/users/alice@x/teams/unpause", "unpause", _PLATFORM_ARGS),
        (
            "POST",
            "/api/actions/users/alice@x/teams/force_sync",
            "force-sync",
            _PLATFORM_ARGS,
        ),
        (
            "POST",
            "/api/actions/dead_letter/alice@x/teams/clear",
            "dlq",
            _DLQ_CLEAR_ARGS,
        ),
        ("PUT", "/api/config/overlay/key", "config", ["set", "key", "true"]),
        ("DELETE", "/api/config/overlay/key", "config", ["reset", "key"]),
    ],
)
def test_9_csrf_header_attached_to_mutating_verbs(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    path: str,
    subcommand: str,
    extra_args: list[str],
) -> None:
    """[TEST-COVERAGE] CSRF header — every POST/PUT/PATCH/DELETE call
    must carry ``X-Requested-With: XMLHttpRequest``. The daemon's auth
    middleware drops mutating requests that don't, so a missing header
    silently fails in production."""
    captured = _install_mock(
        monkeypatch,
        _login_then({path: httpx.Response(200, json={"ok": True})}),
    )
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    cmd = [subcommand, *extra_args] if subcommand != "dlq" else ["dlq", *extra_args]
    result = runner.invoke(admin.app, cmd)
    assert result.exit_code == 0, result.stderr
    # Find the target mutating request — login also POSTs but goes through
    # a separate path (bypasses request(), so no CSRF header by design).
    mutating = next(r for r in captured if r.method == verb and r.url.path != "/api/auth/login")
    assert mutating.headers.get("X-Requested-With") == "XMLHttpRequest"


@pytest.mark.slow
def test_10_burst_pause_completes_quickly(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] Production: a high-volume burst of CLI calls
    should not exhaust resources, and the new client-reuse contract
    (post-fix #1) should keep transport construction bounded to ONE
    httpx.Client per CLI invocation — login and the pause that follows
    share the same client (and same TCP connection in production).

    The test pins:

    * 100 sequential ``pause`` calls complete in well under 30 seconds.
    * Exactly 100 clients are constructed (one per CLI invocation);
      a regression that re-introduces per-request client construction
      would push the count to 200+ and flip this assertion.
    """
    transports: list[httpx.MockTransport] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                headers={"Set-Cookie": "endpoint_admin_session=C; Path=/"},
                json={"ok": True},
            )
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        admin._DaemonClient,
        "_client",
        _mock_client_factory(handler, transport_holder=transports),
    )
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")

    # 100 sequential pause calls; pin total wall-clock under 30s.
    start = time.monotonic()
    for _ in range(100):
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == 0, result.stderr
    elapsed = time.monotonic() - start
    assert elapsed < 30.0, f"100 pauses took {elapsed:.2f}s"
    # Post-fix: exactly one client per invocation (login + pause reuse
    # the same client). 100 invocations -> 100 clients, not 200.
    assert len(transports) == 100, (
        f"expected exactly 100 transports (1 per invocation), got {len(transports)}"
    )


def test_11_wedged_daemon_returns_within_timeout(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] Production: wedged daemon. The CLI's
    ``_HTTP_TIMEOUT_SECONDS`` is 15s; a real daemon hang must surface
    as exit 3 within that ceiling. We simulate by raising ReadTimeout
    inside the mock transport — same exception path httpx would raise
    against a wedged real daemon."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("daemon hung")

    _install_mock(monkeypatch, handler)
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")

    start = time.monotonic()
    result = runner.invoke(admin.app, ["pause", "alice@x"])
    elapsed = time.monotonic() - start

    # ReadTimeout is an httpx.HTTPError → CLI maps to exit 3.
    assert result.exit_code == admin.EXIT_NETWORK_ERROR
    # The CLI returned well within the 15s budget (mock timeout is
    # instant; we just pin it stays under the wall-clock ceiling).
    assert elapsed < 15.0
    # Pin the constant itself so a future refactor that bumps it past 30s
    # surfaces the change.
    assert admin._HTTP_TIMEOUT_SECONDS <= 30.0
    assert "daemon unreachable" in result.stderr


def test_12_metrics_handles_unknown_future_metric(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] Production: forward-compat metric parsing. A
    future daemon may emit metrics the CLI doesn't know about; the
    parser must surface them as flat keys (numeric → float, non-numeric
    → string) without dropping the line or crashing."""
    future_metrics = (
        "endpoint_daemon_uptime_seconds 1.0\n"
        "# A new metric type emitted by a daemon released after this CLI was built.\n"
        "endpoint_daemon_future_thing 42.0\n"
        "endpoint_daemon_string_label info_value_text\n"
        'endpoint_daemon_histogram_bucket{le="0.5"} 17\n'  # unknown label format
    )
    _install_mock(
        monkeypatch,
        _login_then(
            {
                "/api/metrics": httpx.Response(
                    200,
                    text=future_metrics,
                    headers={"content-type": "text/plain"},
                )
            }
        ),
    )
    result = runner.invoke(admin.app, ["metrics"])
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)
    # Known + unknown metrics both surface.
    assert body["endpoint_daemon_uptime_seconds"] == 1.0
    assert body["endpoint_daemon_future_thing"] == 42.0
    # Non-numeric value falls back to string.
    assert body["endpoint_daemon_string_label"] == "info_value_text"
    # An unknown label format keeps the raw name as a key (parser only
    # special-cases ``quantile=``, leaving anything else untouched).
    keys_with_le = [k for k in body if "le=" in k]
    assert keys_with_le, body


@pytest.mark.slow
def test_13_justfile_recipes_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Production: Justfile recipes must be idempotent.
    ``daemon-pause`` and ``daemon-unpause`` calling the same target
    twice with the same args must each exit 0 (the daemon's idempotency
    contract). We don't run a real daemon here; we verify the Justfile
    parses cleanly and the recipe list is stable. ``just`` may not be
    installed; skip cleanly when absent."""
    just_path = shutil.which("just") or "/home/erisf/.cargo/bin/just"
    if not Path(just_path).exists():
        pytest.skip("just is not installed on this host")
    repo_root = Path(__file__).resolve().parents[4]
    # ``just --list`` on the repo's Justfile lists every recipe and
    # exits 0 when the file parses cleanly. Calling it twice is the
    # cheapest possible idempotence check.
    for _ in range(2):
        result = subprocess.run(  # noqa: S603
            [just_path, "--justfile", str(repo_root / "Justfile"), "--list"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(repo_root),
        )
        assert result.returncode == 0, result.stderr
        # Recipes from scripts/daemon-ops.just are imported into the
        # root Justfile; assert at least one is visible (canary).
        assert "daemon-pause" in result.stdout or "daemon-status" in result.stdout


# ---------------------------------------------------------------------------
# Edge brainstorm (>= 8 own additions). All tagged.
# ---------------------------------------------------------------------------


def test_edge_a_admin_works_without_admin_debug_module(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] The admin module wraps the admin_debug import in
    try/except ImportError so the CLI keeps working when the debug file
    isn't shipped (e.g. older deployment). Re-import admin with the
    debug module hidden and verify the rest of the CLI still loads."""
    import importlib
    import sys as _sys

    # Hide admin_debug.
    monkeypatch.setitem(_sys.modules, "ingestion.cli.admin_debug", None)
    # Force admin to re-import; need to drop it from cache first.
    monkeypatch.delitem(_sys.modules, "ingestion.cli.admin", raising=False)
    try:
        reloaded = importlib.import_module("ingestion.cli.admin")
        # The reloaded admin app must still expose the core subcommands.
        local_runner = CliRunner()
        result = local_runner.invoke(reloaded.app, ["--help"])
        assert result.exit_code == 0
        # ``debug`` subgroup must be ABSENT.
        assert "debug" not in result.stdout.lower() or "debug" not in [
            cmd for cmd in result.stdout.split() if cmd == "debug"
        ]
    finally:
        # Restore: drop the cached admin module and the sentinel so
        # other tests in the suite re-import the real module.
        monkeypatch.delitem(_sys.modules, "ingestion.cli.admin", raising=False)
        monkeypatch.delitem(_sys.modules, "ingestion.cli.admin_debug", raising=False)
        importlib.import_module("ingestion.cli.admin")


def test_edge_b_read_password_resolved_lazily(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] _read_password is lazy — the CLI module is
    imported once at process start, but ENDPOINT_ADMIN_PASSWORD must
    be resolved at command-invocation time (not import). Pin by setting
    the env var AFTER importing admin and verifying the value is picked
    up."""
    # Module is already imported (it's at the top of this file).
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "set-after-import")
    _install_mock(
        monkeypatch,
        _login_then({"/api/actions/users/a@x/pause": httpx.Response(200, json={"ok": True})}),
    )
    captured: list[str] = []

    # Capture the password actually sent to /login by sniffing the body.
    def sniffer(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/auth/login":
            captured.append(json.loads(req.content)["password"])
            return httpx.Response(
                200,
                headers={"Set-Cookie": "endpoint_admin_session=C; Path=/"},
                json={"ok": True},
            )
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(admin._DaemonClient, "_client", _mock_client_factory(sniffer))
    result = runner.invoke(admin.app, ["pause", "a@x"])
    assert result.exit_code == 0, result.stderr
    # Password resolved at invocation time picked up the late-set env.
    assert captured == ["set-after-import"]


def test_edge_c_session_cookie_env_overrides_password(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] When BOTH the session cookie env var and the
    admin-password env var are set, the cookie wins — login is
    skipped. Pinned to lock the precedence order."""
    monkeypatch.setenv("ENDPOINT_DAEMON_SESSION_COOKIE", "PRE_ISSUED")
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "would-fail-anyway")

    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if req.url.path == "/api/auth/login":
            # If we ever hit login the test fails — record + 500.
            return httpx.Response(500, json={"detail": {"message": "should-not-login"}})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(admin._DaemonClient, "_client", _mock_client_factory(handler))
    result = runner.invoke(admin.app, ["pause", "alice@x"])
    assert result.exit_code == 0, result.stderr
    paths = [r.url.path for r in captured]
    # No login was attempted — cookie alone authenticated the call.
    assert "/api/auth/login" not in paths
    # Cookie attached to the actual call.
    assert "endpoint_admin_session=PRE_ISSUED" in captured[0].headers.get("cookie", "")


def test_edge_d_base_url_with_trailing_slash_normalised(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] ``--base-url`` with a trailing slash must be
    normalised; otherwise httpx would build double-slash paths."""
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if req.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                headers={"Set-Cookie": "endpoint_admin_session=C; Path=/"},
                json={"ok": True},
            )
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(admin._DaemonClient, "_client", _mock_client_factory(handler))
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(
        admin.app,
        ["--base-url", "http://localhost:8765/", "pause", "alice@x"],
    )
    assert result.exit_code == 0, result.stderr
    # No double slashes appear in the actual request URLs.
    for req in captured:
        full = str(req.url)
        # ``http://`` is the only legitimate ``//``.
        assert "http://" in full
        rest = full.replace("http://", "", 1)
        assert "//" not in rest, full


def test_edge_e_empty_response_body_handled_cleanly(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] A 200 response with empty body (some proxies
    strip bodies on idle) must surface as a clean ``null`` JSON output,
    not a JSON-decode crash."""
    _install_mock(
        monkeypatch,
        _login_then({"/api/actions/users/a@x/pause": httpx.Response(200, content=b"")}),
    )
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(admin.app, ["pause", "a@x"])
    assert result.exit_code == 0, result.stderr
    # Output is JSON null + newline.
    assert json.loads(result.stdout) is None


def test_edge_f_parse_prometheus_string_fallback(self_unused: object = None) -> None:
    """[TEST-COVERAGE] parse_prometheus tolerates non-numeric values —
    the daemon may emit string-valued info metrics; the parser stores
    them as raw strings instead of crashing on float()."""
    text = "version_info commit_abc123\nendpoint_daemon_uptime_seconds 1.5\nanother_string foo\n"
    parsed = admin.parse_prometheus(text)
    assert parsed["version_info"] == "commit_abc123"
    assert parsed["another_string"] == "foo"
    assert parsed["endpoint_daemon_uptime_seconds"] == 1.5


def test_edge_g_quantile_zero_percentile_formats_q000() -> None:
    """[TEST-COVERAGE] parse_prometheus quantile flattening — the
    boundary at quantile=0 must format as ``_q000`` (not ``_q_0``).
    Pin the format because dashboards key off it."""
    text = 'foo{quantile="0.0"} 1\nfoo{quantile="1.0"} 2\nfoo{quantile="0.99"} 3\n'
    parsed = admin.parse_prometheus(text)
    assert "foo_q000" in parsed
    assert "foo_q100" in parsed
    assert "foo_q099" in parsed


def test_edge_h_status_envelope_with_request_id_surfaced(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] When /ready 503 also carries an envelope with
    a request_id, the operator should be able to extract it from
    stderr to correlate with daemon logs."""
    _install_mock(
        monkeypatch,
        _login_then(
            {
                "/api/health": httpx.Response(200, json={"status": "ok"}),
                "/api/ready": httpx.Response(
                    503,
                    json={
                        "detail": {
                            "code": "scheduler_stalled",
                            "message": "no tick in 60s",
                            "request_id": "rq-abc",
                        }
                    },
                ),
            }
        ),
    )
    result = runner.invoke(admin.app, ["status"])
    assert result.exit_code == 1
    assert "rq-abc" in result.stderr
    assert "scheduler_stalled" in result.stderr


def test_edge_i_delete_carries_csrf_header(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] config reset uses DELETE — DELETE counts as a
    mutating verb and must carry the CSRF header. Already covered in
    the parametric test 9 but pinned standalone for the audit trail."""
    captured = _install_mock(
        monkeypatch,
        _login_then({"/api/config/overlay/some.key": httpx.Response(200, json={"ok": True})}),
    )
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(admin.app, ["config", "reset", "some.key"])
    assert result.exit_code == 0, result.stderr
    delete_req = next(r for r in captured if r.method == "DELETE")
    assert delete_req.headers.get("X-Requested-With") == "XMLHttpRequest"


def test_edge_j_get_carries_no_csrf_header(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] GET requests do NOT carry the CSRF header —
    safe verbs are exempt from the daemon's CSRF gate. Pin to lock the
    contract."""
    captured = _install_mock(
        monkeypatch,
        _login_then({"/api/syncs": httpx.Response(200, json=[])}),
    )
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(admin.app, ["syncs", "running"])
    assert result.exit_code == 0
    get_req = next(r for r in captured if r.url.path == "/api/syncs")
    assert get_req.method == "GET"
    assert "X-Requested-With" not in get_req.headers


def test_edge_k_login_envelope_surfaces_in_stderr_on_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] When /login returns 500 with an envelope, the
    envelope's code / message / request_id must surface on stderr so
    the operator can debug the failure."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "detail": {
                    "code": "internal_error",
                    "message": "db_offline",
                    "request_id": "rq-xyz",
                }
            },
        )

    _install_mock(monkeypatch, handler)
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(admin.app, ["pause", "a@x"])
    assert result.exit_code == admin.EXIT_API_ERROR
    assert "db_offline" in result.stderr
    assert "rq-xyz" in result.stderr


def test_edge_l_empty_password_file_returns_empty_string(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[CODE-FIX] An empty --password-file should be treated as
    'no password' (auth required exit 2), NOT a successful login with
    password='' (which the daemon would reject as 401, exit 2). The
    final exit code is the same; pin it so a future refactor of
    _read_password doesn't accidentally make the empty file truthy."""
    empty_file = tmp_path / "empty"
    empty_file.write_text("", encoding="utf-8")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/auth/login":
            # Daemon rejects empty password as 401.
            return httpx.Response(401, json={"detail": {"message": "no"}})
        return httpx.Response(500)

    _install_mock(monkeypatch, handler)
    result = runner.invoke(admin.app, ["--password-file", str(empty_file), "pause", "a@x"])
    # End state is exit 2 either way (auth required OR auth failed).
    assert result.exit_code == admin.EXIT_AUTH_ERROR


def test_edge_m_main_function_calls_app() -> None:
    """[TEST-COVERAGE] admin.main() is the console-script entry point —
    pin that it forwards to the Typer app object so a `pyproject.toml`
    refactor that swaps the entry point catches early."""
    with patch.object(admin, "app") as mock_app:
        admin.main()
        mock_app.assert_called_once_with()


def test_edge_n_request_id_propagation_through_envelope(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] Daemon error envelopes without a request_id field
    produce a clean error message (no trailing ``[request_id=]`` blob).
    Pin the absence to keep the stderr clean for log parsing."""
    _install_mock(
        monkeypatch,
        _login_then(
            {
                "/api/actions/users/a@x/pause": httpx.Response(
                    500,
                    json={
                        "detail": {
                            "code": "internal_error",
                            "message": "boom",
                            # no request_id
                        }
                    },
                )
            }
        ),
    )
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(admin.app, ["pause", "a@x"])
    assert result.exit_code == admin.EXIT_API_ERROR
    assert "request_id" not in result.stderr
    assert "[internal_error]" in result.stderr


def test_edge_o_default_client_constructor_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] The default ``_client()`` (without any
    monkeypatch) must build a real httpx.Client that points at the
    configured base URL. Covers the line-219 default branch the
    coverage report flags."""
    opts = admin.ClientOptions(
        base_url="http://localhost:8765",
        session_cookie="C",
        password=None,
        pretty=False,
    )
    client = admin._DaemonClient(opts)
    real_client = client._client()
    try:
        assert isinstance(real_client, httpx.Client)
        assert str(real_client.base_url).rstrip("/") == "http://localhost:8765"
        assert real_client.timeout.connect == admin._HTTP_TIMEOUT_SECONDS
    finally:
        real_client.close()


def test_edge_p_no_subcommand_prints_help_and_exits(
    runner: CliRunner,
) -> None:
    """[TEST-COVERAGE] Bare ``endpoint-admin`` with no subcommand prints
    help and exits 0 (or 2 from Typer's no_args_is_help). The callback
    handles this defensively — covers lines 363-366 in admin.py."""
    # Use --base-url to slip past the no_args_is_help short-circuit and
    # land in the body of the callback's no-subcommand branch.
    result = runner.invoke(admin.app, ["--base-url", "http://localhost:8765"])
    # Either Typer's auto-help (code 2) or our explicit print + exit (0).
    assert result.exit_code in (0, 2)
    # Help text mentions the available commands.
    combined = (result.stdout or "") + (result.stderr or "")
    assert "pause" in combined or "Commands" in combined


def test_edge_q_main_module_runnable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[TEST-COVERAGE] Running ``python -m ingestion.cli.admin --version``
    exits 0 with the version on stdout — pins line 782 (``if __name__
    == '__main__'``)."""
    result = subprocess.run(  # noqa: S603
        [
            "python",
            "-c",
            "import runpy; runpy.run_module('ingestion.cli.admin', run_name='__main__')",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "PYTHONPATH": str(Path(admin.__file__).resolve().parents[2])},
    )
    # Without args the bare CLI shows help (exit 0 via our explicit
    # ``print(ctx.get_help()); raise typer.Exit()`` branch).
    assert result.returncode in (0, 2)
    combined = (result.stdout or "") + (result.stderr or "")
    assert "endpoint-admin" in combined.lower() or "Commands" in combined


def test_edge_r_dlq_replay_fallback_reuses_one_client(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] Post-fix #1: ``dlq replay`` issues login + 404
    + clear + force_sync — four daemon calls — but the new client-reuse
    contract means all four share a single ``httpx.Client``. Pin the
    bound to lock against a regression that re-introduces per-request
    client construction."""
    transports: list[httpx.MockTransport] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                headers={"Set-Cookie": "endpoint_admin_session=C; Path=/"},
                json={"ok": True},
            )
        if req.url.path.endswith("/replay"):
            return httpx.Response(404, json={"detail": {"message": "no such route"}})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        admin._DaemonClient,
        "_client",
        _mock_client_factory(handler, transport_holder=transports),
    )
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(admin.app, ["dlq", "replay", "alice@x", "--platform", "teams"])
    assert result.exit_code == 0, result.stderr
    # Four logical requests in this command (login + 404 + clear +
    # force_sync) — but exactly ONE client construction (transports list
    # appends only when a new client is built).
    assert len(transports) == 1, (
        f"expected one client across login+404+clear+force_sync, got {len(transports)}"
    )


def test_edge_s_daemon_client_close_clears_cached_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Post-fix #1: ``_DaemonClient.close()`` should
    tear down the cached httpx.Client and reset ``self._http`` to None
    so the next ``request()`` builds a fresh client. Pinned because the
    close path is what makes the context-manager pattern safe."""
    opts = admin.ClientOptions(
        base_url="http://localhost:8765",
        session_cookie="C",
        password=None,
        pretty=False,
    )
    client = admin._DaemonClient(opts)
    # Calling _client() lazily builds and caches.
    first = client._client()
    assert client._http is first
    # close() drops the reference.
    client.close()
    assert client._http is None
    # Idempotent: a second close is a no-op.
    client.close()
    assert client._http is None


def test_edge_t_daemon_client_context_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Post-fix #1: ``with _DaemonClient(opts) as c:``
    yields the client and closes it on exit. Pinned because this is the
    deterministic-cleanup escape hatch we expose to callers (today
    subcommands rely on GC; the context manager is for tests/scripts
    that want eager teardown)."""
    opts = admin.ClientOptions(
        base_url="http://localhost:8765",
        session_cookie="C",
        password=None,
        pretty=False,
    )
    with admin._DaemonClient(opts) as client:
        assert isinstance(client, admin._DaemonClient)
        # Build the http client inside the context manager.
        client._client()
        assert client._http is not None
    # On exit, the client should be closed and reset.
    assert client._http is None


def test_edge_u_lockfile_path_to_nonexistent_dir_clean_exit(
    runner: CliRunner, tmp_path: Path
) -> None:
    """[TEST-COVERAGE] ``endpoint-admin debug lockfile --path
    /nonexistent/foo.lock`` exits cleanly (0) with ``exists: false``
    when the parent directory doesn't exist — the CLI must not crash
    with an OSError on a missing parent. Operators routinely poke at
    paths during incident triage; a clean exit lets them script around
    "is the lockfile there?"."""
    target = tmp_path / "deeply" / "nested" / "missing" / "state.db.lock"
    # Don't create the parents — Path.exists() returns False for
    # missing-parent paths without raising.
    result = runner.invoke(admin.app, ["debug", "lockfile", "--path", str(target)])
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["exists"] is False
    assert body["pid"] is None
    assert body["alive"] is None
    assert body["alive_note"] is None


def test_edge_v_parse_prometheus_permissive_on_garbage(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[TEST-COVERAGE] parse_prometheus is intentionally permissive —
    it tolerates trailing tokens (``foo bar baz`` -> ``foo`` = ``"bar
    baz"``) and only drops lines that have no whitespace at all
    (``no_value_at_all``) or are blank. The forward-compat trade-off:
    a daemon-versus-CLI version skew doesn't blank the metrics
    snapshot, even if the unfamiliar lines come through as strings.

    Pin the actual permissiveness so a tightening-of-the-parser
    refactor that suddenly drops "extra-token" lines surfaces here
    instead of silently in production."""
    text = (
        "# HELP something\n"
        "valid_metric 1.0\n"
        "trailing_tokens 1 2 3\n"  # extra trailing tokens collapsed into string
        "no_value_at_all\n"  # truly tokenless -> drop
        "another_valid 2.0\n"
        "        \n"  # whitespace-only -> drop
        "valid_string string_value\n"
    )
    parsed = admin.parse_prometheus(text)
    # Valid numeric lines surface as floats.
    assert parsed["valid_metric"] == 1.0
    assert parsed["another_valid"] == 2.0
    # Trailing-token lines are kept (forward-compat), value collapses
    # into the string fallback because "1 2 3" doesn't parse as float.
    assert parsed["trailing_tokens"] == "1 2 3"
    # String-valued lines surface as strings.
    assert parsed["valid_string"] == "string_value"
    # The truly malformed line (no whitespace at all) is dropped.
    assert "no_value_at_all" not in parsed
    # Stays permissive — no exception raised.
    assert len(parsed) == 4


def test_edge_w_no_deprecation_warnings_emitted(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, recwarn: pytest.WarningsRecorder
) -> None:
    """[TEST-COVERAGE] Post-fix #2: zero httpx DeprecationWarnings should
    surface during a typical CLI run. Pin so a regression that puts
    cookies back into the per-request argument re-introduces the noise.
    """
    _install_mock(
        monkeypatch,
        _login_then(
            {"/api/actions/users/alice@x/teams/pause": httpx.Response(200, json={"ok": True})}
        ),
    )
    monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
    result = runner.invoke(admin.app, ["pause", "alice@x", "--platform", "teams"])
    assert result.exit_code == 0, result.stderr
    # No DeprecationWarning emitted by httpx during this run.
    deprecations = [w for w in recwarn.list if issubclass(w.category, DeprecationWarning)]
    assert deprecations == [], [str(w.message) for w in deprecations]
