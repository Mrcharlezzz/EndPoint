# Schema Reference

> Every Pydantic model, enum, Protocol interface, TypedDict, and exception
> used in the ingestion pipeline. Source of truth: `src/ingestion/schemas.py`,
> `ports.py`, `exceptions.py`, `config.py`, and per-adapter `credentials.py`.

---

## Enums

### `ContentType` (StrEnum)

Type of content item after normalization.

| Value | Description |
|-------|-------------|
| `assignment` | Homework, lab, quiz with optional due date |
| `announcement` | Forum announcement or news post |
| `event` | Calendar event with start/end time |
| `material` | Lecture slides, resource, page, book, folder |
| `reminder` | System-generated reminder |
| `raw_message` | Chat message (Teams CSA, Telegram) — not yet classified |
| `unknown` | Fallback for unrecognized content |

**Source:** `schemas.py:17`

### `FetchScope` (StrEnum)

What data to fetch. Each adapter supports a subset; unsupported scopes are logged and skipped.

| Value | Teams | Moodle | Telegram |
|-------|-------|--------|----------|
| `messages` | yes | — | yes |
| `assignments` | yes | yes | — |
| `files` | yes | yes | — |
| `events` | — | yes | — |
| `materials` | — | yes | — |
| `forums` | — | yes | — |

**Source:** `schemas.py:29`

### `ErrorCode` (StrEnum)

Structured codes so Django can decide: retry, notify user, or skip.

| Value | Meaning | Typical action |
|-------|---------|----------------|
| `token_expired` | HTTP 401 — credential invalid | Mark expired, notify user |
| `insufficient_scope` | HTTP 403 — missing permissions | Skip resource, log |
| `rate_limited` | HTTP 429 — retries exhausted | Retry later |
| `platform_unavailable` | HTTP 5xx after retries | Retry later |
| `normalization_error` | Malformed API response | Skip item, log |
| `connection_error` | Network failure | Retry later |
| `unknown` | Catch-all | Log, investigate |

**Source:** `schemas.py:43`

---

## Core Models (Pydantic v2)

### `NormalizedItem`

The universal content record. Every platform normalizes into this shape.

| Field | Type | Nullable | Default | Description |
|-------|------|----------|---------|-------------|
| `external_id` | `str` | no | — | Unique per platform. Pattern: `{platform}_{type}_{id}` |
| `platform` | `str` | no | — | `"moodle"`, `"teams"`, or `"telegram"` |
| `content_type` | `ContentType` | no | — | See enum above |
| `title` | `str` | yes | `None` | Title (assignments, announcements, materials). `null` for chat messages |
| `body` | `str` | no | — | **Plain text** (HTML stripped). Always present, may be `""` |
| `course_name` | `str` | yes | `None` | Course fullname (Moodle), Team/channel name (Teams), Group title (Telegram) |
| `course_external_id` | `str` | yes | `None` | Platform-specific course/channel/chat ID |
| `author` | `str` | yes | `None` | Sender display name. `null` for Moodle assignments/materials |
| `published_at` | `datetime` | yes | `None` | UTC. When the item was posted/modified, when available from the source |
| `source_url` | `str` | yes | `None` | Source platform URL. Persisted as top-level `ingested_items.source_url`, not inside `payload` |
| `deadline` | `datetime` | yes | `None` | UTC. Deadline for assignments |
| `submission_format` | `str` | yes | `None` | Assignment submission format |
| `start_time` | `datetime` | yes | `None` | UTC. Event start time |
| `end_time` | `datetime` | yes | `None` | UTC. Event end time |
| `location` | `str` | yes | `None` | Event location |
| `raw_payload` | `dict` | no | `{}` | `RawPayloadEnvelope` as dict — full original API response with metadata |
| `attachments` | `list[FileRef]` | no | `[]` | File references embedded in this item |
| `links` | `list[LinkRef]` | no | `[]` | Links embedded in this item |
| `ingestion_mode` | `str` | no | `"server_poll"` | Always `"server_poll"` for now |

**Source:** `schemas.py:76`

#### `external_id` patterns

| Platform | Pattern | Example |
|----------|---------|---------|
| Moodle | `moodle_{type}_{id}` | `moodle_assign_5897`, `moodle_forum_44201`, `moodle_material_30412` |
| Teams (CSA) | `teams_csa_{messageId}` | `teams_csa_1710345600001` |
| Teams (assignments) | `teams_assignment_{uuid}` | `teams_assignment_a1b2c3d4-e5f6-...` |
| Telegram | `tg_{chatId}_{messageId}` | `tg_-100123_456` |

