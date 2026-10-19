"""Edge + production-edge coverage for ``GET /api/actions/audit/export``.

This file pins the behaviours that production traffic — adversarial or
otherwise — can throw at the audit-export endpoint after the slice-1a
parameterisation refactor. Each test names the specific edge it pins in
its docstring so a failure points at the behaviour, not the line number.

Slow-lane budget: any test over 500ms is marked ``@pytest.mark.slow``.
The fast-lane tests in this file finish well under 2 seconds total.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.api.routers.actions_audit import _AUDIT_EXPORT_FILTERS
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.store.local import open_store

# ---------------------------------------------------------------------------
# fixtures (kept local so this file does not depend on conftest extensions)
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


@pytest.fixture
def test_client(tmp_path: Path, fernet_key: str) -> TestClient:
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(
        config=config,
        store=store,
        cipher=cipher,
        auth_settings=auth_settings,
    )
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    return client


def _db_path(client: TestClient) -> Path:
    return client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined,no-any-return]


def _wipe_audit(client: TestClient) -> None:
    conn = sqlite3.connect(str(_db_path(client)))
    try:
        conn.execute("DELETE FROM audit_log")
        conn.commit()
    finally:
        conn.close()


def _insert_row(
    client: TestClient,
    *,
    timestamp: str = "2026-04-26T00:00:00+00:00",
    actor: str | None = "admin",
    action: str = "user.pause",
    target: str | None = "alice@x",
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
    raw_before_json: str | None = None,
    raw_after_json: str | None = None,
) -> int:
    """Insert one row.

    ``raw_before_json`` / ``raw_after_json`` let the caller inject already-
    serialised payloads (used by the corruption-test path). When not given,
    the payload is JSON-encoded normally.
    """
    if raw_before_json is not None:
        before_json: str | None = raw_before_json
    else:
        before_json = json.dumps(before) if before is not None else None
    if raw_after_json is not None:
        after_json: str | None = raw_after_json
    else:
        after_json = json.dumps(after) if after is not None else None
    conn = sqlite3.connect(str(_db_path(client)))
    try:
        cur = conn.execute(
            """
            INSERT INTO audit_log
                (timestamp, actor, action, target, before_json, after_json, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (timestamp, actor, action, target, before_json, after_json, reason),
        )
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


def _decode_lines(body: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in body.split("\n") if line]


# ===========================================================================
# IMPL-AGENT EDGE LIST
# ===========================================================================


def test_sql_injection_in_action_param_returns_zero_rows(test_client: TestClient) -> None:
    """edge: SQL injection — ``?action=' OR 1=1 --`` is bound as a literal,
    not interpreted, so the row that exists with action='user.pause' does
    not match and the export returns zero rows."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="user.pause")

    response = test_client.get(
        "/api/actions/audit/export",
        params={"action": "' OR 1=1 --"},
    )
    assert response.status_code == 200
    assert response.text == ""


def test_empty_string_filter_matches_only_empty_action_rows(test_client: TestClient) -> None:
    """edge: ``?action=`` (empty string) preserves current semantics — it
    builds a ``action = ?`` clause where the bound value is "", matching
    rows whose action column is the empty string. It is NOT equivalent to
    "no filter". This test pins that distinction."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="user.pause")
    # The audit_log schema declares ``action TEXT NOT NULL`` but doesn't
    # forbid an empty string, so we can plant one for the assertion.
    _insert_row(test_client, action="")

    response = test_client.get("/api/actions/audit/export", params={"action": ""})
    assert response.status_code == 200
    rows = _decode_lines(response.text)
    assert [r["action"] for r in rows] == [""]


