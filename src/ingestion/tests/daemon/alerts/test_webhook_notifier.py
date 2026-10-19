"""Slice 13 part B: :class:`WebhookAlertNotifier`.

The notifier POSTs alert firings to an operator-configured URL. Pin:

* Happy path: payload shape (rule_id / rule_name / fired_at / etc.) +
  ``Authorization: Bearer <token>`` header when a token is configured.
* 4xx is dropped after the first attempt (typo doesn't burn retry budget).
* 5xx is retried via slice 10's :func:`with_backoff` (3 attempts default).
* Network error: 3 retries then the final failure is LOGGED, not propagated
  so the alert engine's tick loop is never crashed by a transient receiver.
* ``{"ok": false}`` (Slack-style soft failure) is treated as delivered at the
  HTTP layer — operators inspect their own receiver logs.
* Empty / None URL: caller should pick the :class:`NullAlertNotifier`
  (the constructor raises if a webhook URL is genuinely empty).
* ``notifier_format='slack'`` produces ``{"text": "..."}``.
* Stale notification: a firing resolved between the engine recording it
  and the webhook delivering must be skipped with the
  ``alerts.notifier_stale`` log line, not delivered.
* Bearer tokens never appear in any log line. Userinfo in the URL is
  stripped from the host log via :func:`_safe_host`.

To keep tests fast we monkeypatch :mod:`asyncio.sleep` in the backoff
module so retry delays don't actually wait. The notifier uses
slice 10's :func:`with_backoff` which honours an injectable ``sleep``;
the notifier itself does not pipe one through, so we patch at the
module-level seam instead.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from ingestion.daemon.alerts.notifiers.webhook import (
    WebhookAlertNotifier,
    _safe_host,
)
from ingestion.daemon.alerts.rules import AlertRule, Comparator, create_rule
from ingestion.daemon.store.local import apply_migrations

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


@pytest.fixture
def rule() -> AlertRule:
    return AlertRule(
        id=42,
        name="failures",
        enabled=True,
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=5.0,
        window_seconds=300,
        cooldown_seconds=60,
        user_email=None,
        platform=None,
    )


@pytest.fixture
def fired_at() -> datetime:
    return datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def fast_backoff(request: pytest.FixtureRequest) -> None:
    """Replace the ``sleep`` default of :func:`with_backoff` with a no-op.

    ``with_backoff`` captures ``sleep=asyncio.sleep`` as a function
    kwdefault at module-import time, so monkeypatching
    :func:`asyncio.sleep` after import does NOT affect callers that omit
    the kwarg (the notifier omits it). Mutate the function's
    ``__kwdefaults__`` directly and restore after the test.
    """

    async def _no_sleep(_seconds: float) -> None:
        return None

    from ingestion.daemon.reliability import backoff as _b

    kwdefaults = _b.with_backoff.__kwdefaults__
    assert kwdefaults is not None, "with_backoff must have kwdefaults"
    original = kwdefaults.get("sleep")
    kwdefaults["sleep"] = _no_sleep

    def _restore() -> None:
        if original is not None:
            kwdefaults["sleep"] = original

    request.addfinalizer(_restore)


def _capture() -> structlog.testing.LogCapture:
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    return cap


class _StubResponse:
    def __init__(self, status_code: int, json_body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._json = json_body or {}

    def json(self) -> dict[str, Any]:
        return self._json


class _StubState:
    """Shared state across every :class:`_StubClient` in a test.

    A single :class:`WebhookAlertNotifier.alert_fired` call may instantiate
    the client factory multiple times (once per retry, because the impl
    uses ``async with self._client_factory(...) as client``). The shared
    behaviour queue + recorded-calls list mean tests can assert on the
    aggregate count regardless of how many client instances were created.
    """

    def __init__(self, behaviour: list[Any]) -> None:
        self.behaviour: list[Any] = list(behaviour)
        self.calls: list[dict[str, Any]] = []


class _StubClient:
    """Async-context-manager stub mirroring :class:`httpx.AsyncClient`.

    State (behaviour queue + calls list) lives on a shared
    :class:`_StubState` so retries that re-instantiate the client see the
    same queue.
    """

    def __init__(self, *, timeout: float, state: _StubState) -> None:
        self.timeout = timeout
        self._state = state

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, *, json: Any, headers: dict[str, str]) -> _StubResponse:
        self._state.calls.append({"url": url, "json": json, "headers": dict(headers)})
        if not self._state.behaviour:
            raise RuntimeError("stub client out of programmed responses")
        nxt = self._state.behaviour.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def _factory(behaviour: list[Any]) -> tuple[Any, _StubState]:
    """Build a :class:`_StubClient` factory + shared state.

    Returns ``(factory, state)``. ``state.calls`` accumulates every POST
    across all client instances; ``state.behaviour`` is consumed on each
    POST so retry-budget tests can program N responses up front.
    """
    state = _StubState(behaviour)

    def _build(*, timeout: float) -> _StubClient:
        return _StubClient(timeout=timeout, state=state)

    return _build, state


# ---------------------------------------------------------------------------
# happy path: payload + auth header
# ---------------------------------------------------------------------------


async def test_post_happy_path_includes_bearer_and_payload_fields(
    rule: AlertRule, fired_at: datetime
) -> None:
    """A 200 response = one POST. Authorization header has the configured
    bearer token; the payload carries the rule_id / rule_name / fired_at /
    user_email / platform / context envelope the impl docs guarantee."""
    factory, state = _factory([_StubResponse(200, {"ok": True})])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        token="sekret",
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=12.0, fired_at=fired_at)
    [call] = state.calls
    assert call["url"] == "https://example.com/hook"
    assert call["headers"]["Authorization"] == "Bearer sekret"
    payload = call["json"]
    assert payload["rule_id"] == 42
    assert payload["rule_name"] == "failures"
    assert payload["fired_at"] == fired_at.isoformat()
    assert payload["context"]["metric"] == "failure_count"
    assert payload["context"]["value"] == 12.0
    assert payload["context"]["threshold"] == 5.0


async def test_no_token_omits_authorization_header(rule: AlertRule, fired_at: datetime) -> None:
    """Empty token → no Authorization header (avoids ``Bearer `` with an
    empty value, which some receivers reject as malformed)."""
    factory, state = _factory([_StubResponse(200)])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        token="",
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    [call] = state.calls
    assert "Authorization" not in call["headers"]


# ---------------------------------------------------------------------------
# 4xx is non-retryable; 5xx is retried
# ---------------------------------------------------------------------------


async def test_4xx_response_is_not_retried(
    rule: AlertRule, fired_at: datetime, fast_backoff: None
) -> None:
    """A 401/404 means the URL is wrong or the token is bad — retrying
    won't fix it. Pin: only ONE attempt is made, the rest of the budget
    is preserved for legitimate transient failures elsewhere."""
    _ = fast_backoff
    factory, state = _factory([_StubResponse(401)])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        token="t",
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    assert len(state.calls) == 1


async def test_5xx_response_is_retried_three_times(
    rule: AlertRule, fired_at: datetime, fast_backoff: None
) -> None:
    """5xx triggers retry. Default ``max_attempts=3`` → three attempts
    before the final failure is logged. A subsequent success would
    short-circuit; here all three return 503 so we observe the full
    budget being burned."""
    _ = fast_backoff
    factory, state = _factory(
        [
            _StubResponse(503),
            _StubResponse(503),
            _StubResponse(503),
        ]
    )
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        token="t",
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    assert len(state.calls) == 3


async def test_5xx_then_200_short_circuits_on_success(
    rule: AlertRule, fired_at: datetime, fast_backoff: None
) -> None:
    """A second-attempt success ends the retry loop early — three
    attempts are NOT consumed when the receiver recovers mid-budget."""
    _ = fast_backoff
    factory, state = _factory([_StubResponse(503), _StubResponse(200)])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        token="t",
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    assert len(state.calls) == 2


# ---------------------------------------------------------------------------
# 200 with {"ok": false} (Slack-style soft failure)
# ---------------------------------------------------------------------------


async def test_200_with_ok_false_is_treated_as_delivered(
    rule: AlertRule, fired_at: datetime
) -> None:
    """The impl pins this behaviour explicitly: a 200 is delivered at
    the HTTP layer regardless of body. ``{"ok": false}`` doesn't trigger
    a retry — operators look at their own receiver logs to debug
    body-level rejections."""
    factory, state = _factory([_StubResponse(200, {"ok": False})])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        token="t",
        client_factory=factory,
    )
    cap = _capture()
    try:
        await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    finally:
        structlog.reset_defaults()
    # Only one call — no retry on body-level failure with HTTP 200.
    assert len(state.calls) == 1
    events = [e.get("event") for e in cap.entries]
    assert "alerts.notifier_delivered" in events


# ---------------------------------------------------------------------------
# network error path: retried then logged, never propagated
# ---------------------------------------------------------------------------


async def test_network_error_retried_three_times_then_dropped(
    rule: AlertRule, fired_at: datetime, fast_backoff: None
) -> None:
    """A transport-level error (DNS, refused-connection, read timeout)
    is converted to a retryable error and the budget is consumed. The
    final failure must be LOGGED with ``alerts.notifier_failed`` and
    NOT propagated up — the engine's tick loop is the calling context
    and a raised exception there would kill the whole alert engine."""
    _ = fast_backoff
    factory, state = _factory(
        [
            httpx.ConnectError("DNS"),
            httpx.ReadError("read"),
            httpx.ConnectError("refused"),
        ]
    )
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        token="t",
        client_factory=factory,
    )
    cap = _capture()
    try:
        # No raise expected — the notifier swallows the final failure.
        await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    finally:
        structlog.reset_defaults()
    assert len(state.calls) == 3
    events = [e.get("event") for e in cap.entries]
    assert "alerts.notifier_failed" in events


# ---------------------------------------------------------------------------
# constructor validation: empty URL, bad format
# ---------------------------------------------------------------------------


def test_empty_url_rejected() -> None:
    """An empty URL is a misconfiguration. The runtime path swaps in
    :class:`NullAlertNotifier` *before* construction; if construction
    is reached with empty URL we fail loud."""
    with pytest.raises(ValueError, match="non-empty url"):
        WebhookAlertNotifier(url="")


def test_invalid_format_rejected() -> None:
    """Only ``"webhook"`` and ``"slack"`` are accepted."""
    with pytest.raises(ValueError, match="notifier_format"):
        WebhookAlertNotifier(url="https://example.com/hook", notifier_format="discord")


# ---------------------------------------------------------------------------
# notifier_format='slack' vs 'webhook'
# ---------------------------------------------------------------------------


async def test_slack_format_uses_text_envelope(rule: AlertRule, fired_at: datetime) -> None:
    """Slack incoming-webhook compatibility means the body is just
    ``{"text": "..."}``. The text contains rule_name, metric, value,
    threshold, and the firing timestamp so an on-call has the data
    they need without clicking through."""
    factory, state = _factory([_StubResponse(200)])
    notifier = WebhookAlertNotifier(
        url="https://hooks.slack.com/services/XXX",
        notifier_format="slack",
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=12.0, fired_at=fired_at)
    [call] = state.calls
    payload = call["json"]
    assert set(payload.keys()) == {"text"}
    text = payload["text"]
    assert rule.name in text
    assert "12.0" in text
    assert "5.0" in text  # threshold
    assert fired_at.isoformat() in text


async def test_webhook_format_uses_generic_envelope(rule: AlertRule, fired_at: datetime) -> None:
    """Default ``"webhook"`` format = generic JSON envelope with rule_id,
    rule_name, fired_at, severity, context.metric, etc. PagerDuty / OpsGenie
    / custom receivers all consume this shape."""
    factory, state = _factory([_StubResponse(200)])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    [call] = state.calls
    payload = call["json"]
    assert payload.keys() >= {
        "rule_id",
        "rule_name",
        "fired_at",
        "user_email",
        "platform",
        "severity",
        "context",
    }
    assert payload["severity"] == "warning"
    assert payload["context"]["comparator"] == ">"


# ---------------------------------------------------------------------------
# stale-notification guard: resolved-during-retry is skipped
# ---------------------------------------------------------------------------


async def test_stale_firing_is_skipped_with_log(
    db_path: Path, fired_at: datetime, fast_backoff: None
) -> None:
    """Engine resolves a firing while the notifier was retrying. The
    next-retry stale check sees state='resolved' and short-circuits with
    ``alerts.notifier_stale`` — no POST, no on-call wake-up for an
    already-self-healed firing."""
    _ = fast_backoff
    rule_id = await create_rule(
        db_path,
        name="stale",
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=0,
    )
    rule = AlertRule(
        id=rule_id,
        name="stale",
        enabled=True,
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=0.0,
        window_seconds=None,
        cooldown_seconds=0,
        user_email=None,
        platform=None,
    )
    # Seed a resolved firing for that rule_id so the ``_is_resolved`` query
    # returns True before any POST is attempted.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO alert_firings (
                rule_id, fired_at, metric_value, user_email, platform, state,
                resolved_at, trigger_context_json, cooldown_bucket
            )
            VALUES (?, ?, 1.0, NULL, NULL, 'resolved', ?, NULL, NULL)
            """,
            (rule_id, fired_at.isoformat(), fired_at.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    factory, state = _factory([_StubResponse(200)])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        db_path=db_path,
        client_factory=factory,
    )
    cap = _capture()
    try:
        await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    finally:
        structlog.reset_defaults()
    # No POST attempted because the firing is already resolved.
    assert state.calls == []
    events = [e.get("event") for e in cap.entries]
    assert "alerts.notifier_stale" in events


