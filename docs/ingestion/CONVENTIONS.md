# Code Conventions

> Patterns, naming, error handling, and module boundaries extracted from the
> actual codebase. Not aspirational — this documents what exists.

---

## Module Boundaries

```
schemas.py          Pure data definitions. No I/O, no logic.
ports.py            Protocol interfaces only. No implementations.
exceptions.py       Exception hierarchy. No logic.
config.py           Pydantic Settings. Env-driven, no I/O.
normalize.py        Pure functions (strip_html, parse_datetime, make_envelope).
pipeline.py         Orchestrator. Calls ports, never imports adapters directly.
clients/base.py     HTTP infrastructure. Retry, backoff, rate limiting.
clients/http.py     Bearer-token HTTP client. Inherits base.
adapters/base.py    Shared adapter logic (scope resolution, credential validation).
adapters/{plat}/    Platform-specific: adapter, normalizer, client, credentials.
```

**Dependency rule:** Dependencies flow downward. Adapters depend on
schemas/clients/normalize. Pipeline depends on ports. Nothing depends on Django.
Circular imports are prevented by `TYPE_CHECKING` guards.

---

## Naming Conventions

### Files

| Pattern | Example | Purpose |
|---------|---------|---------|
| `adapter.py` | `teams/adapter.py` | Platform orchestrator |
| `normalizer.py` | `moodle/normalizer.py` | Raw → NormalizedItem |
| `credentials.py` | `telegram/credentials.py` | TypedDict for platform creds |
| `fetcher.py` / `*_fetcher.py` | `csa_fetcher.py`, `fetcher.py` | API call wrappers |
| `client.py` / `bot_client.py` | `client.py` | HTTP client subclass |
| `endpoints.py` | `teams/endpoints.py` | URL builder functions |

### Classes

| Pattern | Example |
|---------|---------|
| `{Platform}Adapter` | `TeamsAdapter`, `MoodleAdapter`, `TelegramAdapter` |
| `{Platform}Normalizer` | `TeamsNormalizer`, `MoodleNormalizer`, `TelegramNormalizer` |
| `{Platform}Credentials` | `TeamsCredentials` (TypedDict) |
| `{Platform}Config` | `TeamsConfig` (Pydantic Settings) |
| `Base{Thing}` | `BaseAdapter`, `BaseHTTPClient` |

### Functions

- Fetch functions: `fetch_{resource}()` — `fetch_channels()`, `fetch_assignments()`
- Extract functions: `_extract_{thing}()` — `_extract_materials()`, `_extract_files()`
- Normalize methods: `_normalize_{source}()` — `_normalize_csa_message()`, `_normalize_assignment()`
- URL builders: `graph_{resource}()`, `csa_{resource}()`, `edu_{resource}()`

### Variables

- `raw` / `raw_{things}` — unprocessed API response dicts
- `items` — `list[NormalizedItem]`
- `errors` — `list[FetchError]`
- `stats` — `ScopeStats`
- `creds` — typed credentials after cast
- `cursor` — opaque dict for incremental sync
- `on_progress` — optional `ProgressCallback`

### External IDs

| Platform | Pattern |
|----------|---------|
| Moodle | `moodle_{type}_{id}` — `moodle_assign_5897` |
| Teams CSA | `teams_csa_{messageId}` |
| Teams Graph | `teams_graph_{messageId}` |
| Teams Assignments | `teams_assignment_{uuid}` |
| Telegram | `tg_{chatId}_{messageId}` |
| Files (Moodle) | `moodle_file_{moduleId}_{filename}` |
| Files (Telegram) | `tg_file_{fileId}` |

---

## Design Patterns Used

### Template Method (BaseHTTPClient)

`BaseHTTPClient` provides the retry/backoff/rate-limit loop. Subclasses override:

| Method | Default | Moodle Override | Telegram Override |
|--------|---------|-----------------|-------------------|
| `_parse_response()` | `response.json()` | Detect error-in-200 body (`invalidtoken`, `accessexception`) | Unwrap `{ok: true, result: ...}` envelope |
| `_parse_retry_after()` | `Retry-After` header, fallback 5s | (default) | Read `retry_after` from JSON body |
| `_platform_name` | `"unknown"` | (base URL) | (base URL) |

### Ports and Adapters (Hexagonal)

The pipeline (`run_sync`) depends only on Protocol interfaces from `ports.py`.
Concrete implementations are injected by the caller (Django or CLI):

```python
await run_sync(
    adapter=teams_adapter,        # Adapter protocol
    credentials=creds_dict,
    content_sink=django_sink,     # ContentSink protocol
    sync_state=django_state,      # SyncStateStore protocol
    sync_logger=django_logger,    # SyncLogger protocol
    user_id="user123",
)
```

### BaseAdapter Shared Logic

`BaseAdapter` provides:
- `_resolve_scopes()` — filter requested scopes to supported set
- `_validate_credentials()` — check required keys present
- `_semaphore` — `asyncio.Semaphore` for concurrent request bounding
- Context manager support (`__aenter__`/`__aexit__`)

Each adapter sets class-level: `platform`, `_supported_scopes`, `_required_credential_keys`.

### Normalizer Dispatch

Each normalizer's `normalize()` dispatches by `source` context kwarg:

```python
def normalize(self, raw: dict, **context: object) -> NormalizedItem:
    source = str(context.get("source", ""))
    if source == "assignment":
        return self._normalize_assignment(raw)
    if source == "forum_discussion":
        return self._normalize_forum_discussion(raw)
    ...
```