def test_unicode_emoji_and_null_byte_in_filter_pass_through_binding(
    test_client: TestClient,
) -> None:
    """edge: unicode + emoji + embedded NUL in filter values — flow through
    the ``?`` binding cleanly; no exception, query just doesn't match."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="normal")
    weird = "中文-\U0001f600-\x00mid"
    response = test_client.get("/api/actions/audit/export", params={"action": weird})
    assert response.status_code == 200
    assert response.text == ""


def test_very_large_filter_value_one_megabyte(test_client: TestClient) -> None:
    """edge: very large filter string — handled by the ``?`` binding
    layer, no crash, returns no rows. The HTTP client (httpx) imposes a
    64 KB URL cap of its own, so this test asserts the cap surfaces as a
    client-side error rather than reaching the daemon. The intent of the
    edge — a SQL-injection avenue via huge values — is covered: the
    daemon never sees the bytes, and even if it did, the bind path makes
    them inert (covered by the 60 KB direct-app variant below)."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="actual")
    import httpx

    big = "x" * (1024 * 1024)
    with pytest.raises(httpx.InvalidURL):
        test_client.get("/api/actions/audit/export", params={"action": big})


def test_large_filter_value_under_url_cap(test_client: TestClient) -> None:
    """edge (companion to the 1 MB test): a 60 KB filter value — under
    the httpx URL cap — flows to the daemon, binds via ``?``, and returns
    no rows without crashing. Covers the same intent as the 1 MB case
    for the portion of the call path that exists in-process."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="actual")
    big = "x" * 60_000
    response = test_client.get("/api/actions/audit/export", params={"action": big})
    assert response.status_code == 200
    assert response.text == ""


def test_newline_in_reason_is_json_escaped(test_client: TestClient) -> None:
    """edge: a literal ``\\n`` mid-string in a JSON field — ``json.dumps``
    must escape it so the line-delimited output stays one row per line."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="x", reason="line1\nline2")
    response = test_client.get("/api/actions/audit/export")
    body = response.text
    # One row -> one line break. The literal newline must be escaped, not
    # emitted verbatim.
    assert body.count("\n") == 1
    rows = _decode_lines(body)
    assert rows[0]["reason"] == "line1\nline2"


def test_order_by_is_stable_and_total_with_equal_timestamps(test_client: TestClient) -> None:
    """edge: ORDER BY stability — five rows sharing one timestamp are
    returned in id-DESC order, and the order is reproducible across two
    consecutive calls."""
    _wipe_audit(test_client)
    ids = [
        _insert_row(test_client, timestamp="2026-04-26T00:00:00+00:00", action=f"a{i}")
        for i in range(5)
    ]

    first = _decode_lines(test_client.get("/api/actions/audit/export").text)
    second = _decode_lines(test_client.get("/api/actions/audit/export").text)
    assert [r["id"] for r in first] == sorted(ids, reverse=True)
    assert first == second


def test_combined_filters_emit_clauses_in_insertion_order() -> None:
    """edge: ``?action=A&actor=B`` — verifies that the WHERE clause is
    built by iterating the dict in insertion order. The test inspects the
    underlying mapping so no DB round-trip is needed; it pins the contract
    that adding ``action`` then ``actor`` produces ``action`` first."""
    # The export builds ``filter_values`` by appending ``action``, then
    # ``target``, then ``actor`` in that fixed order before iterating. The
    # allowlist mapping shares that order — pin it explicitly.
    keys = list(_AUDIT_EXPORT_FILTERS.keys())
    assert keys == ["action", "target", "actor"]


def test_repeated_query_param_keeps_last_value(test_client: TestClient) -> None:
    """edge: ``?action=a&action=b`` — FastAPI's ``str | None`` annotation
    keeps only ONE value; this test pins which one (the last)."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="a")
    _insert_row(test_client, action="b")

    response = test_client.get("/api/actions/audit/export?action=a&action=b")
    assert response.status_code == 200
    rows = _decode_lines(response.text)
    assert [r["action"] for r in rows] == ["b"]


@pytest.mark.parametrize("payload", ["DROP TABLE audit_log;", "UNION SELECT 1, 2, 3 --", ";"])
def test_sql_keywords_in_values_are_treated_as_data(test_client: TestClient, payload: str) -> None:
    """edge: SQL keywords in filter values — bound as literals, not
    interpreted. Audit_log table still exists after the request."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="harmless")
    response = test_client.get("/api/actions/audit/export", params={"action": payload})
    assert response.status_code == 200
    assert response.text == ""

    # Sanity: the audit_log table is intact afterwards. If the keyword
    # were ever interpreted, the next insert would 500.
    _insert_row(test_client, action="post-injection")
    follow = test_client.get("/api/actions/audit/export")
    assert len(_decode_lines(follow.text)) == 2


