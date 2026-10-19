"""Unit tests for ``ingestion.cli.admin`` (the ``endpoint-admin`` CLI).

The CLI itself is a thin wrapper around the daemon's HTTP control panel;
we exercise it through Typer's :class:`CliRunner` and intercept HTTP
traffic via :class:`httpx.MockTransport`. The implementer's test seams
are :meth:`_DaemonClient._client` (returns the httpx.Client we want to
swap) and :func:`_read_password` (env-driven, so monkeypatch wins).

Goal: 100% line + branch coverage of ``admin.py``. The companion file
``test_cli_admin_debug.py`` covers the debug subcommands.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from ingestion.cli import admin
from typer.testing import CliRunner

# ---------------------------------------------------------------------------
# Test infrastructure: mock-transport plumbing for the daemon's HTTP API.
# ---------------------------------------------------------------------------


Handler = Callable[[httpx.Request], httpx.Response]


def _mock_client_factory(handler: Handler) -> Callable[[Any], httpx.Client]:
    """Return a ``_client()`` replacement that injects ``MockTransport``.

    Mirrors the production ``_DaemonClient._client`` shape: cookies are
    seeded at construction (httpx 0.28 deprecated the per-request
    ``cookies=`` argument), and the constructed client is cached on
    ``self._http`` so a multi-call subcommand reuses the same client
    across its ``request()`` calls — the connection-reuse contract.
    """

    def factory(self: Any) -> httpx.Client:
        cached = getattr(self, "_http", None)
        if cached is not None:
            return cached
        cookies: dict[str, str] = {}
        cookie_value = getattr(self, "_cookie", None)
        if cookie_value:
            cookies[admin._SESSION_COOKIE_NAME] = cookie_value
        client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=self._opts.base_url,
            timeout=admin._HTTP_TIMEOUT_SECONDS,
            follow_redirects=False,
            cookies=cookies,
        )
        self._http = client
        return client

    return factory


def _install_mock(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    """Patch ``_DaemonClient._client`` to use ``handler``.

    Returns a list that captures every request the CLI issued, so tests
    can assert headers / cookies / paths after the run.
    """
    captured: list[httpx.Request] = []

    def wrap(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    monkeypatch.setattr(admin._DaemonClient, "_client", _mock_client_factory(wrap))
    return captured


def _login_then(*, paths: dict[str, httpx.Response]) -> Handler:
    """Build a handler that first answers /api/auth/login with a Set-Cookie,
    then dispatches subsequent requests by URL path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                headers={"Set-Cookie": "endpoint_admin_session=TEST_COOKIE; Path=/"},
                json={"ok": True},
            )
        if request.url.path in paths:
            return paths[request.url.path]
        return httpx.Response(404, json={"detail": {"code": "not_found", "message": "no"}})

    return handler


