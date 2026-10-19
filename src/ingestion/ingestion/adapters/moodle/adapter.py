"""MoodleAdapter — orchestrates scope-based fetching from Moodle REST API.

Fetches course contents once and shares between materials/files handlers
to avoid duplicating API calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ingestion.adapters.base import BaseAdapter
from ingestion.adapters.moodle.client import MoodleRestClient
from ingestion.adapters.moodle.fetcher import (
    fetch_assignments,
    fetch_calendar_events,
    fetch_course_contents,
    fetch_forum_discussions,
    fetch_forums,
    fetch_site_info,
    fetch_user_courses,
)
from ingestion.adapters.moodle.normalizer import MoodleNormalizer
from ingestion.exceptions import IngestionError, TokenExpiredError
from ingestion.schemas import (
    ErrorCode,
    FetchError,
    FetchResult,
    FetchScope,
    FileRef,
    NormalizedItem,
    ScopeStats,
    SyncStats,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ingestion.adapters.moodle.credentials import MoodleCredentials
    from ingestion.config import MoodleConfig
    from ingestion.ports import ProgressCallback

logger = logging.getLogger(__name__)

_MATERIAL_MODNAMES = frozenset({"resource", "page", "folder", "url", "book", "label"})
type MoodleScopeResult = tuple[list[NormalizedItem], list[FetchError], ScopeStats]


def _is_assignment_due_calendar_event(raw: dict) -> bool:
    """Return true for Moodle calendar rows duplicated by the assignment endpoint."""
    return (
        raw.get("eventtype") == "due"
        and raw.get("modulename") == "assign"
        and raw.get("component") == "mod_assign"
    )


class MoodleAdapter(BaseAdapter):
    """Adapter for Moodle LMS via Web Services REST API."""

    platform = "moodle"
    _supported_scopes = frozenset(
        {
            FetchScope.ASSIGNMENTS,
            FetchScope.FORUMS,
            FetchScope.MATERIALS,
            FetchScope.EVENTS,
            FetchScope.FILES,
        }
    )
    _required_credential_keys = ("token", "user_id")

    def __init__(
        self,
        config: MoodleConfig,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(
            max_concurrent_requests=config.max_concurrent_requests,
            sleep_func=sleep_func,
        )
        self._config = config
        self._normalizer = MoodleNormalizer(
            adapter_version="",
            skip_media_extensions=config.skip_media_extensions,
            base_url=config.base_url,
        )
        self._client: MoodleRestClient | None = None

    async def fetch(
        self,
        credentials: dict,
        cursor: dict | None = None,
        scope: frozenset[FetchScope] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> FetchResult:
        """Fetch items from Moodle.

        credentials: {"token": str, "user_id": int}
        """
        validation_error = self._validate_credentials(credentials)
        if validation_error:
            return FetchResult(errors=[validation_error])

        effective_scopes = self._resolve_scopes(scope)
        if not effective_scopes:
            return FetchResult()

        creds: MoodleCredentials = credentials  # type: ignore[assignment]
        token = creds["token"]
        user_id = creds["user_id"]

        all_items: list[NormalizedItem] = []
        all_files: list[FileRef] = []
        all_errors: list[FetchError] = []
        scope_stats: dict[str, ScopeStats] = {}
        start_time = time.monotonic()

        self._client = self._make_client(token)

        try:
            if on_progress:
                on_progress("courses", "Fetching enrolled courses...")

            courses = await fetch_user_courses(self._client, user_id)
            course_ids: list[int] = []
            course_name_map: dict[int, str] = {}
            for course in courses:
                course_id = course.get("id")
                if not isinstance(course_id, int) or course_id <= 0:
                    continue
                course_ids.append(course_id)
                course_name_map[course_id] = str(course.get("fullname", ""))

            if on_progress:
                on_progress("courses", f"Found {len(courses)} courses")

            # Prefetch course contents once if materials or files scope is active
            needs_contents = {FetchScope.MATERIALS, FetchScope.FILES} & effective_scopes
            course_contents: dict[int, list[dict]] = {}
            if needs_contents:
                if on_progress:
                    on_progress("materials", "Prefetching course contents...")
                course_contents = await self._prefetch_course_contents(self._client, course_ids)
                if on_progress:
                    on_progress("materials", f"Prefetched {len(course_contents)} courses")

            # Run independent scopes in parallel
            tasks: list[asyncio.Task[MoodleScopeResult]] = []
            scope_names: list[str] = []

            if FetchScope.ASSIGNMENTS in effective_scopes:
                tasks.append(
                    asyncio.create_task(
                        self._fetch_assignments(self._client, course_ids, on_progress)
                    )
                )
                scope_names.append("assignments")

            if FetchScope.FORUMS in effective_scopes:
                tasks.append(
                    asyncio.create_task(
                        self._fetch_forums(self._client, course_ids, course_name_map, on_progress)
                    )
                )
                scope_names.append("forums")

            if FetchScope.EVENTS in effective_scopes:
                tasks.append(asyncio.create_task(self._fetch_events(self._client, on_progress)))
                scope_names.append("events")

            # Materials and files use prefetched data (no async needed)
            if FetchScope.MATERIALS in effective_scopes:
                items, errs, stats = self._extract_materials(course_contents, course_name_map)
                all_items.extend(items)
                all_errors.extend(errs)
                scope_stats["materials"] = stats
                if on_progress:
                    on_progress("materials", f"{stats.items_fetched} materials extracted")
                    if stats.items_fetched:
                        on_progress("+items", str(stats.items_fetched))

            if FetchScope.FILES in effective_scopes:
                files, errs, stats = self._extract_files(course_contents)
                all_files.extend(files)
                all_errors.extend(errs)
                scope_stats["files"] = stats
                if on_progress:
                    on_progress("files", f"{stats.items_fetched} files found")
                    if stats.items_fetched:
                        on_progress("+files", str(stats.items_fetched))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for name, result in zip(scope_names, results, strict=True):
                if isinstance(result, TokenExpiredError):
                    all_errors.append(
                        FetchError(
                            code=ErrorCode.TOKEN_EXPIRED,
                            message=str(result),
                            scope=name,
                            recoverable=False,
                        )
                    )
                elif isinstance(result, BaseException):
                    all_errors.append(
                        FetchError(
                            code=ErrorCode.UNKNOWN,
                            message=str(result),
                            scope=name,
                            recoverable=True,
                        )
                    )
                else:
                    items, errs, stats = result
                    all_items.extend(items)
                    all_errors.extend(errs)
                    scope_stats[name] = stats

        except TokenExpiredError as exc:
            all_errors.append(
                FetchError(
                    code=ErrorCode.TOKEN_EXPIRED,
                    message=str(exc),
                    recoverable=False,
                )
            )
        except IngestionError as exc:
            all_errors.append(
                FetchError(
                    code=ErrorCode.UNKNOWN,
                    message=str(exc),
                    recoverable=True,
                )
            )
        finally:
            await self._client.close()

        duration = time.monotonic() - start_time
        new_cursor: dict[str, object] = {"last_sync_at": datetime.now(tz=UTC).isoformat()}

        return FetchResult(
            items=all_items,
            files=all_files,
            cursor=new_cursor,
            errors=all_errors,
            stats=SyncStats(
                items_fetched=len(all_items),
                duration_seconds=duration,
                per_scope_stats=scope_stats,
            ),
        )

    async def test_connection(self, credentials: dict) -> bool:
        """Validate Moodle token by calling core_webservice_get_site_info."""
        token = credentials.get("token", "")
        if not token:
            return False
        client = self._make_client(token)
        try:
            info = await fetch_site_info(client)
            return bool(info.get("userid"))
        except Exception:
            logger.debug("Moodle test_connection failed", exc_info=True)
            return False
        finally:
            await client.close()

    async def __aexit__(self, *args: object) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    # --- Private methods ---

    async def _prefetch_course_contents(
        self,
        client: MoodleRestClient,
        course_ids: list[int],
    ) -> dict[int, list[dict]]:
        """Fetch course contents for all courses once. Used by materials + files."""
        contents: dict[int, list[dict]] = {}

        async def fetch_one(course_id: int) -> None:
            try:
                async with self._semaphore:
                    sections = await fetch_course_contents(client, course_id)
                contents[course_id] = sections
            except IngestionError as exc:
                logger.warning("Failed to fetch contents for course %d: %s", course_id, exc)
                contents[course_id] = []

        tasks = [fetch_one(cid) for cid in course_ids]
        await asyncio.gather(*tasks)
        return contents

    async def _fetch_assignments(
        self,
        client: MoodleRestClient,
        course_ids: list[int],
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[NormalizedItem], list[FetchError], ScopeStats]:
        items: list[NormalizedItem] = []
        errors: list[FetchError] = []
        stats = ScopeStats()

        if on_progress:
            on_progress("assignments", "Fetching assignments...")

        async with self._semaphore:
            raw_assignments = await fetch_assignments(client, course_ids)

        if on_progress:
            on_progress("assignments", f"{len(raw_assignments)} assignments fetched")

        for raw in raw_assignments:
            try:
                item = self._normalizer.normalize(raw, source="assignment")
                items.append(item)
                stats.items_fetched += 1
            except Exception as exc:
                errors.append(
                    FetchError(
                        code=ErrorCode.NORMALIZATION_ERROR,
                        message=str(exc),
                        scope=f"assignments/{raw.get('_course_id', '')}",
                    )
                )
                stats.errors += 1

        if on_progress and stats.items_fetched:
            on_progress("+items", str(stats.items_fetched))

        return items, errors, stats

    async def _fetch_forums(
        self,
        client: MoodleRestClient,
        course_ids: list[int],
        course_name_map: dict[int, str],
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[NormalizedItem], list[FetchError], ScopeStats]:
        items: list[NormalizedItem] = []
        errors: list[FetchError] = []
        stats = ScopeStats()

        if on_progress:
            on_progress("forums", "Fetching forums...")

        async with self._semaphore:
            forums = await fetch_forums(client, course_ids, forum_scope=self._config.forum_scope)

        if on_progress:
            on_progress("forums", f"{len(forums)} forums found")

        for forum in forums:
            forum_id_raw = forum.get("id")
            forum_id = forum_id_raw if isinstance(forum_id_raw, int) else 0
            forum_name = str(forum.get("name", ""))
            course_id_raw = forum.get("course")
            course_id = course_id_raw if isinstance(course_id_raw, int) else 0
            # Fixed: use course_name_map from actual courses, not forums list
            course_name = course_name_map.get(course_id, forum_name)

            if forum_id <= 0:
                errors.append(
                    FetchError(
                        code=ErrorCode.UNKNOWN,
                        message=f"Forum {forum_name}: missing numeric id",
                        scope="forums/unknown",
                        recoverable=True,
                    )
                )
                stats.errors += 1
                continue

            try:
                async with self._semaphore:
                    discussions = await fetch_forum_discussions(client, forum_id)
            except IngestionError as exc:
                errors.append(
                    FetchError(
                        code=ErrorCode.UNKNOWN,
                        message=f"Forum {forum_name}: {exc}",
                        scope=f"forums/{forum_id}",
                        recoverable=True,
                    )
                )
                stats.errors += 1
                continue

            if on_progress:
                on_progress("forums", f"  {forum_name}: {len(discussions)} discussions")
                if discussions:
                    on_progress("+items", str(len(discussions)))

            for raw in discussions:
                raw_with_context = {
                    **raw,
                    "_course_name": course_name,
                    "_forum_name": forum_name,
                    "_course_id": course_id,
                }
                try:
                    item = self._normalizer.normalize(raw_with_context, source="forum_discussion")
                    items.append(item)
                    stats.items_fetched += 1
                except Exception as exc:
                    errors.append(
                        FetchError(
                            code=ErrorCode.NORMALIZATION_ERROR,
                            message=str(exc),
                            scope=f"forums/{forum_id}",
                        )
                    )
                    stats.errors += 1

        return items, errors, stats

    async def _fetch_events(
        self,
        client: MoodleRestClient,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[NormalizedItem], list[FetchError], ScopeStats]:
        items: list[NormalizedItem] = []
        errors: list[FetchError] = []
        stats = ScopeStats()

        if on_progress:
            on_progress("events", "Fetching calendar events...")

        earliest = self._config.get_earliest_fetch_date()
        time_from = int(earliest.timestamp())

        async with self._semaphore:
            raw_events = await fetch_calendar_events(client, time_from)

        if on_progress:
            on_progress("events", f"{len(raw_events)} events fetched")
            if raw_events:
                on_progress("+items", str(len(raw_events)))

        for raw in raw_events:
            if _is_assignment_due_calendar_event(raw):
                stats.items_skipped += 1
                continue

            try:
                item = self._normalizer.normalize(raw, source="event")
                items.append(item)
                stats.items_fetched += 1
            except Exception as exc:
                errors.append(
                    FetchError(
                        code=ErrorCode.NORMALIZATION_ERROR,
                        message=str(exc),
                        scope=f"events/{raw.get('id', '')}",
                    )
                )
                stats.errors += 1

        return items, errors, stats

    def _extract_materials(
        self,
        course_contents: dict[int, list[dict]],
        course_name_map: dict[int, str],
    ) -> tuple[list[NormalizedItem], list[FetchError], ScopeStats]:
        """Extract materials from prefetched course contents (sync, no API calls)."""
        items: list[NormalizedItem] = []
        errors: list[FetchError] = []
        stats = ScopeStats()

        for course_id, sections in course_contents.items():
            course_name = course_name_map.get(course_id, "")
            for section in sections:
                for module in section.get("modules", []):
                    modname = module.get("modname", "")
                    if modname not in _MATERIAL_MODNAMES:
                        continue

                    raw_with_context = {
                        **module,
                        "_course_name": course_name,
                        "_course_id": course_id,
                    }
                    try:
                        item = self._normalizer.normalize(raw_with_context, source="material")
                        items.append(item)
                        stats.items_fetched += 1
                    except Exception as exc:
                        errors.append(
                            FetchError(
                                code=ErrorCode.NORMALIZATION_ERROR,
                                message=str(exc),
                                scope=f"materials/{course_id}/{module.get('id', '')}",
                            )
                        )
                        stats.errors += 1

        return items, errors, stats

    def _extract_files(
        self,
        course_contents: dict[int, list[dict]],
    ) -> tuple[list[FileRef], list[FetchError], ScopeStats]:
        """Extract file refs from prefetched course contents (sync, no API calls)."""
        files: list[FileRef] = []
        errors: list[FetchError] = []
        stats = ScopeStats()

        for _course_id, sections in course_contents.items():
            for section in sections:
                for module in section.get("modules", []):
                    module_id = module.get("id", 0)
                    for content in module.get("contents", []):
                        if not isinstance(content, dict):
                            continue
                        if content.get("type") != "file":
                            continue
                        files.append(self._normalizer.make_file_ref(content, module_id=module_id))
                        stats.items_fetched += 1

        return files, errors, stats

    def _make_client(self, token: str) -> MoodleRestClient:
        return MoodleRestClient(
            base_url=self._config.base_url,
            token=token,
            max_retries=self._config.max_retries,
            timeout=self._config.request_timeout,
            requests_per_second=self._config.requests_per_second,
            sleep_func=self._sleep_func,
        )
