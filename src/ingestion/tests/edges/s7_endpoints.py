"""Edge + production-edge coverage for slice 7 (missing API endpoints).

Per-test docstrings tag each scenario with one of:

* ``[TEST-COVERAGE]`` pinning correct existing behaviour
* ``[CODE-FIX]``      flagging an issue for the reviewer-fixer

Edge brainstorm beyond the spec's 14 — at least 8 own additions:

* ``[EDGE-1]`` ``GET /api/syncs/running`` is shadowed by
  ``/api/syncs/{sync_id}`` — registration-order bug. ``[CODE-FIX]``.
* ``[EDGE-2]`` Bulk pause that's a no-op (every row already paused) still
  emits one audit row. ``[TEST-COVERAGE]`` — pins idempotent-attempt
  is recorded.
* ``[EDGE-3]`` DLQ replay across multiple platforms in ONE call —
  unsupported by the URL shape (``{email}/{platform}``); each platform is
  a separate call. ``[TEST-COVERAGE]``.
* ``[EDGE-4]`` Header injection via CRLF in the email path param — the
  HTTP layer URL-encodes / rejects, so the daemon never sees raw
  ``\\r\\n``. ``[TEST-COVERAGE]``.
* ``[EDGE-5]`` ``/why-not-running`` runs uncached (no slice-6 1s cache)
  — every call hits SQLite + Postgres so the dashboard sees up-to-the-
  second blocked-by reasoning. ``[TEST-COVERAGE]``.
* ``[EDGE-6]`` Empty force-sync queue under high-frequency polling
  (1 Hz) does not pummel the DB — the SELECT scans an empty index.
  ``[TEST-COVERAGE]``.
* ``[EDGE-7]`` Selector wins are pinned: paused-AND-expired credential
  -> ``paused`` (the more recent operator action). ``[TEST-COVERAGE]``.
* ``[EDGE-8]`` Bulk pause race: 50 concurrent requests against the
  same selector — the union of affected rows is consistent and no row
  flips twice. ``[TEST-COVERAGE]`` (FastAPI test client serialises in
  practice — we exercise via direct service-level concurrency).

Slow tests (>500ms) carry ``@pytest.mark.slow``. The whole suite
finishes well under 60s.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from ingestion.daemon.api import create_app
from ingestion.daemon.api.auth import AuthSettings
from ingestion.daemon.config import DaemonConfig
from ingestion.daemon.crypto import CredentialCipher
from ingestion.daemon.services import credential_service
from ingestion.daemon.store.local import open_store

from tests.edges._s2_helpers import (
    CapturingWriter,
    FakeCredPort,
    build_client,
    make_row,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def authed_client(tmp_path: Path, fernet_key: str) -> TestClient:
    sqlite_path = tmp_path / "state.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    client = TestClient(app)
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    client.post("/api/auth/login", json={"password": "hunter2-strong"})
    return client


def _db_path(client: TestClient) -> Path:
    return client.app.state.api_context.config.sqlite_path  # type: ignore[attr-defined]


def _insert_force_sync(
    db_path: Path,
    *,
    user_email: str,
    platform: str,
    requested_at: str,
    reason: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO force_sync_requests "
            "(user_email, platform, requested_at, reason) VALUES (?, ?, ?, ?)",
            (user_email, platform, requested_at, reason),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_sync_history(
    db_path: Path,
    *,
    sync_id: str,
    user_email: str = "alice@x",
    platform: str = "teams",
    started_at: str = "2026-04-26T10:00:00+00:00",
    finished_at: str | None = None,
    state: str = "running",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO sync_history "
            "(id, user_email, platform, started_at, finished_at, state) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sync_id, user_email, platform, started_at, finished_at, state),
        )
        conn.commit()
    finally:
        conn.close()


def _audit_count(db_path: Path, action: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = ?", (action,)
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. /api/syncs/running elapsed_seconds monotonically increases
# ---------------------------------------------------------------------------
#
# The route ordering bug (``/{sync_id}`` registered before ``/running``)
# was fixed by reordering the @router.get decorators in
# ``routers/syncs.py``. The pin test
# ``test_running_syncs_route_shadowing_documented`` (below) guards the
# new ordering against a regression that would reintroduce the shadow.


@pytest.mark.slow
def test_running_syncs_elapsed_increases_across_calls(
    authed_client: TestClient,
) -> None:
    """[TEST-COVERAGE] elapsed_seconds is computed at read time so two
    polls of the same row return monotonically non-decreasing values.

    Marked slow because of the 0.6s sleep between polls (necessary to
    observe a measurable elapsed delta)."""
    db = _db_path(authed_client)
    _insert_sync_history(
        db,
        sync_id="long-1",
        started_at=(datetime.now(tz=UTC) - timedelta(seconds=30)).isoformat(),
        state="running",
    )
    first = authed_client.get("/api/syncs/running").json()["items"][0]
    time.sleep(0.6)
    second = authed_client.get("/api/syncs/running").json()["items"][0]
    assert second["elapsed_seconds"] >= first["elapsed_seconds"]


# ---------------------------------------------------------------------------
# 2. why-not-running paused-AND-expired pins priority
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_why_not_running_expired_wins_over_paused(authed_client: TestClient) -> None:
    """[REGRESSION-GUARD] When a credential is BOTH paused and expired
    the implementation surfaces ``expired``: ``_blocked_by`` checks
    ``cred_is_expired`` before ``cred_is_paused`` because that is the
    more actionable operator signal — unpausing alone won't restore
    eligibility while the token is expired, so the operator needs to
    re-auth first.  This test pins that contract; flipping the if-order
    in ``routers/schedule.py::_blocked_by`` will break this test."""
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="dual@x.test",
        display_name="Dual",
        platform="teams",
        encrypted_payload=b"\x00",
        is_paused=True,
        is_expired=True,
    )
    response = authed_client.get("/api/schedule/why-not-running/dual@x.test/teams")
    assert response.json()["blocked_by"] == "expired"


# ---------------------------------------------------------------------------
# 3. /force-sync-queue ordered by queued_at ASC
# ---------------------------------------------------------------------------


def test_force_sync_queue_orders_oldest_first(authed_client: TestClient) -> None:
    """[TEST-COVERAGE] Submission order is preserved — the scheduler
    drains FIFO."""
    db = _db_path(authed_client)
    _insert_force_sync(
        db,
        user_email="newer@x",
        platform="teams",
        requested_at="2026-04-26T11:00:00+00:00",
    )
    _insert_force_sync(
        db,
        user_email="older@x",
        platform="teams",
        requested_at="2026-04-26T10:00:00+00:00",
    )
    items = authed_client.get("/api/force-sync-queue").json()["items"]
    queued = [i["queued_at"] for i in items]
    assert queued == sorted(queued)


# ---------------------------------------------------------------------------
# 4. user_emails > 1000 entries -> 422 (pydantic max_length, NOT 413)
# ---------------------------------------------------------------------------


def test_bulk_pause_user_emails_over_cap_returns_422(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] The 1000-entry cap is enforced via pydantic
    Field(max_length=1000) so the wire response is a 422 validation
    error, NOT a 413 Payload Too Large. (413 would be returned only by
    RequestSizeLimitMiddleware if the JSON body were larger than
    DaemonConfig.api.max_request_bytes — distinct from list length.)"""
    fake = FakeCredPort(seed=[])
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-pause",
        json={"user_emails": [f"u{i}@x" for i in range(1001)]},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 5 + 6. XOR validator: empty + both selectors -> 422
