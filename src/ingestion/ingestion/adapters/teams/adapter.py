"""TeamsAdapter — orchestrates scope-based fetching from Teams APIs.

CSA and Graph are complementary data sources, not fallbacks:
- CSA: message content (chat messages, reactions, threads)
- Graph: structural data (channels, files)
Both run in parallel when both tokens are available.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from ingestion.adapters.base import BaseAdapter
from ingestion.adapters.teams.assignments_fetcher import (
    fetch_assignment_resources,
    fetch_class_assignments,
)
from ingestion.adapters.teams.csa_fetcher import fetch_channel_messages
from ingestion.adapters.teams.endpoints import graph_me
from ingestion.adapters.teams.graph_fetcher import (
    fetch_channel_messages_graph,
    fetch_channels,
    fetch_drive_items_delta,
    fetch_joined_teams,
    fetch_team_drives,
)
from ingestion.adapters.teams.normalizer import TeamsNormalizer
from ingestion.clients.http import AsyncHTTPClient
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

    from ingestion.adapters.teams.credentials import TeamsCredentials
    from ingestion.config import TeamsConfig
    from ingestion.ports import ProgressCallback

logger = logging.getLogger(__name__)
type TeamsItemResult = tuple[list[NormalizedItem], list[FetchError], ScopeStats]
type TeamsFileResult = tuple[list[FileRef], list[FetchError], ScopeStats]
type TeamsScopeResult = TeamsItemResult | TeamsFileResult


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes / (1024 * 1024):.1f}MB"


def _short_name(name: str, max_len: int = 30) -> str:
    return name[:max_len] + "..." if len(name) > max_len else name


class TeamsAdapter(BaseAdapter):
    """Adapter for Microsoft Teams (CSA + Graph APIs)."""

    platform = "teams"
    _supported_scopes = frozenset({FetchScope.MESSAGES, FetchScope.FILES, FetchScope.ASSIGNMENTS})
    _required_credential_keys = ("graph_token", "team_ids")

    def __init__(
        self,
        config: TeamsConfig,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(
            max_concurrent_requests=config.max_concurrent_requests,
            sleep_func=sleep_func,
        )
        self._config = config
        self._normalizer = TeamsNormalizer(
            adapter_version="",
            skip_media_extensions=config.skip_media_extensions,
        )
        self._graph_client: AsyncHTTPClient | None = None
        self._csa_client: AsyncHTTPClient | None = None

    async def fetch(
        self,
        credentials: dict,
        cursor: dict | None = None,
        scope: frozenset[FetchScope] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> FetchResult:
        """Fetch items from Teams.

        credentials: {"graph_token": str, "csa_token": str|None, "team_ids": list[str]}
        """
        validation_error = self._validate_credentials(credentials)
        if validation_error:
            return FetchResult(errors=[validation_error])

        effective_scopes = self._resolve_scopes(scope)
        if not effective_scopes:
            return FetchResult()

        creds: TeamsCredentials = credentials  # type: ignore[assignment]
        graph_token = creds["graph_token"]
        csa_token = creds.get("csa_token")
        team_ids = creds["team_ids"]

        all_items: list[NormalizedItem] = []
        all_files: list[FileRef] = []
        all_errors: list[FetchError] = []
        scope_stats: dict[str, ScopeStats] = {}
        start_time = time.monotonic()

        self._graph_client = self._make_client(self._config.graph_base_url, graph_token)
        self._csa_client = (
            self._make_client(self._config.csa_base_url, csa_token) if csa_token else None
        )

        try:
            # Phase 1: Discover teams and build name map
            team_name_map = await self._discover_teams(
                self._graph_client,
                team_ids,
                on_progress,
            )

            # Phase 2: Run all scopes in parallel
            tasks: list[asyncio.Task[TeamsScopeResult]] = []
            scope_names: list[str] = []

            if FetchScope.MESSAGES in effective_scopes:
                tasks.append(
                    asyncio.create_task(
                        self._fetch_messages(
                            self._graph_client,
                            self._csa_client,
                            team_ids,
                            team_name_map,
                            cursor,
                            on_progress,
                        )
                    )
                )
                scope_names.append("messages")

            if FetchScope.FILES in effective_scopes:
                tasks.append(
                    asyncio.create_task(
                        self._fetch_files(
                            self._graph_client,
                            team_ids,
                            team_name_map,
                            on_progress,
                        )
                    )
                )
                scope_names.append("files")

            if FetchScope.ASSIGNMENTS in effective_scopes:
                tasks.append(
                    asyncio.create_task(
                        self._fetch_assignments(
                            self._graph_client,
                            graph_token,
                            team_ids,
                            team_name_map,
                            on_progress,
                        )
                    )
                )
                scope_names.append("assignments")

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
                    if on_progress:
                        on_progress("errors", f"TOKEN EXPIRED ({name})")
                        on_progress("+errors", "1")
                elif isinstance(result, BaseException):
                    all_errors.append(
                        FetchError(
                            code=ErrorCode.UNKNOWN,
                            message=str(result),
                            scope=name,
                            recoverable=True,
                        )
                    )
                    if on_progress:
                        on_progress("errors", f"{name}: {result!s:.60}")
                        on_progress("+errors", "1")
                elif name == "files":
                    files, errs, stats = cast("TeamsFileResult", result)
                    all_files.extend(files)
                    all_errors.extend(errs)
                    scope_stats[name] = stats
                else:
                    items, errs, stats = cast("TeamsItemResult", result)
                    all_items.extend(items)
                    all_errors.extend(errs)
                    scope_stats[name] = stats

        finally:
            await self._graph_client.close()
            if self._csa_client:
                await self._csa_client.close()

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
        """Validate Graph token by calling /me."""
        graph_token = credentials.get("graph_token", "")
        if not graph_token:
            return False
        client = self._make_client(self._config.graph_base_url, graph_token)
        try:
            await client.get(graph_me(base_url=self._config.graph_base_url))
            return True
        except Exception:
            logger.debug("Teams test_connection failed", exc_info=True)
            return False
        finally:
            await client.close()

    async def __aexit__(self, *args: object) -> None:
        if self._graph_client:
            await self._graph_client.close()
            self._graph_client = None
        if self._csa_client:
            await self._csa_client.close()
            self._csa_client = None

    # --- Discovery ---

    async def _discover_teams(
        self,
        graph_client: AsyncHTTPClient,
        team_ids: list[str],
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, str]:
        """Fetch team display names and report summary via progress."""
        if on_progress:
            on_progress("teams", f"Discovering {len(team_ids)} teams...")

        team_name_map: dict[str, str] = {}
        try:
            all_teams = await fetch_joined_teams(
                graph_client,
                base_url=self._config.graph_base_url,
            )
            for t in all_teams:
                tid = str(t.get("id", ""))
                name = str(t.get("displayName", tid[:12]))
                if tid in team_ids:
                    team_name_map[tid] = name
        except Exception:
            logger.debug("Team discovery failed, falling back to short IDs", exc_info=True)
            for tid in team_ids:
                team_name_map[tid] = tid[:12]

        if on_progress:
            on_progress("teams", f"{len(team_name_map)} teams matched:")
            for name in list(team_name_map.values())[:20]:
                on_progress("teams", f"  {_short_name(name, 50)}")

        return team_name_map

    # --- Messages (parallelized per-channel) ---

    async def _fetch_messages(
        self,
        graph_client: AsyncHTTPClient,
        csa_client: AsyncHTTPClient | None,
        team_ids: list[str],
        team_name_map: dict[str, str],
        cursor: dict | None,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[NormalizedItem], list[FetchError], ScopeStats]:
        items: list[NormalizedItem] = []
        errors: list[FetchError] = []
        stats = ScopeStats()

        # Phase 1: Discover channels across all teams (parallel)
        if on_progress:
            on_progress("messages", "Discovering channels...")

        async def fetch_team_channels(
            team_id: str,
        ) -> tuple[str, list[dict]]:
            async with self._semaphore:
                channels = await fetch_channels(
                    graph_client,
                    team_id,
                    base_url=self._config.graph_base_url,
                )
            return team_id, channels

        channel_tasks = [fetch_team_channels(tid) for tid in team_ids]
        channel_results = await asyncio.gather(*channel_tasks, return_exceptions=True)

        # Build (team_id, channel) pairs and track General channel per team
        all_channel_pairs: list[tuple[str, dict]] = []
        general_channel_map: dict[str, str] = {}  # team_id -> General channel thread ID

        for result in channel_results:
            if isinstance(result, TokenExpiredError):
                raise result
            if isinstance(result, BaseException):
                errors.append(
                    FetchError(
                        code=ErrorCode.UNKNOWN,
                        message=str(result),
                        scope="messages/channels",
                        recoverable=True,
                    )
                )
                stats.errors += 1
                if on_progress:
                    on_progress("errors", f"channels: {result!s:.60}")
                    on_progress("+errors", "1")
                continue
            team_id, channels = result
            if not channels:
                team_name = team_name_map.get(team_id, team_id[:12])
                if on_progress:
                    on_progress("errors", f"[{_short_name(team_name, 20)}] no channel access (403)")
                    on_progress("+errors", "1")
                errors.append(
                    FetchError(
                        code=ErrorCode.UNKNOWN,
                        message=f"[{team_name}] Graph denied channel listing (403)",
                        scope=f"messages/channels/{team_id}",
                        recoverable=True,
                    )
                )
                stats.errors += 1
                continue

            # Find General channel for CSA thread_id
            for ch in channels:
                if ch.get("displayName") == "General":
                    general_channel_map[team_id] = ch.get("id", "")
                    break
            # Fallback: use first channel as team thread ID
            if team_id not in general_channel_map:
                general_channel_map[team_id] = channels[0].get("id", "")

            for ch in channels:
                all_channel_pairs.append((team_id, ch))

        total_channels = len(all_channel_pairs)
        if on_progress:
            on_progress("messages", f"Found {total_channels} channels across {len(team_ids)} teams")

        # Phase 2: Fetch messages from all channels (parallel, semaphore-bounded)
        done_count = 0

        async def fetch_one_channel(
            team_id: str,
            channel: dict,
        ) -> tuple[list[NormalizedItem], list[FetchError], int]:
            nonlocal done_count
            ch_items: list[NormalizedItem] = []
            ch_errors: list[FetchError] = []
            channel_id = str(channel.get("id") or "")
            channel_name = str(channel.get("displayName") or "?")
            team_name = team_name_map.get(team_id, team_id[:12])
            short_team = _short_name(team_name, 20)

            # CSA requires the General channel's ID as the team thread_id
            team_thread_id = general_channel_map.get(team_id) or channel_id

            # Try CSA first (if available), fall back to Graph API
            raw_messages: list[dict] = []
            source = "graph_message"

            if csa_client:
                if on_progress:
                    on_progress("messages", f"[{short_team}] #{channel_name}: fetching (CSA)...")
                try:
                    async with self._semaphore:
                        raw_messages, _ = await fetch_channel_messages(
                            csa_client,
                            team_thread_id,
                            channel_id,
                            page_size=self._config.csa_page_size,
                            base_url=self._config.csa_base_url,
                        )
                    source = "csa"
                except IngestionError:
                    raw_messages = []  # will fall through to Graph

            if not raw_messages:
                if on_progress:
                    tag = "Graph fallback" if csa_client else "Graph"
                    on_progress(
                        "messages",
                        f"[{short_team}] #{channel_name}: fetching ({tag})...",
                    )
                try:
                    async with self._semaphore:
                        raw_messages = await fetch_channel_messages_graph(
                            graph_client,
                            team_id,
                            channel_id,
                            base_url=self._config.graph_base_url,
                        )
                    source = "graph_message"
                    if not raw_messages:
                        # Graph returned [] (403 InsufficientScope)
                        if on_progress:
                            on_progress(
                                "errors",
                                f"[{short_team}] #{channel_name}: no access (403)",
                            )
                            on_progress("+errors", "1")
                        return ch_items, ch_errors, 0
                except IngestionError as graph_exc:
                    ch_errors.append(
                        FetchError(
                            code=ErrorCode.UNKNOWN,
                            message=f"[{short_team}] #{channel_name}: {graph_exc!s:.60}",
                            scope=f"messages/{team_id}/{channel_id}",
                            recoverable=True,
                        )
                    )
                    if on_progress:
                        on_progress(
                            "errors",
                            f"[{short_team}] #{channel_name}: Graph failed",
                        )
                        on_progress("+errors", "1")
                    return ch_items, ch_errors, 0

            msg_count = 0
            for raw in raw_messages:
                try:
                    item = self._normalizer.normalize(
                        raw,
                        source=source,
                        team_name=team_name,
                        channel_id=channel_id,
                    )
                    if item is not None:
                        ch_items.append(item)
                        msg_count += 1
                except Exception as exc:
                    ch_errors.append(
                        FetchError(
                            code=ErrorCode.NORMALIZATION_ERROR,
                            message=str(exc),
                            scope=f"messages/{team_id}/{channel_id}",
                        )
                    )

            done_count += 1
            src_tag = "CSA" if source == "csa" else "Graph"
            if on_progress:
                on_progress(
                    "messages",
                    f"[{short_team}] #{channel_name}: {msg_count} msgs via {src_tag} "
                    f"({done_count}/{total_channels})",
                )
                if msg_count:
                    on_progress("+items", str(msg_count))

            return ch_items, ch_errors, msg_count

        # Launch all channel fetches in parallel (semaphore-bounded)
        msg_tasks = [fetch_one_channel(team_id, ch) for team_id, ch in all_channel_pairs]
        msg_results = await asyncio.gather(*msg_tasks, return_exceptions=True)

        for result in msg_results:
            if isinstance(result, BaseException):
                errors.append(
                    FetchError(
                        code=ErrorCode.UNKNOWN,
                        message=str(result),
                        scope="messages",
                        recoverable=True,
                    )
                )
                stats.errors += 1
            else:
                ch_items, ch_errors, count = result
                items.extend(ch_items)
                errors.extend(ch_errors)
                stats.items_fetched += count
                stats.errors += len(ch_errors)

        return items, errors, stats

    # --- Files (parallelized per-drive) ---

    async def _fetch_files(
        self,
        graph_client: AsyncHTTPClient,
        team_ids: list[str],
        team_name_map: dict[str, str],
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[FileRef], list[FetchError], ScopeStats]:
        files: list[FileRef] = []
        errors: list[FetchError] = []
        stats = ScopeStats()

        # Phase 1: Discover drives across all teams (parallel)
        if on_progress:
            on_progress("files", "Discovering drives...")

        async def fetch_team_drive_list(
            team_id: str,
        ) -> tuple[str, list[dict]]:
            async with self._semaphore:
                drives = await fetch_team_drives(
                    graph_client,
                    team_id,
                    base_url=self._config.graph_base_url,
                )
            return team_id, drives

        drive_tasks = [fetch_team_drive_list(tid) for tid in team_ids]
        drive_results = await asyncio.gather(*drive_tasks, return_exceptions=True)

        all_drive_pairs: list[tuple[str, dict]] = []
        for result in drive_results:
            if isinstance(result, BaseException):
                errors.append(
                    FetchError(
                        code=ErrorCode.UNKNOWN,
                        message=str(result),
                        scope="files/drives",
                        recoverable=True,
                    )
                )
                stats.errors += 1
                if on_progress:
                    on_progress("errors", f"drives: {result!s:.60}")
                    on_progress("+errors", "1")
                continue
            team_id, drives = result
            for d in drives:
                all_drive_pairs.append((team_id, d))

        total_drives = len(all_drive_pairs)
        if on_progress:
            on_progress("files", f"Found {total_drives} drives across {len(team_ids)} teams")

        # Phase 2: Fetch files from all drives (parallel, semaphore-bounded)
        done_count = 0

        async def fetch_one_drive(
            team_id: str,
            drive: dict,
        ) -> tuple[list[FileRef], list[FetchError], int]:
            nonlocal done_count
            d_files: list[FileRef] = []
            d_errors: list[FetchError] = []
            drive_id = str(drive.get("id") or "")
            drive_name = str(drive.get("name") or drive_id[:12])
            team_name = team_name_map.get(team_id, team_id[:12])
            short_team = _short_name(team_name, 20)

            if on_progress:
                on_progress("files", f"[{short_team}] {drive_name}: scanning...")

            try:
                async with self._semaphore:
                    raw_items = await fetch_drive_items_delta(
                        graph_client,
                        drive_id,
                        base_url=self._config.graph_base_url,
                    )
                for raw in raw_items:
                    ref = self._normalizer.make_file_ref(raw)
                    d_files.append(ref)
                    name = raw.get("name", "?")
                    size = raw.get("size", 0)
                    if on_progress:
                        on_progress("files", f"  {name} ({_human_size(size)})")
                        on_progress("+files", "1")
            except IngestionError as exc:
                d_errors.append(
                    FetchError(
                        code=ErrorCode.UNKNOWN,
                        message=f"Drive {drive_name}: {exc}",
                        scope=f"files/{team_id}/{drive_id}",
                    )
                )
                if on_progress:
                    on_progress("errors", f"[{short_team}] {drive_name}: {exc!s:.50}")
                    on_progress("+errors", "1")

            done_count += 1
            if on_progress and d_files:
                on_progress(
                    "files",
                    f"[{short_team}] {drive_name}: {len(d_files)} files "
                    f"({done_count}/{total_drives})",
                )

            return d_files, d_errors, len(d_files)

        file_tasks = [fetch_one_drive(team_id, d) for team_id, d in all_drive_pairs]
        file_results = await asyncio.gather(*file_tasks, return_exceptions=True)

        for result in file_results:
            if isinstance(result, BaseException):
                errors.append(
                    FetchError(
                        code=ErrorCode.UNKNOWN,
                        message=str(result),
                        scope="files",
                        recoverable=True,
                    )
                )
                stats.errors += 1
            else:
                d_files, d_errors, count = result
                files.extend(d_files)
                errors.extend(d_errors)
                stats.items_fetched += count
                stats.errors += len(d_errors)

        return files, errors, stats

    # --- Assignments (Education API, parallelized per-class) ---

    async def _fetch_assignments(
        self,
        graph_client: AsyncHTTPClient,
        graph_token: str,
        team_ids: list[str],
        team_name_map: dict[str, str],
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[NormalizedItem], list[FetchError], ScopeStats]:
        """Fetch assignments from all teams via Education Assignments API.

        Uses the Graph token against assignments.edu.cloud.microsoft (separate
        service that accepts the same token without EduAssignments scopes).
        """
        items: list[NormalizedItem] = []
        errors: list[FetchError] = []
        stats = ScopeStats()

        if on_progress:
            on_progress("assignments", f"Fetching assignments for {len(team_ids)} classes...")

        # The assignments API uses the Graph token but a different base URL
        edu_client = self._make_client(
            self._config.assignments_base_url,
            graph_token,
        )

        try:
            done_count = 0

            async def fetch_class(
                team_id: str,
            ) -> tuple[list[NormalizedItem], list[FetchError], int]:
                nonlocal done_count
                class_items: list[NormalizedItem] = []
                class_errors: list[FetchError] = []
                team_name = team_name_map.get(team_id, team_id[:12])
                short_team = _short_name(team_name, 25)

                if on_progress:
                    on_progress("assignments", f"[{short_team}] fetching...")

                async with self._semaphore:
                    raw_assignments = await fetch_class_assignments(
                        edu_client,
                        team_id,
                        base_url=self._config.assignments_base_url,
                    )

                if not raw_assignments:
                    done_count += 1
                    return class_items, class_errors, 0

                # Fetch resources for each assignment (parallel, semaphore-bounded)
                for raw in raw_assignments:
                    assign_id = str(raw.get("id") or "")

                    # Fetch attached resources (PDFs, etc.)
                    async with self._semaphore:
                        resources = await fetch_assignment_resources(
                            edu_client,
                            team_id,
                            assign_id,
                            base_url=self._config.assignments_base_url,
                        )

                    # Build resource file refs without mutating the original dict
                    file_refs: list[dict] = []
                    for res in resources:
                        rd = res.get("resource", {})
                        file_refs.append(
                            {
                                "displayName": rd.get("displayName", ""),
                                "fileUrl": rd.get("fileUrl", ""),
                                "@odata.type": rd.get("@odata.type", ""),
                            }
                        )

                    raw_with_context = {**raw, "_resources": file_refs}

                    try:
                        item = self._normalizer.normalize(
                            raw_with_context,
                            source="assignment",
                            class_name=team_name,
                            class_id=team_id,
                        )
                        if item is not None:
                            class_items.append(item)
                    except Exception as exc:
                        class_errors.append(
                            FetchError(
                                code=ErrorCode.NORMALIZATION_ERROR,
                                message=str(exc),
                                scope=f"assignments/{team_id}/{assign_id}",
                            )
                        )

                done_count += 1
                if on_progress:
                    on_progress(
                        "assignments",
                        f"[{short_team}] {len(class_items)} assignments "
                        f"({done_count}/{len(team_ids)})",
                    )
                    if class_items:
                        on_progress("+assignments", str(len(class_items)))

                return class_items, class_errors, len(class_items)

            # Launch all class fetches in parallel
            assign_tasks = [fetch_class(tid) for tid in team_ids]
            assign_results = await asyncio.gather(*assign_tasks, return_exceptions=True)

            for result in assign_results:
                if isinstance(result, BaseException):
                    errors.append(
                        FetchError(
                            code=ErrorCode.UNKNOWN,
                            message=str(result),
                            scope="assignments",
                            recoverable=True,
                        )
                    )
                    stats.errors += 1
                else:
                    class_items, class_errors, count = result
                    items.extend(class_items)
                    errors.extend(class_errors)
                    stats.items_fetched += count
                    stats.errors += len(class_errors)

        finally:
            await edu_client.close()

        return items, errors, stats

    def _make_client(self, base_url: str, token: str) -> AsyncHTTPClient:
        return AsyncHTTPClient(
            base_url=base_url,
            token=token,
            max_retries=self._config.max_retries,
            timeout=self._config.request_timeout,
            requests_per_second=self._config.requests_per_second,
            sleep_func=self._sleep_func,
        )