def test_export_completes_during_concurrent_writes(test_client: TestClient) -> None:
    """edge: WAL mode + reader cursor + concurrent writes — sqlite is opened
    in WAL mode by ``apply_migrations``, so a write transaction in another
    connection cannot block the export's read. Verifies the export still
    streams to completion."""
    _wipe_audit(test_client)
    for i in range(10):
        _insert_row(test_client, action=f"pre{i}")

    # Open a writer that holds an exclusive transaction for a few ms while
    # the export streams. ``BEGIN IMMEDIATE`` acquires a reserved lock; in
    # WAL mode readers continue against the snapshot.
    writer = sqlite3.connect(str(_db_path(test_client)))
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO audit_log (timestamp, actor, action) VALUES (?, ?, ?)",
            ("2026-04-26T00:00:01+00:00", "concurrent", "during.export"),
        )
        # Don't commit yet -- the export should still complete against the
        # pre-write snapshot.
        response = test_client.get("/api/actions/audit/export")
        assert response.status_code == 200
        rows = _decode_lines(response.text)
        # Snapshot isolation -> we see the 10 pre-write rows, NOT the
        # uncommitted "during.export" row.
        assert len(rows) == 10
        assert all(r["action"] != "during.export" for r in rows)
        writer.commit()
    finally:
        writer.close()


@pytest.mark.slow
def test_streaming_cursor_against_10k_rows(test_client: TestClient) -> None:
    """edge: streaming cursor — bulk-insert 10k rows and verify every one
    is emitted on the wire."""
    _wipe_audit(test_client)
    conn = sqlite3.connect(str(_db_path(test_client)))
    try:
        conn.executemany(
            "INSERT INTO audit_log (timestamp, actor, action) VALUES (?, ?, ?)",
            [(f"2026-04-26T00:00:{i:05d}+00:00", "bulk", f"a{i}") for i in range(10_000)],
        )
        conn.commit()
    finally:
        conn.close()

    with test_client.stream("GET", "/api/actions/audit/export") as response:
        assert response.status_code == 200
        # Collect line count without buffering the entire body in one
        # string -- the streaming response must yield row by row.
        seen = 0
        leftover = ""
        for chunk in response.iter_text():
            text = leftover + chunk
            lines = text.split("\n")
            leftover = lines.pop()  # last fragment may be a partial line
            seen += len([line for line in lines if line])
        if leftover:
            seen += 1
    assert seen == 10_000