### Context Injection Pattern

Adapters inject extra context into raw dicts before normalization using
underscore-prefixed keys:

```python
raw_with_context = {
    **raw,
    "_course_name": course_name,    # injected by adapter
    "_course_id": course_id,        # injected by adapter
}
item = normalizer.normalize(raw_with_context, source="assignment")
```

Normalizers read `_course_name`, `_course_id`, etc. from the dict. The
underscore prefix signals "this is adapter-injected, not from the API."

---

## Error Handling

### Exception Hierarchy

```
IngestionError          ← catch-all for the entire pipeline
├── TokenExpiredError   ← 401, non-recoverable
├── InsufficientScopeError ← 403, skip and continue
├── RateLimitError      ← 429 after retries exhausted
├── PlatformUnavailableError ← 5xx after retries
└── NormalizationError  ← bad data, skip item
```

### HTTP Error Strategy (BaseHTTPClient)

| Status | Action |
|--------|--------|
| 200 | Parse via `_parse_response()` template method |
| 401 | Raise `TokenExpiredError` immediately (no retry) |
| 403 | Raise `InsufficientScopeError` immediately (no retry) |
| 429 | Parse `Retry-After`, sleep, retry up to `max_retries` |
| 5xx | Exponential backoff (2^attempt), retry up to `max_retries` |
| ConnectError | Exponential backoff, retry up to `max_retries` |
| Timeout | Exponential backoff, retry up to `max_retries` |

### Adapter Error Strategy

- **Partial failures continue:** If one scope fails, others still run
- **Errors collected, not raised:** Each scope returns `list[FetchError]`
- **TokenExpiredError propagates:** If auth fails, the whole fetch stops
- **Normalization failures skip:** Individual items that fail normalization are
  skipped with a `FetchError` logged

### Pipeline Error Strategy

```python
try:
    result = await adapter.fetch(...)
except TokenExpiredError:
    # Mark token expired via TokenStore, return stats with error
except IngestionError:
    # Log, return stats with error
except Exception:
    # Log with traceback, return stats with error
```

The pipeline **never crashes** — it always returns `SyncStats`.

---

## Concurrency Pattern

All adapters use `asyncio.gather()` for parallel independent work, bounded by
`asyncio.Semaphore(max_concurrent_requests)`:

```python
# Parallel channel fetches, semaphore-bounded
async def fetch_one_channel(team_id, channel):
    async with self._semaphore:
        messages = await fetch_channel_messages(client, ...)
    return messages

tasks = [fetch_one_channel(tid, ch) for tid, ch in channel_pairs]
results = await asyncio.gather(*tasks, return_exceptions=True)
```

`return_exceptions=True` ensures one failure doesn't cancel other tasks.
Results are checked individually for exceptions vs successful returns.

---

## Immutability Patterns

- `FileRef` and `FetchError` are frozen Pydantic models (`model_config = ConfigDict(frozen=True)`)
- `RawPayloadEnvelope` is frozen
- `_supported_scopes` is `frozenset`
- `SKIP_MEDIA_EXTENSIONS` is `frozenset`
- Raw API dicts are never mutated — context is injected via `{**raw, "_key": value}` spread

`NormalizedItem`, `ScopeStats`, and `SyncStats` are **not frozen** — they are
built incrementally during processing. This is a pragmatic choice: freezing them
would require building the complete object in one shot, which complicates the
per-item processing loop.

---

## Import Conventions

### `TYPE_CHECKING` guard

Heavy imports used only for type annotations go behind `TYPE_CHECKING`:

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ingestion.adapters.teams.credentials import TeamsCredentials
    from ingestion.config import TeamsConfig
    from ingestion.ports import ProgressCallback
```

This prevents circular imports and reduces import-time overhead.

### `from __future__ import annotations`

Every module starts with this. Enables PEP 604 (`X | None`) and forward
reference syntax without runtime cost.

---

## Logging

- All modules use `logger = logging.getLogger(__name__)`
- Levels follow stdlib convention:
  - `DEBUG`: Connection test results, individual upserts
  - `INFO`: Sync start/complete, cursor saved, scope summaries
  - `WARNING`: Unsupported scope requested, fetch errors, rate limits, retries
  - `ERROR`: Token expired, sync failures
  - `EXCEPTION`: Unexpected errors (includes traceback)
- No `print()` statements in library code
- No structured logging (stdlib text logging only — deliberate choice for simplicity)

---

## Testing Conventions

- Tests use `pytest` + `pytest-asyncio` + `respx` (httpx mocking)
- Test files: `test_{module}.py` in `tests/`
- Fixtures in `conftest.py`: mock ports, fake adapters, sample data
- `noop_sleep` injected into clients/adapters to skip real delays
- `respx` for HTTP mocking — use `url__eq=` for exact URL matching
- No real network calls in tests
- Test naming: `test_{unit}_{scenario}` or `test_{behavior}`

---

## Configuration

- All config via `pydantic-settings` with env prefix `ENDPOINT_`
- Nested config: `ENDPOINT_TEAMS__GRAPH_BASE_URL=...`
- Sensible defaults for all fields (works out of the box for Constructor University)
- `semester_start` with 90-day rolling fallback for time-bounded fetches
- `sleep_func` injection in constructors for testability