### `FileRef` (frozen)

A pointer to a downloadable file. **The ingestion pipeline does NOT download files** — it hands these to Django, which fetches them using stored credentials.

| Field | Type | Nullable | Default | Description |
|-------|------|----------|---------|-------------|
| `external_id` | `str` | no | — | Unique file ID (e.g. `moodle_file_30412_slides.pdf`) |
| `filename` | `str` | no | — | Original filename |
| `download_url` | `str` | no | — | URL to fetch the file (requires platform auth) |
| `content_type` | `str` | yes | `None` | MIME type (e.g. `application/pdf`) |
| `size_bytes` | `int` | yes | `None` | File size. `null` if platform doesn't report it. Validated >= 0 |
| `is_media` | `bool` | no | `False` | `true` for video/audio files (configurable via `SKIP_MEDIA_EXTENSIONS`) |

**Source:** `schemas.py:55`

### `FetchError` (frozen)

Structured error from a fetch operation.

| Field | Type | Nullable | Default | Description |
|-------|------|----------|---------|-------------|
| `code` | `ErrorCode` | no | — | See enum above |
| `message` | `str` | no | — | Human-readable detail |
| `scope` | `str` | no | `""` | Which scope failed (e.g. `"assignments"`) |
| `recoverable` | `bool` | no | `True` | `true` = safe to retry later |
| `timestamp` | `datetime` | no | `now(UTC)` | When the error occurred |

**Source:** `schemas.py:104`

### `ScopeStats`

Per-scope statistics within a sync run.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `items_fetched` | `int` | `0` | Total items returned by API for this scope |
| `items_new` | `int` | `0` | New items (first seen) |
| `items_updated` | `int` | `0` | Items with changed content |
| `items_skipped` | `int` | `0` | Unchanged items (placeholder — always 0) |
| `errors` | `int` | `0` | Number of errors in this scope |

**Source:** `schemas.py:119`

### `SyncStats`

Aggregate statistics from a sync run.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `items_fetched` | `int` | `0` | Total items across all scopes |
| `items_new` | `int` | `0` | New items |
| `items_updated` | `int` | `0` | Updated items |
| `items_skipped` | `int` | `0` | Skipped items (placeholder) |
| `errors` | `int` | `0` | Error count |
| `duration_seconds` | `float` | `0.0` | Wall-clock time |
| `api_call_duration_seconds` | `float` | `0.0` | Time spent in HTTP calls |
| `normalization_duration_seconds` | `float` | `0.0` | Time spent normalizing |
| `per_scope_stats` | `dict[str, ScopeStats]` | `{}` | Breakdown by course/channel ID |

**Source:** `schemas.py:129`

### `FetchResult`

What an adapter returns from a `fetch()` call. The top-level output of the pipeline.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `items` | `list[NormalizedItem]` | `[]` | Content items (assignments, messages, etc.) |
| `files` | `list[FileRef]` | `[]` | Downloadable file references (Django downloads these) |
| `cursor` | `dict \| None` | `None` | Opaque state for incremental sync — pass back on next call |
| `errors` | `list[FetchError]` | `[]` | Structured errors with codes for retry/notify decisions |
| `stats` | `SyncStats` | `SyncStats()` | Timing and count breakdowns |

**Source:** `schemas.py:143`

### `RawPayloadEnvelope` (frozen)

Wraps the original API response inside `NormalizedItem.raw_payload`. Preserved for auditing and reprocessing.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `schema_version` | `str` | `"2"` | Envelope format version |
| `source` | `str` | — | Adapter source tag (see table below) |
| `ingestion_mode` | `str` | — | Always `"server_poll"` |
| `fetched_at` | `datetime` | `now(UTC)` | When this fetch happened |
| `adapter_version` | `str` | `""` | From `pyproject.toml` version |
| `original` | `dict` | `{}` | **Full raw API response** — structure varies by platform |

**Source:** `schemas.py:153`

#### Source tags