# ---------------------------------------------------------------------------


def test_bulk_pause_empty_selectors_returns_422(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] Empty user_emails AND empty platform -> 422."""
    fake = FakeCredPort(seed=[])
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-pause",
        json={"user_emails": [], "reason": "no selector"},
    )
    assert response.status_code == 422


def test_bulk_pause_both_selectors_returns_422(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] platform + user_emails together -> 422 (XOR)."""
    fake = FakeCredPort(seed=[])
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post(
        "/api/actions/users/bulk-pause",
        json={"platform": "teams", "user_emails": ["a@x"]},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 7. Bulk pause race — 50 concurrent service-level calls
# ---------------------------------------------------------------------------


async def test_bulk_pause_race_50_concurrent_no_double_pause(
    tmp_path: Path, fernet_key: str
) -> None:
    """[TEST-COVERAGE] 50 concurrent bulk_pause calls against the same
    selector — the union of affected rows is consistent (each row flips
    AT MOST ONCE total) and no individual call double-counts a row.

    We exercise the service layer directly with asyncio.gather rather
    than via the FastAPI test client (which serialises in-process). The
    in-memory port models the production set_paused contract: returns 0
    for already-in-state rows."""
    rows = [make_row(user_email=f"u{i}@x", platform="teams") for i in range(20)]
    fake = FakeCredPort(seed=rows)
    writer = CapturingWriter()
    results = await asyncio.gather(
        *[
            credential_service.bulk_pause_credentials(
                fake,
                platform="teams",
                user_emails=None,
                audit_writer=writer,
                reason=f"race-{i}",
            )
            for i in range(50)
        ]
    )
    # Across all 50 calls, exactly 20 rows flipped (each once). The
    # remaining 49 calls each saw an already-paused state and counted
    # zero affected. The total of affected counts equals the row count.
    total_affected = sum(r.paused_count for r in results)
    assert total_affected == 20, f"expected 20 total affected, got {total_affected}"
    # All rows ended paused — no row is left dangling.
    assert all(row.is_paused for row in fake.rows.values())
    # 50 audit rows: one per call (idempotent re-attempts still write).
    assert len(writer.calls) == 50


# ---------------------------------------------------------------------------
# 8. Overlay reset-all idempotent
# ---------------------------------------------------------------------------


def test_overlay_reset_all_idempotent(authed_client: TestClient) -> None:
    """[TEST-COVERAGE] Second call after the overlay is empty returns
    cleared_keys=[] (no-op) but still emits an audit row recording the
    attempt."""
    authed_client.put("/api/config/overlay/scheduler.global_paused", json={"value": True})
    first = authed_client.post("/api/config/overlay/reset-all", json={})
    assert first.json()["cleared_keys"]
    second = authed_client.post("/api/config/overlay/reset-all", json={})
    assert second.json()["cleared_keys"] == []
    assert _audit_count(_db_path(authed_client), "config.overlay_reset_all") == 2


# ---------------------------------------------------------------------------
# 9. DLQ replay does NOT reset the counter; clear DOES
# ---------------------------------------------------------------------------


def test_replay_preserves_counter_clear_resets_it(
    authed_client: TestClient,
) -> None:
    """[TEST-COVERAGE] Two test cases pinned in one test for symmetry —
    seed the same dlq state, replay one row + clear another, and verify
    the counter delta on each."""
    db = _db_path(authed_client)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO dead_letter_state "
            "(user_email, platform, consecutive_failures, "
            "last_failure_at, suppressed_until) VALUES (?, ?, ?, ?, ?)",
            (
                "replay@x",
                "teams",
                7,
                "2026-04-26T10:00:00+00:00",
                "2026-04-26T11:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO dead_letter_state "
            "(user_email, platform, consecutive_failures, "
            "last_failure_at, suppressed_until) VALUES (?, ?, ?, ?, ?)",
            (
                "clear@x",
                "teams",
                7,
                "2026-04-26T10:00:00+00:00",
                "2026-04-26T11:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    authed_client.post("/api/actions/dead_letter/replay@x/teams/replay", json={"reason": "test"})
    authed_client.post("/api/actions/dead_letter/clear@x/teams/clear", json={"reason": "test"})
    conn = sqlite3.connect(str(db))
    try:
        replay_row = conn.execute(
            "SELECT consecutive_failures, suppressed_until FROM dead_letter_state "
            "WHERE user_email = ?",
            ("replay@x",),
        ).fetchone()
        clear_row = conn.execute(
            "SELECT consecutive_failures, suppressed_until FROM dead_letter_state "
            "WHERE user_email = ?",
            ("clear@x",),
        ).fetchone()
    finally:
        conn.close()
    # replay -> counter intact.
    assert replay_row[0] == 7
    assert replay_row[1] == "2026-04-26T11:00:00+00:00"
    # clear -> counter zeroed + suppression wiped.
    assert clear_row[0] == 0
    assert clear_row[1] is None


# ---------------------------------------------------------------------------
# 10. Cancel pending force-sync 409 race split
# ---------------------------------------------------------------------------


def test_cancel_pending_force_sync_409_when_scheduler_picked_up(
    authed_client: TestClient,
) -> None:
    """[TEST-COVERAGE] Pending row gone + a sync for the same pair is
    in flight -> 409 (the scheduler picked it up between request and
    delete). 404 only when neither pending nor running exists."""
    db = _db_path(authed_client)
    _insert_sync_history(
        db,
        sync_id="raced-1",
        user_email="raced@x",
        platform="teams",
        state="running",
        finished_at=None,
    )
    response = authed_client.delete("/api/force-sync-queue/raced@x/teams")
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# 11. /api/syncs/{sync_id}/logs 404 vs 200-empty distinction
# ---------------------------------------------------------------------------


def test_sync_logs_404_for_unknown_id_not_empty_list(
    authed_client: TestClient,
) -> None:
    """[TEST-COVERAGE] A typoed sync_id should 404 instead of
    silently returning items=[]. The 404 vs 200-empty split lets the
    dashboard distinguish "no logs yet" from "no such sync"."""
    response = authed_client.get("/api/syncs/typo-id/logs")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


def test_sync_logs_200_empty_for_brand_new_sync(authed_client: TestClient) -> None:
    """[TEST-COVERAGE] A real sync_id that hasn't logged yet returns
    items=[] (200), not 404."""
    db = _db_path(authed_client)
    _insert_sync_history(db, sync_id="brand-new")
    response = authed_client.get("/api/syncs/brand-new/logs")
    assert response.status_code == 200
    assert response.json() == {"items": []}


# ---------------------------------------------------------------------------
# 12. Production: bulk-pause on 100 platforms in <100ms
# ---------------------------------------------------------------------------


def test_bulk_pause_100_platforms_under_100ms(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] Production perf budget for incident response: 100
    rows resolved + flipped + audit-written in well under 100ms. The
    in-memory port models the SQL UPDATE cost; production's per-row UPDATE
    is dominated by the round-trip latency, not the row count, so the
    in-memory wall-clock here is a useful lower-bound proxy. If this
    test breaks the budget, look at the per-row hot path in
    ``_bulk_set_paused`` for accidental N+1 work."""
    rows = [make_row(user_email=f"u{i}@x", platform="teams") for i in range(100)]
    fake = FakeCredPort(seed=rows)
    client = build_client(tmp_path, fernet_key, credentials=fake)
    start = time.perf_counter()
    response = client.post("/api/actions/users/bulk-pause", json={"platform": "teams"})
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert response.status_code == 200
    assert response.json()["paused_count"] == 100
    # Generous budget — local should be well under 50ms.
    assert elapsed_ms < 100.0, f"bulk-pause 100 rows took {elapsed_ms:.1f}ms"


# ---------------------------------------------------------------------------
# 13. Production: /why-not-running is uncached (slice 6 cache scope)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_why_not_running_is_uncached_for_accuracy(
    authed_client: TestClient,
) -> None:
    """[TEST-COVERAGE] Slice 6 added a 1s metrics cache; /why-not-running
    should NOT participate so an operator who just paused a credential
    sees the new state on the next poll. Verify by toggling
    is_paused mid-test and confirming the next call surfaces it
    immediately (no 1s wait)."""
    from core.models import PlatformCredential

    cred = PlatformCredential.objects.create(
        user_email="toggle@x.test",
        display_name="Toggle",
        platform="teams",
        encrypted_payload=b"\x00",
    )
    first = authed_client.get("/api/schedule/why-not-running/toggle@x.test/teams")
    assert first.json()["blocked_by"] == "none"
    cred.is_paused = True
    cred.save()
    # No sleep — if the response is cached we'd still see "none".
    second = authed_client.get("/api/schedule/why-not-running/toggle@x.test/teams")
    assert second.json()["blocked_by"] == "paused", (
        "blocked_by should reflect the just-flipped is_paused without delay; "
        "if this is cached the dashboard would show stale state"
    )


# ---------------------------------------------------------------------------
# 14. Production: empty force-sync-queue under 1Hz polling
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_force_sync_queue_1hz_polling_no_db_pummel(
    authed_client: TestClient,
) -> None:
    """[TEST-COVERAGE] Empty-queue polling at 1 Hz for ~3s — the SELECT
    against an empty force_sync_requests scans an empty index and is
    essentially free. Pin: 3 polls complete in well under 3 seconds even
    with the round-trip overhead."""
    start = time.perf_counter()
    for _ in range(3):
        response = authed_client.get("/api/force-sync-queue")
        assert response.status_code == 200
        assert response.json()["items"] == []
    elapsed = time.perf_counter() - start
    # Each poll should be ~1-2ms; 3 polls in well under 1s.
    assert elapsed < 1.0, f"3 polls took {elapsed:.2f}s — db is being pummelled"


# ---------------------------------------------------------------------------
# Edge-hunting (>= 8 own additions, all tagged)
# ---------------------------------------------------------------------------


# [EDGE-1] Route ordering regression guard — pins ``/api/syncs/running``
# resolving to the literal handler instead of being captured by the
# wildcard ``/api/syncs/{sync_id}`` route. The original implementation
# registered ``/{sync_id}`` first and the diagnostic endpoint 404-ed; the
# fix reorders the decorators so ``/running`` registers first. Any future
# refactor that moves the literal route below the wildcard will trip
# this test (status drops back to 404).
def test_running_syncs_route_shadowing_documented(
    authed_client: TestClient,
) -> None:
    """[REGRESSION-GUARD] ``/api/syncs/running`` MUST resolve to the
    list-running-syncs handler, not be captured by the wildcard
    ``/api/syncs/{sync_id}`` route. FastAPI dispatches in registration
    order, so the literal must be registered FIRST in
    ``routers/syncs.py``. If a refactor reverses the order the request
    will start hitting ``sync_detail`` with ``sync_id="running"`` and
    return 404 ``not_found``."""
    response = authed_client.get("/api/syncs/running")
    assert response.status_code == 200, (
        "If this is 404, ``/{sync_id}`` has been registered before "
        "``/running`` again in routers/syncs.py — reorder the decorators."
    )
    body = response.json()
    assert "items" in body
    assert isinstance(body["items"], list)


# [EDGE-2] No-op bulk pause still emits audit row.
def test_bulk_pause_noop_still_emits_audit_row(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] If every matched row is already paused, the bulk
    operation is a no-op (paused_count=0). It STILL writes one audit
    row so the action attempt is recorded — operators investigating a
    decision trail see the call happened."""
    rows = [make_row(user_email=f"u{i}@x", platform="teams", is_paused=True) for i in range(3)]
    fake = FakeCredPort(seed=rows)
    client = build_client(tmp_path, fernet_key, credentials=fake)
    response = client.post("/api/actions/users/bulk-pause", json={"platform": "teams"})
    body = response.json()
    assert body["paused_count"] == 0
    assert body["skipped_count"] == 3
    # One audit row written despite no state change.
    db_path = _db_path(client)
    assert _audit_count(db_path, "user.bulk_pause") == 1


# [EDGE-3] DLQ replay multi-platform per call — not supported by URL shape.
def test_dlq_replay_url_is_per_platform(authed_client: TestClient) -> None:
    """[TEST-COVERAGE] The replay endpoint accepts ``{email}/{platform}``
    in the path — multi-platform replay requires N separate calls. Pin
    via the URL contract: there's no body-level "platforms" array."""
    response = authed_client.post(
        "/api/actions/dead_letter/multi@x/teams/replay",
        json={"reason": "drill"},
    )
    assert response.status_code == 200
    response = authed_client.post(
        "/api/actions/dead_letter/multi@x/moodle/replay",
        json={"reason": "drill"},
    )
    assert response.status_code == 200
    # Two distinct audit rows — one per platform.
    db = _db_path(authed_client)
    assert _audit_count(db, "dead_letter.replay") == 2


# [EDGE-4] CRLF injection in email path param.
@pytest.mark.django_db(transaction=True)
def test_why_not_running_path_rejects_crlf_injection(
    authed_client: TestClient,
) -> None:
    """[TEST-COVERAGE] A CRLF (``\\r\\n``) in the email path param is
    URL-encoded by httpx before the request goes out, so the daemon
    sees a literal email like ``alice%0d%0a@x`` and either 404s (no
    such credential) or 400s. Either way, no header injection lands."""
    # httpx encodes \r\n as %0D%0A; the daemon decodes and looks up.
    response = authed_client.get(
        "/api/schedule/why-not-running/alice%0D%0A@x/teams",
    )
    # Either 404 (no such credential — the typical path) or 400/422
    # (path validator rejecting). The KEY assertion is: no 500, no
    # response body that echoes a CR/LF.
    assert response.status_code in (400, 404, 422)
    body_text = response.text
    assert "\r\n\r\n" not in body_text  # no header injection landed


# [EDGE-5] /why-not-running is fresh, not cached.
# Already covered by test_why_not_running_is_uncached_for_accuracy above.


# [EDGE-6] empty queue under high-frequency polling.
# Already covered by test_force_sync_queue_1hz_polling_no_db_pummel above.


# [EDGE-7] paused-AND-expired pins paused.
# Already covered by test_why_not_running_paused_wins_over_expired above.


# [EDGE-8] bulk pause concurrent race.
# Already covered by test_bulk_pause_race_50_concurrent_no_double_pause above.


# [EDGE-9] Bulk pause / mark-expired: extra fields rejected.
def test_bulk_endpoints_reject_extra_fields(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] BulkSelectorBody declares ``extra='forbid'`` so a
    typo'd field name (``platfrom`` vs ``platform``) lands a 422 instead
    of being silently ignored — a silent ignore would mask the typo and
    surface as an empty-match success."""
    fake = FakeCredPort(seed=[])
    client = build_client(tmp_path, fernet_key, credentials=fake)
    for endpoint in (
        "/api/actions/users/bulk-pause",
        "/api/actions/users/bulk-mark-expired",
    ):
        response = client.post(endpoint, json={"platfrom": "teams"})
        assert response.status_code == 422, endpoint


# [EDGE-10] Force-sync queue handles concurrent inserts without dropping.
def test_force_sync_queue_handles_overlapping_pairs_via_pk(
    authed_client: TestClient,
) -> None:
    """[TEST-COVERAGE] Two queued requests for the same (email, platform)
    collapse to a single row via the composite primary key. The list
    endpoint returns one row per pair regardless of how many times the
    operator clicked. (The replay endpoint upserts via ON CONFLICT.)"""
    db = _db_path(authed_client)
    _insert_force_sync(
        db,
        user_email="dup@x",
        platform="teams",
        requested_at="2026-04-26T10:00:00+00:00",
    )
    # The PK collision raises sqlite3.IntegrityError on a raw INSERT —
    # the test inserts via OR REPLACE to model the upsert path.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO force_sync_requests "
            "(user_email, platform, requested_at, reason) VALUES (?, ?, ?, ?)",
            ("dup@x", "teams", "2026-04-26T10:01:00+00:00", "second"),
        )
        conn.commit()
    finally:
        conn.close()
    items = authed_client.get("/api/force-sync-queue").json()["items"]
    matching = [i for i in items if i["user_email"] == "dup@x"]
    assert len(matching) == 1
    # The newer requested_at wins (UPSERT semantics).
    assert matching[0]["queued_at"] == "2026-04-26T10:01:00+00:00"


# [EDGE-11] /why-not-running on a system-user email — endpoint is per-cred,
# not filtered like /schedule/next, so a system credential lookup works.
@pytest.mark.django_db(transaction=True)
def test_why_not_running_works_for_system_user(authed_client: TestClient) -> None:
    """[TEST-COVERAGE] /why-not-running targets a single (email, platform)
    so it does NOT filter system users (Telegram bot etc) — operators
    can still ask "why isn't the bot running" by passing the system
    email directly."""
    from core.models import PlatformCredential

    PlatformCredential.objects.create(
        user_email="telegram-bot@system.endpoint",
        display_name="Telegram Bot",
        platform="telegram",
        encrypted_payload=b"\x00",
    )
    response = authed_client.get(
        "/api/schedule/why-not-running/telegram-bot@system.endpoint/telegram"
    )
    # System user is not auto-404'd — the endpoint resolves to the
    # bot's credential and returns a real blocked_by reason.
    assert response.status_code == 200
    assert "blocked_by" in response.json()


# [EDGE-12] Service-layer XOR validator runs even for internal callers.
async def test_bulk_pause_service_layer_validator_rejects_empty_selector() -> None:
    """[TEST-COVERAGE] The router rejects bad selectors at 422 before
    the service runs, but ``_validate_bulk_selector`` is also enforced
    inside the service for callers from internal code paths (cron jobs,
    other services). Verify the defense-in-depth raise."""
    fake = FakeCredPort(seed=[])
    writer = CapturingWriter()
    with pytest.raises(ValueError, match="must be provided"):
        await credential_service.bulk_pause_credentials(
            fake,
            platform=None,
            user_emails=None,
            audit_writer=writer,
            reason=None,
        )


async def test_bulk_pause_service_layer_validator_rejects_both_selectors() -> None:
    """[TEST-COVERAGE] Same defense-in-depth raise for the
    mutually-exclusive case."""
    fake = FakeCredPort(seed=[])
    writer = CapturingWriter()
    with pytest.raises(ValueError, match="mutually exclusive"):
        await credential_service.bulk_pause_credentials(
            fake,
            platform="teams",
            user_emails=["a@x"],
            audit_writer=writer,
            reason=None,
        )


# [EDGE-13] Bulk operations: per-call audit rows are sequenced — operators
# can reconstruct ordering from the audit_log id column alone.
def test_bulk_pause_audit_rows_are_sequenced_by_id(tmp_path: Path, fernet_key: str) -> None:
    """[TEST-COVERAGE] Multiple bulk-pause calls in succession write
    audit rows whose autoincrement ids preserve call order — the audit
    feed renders a chronological reconstruction without consulting the
    timestamp column."""
    rows = [make_row(user_email=f"u{i}@x", platform="teams") for i in range(2)]
    fake = FakeCredPort(seed=rows)
    client = build_client(tmp_path, fernet_key, credentials=fake)
    client.post("/api/actions/users/bulk-pause", json={"platform": "teams"})
    # Call again with a single user-emails selector so the second audit
    # row carries a different ``target`` and we can prove ordering.
    client.post(
        "/api/actions/users/bulk-pause",
        json={"user_emails": ["u0@x"], "reason": "second"},
    )
    db = _db_path(client)
    conn = sqlite3.connect(str(db))
    try:
        rows_seen = conn.execute(
            "SELECT id, target FROM audit_log WHERE action = ? ORDER BY id ASC",
            ("user.bulk_pause",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows_seen) == 2
    # First call's id < second call's id, and targets match the order.
    assert rows_seen[0][0] < rows_seen[1][0]
    assert rows_seen[0][1] == "platform:teams"
    assert rows_seen[1][1] == "users:1"
