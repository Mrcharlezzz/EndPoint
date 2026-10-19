# Ingestion Layer — Integration Guide

> **Audience:** Your LLM coding agent building the Django backend that consumes
> this ingestion library. Paste this file into your agent's context.
>
> Your code **implements the Protocol interfaces** and **calls `run_sync()`**.
> You do not modify the ingestion library itself.
> This document tells you everything you need to integrate correctly.

---

## What This Layer Does

The `endpoint-ingestion` package fetches academic content from three platforms
(Microsoft Teams, Moodle, Telegram), normalizes it into a unified schema
(`NormalizedItem`), and returns structured results. It is a pure async Python
library with zero Django dependencies.

**Your Django backend must:**
1. Implement 4 Protocol interfaces (storage, state, logging, credentials)
2. Call `pipeline.run_sync()` from Celery tasks on a schedule
3. Store the returned `NormalizedItem` data in your Django models
4. Handle file downloads from `FileRef` URLs using stored credentials
5. Handle token refresh (especially for Teams)

**Data freshness:** Depends on your Celery schedule. Recommended: Teams messages
every 5 min, Moodle every 15 min, Telegram every 1-2 min.

---

## Installation

```bash
pip install endpoint-ingestion
# or in your Django project's requirements:
# endpoint-ingestion>=2.1.0
```

The package is built with hatchling and managed via uv. It has no Django
dependency — only httpx, pydantic, and pydantic-settings.

---

## Imports

Everything you need to import:

```python
# Pipeline entry point
from ingestion.pipeline import run_sync

# Adapters (construct one per platform)
from ingestion.adapters.teams.adapter import TeamsAdapter
from ingestion.adapters.moodle.adapter import MoodleAdapter
from ingestion.adapters.telegram.adapter import TelegramAdapter

# Configuration
from ingestion.config import (
    IngestionConfig,
    TeamsConfig,
    MoodleConfig,
    TelegramConfig,
)

# Schemas (for type hints and data handling)
from ingestion.schemas import (
    ContentType,       # Enum: assignment, announcement, event, material, raw_message, ...
    FetchScope,        # Enum: messages, assignments, files, events, materials, forums
    ErrorCode,         # Enum: token_expired, rate_limited, ...
    NormalizedItem,    # The universal content record
    FileRef,           # Downloadable file reference
    FetchError,        # Structured error with code + recoverable flag
    FetchResult,       # Top-level adapter return value
    SyncStats,         # Timing and count statistics
    ScopeStats,        # Per-scope breakdown
)

# Protocols (your Django classes must satisfy these)
from ingestion.ports import (
    Adapter,           # Already implemented by TeamsAdapter etc.
    TokenStore,        # You implement this
    ContentSink,       # You implement this
    SyncStateStore,    # You implement this
    SyncLogger,        # You implement this
)

# Exceptions (for error handling in your Celery tasks)
from ingestion.exceptions import (
    IngestionError,          # Base — catch-all
    TokenExpiredError,       # 401 — mark expired, notify user
    InsufficientScopeError,  # 403 — skip resource
    RateLimitError,          # 429 — retry later
    PlatformUnavailableError,# 5xx — retry later
    NormalizationError,      # Bad data — skip item
)
```

---

## Protocol Implementations You Must Write

### 1. `ContentSink` — Store normalized items

```python
from ingestion.ports import ContentSink
from ingestion.schemas import NormalizedItem


class DjangoContentSink:
    """Implements ContentSink protocol. Stores NormalizedItems in Django models."""

    async def exists(self, platform: str, external_id: str) -> bool:
        """Check if an item with this platform + external_id already exists."""
        # Example:
        # return await sync_to_async(
        #     ContentItem.objects.filter(platform=platform, external_id=external_id).exists
        # )()
        ...

    async def upsert(self, item: NormalizedItem) -> str:
        """Insert or update a single item. Return the item's DB ID as string."""
        ...

    async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]:
        """Bulk upsert. This is the hot path — run_sync calls this with ALL items.

        Dedup logic: use item.external_id + item.platform as the unique key.
        If exists → update. If new → insert.

        Return list of DB IDs for upserted items.
        """
        ...
```

**Key fields to store from `NormalizedItem`:**