| Tag | Platform | Content |
|-----|----------|---------|
| `moodle_assign` | Moodle | Assignment from `mod_assign_get_assignments` |
| `moodle_forum` | Moodle | Forum discussion from `mod_forum_get_forum_discussions` |
| `moodle_material` | Moodle | Resource/page/folder/book from `core_course_get_contents` |
| `moodle_calendar` | Moodle | Calendar event from `core_calendar_get_action_events_by_timesort` |
| `teams_csa` | Teams | Chat message via Cloud Service API |
| `teams_graph` | Teams | Chat message via Graph API (fallback) |
| `teams_edu` | Teams | Assignment via Education API |
| `telegram_message` | Telegram | New message |
| `telegram_edited_message` | Telegram | Edited message (same `external_id` as original) |

---

## Protocol Interfaces (ports.py)

These define the contracts that Django (or CLI) must implement. The ingestion pipeline programs against these abstractions, never against concrete implementations.

**Type alias:** `ProgressCallback = Callable[[str, str], None]` — `(scope, detail_message)`

### `Adapter`

```python
class Adapter(Protocol):
    platform: str

    async def fetch(
        self,
        credentials: dict,
        cursor: dict | None = None,
        scope: frozenset[FetchScope] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> FetchResult: ...

    async def test_connection(self, credentials: dict) -> bool: ...
```

- Stateless: credentials and cursors are passed as arguments
- `scope=None` means fetch all supported scopes
- `on_progress` is optional — CLI uses it for live display
- `test_connection` validates credentials without fetching data

### `Normalizer`

```python
class Normalizer(Protocol):
    def normalize(self, raw: dict, **context: object) -> NormalizedItem: ...
```

- Transforms a raw API response dict into a `NormalizedItem`
- `context` carries adapter-specific metadata (source tag, course name, etc.)

### `TokenStore`

```python
class TokenStore(Protocol):
    async def get_credentials(self, user_id: str, platform: str) -> dict | None: ...
    async def update_credentials(self, user_id: str, platform: str, credentials: dict) -> None: ...
    async def mark_expired(self, user_id: str, platform: str) -> None: ...
```

- Django: encrypted `PlatformConnection` model
- CLI: local JSON file
- `mark_expired` triggers user notification for re-authentication

### `ContentSink`

```python
class ContentSink(Protocol):
    async def exists(self, platform: str, external_id: str) -> bool: ...
    async def upsert(self, item: NormalizedItem) -> str: ...
    async def upsert_batch(self, items: list[NormalizedItem]) -> list[str]: ...
```

- Django: `ContentItem` model with dedup logic
- CLI: JSON file export
- `upsert_batch` is the hot path — `run_sync` calls it with all items at once
- Returns item IDs (format is implementation-defined)

### `SyncStateStore`

```python
class SyncStateStore(Protocol):
    async def get_cursor(self, user_id: str, platform: str, scope: str) -> dict | None: ...
    async def set_cursor(self, user_id: str, platform: str, scope: str, cursor: dict) -> None: ...
```

- Persists opaque cursor dicts for incremental fetching
- Cursor is only saved on **full completion** — partial failures don't advance it

### `SyncLogger`

```python
class SyncLogger(Protocol):
    async def log_start(self, user_id: str, platform: str) -> str: ...
    async def log_complete(self, sync_id: str, stats: SyncStats) -> None: ...
    async def log_failure(self, sync_id: str, error: str) -> None: ...
```

- Audit trail for sync operations
- `log_start` returns a `sync_id` used to correlate completion/failure

---

## Typed Credentials (TypedDict)

Adapters accept `dict` at the public boundary (for Django compatibility) but internally cast to these for type-safe access.

### `TeamsCredentials`

```python
class TeamsCredentials(TypedDict):
    graph_token: str           # Bearer token for Graph API + Assignments API
    csa_token: NotRequired[str]  # Bearer token for CSA API (optional — CSA skipped if absent)
    team_ids: list[str]        # Team/class IDs to fetch from
    refresh_url: NotRequired[str]  # Direct channel URL for headless token refresh
```

**Source:** `adapters/teams/credentials.py`

### `MoodleCredentials`

```python
class MoodleCredentials(TypedDict):
    token: str   # Moodle web service token
    user_id: int # Moodle user ID (numeric)
```

**Source:** `adapters/moodle/credentials.py`

### `TelegramCredentials`

```python
class TelegramCredentials(TypedDict):
    bot_token: str  # Telegram Bot API token (from @BotFather)
```

**Source:** `adapters/telegram/credentials.py`

---

## Exception Hierarchy

All exceptions inherit from `IngestionError` — callers can catch the entire family with one `except` clause.

