"""Unit tests for ``ingestion.cli.admin_debug`` (the ``debug`` subgroup).

Read-only diagnostic helpers: ``state-dump``, ``lockfile``,
``queue-stats``, ``failing-creds``. The lockfile reader bypasses the
daemon entirely — it parses a file on disk — so those tests don't need
HTTP at all. The other three reuse the same MockTransport plumbing as
:mod:`tests.test_cli_admin`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from ingestion.cli import admin, admin_debug
from typer.testing import CliRunner

# ---------------------------------------------------------------------------
# Local plumbing (mirrors the helpers in test_cli_admin.py — duplicating
# them keeps the two test modules independent so each can run alone).
# ---------------------------------------------------------------------------


Handler = Callable[[httpx.Request], httpx.Response]


def _mock_client_factory(handler: Handler) -> Callable[[Any], httpx.Client]:
    """Return a ``_client()`` replacement that injects ``MockTransport``.

    Mirrors the production ``_DaemonClient._client`` shape so cookies
    are seeded at construction (httpx 0.28 deprecated per-request
    ``cookies=``) and the client is cached on ``self._http`` for reuse
    across multi-call subcommands.
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
    captured: list[httpx.Request] = []

    def wrap(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    monkeypatch.setattr(admin._DaemonClient, "_client", _mock_client_factory(wrap))
    return captured


def _state_dump_handler(payloads: dict[str, httpx.Response]) -> Handler:
    """Build a handler keyed by URL path that auto-handles login + missing
    paths return 404."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/auth/login":
            return httpx.Response(
                200,
                headers={"Set-Cookie": "endpoint_admin_session=C; Path=/"},
                json={"ok": True},
            )
        return payloads.get(
            req.url.path,
            httpx.Response(404, json={"detail": {"message": "missing"}}),
        )

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
# state-dump
# ---------------------------------------------------------------------------


class TestStateDump:
    def test_state_dump_aggregates_all_sources(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_mock(
            monkeypatch,
            _state_dump_handler(
                {
                    "/api/syncs": httpx.Response(200, json=[{"id": "s1"}]),
                    "/api/errors": httpx.Response(200, json={"counts": {"dlq": 5}}),
                    "/api/audit": httpx.Response(200, json=[{"event": "x"}]),
                    "/api/config/overlay": httpx.Response(200, json={"k": "v"}),
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "state-dump"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        # Every documented source is present under "snapshot".
        for label in (
            "running_syncs",
            "failed_syncs",
            "errors_feed",
            "audit_tail",
            "config_overlay",
        ):
            assert label in body["snapshot"], f"missing source {label}"
        # Limits surfaced — 100/100/100.
        assert body["limits"]["audit_tail_rows"] == 100
        assert body["limits"]["dlq_tail_rows"] == 100
        assert body["limits"]["syncs_tail_rows"] == 100
        # ``running_syncs`` and ``failed_syncs`` both hit /api/syncs with
        # a different ``state`` query.
        sync_states = sorted(r.url.params["state"] for r in captured if r.url.path == "/api/syncs")
        assert sync_states == ["failed", "running"]

    def test_state_dump_one_failing_endpoint_does_not_blank_others(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One source returning 500 → that source becomes ``null``;
        siblings are untouched."""
        _install_mock(
            monkeypatch,
            _state_dump_handler(
                {
                    "/api/syncs": httpx.Response(500, json={"detail": {"message": "boom"}}),
                    "/api/errors": httpx.Response(200, json={"counts": {"dlq": 0}}),
                    "/api/audit": httpx.Response(200, json=[]),
                    "/api/config/overlay": httpx.Response(200, json={}),
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "state-dump"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        # Failing sources are None; healthy sources keep their data.
        assert body["snapshot"]["running_syncs"] is None
        assert body["snapshot"]["failed_syncs"] is None
        assert body["snapshot"]["errors_feed"] == {"counts": {"dlq": 0}}
        # stderr carries the skip notes.
        assert "[debug] source=running_syncs skipped" in result.stderr

    def test_state_dump_pretty_flag(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _state_dump_handler(
                {
                    "/api/syncs": httpx.Response(200, json=[]),
                    "/api/errors": httpx.Response(200, json={}),
                    "/api/audit": httpx.Response(200, json=[]),
                    "/api/config/overlay": httpx.Response(200, json={}),
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["--pretty", "debug", "state-dump"])
        assert result.exit_code == 0
        # Pretty output has indented multi-line output.
        assert "\n  " in result.stdout


# ---------------------------------------------------------------------------
# lockfile
# ---------------------------------------------------------------------------


class TestLockfile:
    def test_not_found(self, runner: CliRunner, tmp_path: Path) -> None:
        target = tmp_path / "missing.lock"
        result = runner.invoke(admin.app, ["debug", "lockfile", "--path", str(target)])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["exists"] is False
        assert body["pid"] is None
        assert body["raw"] is None
        assert body["parse_error"] is None
        # No pid means no alive_note (nothing to disambiguate).
        assert body["alive_note"] is None

    def test_well_formed(self, runner: CliRunner, tmp_path: Path) -> None:
        target = tmp_path / "ok.lock"
        target.write_text("12345\nhost-a\n2026-04-26T00:00:00Z\n", encoding="utf-8")
        result = runner.invoke(admin.app, ["debug", "lockfile", "--path", str(target)])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["exists"] is True
        assert body["pid"] == 12345
        assert body["hostname"] == "host-a"
        assert body["started_at"] == "2026-04-26T00:00:00Z"
        # ``alive`` is bool-or-None; a random 5-digit pid is unlikely to
        # match this process, so we only assert the type contract.
        assert body["alive"] in (True, False, None)
        # ``alive_note`` is a non-empty string for any pid'd lockfile —
        # disambiguates the bare alive flag for incident response.
        assert isinstance(body["alive_note"], str)
        assert body["alive_note"]
        # ``current_hostname`` is always present.
        assert isinstance(body["current_hostname"], str)
        assert len(body["current_hostname"]) > 0

    def test_default_path_used_when_no_flag(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --path, the default ``/var/lib/ingestor/state.db.lock``
        is used; we patch the DEFAULT_LOCKFILE_PATH so the test doesn't
        depend on filesystem state."""
        monkeypatch.setattr(
            admin_debug,
            "DEFAULT_LOCKFILE_PATH",
            Path("/nonexistent/somewhere.lock"),
        )
        result = runner.invoke(admin.app, ["debug", "lockfile"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["path"] == "/nonexistent/somewhere.lock"
        assert body["exists"] is False

    def test_malformed_one_line(self, runner: CliRunner, tmp_path: Path) -> None:
        target = tmp_path / "bad.lock"
        target.write_text("only-one-line\n", encoding="utf-8")
        result = runner.invoke(admin.app, ["debug", "lockfile", "--path", str(target)])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["exists"] is True
        assert body["raw"] == "only-one-line\n"
        assert body["parse_error"] is not None
        assert "malformed" in body["parse_error"]

    def test_malformed_non_integer_pid(self, runner: CliRunner, tmp_path: Path) -> None:
        target = tmp_path / "bad-pid.lock"
        target.write_text("not-a-pid\nhost-a\n2026-04-26T00:00:00Z\n", encoding="utf-8")
        result = runner.invoke(admin.app, ["debug", "lockfile", "--path", str(target)])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["exists"] is True
        assert body["pid"] is None
        assert body["hostname"] == "host-a"
        assert body["started_at"] == "2026-04-26T00:00:00Z"
        assert "non-integer pid" in body["parse_error"]

    def test_read_oserror_returns_parse_error(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / "exists.lock"
        target.write_text("0\nhost\n", encoding="utf-8")

        def fail_read(self: Path, *_args: object, **_kwargs: object) -> str:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", fail_read)
        result = runner.invoke(admin.app, ["debug", "lockfile", "--path", str(target)])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["exists"] is True
        assert "read failed" in body["parse_error"]


class TestPidAliveNote:
    """Pin every branch of the disambiguator that translates the bare
    ``alive`` flag into a one-sentence operator-facing explanation."""

    def test_alive_true_says_running(self) -> None:
        note = admin_debug._pid_alive_note(12345, True, "host-a")
        assert "running" in note
        assert "12345" in note
        assert "cross-check hostname" in note

    def test_alive_false_with_valid_pid_says_not_running(self) -> None:
        note = admin_debug._pid_alive_note(12345, False, "host-a")
        assert "not running" in note
        assert "12345" in note

    def test_alive_false_with_invalid_pid_says_invalid(self) -> None:
        note = admin_debug._pid_alive_note(0, False, None)
        assert "invalid pid" in note
        assert "malformed" in note

    def test_alive_none_with_cross_host_explains_hostname_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(admin_debug.socket, "gethostname", lambda: "current-host")
        note = admin_debug._pid_alive_note(12345, None, "different-host")
        assert "different host" in note
        assert "different-host" in note
        assert "current-host" in note

    def test_alive_none_with_same_host_explains_perm_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(admin_debug.socket, "gethostname", lambda: "host-a")
        note = admin_debug._pid_alive_note(12345, None, "host-a")
        assert "no permission" in note
        assert "another user" in note

    def test_alive_none_with_unknown_hostname_falls_back_to_perm_denied(
        self,
    ) -> None:
        # When the lockfile has no hostname, we can't tell if it's
        # cross-host; fall back to the perm-denied explanation.
        note = admin_debug._pid_alive_note(12345, None, None)
        assert "no permission" in note


class TestPidAlive:
    def test_pid_zero_returns_false(self) -> None:
        # A pid <= 0 short-circuits to False without calling os.kill.
        assert admin_debug._pid_alive(0) is False
        assert admin_debug._pid_alive(-1) is False

    def test_self_pid_alive(self) -> None:
        import os

        # Our own pid must be alive.
        assert admin_debug._pid_alive(os.getpid()) is True

    def test_dead_pid_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raises(_pid: int, _sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr("os.kill", raises)
        assert admin_debug._pid_alive(99999) is False

    def test_permission_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raises(_pid: int, _sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr("os.kill", raises)
        # PermissionError -> we can't tell -> None.
        assert admin_debug._pid_alive(1) is None


# ---------------------------------------------------------------------------
# queue-stats
# ---------------------------------------------------------------------------


_QUEUE_METRICS = (
    "endpoint_daemon_dead_letter_count 7\n"
    "endpoint_daemon_queue_depth 3\n"
    "endpoint_daemon_active_syncs 1\n"
    "endpoint_daemon_workers_available 2\n"
    "endpoint_daemon_uptime_seconds 9000\n"
)


class TestQueueStats:
    def test_queue_stats_happy_path(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _state_dump_handler(
                {
                    "/api/metrics": httpx.Response(
                        200,
                        text=_QUEUE_METRICS,
                        headers={"content-type": "text/plain"},
                    ),
                    "/api/errors": httpx.Response(200, json={"counts": {"dlq": 7}}),
                    "/api/audit": httpx.Response(200, json=[{"event": "e"}]),
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "queue-stats"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["dead_letter_count"] == 7.0
        assert body["queue_depth"] == 3.0
        assert body["active_syncs"] == 1.0
        assert body["workers_available"] == 2.0
        assert body["uptime_seconds"] == 9000.0
        assert body["errors_feed_counts"] == {"dlq": 7}
        assert body["audit_sample_returned"] == 1

    def test_queue_stats_missing_gauges_default_to_none(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``_gauge`` helper returns None when the key is absent or
        non-numeric — so a stripped-down /metrics should yield None for
        all but the surfaced ones."""
        _install_mock(
            monkeypatch,
            _state_dump_handler(
                {
                    "/api/metrics": httpx.Response(
                        200,
                        text="",
                        headers={"content-type": "text/plain"},
                    ),
                    "/api/errors": httpx.Response(200, json={}),
                    "/api/audit": httpx.Response(200, json=[]),
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "queue-stats"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        for key in (
            "dead_letter_count",
            "queue_depth",
            "active_syncs",
            "workers_available",
            "uptime_seconds",
        ):
            assert body[key] is None, f"{key} should be None when /metrics is empty"
        # errors_feed_counts is None when the body has no ``counts`` key.
        assert body["errors_feed_counts"] is None
        assert body["audit_sample_returned"] == 0

    def test_queue_stats_metrics_with_string_value(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-numeric metric value falls back to a string in
        ``parse_prometheus``; ``_gauge`` returns None for non-numeric
        values, leaving the gauge as None in the output."""
        _install_mock(
            monkeypatch,
            _state_dump_handler(
                {
                    "/api/metrics": httpx.Response(
                        200,
                        text="endpoint_daemon_dead_letter_count NOT_A_NUMBER\n",
                        headers={"content-type": "text/plain"},
                    ),
                    "/api/errors": httpx.Response(200, json={}),
                    "/api/audit": httpx.Response(200, json=[]),
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "queue-stats"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        # parse_prometheus stored it as a string; _gauge returned None.
        assert body["dead_letter_count"] is None


# ---------------------------------------------------------------------------
# failing-creds
# ---------------------------------------------------------------------------


class TestFailingCreds:
    def test_failing_creds_dedupes_overlap(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _state_dump_handler(
                {
                    "/api/credentials": httpx.Response(
                        200,
                        json=[
                            {"user_email": "a@x", "platform": "teams"},
                            {"user_email": "b@x", "platform": "moodle"},
                            "not-a-dict-row",  # filtered out
                        ],
                    ),
                    "/api/errors": httpx.Response(
                        200,
                        json={
                            "items": [
                                {"user_email": "a@x", "platform": "teams"},
                                {"user_email": "c@x", "platform": "telegram"},
                                ["bogus"],  # filtered out
                            ]
                        },
                    ),
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "failing-creds"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        # Only dict rows survive.
        assert len(body["expired_credentials"]) == 2
        assert len(body["dead_letter_entries"]) == 2
        # Overlap is exactly one (a@x/teams).
        assert body["summary"]["overlap_count"] == 1
        assert body["summary"]["expired_count"] == 2
        assert body["summary"]["dead_letter_count"] == 2

    def test_failing_creds_empty_responses(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_mock(
            monkeypatch,
            _state_dump_handler(
                {
                    "/api/credentials": httpx.Response(200, json=[]),
                    "/api/errors": httpx.Response(200, json={"items": []}),
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "failing-creds"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["summary"]["expired_count"] == 0
        assert body["summary"]["dead_letter_count"] == 0
        assert body["summary"]["overlap_count"] == 0

    def test_failing_creds_handles_bad_response_shapes(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``/api/credentials`` returns a dict (not a list); ``/api/errors``
        returns a list (not a dict). Both should degrade to empty rows
        without crashing."""
        _install_mock(
            monkeypatch,
            _state_dump_handler(
                {
                    "/api/credentials": httpx.Response(200, json={"unexpected": "shape"}),
                    "/api/errors": httpx.Response(200, json=["unexpected", "shape"]),
                }
            ),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "failing-creds"])
        assert result.exit_code == 0
        body = json.loads(result.stdout)
        assert body["summary"]["expired_count"] == 0
        assert body["summary"]["dead_letter_count"] == 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestInternalHelpers:
    def test_filter_dict_rows(self) -> None:
        items = [{"a": 1}, "string", 42, {"b": 2}, None, ["nested"]]
        out = admin_debug._filter_dict_rows(items)
        assert out == [{"a": 1}, {"b": 2}]

    def test_filter_dict_rows_handles_non_iterable_input(self) -> None:
        # The helper expects ``Any`` and iterates; passing a list always
        # works. We pin the typical contract here.
        assert admin_debug._filter_dict_rows([]) == []

    def test_pair_extracts_email_platform(self) -> None:
        assert admin_debug._pair({"user_email": "a@x", "platform": "teams"}) == (
            "a@x",
            "teams",
        )

    def test_pair_handles_missing_keys(self) -> None:
        assert admin_debug._pair({}) == ("", "")
        assert admin_debug._pair({"user_email": None, "platform": None}) == ("", "")

    def test_summary_builder_full_round_trip(self, tmp_path: Path) -> None:
        """``_summary`` defaults all fields then applies overrides; verify
        a typical override set produces the right shape."""
        summary = admin_debug._summary(
            tmp_path / "fake.lock",
            exists=True,
            pid=42,
            hostname="h",
            started_at="2026-04-26T00:00:00Z",
            alive=True,
            raw="42\nh\n2026-04-26T00:00:00Z\n",
        )
        assert summary.exists is True
        assert summary.pid == 42
        assert summary.hostname == "h"
        assert summary.alive is True
        assert summary.parse_error is None

    def test_safe_query_swallows_typer_exit(
        self,
        runner: CliRunner,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``_safe_query`` converts a typer.Exit into a stderr note + None
        so a single broken endpoint doesn't blank the rest of the dump."""
        import typer as t

        class _FailingClient:
            def request(self, *_args: object, **_kwargs: object) -> Any:
                raise t.Exit(code=3)

        result = admin_debug._safe_query(
            _FailingClient(),
            "GET",
            "/api/foo",
            params=None,
            label="foo",
        )
        assert result is None
        # Notice the underscore module reference is correct — _safe_query
        # uses ``__import__("sys")``.
        captured = capsys.readouterr()
        assert "[debug] source=foo skipped" in captured.err
        assert "exit_code=3" in captured.err

    def test_emit_json_uses_pretty_from_context(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``_emit_json`` honours the ``client_options.pretty`` flag from
        the Typer context object when no explicit override is passed."""
        import typer as t

        ctx = t.Context(
            t.core.TyperCommand("x", callback=lambda: None)  # type: ignore[arg-type]
        )
        ctx.obj = {
            "client_options": admin.ClientOptions(
                base_url="http://x",
                session_cookie=None,
                password=None,
                pretty=True,
            )
        }
        admin_debug._emit_json(ctx, {"k": "v"})
        out = capsys.readouterr().out
        # Pretty serialised → indented body.
        assert "  " in out

    def test_emit_json_with_no_options_in_context(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When the Typer context bag is empty (debug command run outside
        the normal callback wiring), ``_emit_json`` should default to
        compact and not crash."""
        import typer as t

        ctx = t.Context(
            t.core.TyperCommand("x", callback=lambda: None)  # type: ignore[arg-type]
        )
        ctx.obj = {}  # no client_options
        admin_debug._emit_json(ctx, {"k": "v"})
        out = capsys.readouterr().out
        # Compact (no indent).
        assert json.loads(out) == {"k": "v"}

    def test_emit_json_with_none_obj(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``ctx.obj`` is None on some invocation paths; the emitter must
        still work."""
        import typer as t

        ctx = t.Context(
            t.core.TyperCommand("x", callback=lambda: None)  # type: ignore[arg-type]
        )
        ctx.obj = None
        admin_debug._emit_json(ctx, {"k": "v"})
        out = capsys.readouterr().out
        assert json.loads(out) == {"k": "v"}

    def test_emit_json_explicit_pretty_overrides_ctx(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When ``pretty`` is passed explicitly, ctx is ignored."""
        import typer as t

        ctx = t.Context(
            t.core.TyperCommand("x", callback=lambda: None)  # type: ignore[arg-type]
        )
        ctx.obj = {
            "client_options": admin.ClientOptions(
                base_url="http://x",
                session_cookie=None,
                password=None,
                pretty=False,
            )
        }
        admin_debug._emit_json(ctx, {"k": "v"}, pretty=True)
        out = capsys.readouterr().out
        assert "  " in out


# ---------------------------------------------------------------------------
# audit-completeness-check (slice 12)
# ---------------------------------------------------------------------------


class TestAuditCompletenessCheck:
    def test_empty_audit_log_reports_every_known_action_missing(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh DB: zero rows in audit_log → every known action lands
        in ``missing``. ``unexpected`` stays empty."""
        _install_mock(
            monkeypatch,
            _state_dump_handler({"/api/audit": httpx.Response(200, json=[])}),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "audit-completeness-check"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["window_hours"] == 24
        assert body["rows_seen"] == 0
        # Every known action is reported missing — the allowlist is
        # the canonical inventory.
        for action in admin_debug._KNOWN_AUDIT_ACTIONS:  # noqa: SLF001
            assert action in body["missing"]
        assert body["unexpected"] == []

    def test_known_actions_seen_in_window_drop_off_missing(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Audit feed carries one row per known action → ``missing`` empty."""
        rows = [
            {"action": a, "timestamp": "2026-04-26T00:00:00+00:00"}
            for a in admin_debug._KNOWN_AUDIT_ACTIONS
        ]  # noqa: SLF001
        _install_mock(
            monkeypatch,
            _state_dump_handler({"/api/audit": httpx.Response(200, json=rows)}),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "audit-completeness-check"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["missing"] == []
        assert body["unexpected"] == []

    def test_unknown_action_in_feed_lands_in_unexpected(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An action that's NOT in the allowlist → ``unexpected``."""
        rows = [
            {"action": "user.pause", "timestamp": "2026-04-26T00:00:00+00:00"},
            {"action": "wholly.unknown.action", "timestamp": "2026-04-26T00:00:00+00:00"},
        ]
        _install_mock(
            monkeypatch,
            _state_dump_handler({"/api/audit": httpx.Response(200, json=rows)}),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "audit-completeness-check"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert "wholly.unknown.action" in body["unexpected"]
        # user.pause was in the feed -> not missing.
        assert "user.pause" not in body["missing"]

    def test_custom_hours_flag_propagates_to_query(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_mock(
            monkeypatch,
            _state_dump_handler({"/api/audit": httpx.Response(200, json=[])}),
        )
        monkeypatch.setenv("ENDPOINT_ADMIN_PASSWORD", "pw")
        result = runner.invoke(admin.app, ["debug", "audit-completeness-check", "--hours", "72"])
        assert result.exit_code == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["window_hours"] == 72
        # Confirm the ``since`` query param actually went out.
        audit_calls = [r for r in captured if r.url.path == "/api/audit"]
        assert audit_calls, "expected at least one /api/audit call"
        assert "since" in audit_calls[0].url.params