| Field | Type | Nullable | What it is |
|-------|------|----------|------------|
| `external_id` | `str` | no | Unique per platform (e.g. `moodle_assign_5897`) |
| `platform` | `str` | no | `"moodle"`, `"teams"`, or `"telegram"` |
| `content_type` | `ContentType` | no | `assignment`, `announcement`, `event`, `material`, `raw_message`, `unknown` |
| `title` | `str \| None` | yes | Title. `null` for chat messages |
| `body` | `str` | no | Plain text (HTML stripped). Always present, may be `""` |
| `course_name` | `str \| None` | yes | Human-readable course/team/group name |
| `course_external_id` | `str \| None` | yes | Platform-specific course/channel/chat ID |
| `author` | `str \| None` | yes | Sender. `null` for Moodle assignments/materials |
| `published_at` | `datetime \| None` | yes | UTC. When posted/modified, when available from the source |
| `source_url` | `str \| None` | yes | Source platform URL. Persist as top-level `ingested_items.source_url`, not inside `payload` |
| `deadline` | `datetime \| None` | yes | UTC. Assignment deadline |
| `submission_format` | `str \| None` | yes | Assignment submission format |
| `start_time` | `datetime \| None` | yes | UTC. Event start time |
| `end_time` | `datetime \| None` | yes | UTC. Event end time |
| `location` | `str \| None` | yes | Event location |
| `raw_payload` | `dict` | no | Full raw API response wrapped in `RawPayloadEnvelope` |
| `attachments` | `list[FileRef]` | no | File references. May be `[]` |
| `links` | `list[LinkRef]` | no | Link references. May be `[]` |
| `ingestion_mode` | `str` | no | Always `"server_poll"` for now |

**Unique constraint:** `(platform, external_id)` — this is your upsert key.

### 2. `TokenStore` — Read/write platform credentials

```python
class DjangoTokenStore:
    """Implements TokenStore protocol. Encrypted credential storage."""

    async def get_credentials(self, user_id: str, platform: str) -> dict | None:
        """Return credentials dict for this user+platform, or None if not stored.

        Expected dict shapes:
          Teams:    {"graph_token": str, "csa_token": str|None, "team_ids": list[str]}
          Moodle:   {"token": str, "user_id": int}
          Telegram: {"bot_token": str}
        """
        ...

    async def update_credentials(self, user_id: str, platform: str, credentials: dict) -> None:
        """Store or update credentials. Called after token refresh."""
        ...

    async def mark_expired(self, user_id: str, platform: str) -> None:
        """Mark credentials as expired. Trigger user notification for re-auth.

        Called by run_sync when adapter raises TokenExpiredError.
        """
        ...
```

### 3. `SyncStateStore` — Persist sync cursors

```python
class DjangoSyncStateStore:
    """Implements SyncStateStore protocol. Cursor persistence for incremental sync."""

    async def get_cursor(self, user_id: str, platform: str, scope: str) -> dict | None:
        """Return the last saved cursor, or None for first sync.

        The cursor is opaque — just store and return the dict as-is.
        Teams cursor: {"last_sync_at": "2026-03-13T14:30:00+00:00"}
        Telegram cursor: {"last_update_id": 123456}
        Moodle cursor: {"last_sync_at": "2026-03-13T14:30:00+00:00"}
        """
        ...

    async def set_cursor(self, user_id: str, platform: str, scope: str, cursor: dict) -> None:
        """Save cursor. Only called on full successful completion.

        If a sync fails partway, the cursor is NOT updated — next run
        re-fetches from the last checkpoint.
        """
        ...
```

### 4. `SyncLogger` — Audit trail

```python
class DjangoSyncLogger:
    """Implements SyncLogger protocol. Audit log for sync operations."""

    async def log_start(self, user_id: str, platform: str) -> str:
        """Log sync start. Return a unique sync_id (e.g. UUID)."""
        ...

    async def log_complete(self, sync_id: str, stats: SyncStats) -> None:
        """Log successful completion with stats."""
        ...

    async def log_failure(self, sync_id: str, error: str) -> None:
        """Log sync failure with error message."""
        ...
```

---

## Calling `run_sync()` from Celery