@pytest.fixture
def runner() -> CliRunner:
    # Modern Typer/Click splits stdout/stderr by default; ``mix_stderr``
    # was removed.
    return CliRunner()


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Don't let host env leak into the CLI."""
    for var in (
        "ENDPOINT_ADMIN_PASSWORD",
        "ENDPOINT_DAEMON_SESSION_COOKIE",
        "ENDPOINT_DAEMON_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# Helpers (tiny utility checks)
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_validate_base_url_strips_trailing_slash(self) -> None:
        assert admin._validate_base_url("http://x:8765/") == "http://x:8765"

    def test_validate_base_url_accepts_https(self) -> None:
        assert admin._validate_base_url("https://x:8765") == "https://x:8765"

    def test_validate_base_url_rejects_bare_localhost(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(typer.Exit) as exc:
            admin._validate_base_url("localhost:8080")
        assert exc.value.exit_code == admin.EXIT_VALIDATION_ERROR
        captured = capsys.readouterr()
        assert "must include http://" in captured.err

    def test_read_password_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "secret-from-env")
        assert admin._read_password(None) == "secret-from-env"

    def test_read_password_from_file(self, tmp_path: Path) -> None:
        target = tmp_path / "pw"
        target.write_text("file-secret\n", encoding="utf-8")
        assert admin._read_password(target) == "file-secret"

    def test_read_password_missing_file_exits_4(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(typer.Exit) as exc:
            admin._read_password(tmp_path / "no-such-file")
        assert exc.value.exit_code == admin.EXIT_VALIDATION_ERROR
        assert "could not read" in capsys.readouterr().err

    def test_read_password_returns_none_when_unset(self) -> None:
        assert admin._read_password(None) is None

    def test_coerce_value_json_literals(self) -> None:
        assert admin._coerce_value("true") is True
        assert admin._coerce_value("false") is False
        assert admin._coerce_value("42") == 42
        assert admin._coerce_value("0.5") == 0.5
        assert admin._coerce_value('{"a": 1}') == {"a": 1}

    def test_coerce_value_bare_string_passthrough(self) -> None:
        assert admin._coerce_value("hello") == "hello"

    def test_print_json_compact(self, capsys: pytest.CaptureFixture[str]) -> None:
        admin._print_json({"a": 1, "b": 2}, pretty=False)
        captured = capsys.readouterr()
        # Compact format has no leading whitespace.
        assert captured.out.startswith("{")
        # No newline inside the json blob.
        assert "\n  " not in captured.out

    def test_print_json_pretty(self, capsys: pytest.CaptureFixture[str]) -> None:
        admin._print_json({"a": 1}, pretty=True)
        out = capsys.readouterr().out
        assert "  " in out  # indentation present


# ---------------------------------------------------------------------------
# Pause / unpause (user-level + per-credential)
# ---------------------------------------------------------------------------


class TestPause:
    def test_user_level_pause_calls_user_endpoint(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/users/alice@x/pause": httpx.Response(
                        200, json={"ok": True, "scope": "user"}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body == {"ok": True, "scope": "user"}
        # Login + pause: two requests.
        assert [r.url.path for r in captured] == [
            "/api/auth/login",
            "/api/actions/users/alice@x/pause",
        ]
        # Mutating verb carries the CSRF header.
        pause_req = captured[1]
        assert pause_req.method == "POST"
        assert pause_req.headers.get("X-Requested-With") == "XMLHttpRequest"

    def test_platform_scoped_pause_calls_credential_endpoint(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/users/alice@x/teams/pause": httpx.Response(
                        200, json={"ok": True, "scope": "credential"}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(
            admin.app, ["pause", "alice@x", "--platform", "teams", "--reason", "test"]
        )
        assert result.exit_code == 0, result.stderr
        assert "/api/actions/users/alice@x/teams/pause" in [r.url.path for r in captured]
        # Reason was forwarded as the JSON body.
        pause_req = next(r for r in captured if r.url.path.endswith("/pause"))
        assert json.loads(pause_req.content) == {"reason": "test"}


class TestUnpause:
    def test_user_level_unpause(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock(
            monkeypatch,
            _login_then(
                paths={"/api/actions/users/alice@x/unpause": httpx.Response(200, json={"ok": True})}
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["unpause", "alice@x"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"ok": True}

    def test_platform_scoped_unpause(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/users/alice@x/moodle/unpause": httpx.Response(
                        200, json={"ok": True}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(
            admin.app,
            ["unpause", "alice@x", "--platform", "moodle", "--reason", "rotation"],
        )
        assert result.exit_code == 0
        unpause_req = next(r for r in captured if r.url.path.endswith("/unpause"))
        assert json.loads(unpause_req.content) == {"reason": "rotation"}


# ---------------------------------------------------------------------------
# Force-sync
# ---------------------------------------------------------------------------


class TestForceSync:
    def test_happy_path(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/users/alice@x/teams/force_sync": httpx.Response(
                        200, json={"queued": True}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["force-sync", "alice@x", "--platform", "teams"])
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout) == {"queued": True}

    def test_reason_forwarded(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/users/alice@x/teams/force_sync": httpx.Response(
                        200, json={"queued": True}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(
            admin.app,
            ["force-sync", "alice@x", "--platform", "teams", "--reason", "manual"],
        )
        assert result.exit_code == 0
        sync_req = next(r for r in captured if r.url.path.endswith("force_sync"))
        assert json.loads(sync_req.content) == {"reason": "manual"}


# ---------------------------------------------------------------------------
# DLQ clear / replay
# ---------------------------------------------------------------------------


class TestDLQ:
    def test_clear_happy_path(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/dead_letter/alice@x/moodle/clear": httpx.Response(
                        200, json={"ok": True, "rows_affected": 3}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["dlq", "clear", "alice@x", "--platform", "moodle"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["rows_affected"] == 3

    def test_replay_uses_dedicated_endpoint(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/dead_letter/alice@x/teams/replay": httpx.Response(
                        200, json={"ok": True, "method": "replay"}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["dlq", "replay", "alice@x", "--platform", "teams"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["method"] == "replay"
        # No fallback path was ever called.
        paths = {r.url.path for r in captured}
        assert "/api/actions/dead_letter/alice@x/teams/clear" not in paths
        assert "/api/actions/users/alice@x/teams/force_sync" not in paths

    def test_replay_falls_back_on_404(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the dedicated /replay endpoint isn't shipped (404), the
        CLI falls back to a clear+force-sync pair so the audit log still
        shows a coherent op."""
        captured = _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/dead_letter/alice@x/teams/replay": httpx.Response(
                        404, json={"detail": {"code": "not_found", "message": "no replay"}}
                    ),
                    "/api/actions/dead_letter/alice@x/teams/clear": httpx.Response(
                        200, json={"ok": True}
                    ),
                    "/api/actions/users/alice@x/teams/force_sync": httpx.Response(
                        200, json={"queued": True}
                    ),
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["dlq", "replay", "alice@x", "--platform", "teams"])
        assert result.exit_code == 0, result.stderr
        # Last printed payload is force_sync's response.
        body = json.loads(result.stdout)
        assert body == {"queued": True}
        # All three requests were attempted.
        paths = [r.url.path for r in captured]
        assert "/api/actions/dead_letter/alice@x/teams/replay" in paths
        assert "/api/actions/dead_letter/alice@x/teams/clear" in paths
        assert "/api/actions/users/alice@x/teams/force_sync" in paths
        # Fallback warning emitted on stderr.
        assert "replay endpoint unavailable" in result.stderr

    def test_replay_propagates_non_api_exit(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When replay fails for a non-API reason (eg auth/network) the
        CLI does NOT silently fall back — we surface the original code."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                return httpx.Response(
                    200,
                    headers={"Set-Cookie": "endpoint_admin_session=C; Path=/"},
                    json={"ok": True},
                )
            return httpx.Response(401, json={"detail": {"message": "expired"}})

        _install_mock(monkeypatch, handler)
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["dlq", "replay", "alice@x", "--platform", "teams"])
        assert result.exit_code == admin.EXIT_AUTH_ERROR


# ---------------------------------------------------------------------------
# Status (combines /health + /ready)
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_happy_path(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/health": httpx.Response(200, json={"status": "ok"}),
                    "/api/ready": httpx.Response(
                        200,
                        json={"status": "ready", "checks": {"db": "ok"}},
                    ),
                }
            ),
        )
        # /health and /ready don't require auth — no env var needed.
        result = runner.invoke(admin.app, ["status"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["health"] == {"status": "ok"}
        assert body["ready"]["status"] == "ready"
        assert "cli_version" in body  # version surfaced for skew detection

    def test_status_exit_1_when_ready_503(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/health": httpx.Response(200, json={"status": "ok"}),
                    "/api/ready": httpx.Response(
                        503, json={"detail": {"code": "not_ready", "message": "scheduler stalled"}}
                    ),
                }
            ),
        )
        result = runner.invoke(admin.app, ["status"])
        assert result.exit_code == 1
        # JSON still printed so operator sees the body.
        body = json.loads(result.stdout)
        assert body["ready"]["status"] == "unhealthy"

    def test_status_other_non_api_exit_is_propagated(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If /ready fails with auth/network rather than API error the
        original exit code propagates (not silently degraded)."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/health":
                return httpx.Response(200, json={"status": "ok"})
            raise httpx.ConnectError("boom")

        _install_mock(monkeypatch, handler)
        result = runner.invoke(admin.app, ["status"])
        assert result.exit_code == admin.EXIT_NETWORK_ERROR


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


_METRICS_TEXT = (
    "# HELP endpoint_daemon_uptime_seconds Daemon uptime\n"
    "# TYPE endpoint_daemon_uptime_seconds gauge\n"
    "endpoint_daemon_uptime_seconds 12345.6\n"
    "# TYPE endpoint_daemon_active_syncs gauge\n"
    "endpoint_daemon_active_syncs 2\n"
    'endpoint_daemon_sync_duration_seconds{quantile="0.5"} 1.5\n'
    'endpoint_daemon_sync_duration_seconds{quantile="0.95"} 4.2\n'
)


class TestMetrics:
    def test_parse_prometheus_basic(self) -> None:
        parsed = admin.parse_prometheus(_METRICS_TEXT)
        assert parsed["endpoint_daemon_uptime_seconds"] == 12345.6
        assert parsed["endpoint_daemon_active_syncs"] == 2.0
        assert parsed["endpoint_daemon_sync_duration_seconds_q050"] == 1.5
        assert parsed["endpoint_daemon_sync_duration_seconds_q095"] == 4.2

    def test_parse_prometheus_skips_comments_and_blank_lines(self) -> None:
        text = "\n# HELP foo bar\n\n# TYPE foo gauge\nfoo 1\n"
        parsed = admin.parse_prometheus(text)
        assert parsed == {"foo": 1.0}

    def test_parse_prometheus_skips_malformed_lines(self) -> None:
        text = "good_metric 1\nbroken_no_value\n"
        parsed = admin.parse_prometheus(text)
        assert parsed == {"good_metric": 1.0}

    def test_parse_prometheus_string_fallback_for_non_numeric(self) -> None:
        text = "label_with_string foo_bar_value\n"
        parsed = admin.parse_prometheus(text)
        # Non-numeric value falls back to the raw string.
        assert parsed["label_with_string"] == "foo_bar_value"

    def test_parse_prometheus_unparsable_quantile_label(self) -> None:
        text = 'foo{quantile="bogus"} 7\n'
        parsed = admin.parse_prometheus(text)
        assert parsed == {"foo_q_bogus": 7.0}

    def test_metrics_command_prints_parsed(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/metrics": httpx.Response(
                        200,
                        text=_METRICS_TEXT,
                        headers={"content-type": "text/plain; version=0.0.4"},
                    )
                }
            ),
        )
        result = runner.invoke(admin.app, ["metrics"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["endpoint_daemon_active_syncs"] == 2.0


# ---------------------------------------------------------------------------
# Syncs running
# ---------------------------------------------------------------------------


class TestSyncsRunning:
    def test_running_query(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/syncs": httpx.Response(
                        200,
                        json=[{"id": "s1", "state": "running"}],
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["syncs", "running", "--limit", "10"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body[0]["state"] == "running"
        # state and limit propagate as query params.
        sync_req = next(r for r in captured if r.url.path == "/api/syncs")
        assert sync_req.url.params["state"] == "running"
        assert sync_req.url.params["limit"] == "10"


# ---------------------------------------------------------------------------
# Config get / set / reset
# ---------------------------------------------------------------------------


class TestConfig:
    def test_get_returns_value_when_set(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/config/overlay": httpx.Response(
                        200, json={"scheduler.global_paused": True}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["config", "get", "scheduler.global_paused"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body == {"key": "scheduler.global_paused", "value": True, "set": True}

    def test_get_returns_unset_when_absent(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _login_then(paths={"/api/config/overlay": httpx.Response(200, json={})}),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["config", "get", "missing.key"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body == {"key": "missing.key", "value": None, "set": False}

    def test_get_unexpected_response_shape_exits_api_error(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _login_then(paths={"/api/config/overlay": httpx.Response(200, json=["unexpected"])}),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["config", "get", "x"])
        assert result.exit_code == admin.EXIT_API_ERROR
        assert "unexpected response shape" in result.stderr

    def test_set_with_safe_key(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/config/overlay/scheduler.global_paused": httpx.Response(
                        200, json={"ok": True}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(
            admin.app,
            ["config", "set", "scheduler.global_paused", "true"],
        )
        assert result.exit_code == 0, result.stderr
        # No suspicious-key warning for benign keys.
        assert "looks like a secret" not in result.stderr
        # PUT carries the CSRF header and the coerced JSON value.
        put_req = next(r for r in captured if r.method == "PUT")
        assert put_req.headers.get("X-Requested-With") == "XMLHttpRequest"
        assert json.loads(put_req.content) == {"value": True}

    def test_set_warns_on_suspicious_key(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/config/overlay/auth.session_secret": httpx.Response(
                        200, json={"ok": True}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(
            admin.app,
            ["config", "set", "auth.session_secret", '"new-value"'],
        )
        assert result.exit_code == 0, result.stderr
        assert "looks like a secret" in result.stderr

    def test_reset_calls_delete(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/config/overlay/scheduler.global_paused": httpx.Response(
                        200, json={"ok": True}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["config", "reset", "scheduler.global_paused"])
        assert result.exit_code == 0, result.stderr
        del_req = next(r for r in captured if r.method == "DELETE")
        assert del_req.url.path == "/api/config/overlay/scheduler.global_paused"
        # DELETE is mutating, so it carries CSRF.
        assert del_req.headers.get("X-Requested-With") == "XMLHttpRequest"


# ---------------------------------------------------------------------------
# Auth flows
# ---------------------------------------------------------------------------


class TestAuthFlows:
    def test_session_cookie_env_skips_login(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_mock(
            monkeypatch,
            lambda req: httpx.Response(200, json={"ok": True}),
        )
        monkeypatch.setenv("ENDPOINT_DAEMON_SESSION_COOKIE", "PRE_ISSUED")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == 0
        # Only the pause call — no /api/auth/login.
        paths = [r.url.path for r in captured]
        assert "/api/auth/login" not in paths
        assert "/api/actions/users/alice@x/pause" in paths
        # The pre-issued cookie is attached.
        pause_req = captured[0]
        cookie = pause_req.headers.get("cookie", "")
        assert "endpoint_admin_session=PRE_ISSUED" in cookie

    def test_session_cookie_flag_overrides_env(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strict precedence (post fix #3): when both the
        ``--session-cookie`` flag and ``ENDPOINT_DAEMON_SESSION_COOKIE``
        env var are set, the flag wins. Typer's standard envvar handling
        treats an explicit flag as higher-priority than the env-derived
        default, and ``_resolve_options`` no longer fabricates a second
        env lookup that would muddy the order."""
        captured = _install_mock(
            monkeypatch,
            lambda req: httpx.Response(200, json={"ok": True}),
        )
        monkeypatch.setenv("ENDPOINT_DAEMON_SESSION_COOKIE", "FROM_ENV")
        result = runner.invoke(
            admin.app,
            ["--session-cookie", "FROM_FLAG", "pause", "alice@x"],
        )
        assert result.exit_code == 0
        cookie_header = captured[0].headers.get("cookie", "")
        # The flag value wins — env is ignored.
        assert "endpoint_admin_session=FROM_FLAG" in cookie_header
        assert "FROM_ENV" not in cookie_header
        # Login was skipped — the flag-supplied cookie authenticated us.
        assert "/api/auth/login" not in [r.url.path for r in captured]

    def test_session_cookie_blank_falls_through_to_password(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strict precedence (post fix #3): a whitespace-only / empty
        cookie value is treated as "not set" so the password path
        engages instead of impersonating a real cookie. Pin so a future
        refactor that re-introduces ``or os.environ.get(...)`` doesn't
        silently make blanks truthy."""
        login_count = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/auth/login":
                login_count["n"] += 1
                return httpx.Response(
                    200,
                    headers={"Set-Cookie": "endpoint_admin_session=POST_LOGIN; Path=/"},
                    json={"ok": True},
                )
            return httpx.Response(200, json={"ok": True})

        _install_mock(monkeypatch, handler)
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        # Pass a whitespace-only cookie via the flag; expect login to be
        # invoked (flag is treated as "not set").
        result = runner.invoke(
            admin.app,
            ["--session-cookie", "   ", "pause", "alice@x"],
        )
        assert result.exit_code == 0, result.stderr
        assert login_count["n"] == 1, "blank cookie should fall through to password login"

    def test_password_file_resolves_password(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        pw_file = tmp_path / "pw.txt"
        pw_file.write_text("file-pw\n", encoding="utf-8")
        captured = _install_mock(
            monkeypatch,
            _login_then(
                paths={"/api/actions/users/alice@x/pause": httpx.Response(200, json={"ok": True})}
            ),
        )
        result = runner.invoke(
            admin.app,
            ["--password-file", str(pw_file), "pause", "alice@x"],
        )
        assert result.exit_code == 0, result.stderr
        login = next(r for r in captured if r.url.path == "/api/auth/login")
        assert json.loads(login.content) == {"password": "file-pw"}

    def test_no_creds_at_all_exits_2(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            lambda req: httpx.Response(500, text="should-never-reach"),
        )
        # No env, no flag.
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == admin.EXIT_AUTH_ERROR
        assert "authentication required" in result.stderr

    def test_login_wrong_password_exits_2(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/auth/login":
                return httpx.Response(401, json={"detail": {"message": "nope"}})
            raise AssertionError("should not have advanced past login")

        _install_mock(monkeypatch, handler)
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "wrong")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == admin.EXIT_AUTH_ERROR
        assert "authentication failed" in result.stderr

    def test_login_403_exits_2(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        """/login can return 403 when the daemon enforces an IP allowlist."""

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"detail": {"message": "forbidden"}})

        _install_mock(monkeypatch, handler)
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == admin.EXIT_AUTH_ERROR

    def test_login_500_exits_1(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": {"message": "kaboom"}})

        _install_mock(monkeypatch, handler)
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == admin.EXIT_API_ERROR

    def test_login_succeeds_but_no_cookie_exits_2(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The daemon answered 200 to /login but didn't set the session
        cookie — protocol violation, surface as auth failure."""

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        _install_mock(monkeypatch, handler)
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == admin.EXIT_AUTH_ERROR
        assert "no session cookie" in result.stderr

    def test_login_network_error_exits_3(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Network error during the login itself surfaces as exit 3."""

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("can't reach")

        _install_mock(monkeypatch, handler)
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == admin.EXIT_NETWORK_ERROR
        assert "daemon unreachable" in result.stderr


# ---------------------------------------------------------------------------
# Network / API error handling at the request level
# ---------------------------------------------------------------------------


class TestRequestErrors:
    def test_request_network_error_exits_3(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/auth/login":
                return httpx.Response(
                    200,
                    headers={"Set-Cookie": "endpoint_admin_session=C; Path=/"},
                    json={"ok": True},
                )
            raise httpx.ConnectError("cannot reach")

        _install_mock(monkeypatch, handler)
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == admin.EXIT_NETWORK_ERROR
        assert "daemon unreachable" in result.stderr

    def test_401_after_login_exits_2(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cookie expired between login and the actual call."""

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/auth/login":
                return httpx.Response(
                    200,
                    headers={"Set-Cookie": "endpoint_admin_session=C; Path=/"},
                    json={"ok": True},
                )
            return httpx.Response(401, json={"detail": {"message": "expired"}})

        _install_mock(monkeypatch, handler)
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == admin.EXIT_AUTH_ERROR

    def test_api_error_envelope_message_surfaced(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/users/alice@x/pause": httpx.Response(
                        500,
                        json={
                            "detail": {
                                "code": "internal_error",
                                "message": "boom",
                                "request_id": "rq-1",
                            }
                        },
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == admin.EXIT_API_ERROR
        # Code, message, request id all appear.
        assert "[internal_error]" in result.stderr
        assert "boom" in result.stderr
        assert "request_id=rq-1" in result.stderr

    def test_api_error_non_envelope_body(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Some upstream proxies replace the daemon's body with non-JSON."""
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/users/alice@x/pause": httpx.Response(
                        502, text="<html>bad gateway</html>"
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == admin.EXIT_API_ERROR
        # default + body fragment surface on stderr.
        assert "HTTP 502" in result.stderr
        assert "<html>" in result.stderr

    def test_api_error_json_with_non_dict_detail(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``detail`` is a string instead of the daemon's envelope dict."""
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/users/alice@x/pause": httpx.Response(
                        500, json={"detail": "raw string detail"}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == admin.EXIT_API_ERROR
        # Falls back to default + repr.
        assert "HTTP 500" in result.stderr

    def test_response_with_no_content(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 204-style empty response is treated as ``None`` (still printed
        as JSON ``null``) — no crash, clean exit."""
        _install_mock(
            monkeypatch,
            _login_then(
                paths={"/api/actions/users/alice@x/pause": httpx.Response(204, content=b"")}
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["pause", "alice@x"])
        assert result.exit_code == 0
        # JSON null printed.
        assert json.loads(result.stdout) is None

    def test_text_response_without_json_content_type(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A text/plain body is returned as a string (the metrics endpoint
        relies on this branch)."""
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/metrics": httpx.Response(
                        200,
                        text="endpoint_daemon_uptime_seconds 1.0\n",
                        headers={"content-type": "text/plain; version=0.0.4"},
                    )
                }
            ),
        )
        result = runner.invoke(admin.app, ["metrics"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Output channel separation
# ---------------------------------------------------------------------------


class TestOutputChannels:
    def test_pretty_flag_indents_output(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _login_then(
                paths={
                    "/api/actions/users/alice@x/pause": httpx.Response(
                        200, json={"ok": True, "deep": {"k": "v"}}
                    )
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["--pretty", "pause", "alice@x"])
        assert result.exit_code == 0
        # Pretty output has indentation on multiple lines.
        assert "\n  " in result.stdout

    def test_warnings_go_to_stderr_not_stdout(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _login_then(
                paths={"/api/config/overlay/auth.password": httpx.Response(200, json={"ok": True})}
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["config", "set", "auth.password", '"x"'])
        assert result.exit_code == 0
        # JSON on stdout.
        assert json.loads(result.stdout) == {"ok": True}
        # Warning on stderr (key looks like a secret).
        assert "looks like a secret" in result.stderr
        assert "looks like a secret" not in result.stdout


# ---------------------------------------------------------------------------
# Top-level options + version + bare invocation
# ---------------------------------------------------------------------------


class TestTopLevel:
    def test_version_flag(self, runner: CliRunner) -> None:
        result = runner.invoke(admin.app, ["--version"])
        assert result.exit_code == 0
        assert "endpoint-admin" in result.stdout

    def test_invalid_base_url_exits_4(self, runner: CliRunner) -> None:
        result = runner.invoke(
            admin.app,
            ["--base-url", "localhost:8080", "pause", "alice@x"],
        )
        assert result.exit_code == admin.EXIT_VALIDATION_ERROR
        assert "must include http://" in result.stderr

    def test_no_command_shows_help(self, runner: CliRunner) -> None:
        result = runner.invoke(admin.app, [])
        # Bare invocation prints help and exits cleanly.
        # Typer's no_args_is_help may use code 0 or 2 depending on path.
        assert result.exit_code in (0, 2)

    def test_main_function_invokes_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``admin.main()`` is the console-script entry point — verify it
        forwards to the Typer app object."""
        called: list[bool] = []

        def fake_app(*_args: object, **_kwargs: object) -> None:
            called.append(True)

        monkeypatch.setattr(admin, "app", fake_app)
        admin.main()
        assert called == [True]


# ---------------------------------------------------------------------------
# Memoised cookie reuse — verify _ensure_cookie short-circuits after login
# ---------------------------------------------------------------------------


class TestCookieMemoisation:
    def test_status_logs_in_only_once(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``status`` issues two requests (/health, /ready). Both are
        ``require_auth=False`` so login isn't triggered. But adding a
        require_auth call after them must reuse the cached cookie."""
        # Easier proof: run a multi-call command (dlq replay falling
        # back to clear+force-sync issues 3 calls) and confirm exactly
        # one /api/auth/login.
        login_count = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/auth/login":
                login_count["n"] += 1
                return httpx.Response(
                    200,
                    headers={"Set-Cookie": "endpoint_admin_session=C; Path=/"},
                    json={"ok": True},
                )
            if req.url.path.endswith("/replay"):
                return httpx.Response(404, json={"detail": {"message": "no"}})
            return httpx.Response(200, json={"ok": True})

        _install_mock(monkeypatch, handler)
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["dlq", "replay", "alice@x", "--platform", "teams"])
        assert result.exit_code == 0
        assert login_count["n"] == 1


# ---------------------------------------------------------------------------
# Format envelope helper directly
# ---------------------------------------------------------------------------


class TestFormatEnvelope:
    def test_format_envelope_full(self) -> None:
        client = admin._DaemonClient(
            admin.ClientOptions(
                base_url="http://x", session_cookie=None, password=None, pretty=False
            )
        )
        response = httpx.Response(
            500,
            json={
                "detail": {
                    "code": "boom",
                    "message": "kaboom",
                    "request_id": "rid-1",
                }
            },
        )
        msg = client._format_envelope(response, default="default")
        assert "[boom]" in msg
        assert "kaboom" in msg
        assert "request_id=rid-1" in msg

    def test_format_envelope_empty_text_fallback(self) -> None:
        client = admin._DaemonClient(
            admin.ClientOptions(
                base_url="http://x", session_cookie=None, password=None, pretty=False
            )
        )
        # Response with empty body and no content-type returns reason_phrase.
        response = httpx.Response(502, content=b"")
        msg = client._format_envelope(response, default="def")
        # Default + ': ' followed by reason_phrase or empty body.
        assert msg.startswith("def:")