```
IngestionError (base)
├── TokenExpiredError          # HTTP 401 — mark expired, notify user
├── InsufficientScopeError     # HTTP 403 — skip resource, continue
│   └── .resource: str         # Which resource was denied
├── RateLimitError             # HTTP 429 — retries exhausted
│   └── .retry_after: float | None
├── PlatformUnavailableError   # HTTP 5xx after retries
│   └── .platform: str
└── NormalizationError         # Malformed API data — skip item, log
    └── .item_id: str
```

**Source:** `exceptions.py`

---

## Configuration (Pydantic Settings)

All config is environment-driven via `pydantic-settings`. Env prefix: `ENDPOINT_`, nested delimiter: `__`.

### `IngestionConfig` (top-level)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `teams` | `TeamsConfig` | `TeamsConfig()` | Teams adapter config |
| `moodle` | `MoodleConfig` | `MoodleConfig()` | Moodle adapter config |
| `telegram` | `TelegramConfig` | `TelegramConfig()` | Telegram adapter config |
| `adapter_version` | `str` | from `pyproject.toml` | Stamped into `RawPayloadEnvelope` |

### `TeamsConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `graph_base_url` | `str` | `https://graph.microsoft.com/v1.0` | Graph API base |
| `csa_base_url` | `str` | `https://teams.cloud.microsoft/api/csa/emea/api/v2` | CSA API base |
| `assignments_base_url` | `str` | `https://assignments.edu.cloud.microsoft` | Education Assignments API |
| `csa_page_size` | `int` | `50` | Messages per CSA page |
| `max_retries` | `int` | `3` | HTTP retry attempts |
| `request_timeout` | `int` | `30` | HTTP timeout (seconds) |
| `requests_per_second` | `float` | `10.0` | Rate limit |
| `max_concurrent_requests` | `int` | `5` | Semaphore concurrency cap |
| `skip_media_extensions` | `frozenset[str]` | `.mp4,.mov,.avi,.mkv,.wmv,.webm,.m4v` | Skip these file types |
| `semester_start` | `date \| None` | `None` | Fetch from this date; falls back to 90-day rolling window |

### `MoodleConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `base_url` | `str` | `https://elearning.constructor.university` | Moodle instance URL |
| `request_timeout` | `int` | `30` | HTTP timeout (seconds) |
| `max_retries` | `int` | `3` | HTTP retry attempts |
| `requests_per_second` | `float` | `10.0` | Rate limit |
| `max_concurrent_requests` | `int` | `5` | Semaphore concurrency cap |
| `include_grades` | `bool` | `False` | Include grade data |
| `forum_scope` | `str` | `"announcements"` | `"announcements"` or `"all"` |
| `skip_media_extensions` | `frozenset[str]` | (same as Teams) | Skip these file types |
| `semester_start` | `date \| None` | `None` | Fetch from this date; falls back to 90-day rolling window |

### `TelegramConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `api_base_url` | `str` | `https://api.telegram.org` | Telegram Bot API base |
| `poll_timeout` | `int` | `0` | Long-poll timeout (0 = short poll) |
| `max_updates` | `int` | `100` | Max updates per `getUpdates` call |
| `request_timeout` | `int` | `30` | HTTP timeout (seconds) |
| `max_retries` | `int` | `3` | HTTP retry attempts |
| `requests_per_second` | `float` | `1.0` | Rate limit (Telegram is strict) |
| `max_concurrent_requests` | `int` | `5` | Semaphore concurrency cap |

**Source:** `config.py`

---

## Shared Normalizer Helpers (normalize.py)

Utility functions used by all three platform normalizers.

| Function | Signature | Description |
|----------|-----------|-------------|
| `strip_html` | `(html: str) -> str` | Remove HTML tags via regex, return plain text |
| `parse_iso_datetime` | `(value: str \| None) -> datetime \| None` | Parse ISO 8601, handles `Z` suffix |
| `timestamp_to_datetime` | `(ts: int \| None) -> datetime \| None` | Unix timestamp to UTC datetime. Returns `None` for `0` (Moodle uses 0 = "not set") |
| `make_envelope` | `(source: str, original: dict, adapter_version: str) -> dict` | Wrap raw payload in `RawPayloadEnvelope` and serialize to dict |

**Source:** `normalize.py`

---

## Mock Examples

### Moodle Assignment

