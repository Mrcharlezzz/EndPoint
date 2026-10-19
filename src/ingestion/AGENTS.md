# Ingestion — Agent Rules

Read `docs/ingestion/CONVENTIONS.md` for naming and patterns. This file covers what will break silently.

## When Writing Code

The pipeline (`pipeline.py`) MUST NEVER import an adapter. Callers inject adapters via Protocol interfaces from `ports.py`.

Utility code in `scripts/` is not part of the production package surface and is excluded from Ruff's default component-wide lint/format gate. Production-quality checks target `ingestion/` and the test suite.

Teams uses 3 separate APIs in parallel — Graph (channels, files, metadata), CSA (messages, threads), Education (assignments). Do NOT try to fetch everything from one API.

Moodle returns errors INSIDE 200 OK responses. The words `invalidtoken` and `accessexception` in the response body are errors, not data. This is handled in `_parse_response()` — do not bypass it.

Telegram `getUpdates` is long-polling, not a standard REST call. The `offset` parameter acknowledges previous messages — sending the wrong offset loses messages permanently.

## Cursor Invariant

`run_sync()` saves cursors ONLY on full successful completion. Violating this causes data loss on retry — users re-fetch nothing or skip messages. Do NOT advance cursors on partial failure.

## Concurrency

Bound all concurrent requests with `asyncio.Semaphore` via `self._semaphore`. Never fire unbounded `asyncio.gather()`. Always use `return_exceptions=True` so one failure doesn't cancel siblings.

## Context Injection

Adapters inject metadata into raw dicts with underscore-prefixed keys before normalization:
```python
# RIGHT — spread, prefixed
{**raw, "_course_name": name, "_course_id": cid}

# WRONG — mutates original, no prefix
raw["course_name"] = name
```

## External ID Formats

These are unique keys per platform. Breaking the format breaks deduplication:
- Moodle: `moodle_{type}_{id}` — e.g. `moodle_assign_5897`
- Teams CSA: `teams_csa_{messageId}`
- Teams Graph: `teams_graph_{messageId}`
- Teams Edu: `teams_assignment_{uuid}`
- Telegram: `tg_{chatId}_{messageId}`

## BaseHTTPClient

Template method. Override ONLY `_parse_response()`, `_parse_retry_after()`, `_platform_name`. Do NOT override `request()` or `_retry_loop()`.

Inject `sleep_func` in constructors for testability — never `time.sleep` or `asyncio.sleep` directly in non-test code.

## When Writing Tests

Use `respx` for HTTP mocking — not `unittest.mock.patch`.
Inject `noop_sleep` to skip delays.
No real network calls. Ever.
For CLI testing, use `--extra cli`: `uv sync --extra cli`.