```python
import asyncio
from celery import shared_task
from ingestion.pipeline import run_sync
from ingestion.adapters.teams.adapter import TeamsAdapter
from ingestion.config import TeamsConfig
from ingestion.schemas import FetchScope


@shared_task
def sync_teams_messages(user_id: str):
    """Celery task: sync Teams messages for one user."""
    asyncio.run(_sync_teams_messages(user_id))


async def _sync_teams_messages(user_id: str):
    # 1. Construct adapter
    config = TeamsConfig()  # reads from env vars (ENDPOINT_TEAMS__*)
    adapter = TeamsAdapter(config)

    # 2. Construct protocol implementations
    token_store = DjangoTokenStore()
    content_sink = DjangoContentSink()
    sync_state = DjangoSyncStateStore()
    sync_logger = DjangoSyncLogger()

    # 3. Load credentials
    credentials = await token_store.get_credentials(user_id, "teams")
    if not credentials:
        return  # No credentials stored — user hasn't authenticated yet

    # 4. Run sync (scoped to messages only for frequent polling)
    stats = await run_sync(
        adapter=adapter,
        credentials=credentials,
        content_sink=content_sink,
        sync_state=sync_state,
        sync_logger=sync_logger,
        user_id=user_id,
        scope=frozenset({FetchScope.MESSAGES}),
        token_store=token_store,  # Optional — enables auto mark_expired on 401
    )

    # 5. Stats available for monitoring
    # stats.items_fetched, stats.items_new, stats.errors, stats.duration_seconds
```

### Recommended Celery Beat Schedule

```python
CELERY_BEAT_SCHEDULE = {
    "sync-teams-messages": {
        "task": "yourapp.tasks.sync_teams_messages_all_users",
        "schedule": 300,  # every 5 minutes
    },
    "sync-teams-structural": {
        "task": "yourapp.tasks.sync_teams_structural_all_users",
        "schedule": 86400,  # once daily (assignments + files)
    },
    "sync-moodle": {
        "task": "yourapp.tasks.sync_moodle_all_users",
        "schedule": 900,  # every 15 minutes
    },
    "sync-telegram": {
        "task": "yourapp.tasks.sync_telegram_all_users",
        "schedule": 120,  # every 2 minutes
    },
}
```

---

## `run_sync()` Signature

```python
async def run_sync(
    adapter: Adapter,
    credentials: dict,
    content_sink: ContentSink,
    sync_state: SyncStateStore,
    sync_logger: SyncLogger,
    user_id: str,
    scope: frozenset[FetchScope] | None = None,  # None = all scopes
    token_store: TokenStore | None = None,        # Optional: auto mark_expired
) -> SyncStats
```

**What it does:**
1. `sync_logger.log_start()` → sync_id
2. `sync_state.get_cursor()` → cursor
3. `adapter.fetch(credentials, cursor, scope)` → FetchResult
4. For each item: `content_sink.exists()` to count new vs updated
5. `content_sink.upsert_batch(items)` → item_ids
6. `sync_state.set_cursor()` — **only on full completion**
7. `sync_logger.log_complete()` or `log_failure()`
8. Returns `SyncStats`

**What it does NOT do:**
- Schedule itself (your Celery does that)
- Download files (returns `FileRef` URLs — you download)
- Run LLM extraction (Telegram items are `raw_message`)
- Handle token refresh (raises `TokenExpiredError`, you handle)
- Cross-platform dedup (your code handles)

---

## Handling `FetchError` Codes

`FetchResult.errors` contains structured errors. Use `error.code` to decide:

| Code | Meaning | Your Action |
|------|---------|-------------|
| `token_expired` | 401 — credential invalid | Mark expired, notify user, skip retry |
| `insufficient_scope` | 403 — missing permissions | Log, skip resource, don't retry |
| `rate_limited` | 429 — retries exhausted | Retry in next scheduled run |
| `platform_unavailable` | 5xx / network failure | Retry in next scheduled run |
| `normalization_error` | Malformed API response | Log for investigation, skip item |
| `connection_error` | Network failure | Retry in next scheduled run |
| `unknown` | Catch-all | Log, retry in next scheduled run |

**`error.recoverable`:** If `True`, safe to retry. If `False`, don't retry
without fixing the root cause (e.g. re-authenticating).

---

## Handling Files (`FileRef`)

Files come in two places:
1. `FetchResult.files` — standalone files (e.g. Teams drive files)
2. `NormalizedItem.attachments` — files attached to specific items

```python
from ingestion.schemas import FileRef

# FileRef fields:
# .external_id   — unique file ID
# .filename      — original filename
# .download_url  — URL to fetch (requires platform auth)
# .content_type  — MIME type (may be None)
# .size_bytes    — file size (may be None)
# .is_media      — True for video/audio (you may want to skip these)
```

**Download requires platform credentials.** The ingestion library does NOT
download files — it only returns URLs. Your Django code must:
1. Use the stored credentials (Graph token for Teams, Moodle token, bot token for Telegram)
2. Download the file from `download_url`
3. Store in S3 or equivalent

**Telegram caveat:** Download URLs contain the bot token in the path
(`/file/bot{token}/{file_id}`). This is by Telegram's API design and is documented.

