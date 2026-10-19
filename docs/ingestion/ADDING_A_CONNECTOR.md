# Adding a New Connector

> Step-by-step guide for adding a new platform source to the ingestion pipeline.
> Follow this checklist in order.

---

## 1. Create the adapter directory

```
src/ingestion/adapters/{platform}/
├── __init__.py
├── adapter.py          # {Platform}Adapter(BaseAdapter)
├── normalizer.py       # {Platform}Normalizer
├── credentials.py      # {Platform}Credentials (TypedDict)
└── client.py           # {Platform}Client(BaseHTTPClient) — if needed
```

---

## 2. Define credentials (`credentials.py`)

```python
from __future__ import annotations
from typing import TypedDict

class CanvasCredentials(TypedDict):
    api_token: str
    base_url: str
```

Keep it minimal: only what the adapter needs to authenticate.

---

## 3. Create the HTTP client (`client.py`) — if needed

Only create a custom client if the platform has non-standard response parsing.
For standard Bearer-token JSON APIs, use `AsyncHTTPClient` directly.

```python
from __future__ import annotations
from typing import Any
import httpx
from ingestion.clients.base import BaseHTTPClient

class CanvasClient(BaseHTTPClient):
    """Canvas LMS API client."""

    @property
    def _platform_name(self) -> str:
        return "canvas"

    def _parse_response(self, response: httpx.Response, *, url: str) -> Any:
        """Override if the platform has quirks (error-in-200, envelope wrapping)."""
        # Canvas returns standard JSON on 200
        return response.json()
```

Override `_parse_response()` for platforms that:
- Return errors with 200 status (Moodle does this)
- Wrap responses in envelopes (Telegram does `{ok: true, result: ...}`)

Override `_parse_retry_after()` if 429 responses use non-standard retry info.

---

## 4. Write the normalizer (`normalizer.py`)

```python
from __future__ import annotations
from ingestion.normalize import make_envelope, strip_html, parse_iso_datetime
from ingestion.schemas import ContentType, FileRef, NormalizedItem

class CanvasNormalizer:
    """Convert raw Canvas API dicts to NormalizedItem."""

    def __init__(self, adapter_version: str = "") -> None:
        self._adapter_version = adapter_version

    def normalize(self, raw: dict, **context: object) -> NormalizedItem:
        source = str(context.get("source", ""))
        if source == "assignment":
            return self._normalize_assignment(raw)
        raise ValueError(f"Unknown source: {source!r}")

    def _normalize_assignment(self, raw: dict) -> NormalizedItem:
        return NormalizedItem(
            external_id=f"canvas_assign_{raw.get('id', '')}",
            platform="canvas",
            content_type=ContentType.ASSIGNMENT,
            title=raw.get("name"),
            body=strip_html(raw.get("description", "")),
            course_name=str(raw.get("_course_name", "")),
            course_external_id=str(raw.get("_course_id", "")),
            published_at=parse_iso_datetime(raw.get("updated_at")),
            deadline=parse_iso_datetime(raw.get("due_date")),
            raw_payload=make_envelope("canvas_assign", raw, self._adapter_version),
        )
```

**Rules:**
- Use `make_envelope()` for `raw_payload` — wraps in `RawPayloadEnvelope`
- Use `strip_html()` for `body` — always plain text
- Use `parse_iso_datetime()` or `timestamp_to_datetime()` for dates
- Prefix `external_id` with `{platform}_{type}_{id}`
- Inject course context via `_`-prefixed keys in the raw dict

---

## 5. Write the adapter (`adapter.py`)

```python
from __future__ import annotations
import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ingestion.adapters.base import BaseAdapter
from ingestion.clients.http import AsyncHTTPClient
from ingestion.schemas import (
    ErrorCode, FetchError, FetchResult, FetchScope,
    NormalizedItem, ScopeStats, SyncStats,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from ingestion.ports import ProgressCallback

logger = logging.getLogger(__name__)


class CanvasAdapter(BaseAdapter):
    platform = "canvas"
    _supported_scopes = frozenset({FetchScope.ASSIGNMENTS, FetchScope.FILES})
    _required_credential_keys = ("api_token", "base_url")

    def __init__(self, config, sleep_func=None):
        super().__init__(
            max_concurrent_requests=config.max_concurrent_requests,
            sleep_func=sleep_func,
        )
        self._config = config
        self._normalizer = CanvasNormalizer(adapter_version="")

    async def fetch(
        self,
        credentials: dict,
        cursor: dict | None = None,
        scope: frozenset[FetchScope] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> FetchResult:
        # 1. Validate credentials
        validation_error = self._validate_credentials(credentials)
        if validation_error:
            return FetchResult(errors=[validation_error])

        # 2. Resolve scopes
        effective_scopes = self._resolve_scopes(scope)
        if not effective_scopes:
            return FetchResult()

        # 3. Build client, fetch, normalize
        all_items: list[NormalizedItem] = []
        all_errors: list[FetchError] = []
        start_time = time.monotonic()

        client = AsyncHTTPClient(
            base_url=credentials["base_url"],
            token=credentials["api_token"],
            max_retries=self._config.max_retries,
            timeout=self._config.request_timeout,
            requests_per_second=self._config.requests_per_second,
            sleep_func=self._sleep_func,
        )

        try:
            # ... platform-specific fetch logic ...
            # Use self._semaphore for concurrent operations
            # Use asyncio.gather() for parallel independent work
            # Collect errors in all_errors, don't raise
            pass
        finally:
            await client.close()

        duration = time.monotonic() - start_time
        return FetchResult(
            items=all_items,
            cursor={"last_sync_at": datetime.now(tz=UTC).isoformat()},
            errors=all_errors,
            stats=SyncStats(
                items_fetched=len(all_items),
                duration_seconds=duration,
            ),
        )

    async def test_connection(self, credentials: dict) -> bool:
        # Quick validation call (e.g. /api/v1/users/self)
        ...
```

