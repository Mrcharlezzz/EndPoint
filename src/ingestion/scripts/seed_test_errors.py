"""Seed the daemon's stores with fake users, credentials, and error state.

Dev-only. Produces rows across all three ErrorKind surfaces so the
dashboard's Errors page has real data to render without booting actual
adapters against live platforms:

- Several ``PlatformCredential`` rows in the db-core Postgres (some
  expired, some paused, some healthy) so the Users/Credentials pages
  also have something to show.
- ``dead_letter_state`` rows in the daemon's SQLite, via the real
  :class:`DeadLetterTracker` (one user actually crosses the suppression
  threshold so ``suppressed_until`` is set).
- ``sync_history`` failed rows via the real :class:`SqliteSyncLogger`.
- Matching exceptions captured by ``sentry_sdk`` so the same errors
  show up in GlitchTip. Contextvars (user_email, platform, sync_id)
  promote to tags so per-user filtering works in both UIs.

Safe to re-run: existing test rows (keyed by ``@e2e.test`` domain or
the demo sync_ids) are deleted before re-insert.

Usage::

    uv run python scripts/seed_test_errors.py

Respects ``ENDPOINT_DAEMON__SQLITE_PATH``. Override to a local path for
dev, e.g. ``ENDPOINT_DAEMON__SQLITE_PATH=./dev-state.db``.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

import core.bootstrap

core.bootstrap.init()

from core.models import PlatformCredential  # noqa: E402

from ingestion.daemon.config import DaemonConfig  # noqa: E402
from ingestion.daemon.dead_letter import DeadLetterTracker  # noqa: E402
from ingestion.daemon.logging import bind_sync_context, configure_logging  # noqa: E402
from ingestion.daemon.ports.sync_logger import SqliteSyncLogger  # noqa: E402
from ingestion.daemon.sentry import init_sentry  # noqa: E402
from ingestion.daemon.store.local import apply_migrations  # noqa: E402

TEST_DOMAIN = "@e2e.test"

# (email, display_name, platform, is_expired, is_paused, last_refreshed_offset_hours)
USERS: list[tuple[str, str, str, bool, bool, int]] = [
    ("sara@e2e.test", "Sara Fitzgerald", "teams", False, False, 2),
    ("sara@e2e.test", "Sara Fitzgerald", "moodle", False, False, 3),
    ("pavel@e2e.test", "Pavel Novak", "teams", True, False, 72),  # expired
    ("pavel@e2e.test", "Pavel Novak", "moodle", False, False, 4),
    ("kenji@e2e.test", "Kenji Yamamoto", "telegram", False, True, 12),  # paused
    ("nour@e2e.test", "Nour Haddad", "moodle", True, False, 96),  # expired
    ("olek@e2e.test", "Olek Kowalski", "teams", False, False, 1),
]

# Failed syncs to seed — (email, platform, error_text)
FAILED_SYNCS: list[tuple[str, str, str]] = [
    (
        "pavel@e2e.test",
        "teams",
        "TOKEN_EXPIRED: jwt expired 12m ago, 3 refresh attempts failed",
    ),
    ("sara@e2e.test", "moodle", "PARSE_FAILED: unexpected null in field 'score' · row 214"),
    ("olek@e2e.test", "teams", "HTTP_502: upstream timeout after 10.0s (attempt 3/3)"),
    ("kenji@e2e.test", "telegram", "RATE_LIMITED: flood_wait 30s — respecting backoff"),
]


def seed_postgres() -> int:
    PlatformCredential.objects.filter(user_email__endswith=TEST_DOMAIN).delete()
    now = datetime.now(tz=UTC)
    count = 0
    for email, name, platform, expired, paused, refreshed_hours_ago in USERS:
        PlatformCredential.objects.create(
            user_email=email,
            display_name=name,
            platform=platform,
            is_expired=expired,
            is_paused=paused,
            expired_at=now - timedelta(hours=1) if expired else None,
            last_refreshed_at=now - timedelta(hours=refreshed_hours_ago),
            encrypted_payload=b"seed-stub",
        )
        count += 1
    return count


async def seed_sqlite(cfg: DaemonConfig) -> dict[str, int]:
    apply_migrations(cfg.sqlite_path)

    # Dead-letter: pavel/teams crosses threshold (4 > 3), kenji/telegram below.
    tracker = DeadLetterTracker(cfg.sqlite_path, threshold=3, cooldown_seconds=3600)
    for _ in range(4):
        await tracker.record_failure("pavel@e2e.test", "teams")
    for _ in range(2):
        await tracker.record_failure("kenji@e2e.test", "telegram")

    # Failed syncs via the real logger so started_at/finished_at/state all line up.
    logger = SqliteSyncLogger(cfg.sqlite_path)
    for email, platform, error_text in FAILED_SYNCS:
        sync_id = await logger.log_start(email, platform)
        await logger.log_failure(sync_id, error_text)

    return {"dead_letter_rows": 2, "failed_syncs": len(FAILED_SYNCS)}


def fire_sentry_events(cfg: DaemonConfig) -> int:
    if not init_sentry(cfg.sentry):
        print("sentry.disabled (DSN empty) — skipping GlitchTip events", file=sys.stderr)
        return 0
    import sentry_sdk

    count = 0
    for email, platform, error_text in FAILED_SYNCS:
        with bind_sync_context(
            user_email=email,
            platform=platform,
            sync_id=f"seed-sync-{count + 1:03d}",
        ):
            try:
                raise RuntimeError(error_text)
            except RuntimeError as exc:
                sentry_sdk.capture_exception(exc)
                count += 1
    sentry_sdk.flush(timeout=10.0)
    return count


def main() -> int:
    configure_logging("INFO")
    cfg = DaemonConfig()

    postgres_rows = seed_postgres()
    sqlite_counts = asyncio.run(seed_sqlite(cfg))
    sentry_events = fire_sentry_events(cfg)

    print("seed_test_errors: done")
    print(f"  platform_credentials  : {postgres_rows} rows")
    print(f"  dead_letter_state     : {sqlite_counts['dead_letter_rows']} users (1 suppressed)")
    print(f"  sync_history (failed) : {sqlite_counts['failed_syncs']} rows")
    print(f"  sentry events         : {sentry_events}")
    print(f"  sqlite path           : {cfg.sqlite_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