---

## Credential Shapes by Platform

### Teams

```python
{
    "graph_token": "eyJ...",           # Required. Bearer token for Graph + Assignments API
    "csa_token": "eyJ...",            # Optional. Bearer for CSA API. If absent, CSA skipped
    "team_ids": ["uuid1", "uuid2"],   # Required. Which teams/classes to fetch from
    "refresh_url": "https://...",     # Optional. For headless token refresh
}
```

Teams tokens expire: Graph ~90 min, CSA ~24h. You need a daily Celery task
that refreshes tokens via headless Playwright (SSO cookie rotation). See
`cli/auth/teams_login.py` for the refresh implementation.

### Moodle

```python
{
    "token": "abc123...",  # Required. Moodle web service token
    "user_id": 12345,      # Required. Moodle user ID (numeric)
}
```

Token obtained via `/login/token.php?service=moodle_mobile_app`. Long-lived
(doesn't expire unless revoked by admin).

### Telegram

```python
{
    "bot_token": "123456:ABC-..."  # Required. From @BotFather
}
```

Bot token is permanent. **Privacy mode must be OFF** for the bot to see
group messages.

---

## `NormalizedItem` Field Semantics & Edge Cases

### Nullable fields — what null means

| Field | When null | Domain meaning |
|-------|----------|----------------|
| `title` | Chat messages (Teams, Telegram) | Messages don't have titles |
| `course_name` | Rare — fallback failure | Couldn't resolve course name |
| `course_external_id` | Rare | Couldn't resolve course ID |
| `author` | Moodle assignments/materials | Moodle API doesn't expose creator |
| `published_at` | Sources without created/modified timestamps | No reliable publish timestamp available |
| `deadline` | Messages, materials, announcements | No assignment deadline concept |

### Timezone

All `datetime` fields are **UTC**. Convert on API output if your frontend
needs a different timezone.

### Platform values

`NormalizedItem.platform` is always one of: `"moodle"`, `"teams"`, `"telegram"`.

### Content types by platform

| Platform | Possible `content_type` values |
|----------|-------------------------------|
| Moodle | `assignment`, `announcement`, `event`, `material` |
| Teams | `raw_message`, `assignment` |
| Telegram | `raw_message` |

### `raw_payload` structure

Always a `RawPayloadEnvelope` dict:

```python
{
    "schema_version": "2",
    "source": "moodle_assign",     # Source tag (see below)
    "ingestion_mode": "server_poll",
    "fetched_at": "2026-03-13T14:30:00+00:00",
    "adapter_version": "2.1.0",
    "original": { ... }            # Full raw API response
}
```

Source tags: `moodle_assign`, `moodle_forum`, `moodle_material`, `moodle_calendar`,
`teams_csa`, `teams_graph`, `teams_edu`, `telegram_message`,
`telegram_edited_message`.

---

## What You Must NOT Do

- **Do NOT import from adapter internals** (`adapters/teams/csa_fetcher.py`, etc.).
  Your interface is: construct adapter → call `run_sync()` → read results.
- **Do NOT write to ingestion schemas or modify NormalizedItem instances.**
  Map them to your own Django models.
- **Do NOT assume `external_id` ordering equals chronological order.**
  IDs are platform-specific strings, not sequential.
- **Do NOT add Django signals to ingestion models** — there are no ingestion
  models. The ingestion library returns Pydantic objects.
- **Do NOT cache/denormalize ingestion data** in a separate table. Store once,
  query the source of truth.
- **Do NOT call adapter.fetch() directly** for production use. Use `run_sync()`
  — it handles cursor management, upsert orchestration, and audit logging.
- **Do NOT advance the cursor manually.** `run_sync()` handles this and only
  saves cursors on full completion.

---

## Worked Example: Full Moodle Integration

```python
# yourapp/ingestion/sinks.py
from asgiref.sync import sync_to_async
from ingestion.schemas import NormalizedItem, SyncStats
from yourapp.models import ContentItem, SyncLog, SyncState, PlatformCredential


class DjangoContentSink:
    async def exists(self, platform: str, external_id: str) -> bool:
        return await sync_to_async(
            ContentItem.objects.filter(
                platform=platform, external_id=external_id
            ).exists
        )()

    async def upsert(self, item: NormalizedItem) -> str:
        obj, _ = await sync_to_async(ContentItem.objects.update_or_create)(
            platform=item.platform,
            external_id=item.external_id,
            defaults={
                "content_type": item.content_type.value,
                "title": item.title,
                "body": item.body,
                "course_name": item.course_name,
                "course_external_id": item.course_external_id,
                "author": item.author,
                "published_at": item.published_at,
                "deadline": item.deadline,
                "submission_format": item.submission_format,
                "start_time": item.start_time,
                "end_time": item.end_time,
                "location": item.location,
                "raw_payload": item.raw_payload,
                "ingestion_mode": item.ingestion_mode,
            },
        )
        return str(obj.pk)

    async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]:
        return [await self.upsert(item) for item in items]


class DjangoTokenStore:
    async def get_credentials(self, user_id: str, platform: str) -> dict | None:
        try:
            cred = await sync_to_async(PlatformCredential.objects.get)(
                user_id=user_id, platform=platform
            )
            return cred.decrypted_credentials  # your encrypted field
        except PlatformCredential.DoesNotExist:
            return None

    async def update_credentials(self, user_id: str, platform: str, credentials: dict) -> None:
        await sync_to_async(PlatformCredential.objects.update_or_create)(
            user_id=user_id,
            platform=platform,
            defaults={"encrypted_credentials": credentials},
        )

    async def mark_expired(self, user_id: str, platform: str) -> None:
        await sync_to_async(
            PlatformCredential.objects.filter(
                user_id=user_id, platform=platform
            ).update
        )(is_expired=True)
        # TODO: trigger user notification


class DjangoSyncStateStore:
    async def get_cursor(self, user_id: str, platform: str, scope: str) -> dict | None:
        try:
            state = await sync_to_async(SyncState.objects.get)(
                user_id=user_id, platform=platform, scope=scope
            )
            return state.cursor
        except SyncState.DoesNotExist:
            return None

    async def set_cursor(self, user_id: str, platform: str, scope: str, cursor: dict) -> None:
        await sync_to_async(SyncState.objects.update_or_create)(
            user_id=user_id,
            platform=platform,
            scope=scope,
            defaults={"cursor": cursor},
        )


class DjangoSyncLogger:
    async def log_start(self, user_id: str, platform: str) -> str:
        log = await sync_to_async(SyncLog.objects.create)(
            user_id=user_id, platform=platform, status="running"
        )
        return str(log.pk)

    async def log_complete(self, sync_id: str, stats: SyncStats) -> None:
        await sync_to_async(
            SyncLog.objects.filter(pk=sync_id).update
        )(
            status="completed",
            items_fetched=stats.items_fetched,
            items_new=stats.items_new,
            errors=stats.errors,
            duration_seconds=stats.duration_seconds,
        )

    async def log_failure(self, sync_id: str, error: str) -> None:
        await sync_to_async(
            SyncLog.objects.filter(pk=sync_id).update
        )(status="failed", error_message=error)
```

```python
# yourapp/ingestion/tasks.py
import asyncio
from celery import shared_task
from ingestion.pipeline import run_sync
from ingestion.adapters.moodle.adapter import MoodleAdapter
from ingestion.config import MoodleConfig
from yourapp.ingestion.sinks import (
    DjangoContentSink, DjangoTokenStore, DjangoSyncStateStore, DjangoSyncLogger
)


@shared_task
def sync_moodle(user_id: str):
    asyncio.run(_sync_moodle(user_id))


async def _sync_moodle(user_id: str):
    adapter = MoodleAdapter(MoodleConfig())
    token_store = DjangoTokenStore()

    credentials = await token_store.get_credentials(user_id, "moodle")
    if not credentials:
        return

    stats = await run_sync(
        adapter=adapter,
        credentials=credentials,
        content_sink=DjangoContentSink(),
        sync_state=DjangoSyncStateStore(),
        sync_logger=DjangoSyncLogger(),
        user_id=user_id,
        token_store=token_store,
    )

    if stats.errors > 0:
        # Check FetchResult.errors for token_expired etc.
        # (run_sync already called mark_expired if applicable)
        pass
```

---

## Source Code Reference

| File | What it contains |
|------|-----------------|
| `src/ingestion/pipeline.py` | `run_sync()` — the entry point |
| `src/ingestion/schemas.py` | All Pydantic models and enums |
| `src/ingestion/ports.py` | Protocol interfaces to implement |
| `src/ingestion/config.py` | Configuration (env-driven) |
| `src/ingestion/exceptions.py` | Exception hierarchy |
| `src/ingestion/adapters/teams/adapter.py` | TeamsAdapter |
| `src/ingestion/adapters/moodle/adapter.py` | MoodleAdapter |
| `src/ingestion/adapters/telegram/adapter.py` | TelegramAdapter |