**Checklist for the adapter:**
- [ ] Set `platform`, `_supported_scopes`, `_required_credential_keys`
- [ ] Inherit from `BaseAdapter`
- [ ] Call `self._validate_credentials()` first
- [ ] Call `self._resolve_scopes()` second
- [ ] Use `self._semaphore` for concurrent HTTP calls
- [ ] Collect errors in `list[FetchError]`, don't raise (except `TokenExpiredError`)
- [ ] Return `FetchResult` with items, cursor, errors, stats
- [ ] Close client in `finally` block
- [ ] Implement `test_connection()`

---

## 6. Add configuration (`config.py`)

Add a new config class in `src/ingestion/config.py`:

```python
class CanvasConfig(BaseSettings):
    base_url: str = "https://canvas.constructor.university"
    request_timeout: int = 30
    max_retries: int = 3
    requests_per_second: float = 10.0
    max_concurrent_requests: int = 5
    semester_start: date | None = None
```

Add it to `IngestionConfig`:

```python
class IngestionConfig(BaseSettings):
    teams: TeamsConfig = Field(default_factory=TeamsConfig)
    moodle: MoodleConfig = Field(default_factory=MoodleConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    canvas: CanvasConfig = Field(default_factory=CanvasConfig)  # NEW
    adapter_version: str = __version__
```

---

## 7. Add the `FetchScope` — if needed

If your platform introduces a new scope not already in the enum:

```python
# schemas.py
class FetchScope(StrEnum):
    MESSAGES = "messages"
    ASSIGNMENTS = "assignments"
    FILES = "files"
    EVENTS = "events"
    MATERIALS = "materials"
    FORUMS = "forums"
    GRADES = "grades"  # NEW — if Canvas needs it
```

Most platforms can use existing scopes.

---

## 8. Write tests

Create test files in `tests/`:

```
tests/
├── test_{platform}_adapter.py
├── test_{platform}_normalizer.py
└── test_{platform}_client.py       # if custom client exists
```

**Test patterns:**

```python
import pytest
import respx
from httpx import Response

@pytest.mark.asyncio
async def test_fetch_assignments(noop_sleep):
    config = CanvasConfig(base_url="https://canvas.test")
    adapter = CanvasAdapter(config, sleep_func=noop_sleep)

    with respx.mock:
        respx.get(url__eq="https://canvas.test/api/v1/courses").mock(
            return_value=Response(200, json=[...])
        )
        result = await adapter.fetch(
            {"api_token": "test", "base_url": "https://canvas.test"},
            scope=frozenset({FetchScope.ASSIGNMENTS}),
        )

    assert len(result.items) == 2
    assert result.items[0].platform == "canvas"
    assert result.errors == []
```

**Required test coverage:**
- [ ] Successful fetch with mock responses
- [ ] Empty response handling
- [ ] Token expired (401) → `TokenExpiredError`
- [ ] Rate limited (429) → retry behavior
- [ ] Invalid credentials → `FetchError` returned
- [ ] Normalization failure → item skipped, error collected
- [ ] `test_connection()` success and failure
- [ ] Scope resolution (supported vs unsupported scopes)

---

## 9. Update `__init__.py` exports

```python
# adapters/{platform}/__init__.py
from ingestion.adapters.canvas.adapter import CanvasAdapter

__all__ = ["CanvasAdapter"]
```

---

## 10. Pre-merge checklist

- [ ] All existing tests still pass (`pytest tests/ -v`)
- [ ] New adapter tests pass
- [ ] `ruff check src/ tests/` — clean
- [ ] `ruff format --check src/ tests/` — clean
- [ ] `pyright` — no new errors
- [ ] Adapter follows the error handling pattern (errors collected, not raised)
- [ ] All datetime fields are UTC
- [ ] `external_id` follows `{platform}_{type}_{id}` convention
- [ ] `raw_payload` uses `make_envelope()` wrapper
- [ ] Configuration has sensible defaults
- [ ] TypedDict credentials defined
- [ ] `_supported_scopes` is accurate
- [ ] `test_connection()` implemented
- [ ] No hardcoded URLs (use config)
- [ ] `sleep_func` injection for testability