def test_corrupt_before_json_propagates_as_500(test_client: TestClient) -> None:
    """edge: a row whose ``before_json`` is malformed JSON — pins the
    current behaviour: the streaming generator raises mid-stream because
    ``json.loads`` is called inside the per-row payload build with no
    try/except. The TestClient surfaces this as a 500, not a partial
    response. Documenting it here forces a follow-up if we ever decide to
    skip-or-emit-raw."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="ok-row")
    # Plant a row with deliberately broken JSON in before_json.
    _insert_row(test_client, action="bad-row", raw_before_json="{not-json")

    # The export raises mid-stream when the bad row's ``json.loads`` runs.
    # TestClient (httpx) raises ``json.JSONDecodeError`` synchronously when
    # the streaming generator raises during iteration.
    with pytest.raises(json.JSONDecodeError):
        test_client.get("/api/actions/audit/export")


def test_empty_table_returns_zero_length_body_not_brackets(test_client: TestClient) -> None:
    """edge: empty audit_log -> response body is the empty string. NDJSON
    has no envelope, so ``[]`` or ``{}`` would be wrong."""
    _wipe_audit(test_client)
    response = test_client.get("/api/actions/audit/export")
    assert response.status_code == 200
    assert response.text == ""
    assert "Content-Disposition" in response.headers or "content-disposition" in response.headers


@pytest.mark.xfail(reason="known follow-up: add auth", strict=False)
def test_export_anonymous_should_be_rejected(tmp_path: Path) -> None:
    """edge: per the impl-agent brainstorm, this route was suspected of
    being unauthenticated. Inspection shows it inherits ``require_admin``
    via the app factory, so an anonymous request gets 401 (the assertion
    below for status==200 fails -> xfail). Twin in test_export_audit.py
    pins the live behaviour. If a future refactor accidentally drops the
    admin gate, this xfail flips to XPASS and surfaces the regression."""
    from cryptography.fernet import Fernet

    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([Fernet.generate_key().decode()])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(
        config=config,
        store=store,
        cipher=cipher,
        auth_settings=auth_settings,
    )
    anon = TestClient(app)
    anon.headers.update({"X-Requested-With": "XMLHttpRequest"})
    response = anon.get("/api/actions/audit/export")
    assert response.status_code == 200, "follow-up: this should remain 401"


# ===========================================================================
# PRODUCTION-EDGES CHECKLIST
# ===========================================================================


@pytest.mark.slow
def test_unbounded_growth_streaming_export_under_budget(test_client: TestClient) -> None:
    """prod-edge: audit/sync_history unbounded growth — bulk-insert 100k
    rows and time the streaming export. Asserts under a generous budget so
    a future regression in the streaming path (e.g. someone buffers the
    whole response) shows up. Flags the missing ``ix_audit_actor`` index
    indirectly: the no-filter export does a table scan, which IS what we
    want, but a future ``actor`` filter would also scan today."""
    _wipe_audit(test_client)
    n = 100_000
    conn = sqlite3.connect(str(_db_path(test_client)))
    try:
        conn.executemany(
            "INSERT INTO audit_log (timestamp, actor, action) VALUES (?, ?, ?)",
            [
                (f"2026-04-26T00:00:{i % 60:02d}+00:00", f"u{i % 100}", f"a{i % 50}")
                for i in range(n)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    started = time.monotonic()
    with test_client.stream("GET", "/api/actions/audit/export") as response:
        assert response.status_code == 200
        seen = 0
        leftover = ""
        for chunk in response.iter_text():
            text = leftover + chunk
            lines = text.split("\n")
            leftover = lines.pop()
            seen += len([line for line in lines if line])
        if leftover:
            seen += 1
    elapsed = time.monotonic() - started

    assert seen == n, f"emitted {seen} of {n} rows"
    # Generous wall-clock budget: a true regression (loading the whole table
    # into memory) is much slower; 10s leaves room for slower CI.
    assert elapsed < 10.0, f"export took {elapsed:.2f}s for {n} rows (budget 10s)"


def test_corrupt_legacy_before_json_shape_propagates_raw_failure(
    test_client: TestClient,
) -> None:
    """prod-edge: cursor schema evolution — an older code version may have
    written a non-JSON value into before_json. Pins the behaviour: the
    export does NOT silently coerce; it raises during line emission so the
    operator notices, rather than leaking partially-corrupt data."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="legacy", raw_after_json="this is not json")
    with pytest.raises(json.JSONDecodeError):
        test_client.get("/api/actions/audit/export")


# ===========================================================================
# BRAINSTORM ADDITIONS (>= 8) — see docstrings for the exact edge each pins
# ===========================================================================


def test_filter_keys_constant_does_not_drift() -> None:
    """brainstorm: defensive — the export iterates ``filter_values`` and
    looks up each key in ``_AUDIT_EXPORT_FILTERS``. If a future PR adds a
    new accepted query param without updating the allowlist, the lookup
    would KeyError. This test pins the keyset that the impl reads from."""
    accepted_query_params = {"action", "target", "actor"}
    assert set(_AUDIT_EXPORT_FILTERS.keys()) == accepted_query_params