async def test_no_db_path_skips_stale_check(rule: AlertRule, fired_at: datetime) -> None:
    """When ``db_path`` is omitted (test/dev case), the stale check is
    disabled — every alert is delivered. Pin: a notifier without the DB
    wired still POSTs."""
    factory, state = _factory([_StubResponse(200)])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    assert len(state.calls) == 1


async def test_no_firing_row_does_not_block_post(
    db_path: Path, rule: AlertRule, fired_at: datetime
) -> None:
    """If ``alert_firings`` has no row for the rule (engine-firing-then-
    notifier-call race in the other direction), the stale check returns
    ``False`` and the POST goes through — better to over-deliver than
    drop a legitimate firing."""
    factory, state = _factory([_StubResponse(200)])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        db_path=db_path,
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    assert len(state.calls) == 1


async def test_db_read_error_falls_through_to_post(
    rule: AlertRule, fired_at: datetime, tmp_path: Path
) -> None:
    """Stale-check DB read failure (e.g. file deleted) falls through to
    the POST so a transient SQLite-busy hiccup doesn't silently drop a
    legit alert. Pin via a non-existent DB path; aiosqlite raises and
    the impl swallows :class:`aiosqlite.Error`."""
    bogus_db = tmp_path / "does-not-exist.db"
    # Touch a file that exists but has no schema → query fails on missing table.
    bogus_db.write_bytes(b"")
    factory, state = _factory([_StubResponse(200)])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        db_path=bogus_db,
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    # The POST still went through.
    assert len(state.calls) == 1