```json
{
  "external_id": "moodle_assign_5897",
  "platform": "moodle",
  "content_type": "assignment",
  "title": "Homework 3: Finite State Machines",
  "body": "Design a Moore FSM that detects the sequence 1011 in a serial input stream.",
  "course_name": "DSCA Tutorial",
  "course_external_id": "9273",
  "author": null,
  "published_at": "2026-02-28T15:32:12+00:00",
  "deadline": "2026-03-20T23:59:00+00:00",
  "raw_payload": {
    "schema_version": "2",
    "source": "moodle_assign",
    "ingestion_mode": "server_poll",
    "adapter_version": "2.1.0",
    "fetched_at": "2026-03-13T14:30:00+00:00",
    "original": { "id": 5897, "name": "...", "intro": "...", "duedate": 1742515140 }
  },
  "attachments": [],
  "ingestion_mode": "server_poll"
}
```

### Teams Chat Message (CSA)

```json
{
  "external_id": "teams_csa_1710345600001",
  "platform": "teams",
  "content_type": "raw_message",
  "title": null,
  "body": "Reminder: group project proposal is due this Sunday.",
  "course_name": "CS3401 Machine Learning",
  "course_external_id": "19:abc123@thread.tacv2",
  "author": "Example Instructor",
  "published_at": "2026-03-12T16:00:00+00:00",
  "deadline": null,
  "raw_payload": {
    "schema_version": "2",
    "source": "teams_csa",
    "ingestion_mode": "server_poll",
    "adapter_version": "2.1.0",
    "fetched_at": "2026-03-13T14:35:00+00:00",
    "original": { "id": "1710345600001", "imDisplayName": "Example Instructor", "..." : "..." }
  },
  "attachments": [],
  "ingestion_mode": "server_poll"
}
```

### Teams Assignment (Education API)

```json
{
  "external_id": "teams_assignment_a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "platform": "teams",
  "content_type": "assignment",
  "title": "Lab 5: Decision Trees & Random Forests",
  "body": "Implement a decision tree classifier from scratch.",
  "course_name": "CS3401 Machine Learning",
  "course_external_id": "d4e5f6a7-b8c9-0123-4567-89abcdef0123",
  "author": null,
  "published_at": "2026-03-10T08:00:00+00:00",
  "deadline": "2026-03-21T23:59:00+00:00",
  "raw_payload": { "schema_version": "2", "source": "teams_edu", "..." : "..." },
  "attachments": [
    {
      "external_id": "drive_item_456def",
      "filename": "wine_quality.csv",
      "download_url": "https://graph.microsoft.com/v1.0/drives/b!xyz/items/456def/content",
      "content_type": "text/csv",
      "size_bytes": 264000,
      "is_media": false
    }
  ],
  "ingestion_mode": "server_poll"
}
```

### Telegram Message

```json
{
  "external_id": "tg_-100123_456",
  "platform": "telegram",
  "content_type": "raw_message",
  "title": null,
  "body": "Office hours moved to Thursday this week.",
  "course_name": "CS3401 Study Group",
  "course_external_id": "-100123",
  "author": "Example Instructor",
  "published_at": "2026-03-12T09:15:00+00:00",
  "deadline": null,
  "raw_payload": { "schema_version": "2", "source": "telegram_message", "..." : "..." },
  "attachments": [],
  "ingestion_mode": "server_poll"
}
```

### Full FetchResult (2 items)

```json
{
  "items": [
    { "external_id": "moodle_assign_5897", "..." : "..." },
    { "external_id": "moodle_forum_44201", "..." : "..." }
  ],
  "files": [
    {
      "external_id": "moodle_file_30412_DSCA_Lecture9.pdf",
      "filename": "DSCA_Lecture9_Sequential_Logic.pdf",
      "download_url": "https://moodle.uni.edu/...",
      "content_type": "application/pdf",
      "size_bytes": 4812800,
      "is_media": false
    }
  ],
  "cursor": { "last_sync_at": "2026-03-13T14:30:00+00:00" },
  "errors": [],
  "stats": {
    "items_fetched": 4,
    "items_new": 2,
    "items_updated": 1,
    "items_skipped": 1,
    "errors": 0,
    "duration_seconds": 3.42,
    "api_call_duration_seconds": 2.81,
    "normalization_duration_seconds": 0.04,
    "per_scope_stats": {
      "9273": { "items_fetched": 4, "items_new": 2, "items_updated": 1, "items_skipped": 1, "errors": 0 }
    }
  }
}
```