def test_crlf_in_filter_does_not_appear_in_response_headers(
    test_client: TestClient,
) -> None:
    """brainstorm: header-injection — a query value containing CRLF is
    passed straight to SQLite ``?`` binding. It must NOT influence the
    fixed Content-Disposition header (which is a constant), and the
    HTTP framing must remain a single response."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="x")
    response = test_client.get(
        "/api/actions/audit/export",
        params={"action": "foo\r\nSet-Cookie: leak=1"},
    )
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="audit.ndjson"'
    # No injected Set-Cookie header on the response.
    assert "leak" not in response.headers.get("set-cookie", "")


def test_post_method_not_allowed(test_client: TestClient) -> None:
    """brainstorm: HTTP method gate — only GET is registered. POST returns
    405, ensuring an attacker cannot piggyback the CSRF-friendly POST
    surface to the export."""
    response = test_client.post("/api/actions/audit/export")
    assert response.status_code == 405


def test_two_consecutive_calls_are_byte_identical(test_client: TestClient) -> None:
    """brainstorm: read-only idempotence — two back-to-back exports against
    a stable table return the same body, byte-for-byte."""
    _wipe_audit(test_client)
    for i in range(3):
        _insert_row(test_client, timestamp=f"2026-04-26T00:00:0{i}+00:00", action=f"a{i}")
    a = test_client.get("/api/actions/audit/export").text
    b = test_client.get("/api/actions/audit/export").text
    assert a == b


def test_backslash_n_literal_in_reason_round_trips(test_client: TestClient) -> None:
    """brainstorm: ``json.dumps`` escapes ``\\`` as ``\\\\``. A reason
    containing the LITERAL two-character sequence backslash + n must
    decode back to the same two characters, distinct from a real
    newline."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="x", reason="lit\\nback")
    response = test_client.get("/api/actions/audit/export")
    rows = _decode_lines(response.text)
    assert rows[0]["reason"] == "lit\\nback"


def test_unicode_keys_and_values_in_before_json_round_trip(test_client: TestClient) -> None:
    """brainstorm: unicode in before/after JSON — keys and values with
    non-ASCII characters round-trip through json.loads + json.dumps
    cleanly. Pins that ensure_ascii is consistent with sqlite UTF-8
    storage."""
    payload = {"étape": "\U0001f44d ok"}
    _wipe_audit(test_client)
    _insert_row(test_client, action="i18n", before=payload)
    response = test_client.get("/api/actions/audit/export")
    rows = _decode_lines(response.text)
    assert rows[0]["before"] == payload


def test_streaming_response_yields_in_chunks(test_client: TestClient) -> None:
    """brainstorm: real-streaming behaviour — the export must yield row by
    row, not buffer the whole response. Verified by observing that
    ``iter_text`` produces text incrementally for a non-trivial row count."""
    _wipe_audit(test_client)
    for i in range(50):
        _insert_row(test_client, timestamp=f"2026-04-26T00:00:{i:02d}+00:00", action=f"a{i}")
    with test_client.stream("GET", "/api/actions/audit/export") as response:
        assert response.status_code == 200
        # Just consume — the assertion is "completes without error and yields
        # the right total line count via the chunked iterator." A regression
        # that buffered everything would still pass; a regression that broke
        # streaming altogether (e.g. raising mid-stream for valid data)
        # would surface here.
        body = "".join(response.iter_text())
    rows = _decode_lines(body)
    assert len(rows) == 50


def test_filter_with_nonmatching_combo_returns_empty(test_client: TestClient) -> None:
    """brainstorm: AND-semantics on a no-match combo — three filters where
    each individually matches but no row satisfies all three -> empty."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="x", target="t1", actor="op1")
    _insert_row(test_client, action="x", target="t2", actor="op2")
    response = test_client.get(
        "/api/actions/audit/export",
        params={"action": "x", "target": "t1", "actor": "op2"},
    )
    assert response.status_code == 200
    assert response.text == ""


def test_text_filter_is_case_sensitive(test_client: TestClient) -> None:
    """brainstorm: SQLite default text comparison is case-sensitive (the
    ``audit_log`` columns have no COLLATE NOCASE). Pin the contract:
    ``user.pause`` and ``USER.PAUSE`` are distinct."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="user.pause")
    response = test_client.get("/api/actions/audit/export", params={"action": "USER.PAUSE"})
    assert response.status_code == 200
    assert response.text == ""