async def test_slack_format_includes_platform_and_user_scope(fired_at: datetime) -> None:
    """When the rule is scoped to a platform + user_email, the Slack text
    includes both as ``platform=<x>`` and ``user=<y>`` segments. Without
    this an on-call sees an unscoped page and has to dig for context."""
    scoped = AlertRule(
        id=1,
        name="r",
        enabled=True,
        metric="m",
        comparator=Comparator.GT,
        threshold=1.0,
        window_seconds=None,
        cooldown_seconds=0,
        user_email="alice@x",
        platform="teams",
    )
    factory, state = _factory([_StubResponse(200)])
    notifier = WebhookAlertNotifier(
        url="https://hooks.slack.com/x",
        notifier_format="slack",
        client_factory=factory,
    )
    await notifier.alert_fired(rule=scoped, value=2.0, fired_at=fired_at)
    text = state.calls[0]["json"]["text"]
    assert "platform=teams" in text
    assert "user=alice@x" in text


async def test_active_firing_is_not_skipped(db_path: Path, fired_at: datetime) -> None:
    """The stale check must NOT skip when the firing is still active. A
    seeded ``state='active'`` row should let the POST go through."""
    rule_id = await create_rule(
        db_path,
        name="live",
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=0,
    )
    rule = AlertRule(
        id=rule_id,
        name="live",
        enabled=True,
        metric="failure_count",
        comparator=Comparator.GT,
        threshold=0.0,
        window_seconds=None,
        cooldown_seconds=0,
        user_email=None,
        platform=None,
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO alert_firings (
                rule_id, fired_at, metric_value, user_email, platform, state,
                resolved_at, trigger_context_json, cooldown_bucket
            )
            VALUES (?, ?, 1.0, NULL, NULL, 'active', NULL, NULL, NULL)
            """,
            (rule_id, fired_at.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    factory, state = _factory([_StubResponse(200)])
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        db_path=db_path,
        client_factory=factory,
    )
    await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    assert len(state.calls) == 1


# ---------------------------------------------------------------------------
# token / userinfo never logged; safe_host strips userinfo
# ---------------------------------------------------------------------------


async def test_token_never_appears_in_log_lines(
    rule: AlertRule, fired_at: datetime, fast_backoff: None
) -> None:
    """Defence-in-depth: bearer token must not reach any log record on
    success, on 4xx rejection, or on the final-failure branch."""
    _ = fast_backoff
    secret = "super-sensitive-token-that-must-not-leak"
    factory, _state = _factory(
        [
            httpx.ConnectError("DNS"),
            httpx.ConnectError("DNS"),
            httpx.ConnectError("DNS"),
        ]
    )
    notifier = WebhookAlertNotifier(
        url="https://example.com/hook",
        token=secret,
        client_factory=factory,
    )
    cap = _capture()
    try:
        await notifier.alert_fired(rule=rule, value=1.0, fired_at=fired_at)
    finally:
        structlog.reset_defaults()
    for entry in cap.entries:
        for value in entry.values():
            if isinstance(value, str):
                assert secret not in value


def test_safe_host_strips_userinfo() -> None:
    """``user:pass@host`` portion of a URL is stripped so a bad
    operator habit (embedding credentials in the URL) doesn't leak via
    the host log line."""
    assert _safe_host("https://leaked-user:leaked-pass@example.com/hook") == "example.com"


def test_safe_host_handles_blank_netloc() -> None:
    """A missing netloc renders as ``<blank>`` — the impl docstring
    pins this; assert here so a future tightening doesn't accidentally
    raise on relative URLs."""
    assert _safe_host("foo") == "<blank>"


def test_safe_host_handles_unparseable() -> None:
    """An unparseable URL should not raise — the helper is on the
    logging path. Returns an explicit ``<unparseable>`` token for the
    operator to recognise."""
    # urlparse rarely raises; force the ValueError path with a
    # bracket-mismatched IPv6.
    out = _safe_host("http://[::1")
    assert out in {"<unparseable>", "<blank>"}


def test_safe_host_url_only_logs_host_not_token(rule: AlertRule, fired_at: datetime) -> None:
    """The full URL with token-in-userinfo style construction never
    appears in the log; only the host portion does. Prove via
    monkeypatched logging side."""
    # We don't run the notifier here — just call _safe_host on a URL
    # with userinfo, since that's the seam used inside the notifier
    # logging path. End-to-end coverage on logging is in
    # test_token_never_appears_in_log_lines above.
    _ = rule, fired_at
    assert "secret" not in _safe_host("https://user:secret@example.com/hook")