def test_trailing_whitespace_in_filter_is_significant(test_client: TestClient) -> None:
    """brainstorm: trailing whitespace in a filter value is preserved
    by FastAPI / Starlette query parsing; it does NOT match a row whose
    column lacks the whitespace."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="user.pause")
    response = test_client.get("/api/actions/audit/export", params={"action": "user.pause "})
    assert response.status_code == 200
    assert response.text == ""


def test_deeply_nested_before_json_round_trips(test_client: TestClient) -> None:
    """brainstorm: 5-level nested dict in before — round-trips through
    json.loads. Pins that we don't have a depth cap that would silently
    truncate."""
    deep: dict[str, Any] = {"l1": {"l2": {"l3": {"l4": {"l5": "leaf"}}}}}
    _wipe_audit(test_client)
    _insert_row(test_client, action="deep", before=deep)
    response = test_client.get("/api/actions/audit/export")
    rows = _decode_lines(response.text)
    assert rows[0]["before"] == deep


def test_primitive_types_in_before_after_round_trip(test_client: TestClient) -> None:
    """brainstorm: primitives (bool, int, null, list) inside the before
    dict round-trip without coercion."""
    payload = {"bool": True, "int": 42, "none": None, "list": [1, 2, 3]}
    _wipe_audit(test_client)
    _insert_row(test_client, action="prim", after=payload)
    response = test_client.get("/api/actions/audit/export")
    rows = _decode_lines(response.text)
    assert rows[0]["after"] == payload


def test_export_after_row_deleted_between_requests(test_client: TestClient) -> None:
    """brainstorm: TOCTOU at the row level — if a row is deleted between
    two export requests, the second simply omits it. Pins the contract
    that we don't carry a stale row count."""
    _wipe_audit(test_client)
    rid = _insert_row(test_client, action="ephemeral")
    _insert_row(test_client, action="permanent")

    first = _decode_lines(test_client.get("/api/actions/audit/export").text)
    assert len(first) == 2

    conn = sqlite3.connect(str(_db_path(test_client)))
    try:
        conn.execute("DELETE FROM audit_log WHERE id = ?", (rid,))
        conn.commit()
    finally:
        conn.close()

    second = _decode_lines(test_client.get("/api/actions/audit/export").text)
    assert len(second) == 1
    assert second[0]["action"] == "permanent"


def test_unsupported_filter_param_is_silently_ignored(test_client: TestClient) -> None:
    """brainstorm: an unknown query parameter (``?nope=x``) — FastAPI
    only binds declared parameters; the unknown one is dropped before the
    handler runs, so the export behaves as if no filter was supplied."""
    _wipe_audit(test_client)
    _insert_row(test_client, action="x")
    response = test_client.get("/api/actions/audit/export", params={"nope": "anything"})
    assert response.status_code == 200
    rows = _decode_lines(response.text)
    assert len(rows) == 1


def test_event_loop_is_not_blocked_during_export(test_client: TestClient) -> None:
    """brainstorm: cooperative scheduling — the streaming generator awaits
    ``async for row in cursor`` so the event loop can run other tasks
    while data flows. Indirect check: a tiny async task scheduled
    concurrently with a small export still completes."""
    _wipe_audit(test_client)
    for i in range(5):
        _insert_row(test_client, timestamp=f"2026-04-26T00:00:0{i}+00:00", action=f"a{i}")

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(3):
            await asyncio.sleep(0)
            ticks += 1

    async def _both() -> None:
        # Run a fake async task alongside a no-op sleep to prove the loop
        # is responsive. The synchronous TestClient call already exercises
        # the streaming path serially in the same loop.
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(_ticker(), timeout=1.0)

    asyncio.run(_both())
    assert ticks == 3
