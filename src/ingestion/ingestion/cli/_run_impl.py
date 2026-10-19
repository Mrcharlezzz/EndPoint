"""Run command implementation — fetch data from authenticated platforms.

Handles: auto-detect credentials, pre-flight validation, fetch with Rich
progress display, dry-run mode, scope/platform filtering, incremental cursors.

Split into sync (pre_run_check) and async (execute_run_sync) to avoid
questionary/prompt_toolkit conflict with asyncio.run().
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import Group

    from ingestion.adapters.base import BaseAdapter
    from ingestion.ports import ProgressCallback

from ingestion.cli.app import state
from ingestion.cli.display import (
    PLATFORM_NAMES,
    SYM_FAIL,
    SYM_OK,
    SYM_PENDING,
    format_duration,
    print_status,
)
from ingestion.cli.state import (
    append_history,
    classify_item,
    compute_item_hash,
    load_all_credentials,
    load_cursor,
    load_seen_ids,
    save_output,
    save_seen_ids,
    update_seen_id,
)
from ingestion.config import IngestionConfig
from ingestion.schemas import FetchResult, FetchScope, SyncStats

console = Console(stderr=True)

PLATFORM_SCOPES: dict[str, frozenset[FetchScope]] = {
    "teams": frozenset({FetchScope.MESSAGES, FetchScope.FILES, FetchScope.ASSIGNMENTS}),
    "moodle": frozenset(
        {
            FetchScope.ASSIGNMENTS,
            FetchScope.FORUMS,
            FetchScope.MATERIALS,
            FetchScope.EVENTS,
            FetchScope.FILES,
        }
    ),
    "telegram": frozenset({FetchScope.MESSAGES}),
}


def pre_run_check(
    cli_platforms: list[str],
    cli_scopes: list[str],
) -> tuple[bool, list[str], list[str]]:
    """Interactive pre-run menu: login, pick platforms/scopes, confirm.

    Always shows the menu regardless of credential state.
    Returns (should_proceed, platforms, scopes).
    """
    import questionary

    config_dir = state.config_dir

    while True:
        all_creds = load_all_credentials(config_dir)

        # Build status display
        console.print()
        for p in PLATFORM_NAMES:
            if p in all_creds:
                console.print(f"  [green]✔[/green] {p:<12} logged in")
            else:
                console.print(f"  [red]✖[/red] {p:<12} no credentials")
        console.print()

        # Build menu choices
        choices = []
        authenticated = [p for p in PLATFORM_NAMES if p in all_creds]
        if authenticated:
            choices.append("Fetch now")
        choices.extend(
            [
                "Login to Teams",
                "Login to Moodle",
                "Login to Telegram",
                "Exit",
            ]
        )

        action = questionary.select("What would you like to do?", choices=choices).ask()
        if action is None or action == "Exit":
            return False, [], []

        if action == "Fetch now":
            break

        # Handle login
        if "Teams" in action:
            from ingestion.cli.auth.teams_login import run_teams_login

            run_teams_login()
        elif "Moodle" in action:
            from ingestion.cli.auth.moodle_login import run_moodle_login

            run_moodle_login()
        elif "Telegram" in action:
            from ingestion.cli.auth.telegram_login import run_telegram_login

            run_telegram_login()

        console.print()

    # If CLI flags were passed, use those directly
    if cli_platforms or cli_scopes:
        return True, cli_platforms, cli_scopes

    # Pick platforms
    all_creds = load_all_credentials(config_dir)
    authenticated = [p for p in PLATFORM_NAMES if p in all_creds]

    if len(authenticated) == 1:
        selected_platforms = authenticated
    else:
        selected_platforms = questionary.checkbox(
            "Select platforms to fetch:",
            choices=[questionary.Choice(title=p, value=p, checked=True) for p in authenticated],
        ).ask()
        if not selected_platforms:
            return False, [], []

    # Pick scopes
    available_scopes: set[str] = set()
    for p in selected_platforms:
        for s in PLATFORM_SCOPES.get(p, frozenset()):
            available_scopes.add(s.value)

    sorted_scopes = sorted(available_scopes)
    if len(sorted_scopes) > 1:
        selected_scopes = questionary.checkbox(
            "Select scopes (leave all checked for everything):",
            choices=[questionary.Choice(title=s, value=s, checked=True) for s in sorted_scopes],
        ).ask()
        if selected_scopes is None:
            return False, [], []
        # If all selected, pass empty list (means "all")
        if set(selected_scopes) == available_scopes:
            selected_scopes = []
    else:
        selected_scopes = []

    return True, selected_platforms, selected_scopes


async def execute_run_sync(
    platforms: list[str],
    scopes: list[str],
    incremental: bool,
    dry_run: bool,
    profile: bool = False,
) -> None:
    """Async run implementation — all network I/O happens here."""
    config = IngestionConfig()
    config_dir = state.config_dir

    all_creds = load_all_credentials(config_dir)
    target_platforms = platforms if platforms else [p for p in PLATFORM_NAMES if p in all_creds]

    # Pre-flight validation (with silent token refresh for Teams)
    console.print()
    print_status(SYM_PENDING, "Checking credentials...")
    t_cred_start = time.monotonic()

    valid_platforms: list[str] = []
    for platform in target_platforms:
        creds = all_creds.get(platform)
        if not creds:
            print_status(SYM_FAIL, f"{platform:<12} no credentials")
            continue

        # Auto-refresh expired Teams tokens via saved SSO cookies
        if platform == "teams":
            creds, all_creds = _try_teams_refresh_if_expired(creds, all_creds, config_dir)

        is_valid = await _test_connection(platform, creds, config)
        if is_valid:
            print_status(SYM_OK, f"{platform:<12} valid")
            valid_platforms.append(platform)
        else:
            print_status(SYM_FAIL, f"{platform:<12} invalid or expired")

    t_cred_end = time.monotonic()

    if not valid_platforms:
        console.print()
        print_status(SYM_FAIL, "No valid credentials. Run [bold]ingest login <platform>[/bold]")
        return

    # Resolve scopes
    scope_filter: frozenset[FetchScope] | None = None
    if scopes:
        scope_filter = frozenset(FetchScope(s) for s in scopes)

    # Dry run
    if dry_run:
        _print_dry_run(valid_platforms, all_creds, scope_filter, incremental, config_dir)
        return

    # Execute fetch
    console.print()
    console.print(f"  Starting fetch for: {', '.join(valid_platforms)}")
    console.print()

    start_time = time.monotonic()
    total_items = 0
    total_new = 0
    total_updated = 0
    total_errors = 0
    all_results: list[dict] = []
    all_files: list[dict] = []
    all_fetch_errors: list[dict] = []
    all_profiles: list[dict[str, object]] = []

    seen_ids = load_seen_ids(config_dir)

    for platform in valid_platforms:
        creds = all_creds[platform]
        adapter = _make_adapter(platform, config)

        cursor = load_cursor(platform, config_dir) if incremental else None
        effective_scope = _resolve_platform_scopes(platform, scope_filter)

        console.print(f"  [bold]{platform}[/bold]")

        # Live progress display during fetch
        progress = _FetchProgress(platform)
        profiler = _ProfileCollector() if profile else None

        try:
            from rich.live import Live

            with Live(
                progress.render(),
                console=console,
                refresh_per_second=8,
                transient=True,
            ) as live:
                base_callback = _make_live_progress_callback(progress, live)

                if profiler is not None:

                    def callback(
                        category: str,
                        message: str,
                        _p: _ProfileCollector = profiler,
                        _b: object = base_callback,
                    ) -> None:  # noqa: E501
                        _p.record(category, message)
                        _b(category, message)  # type: ignore[operator]
                else:
                    callback = base_callback  # type: ignore[assignment]

                t_fetch_start = time.monotonic()
                result: FetchResult = await adapter.fetch(
                    creds,
                    cursor,
                    effective_scope,
                    on_progress=callback,
                )
                t_fetch_end = time.monotonic()
        except Exception as exc:
            print_status(SYM_FAIL, f"  {platform} failed: {exc}")
            total_errors += 1
            continue

        if profiler is not None:
            profiler.mark_phase("fetch", t_fetch_start, t_fetch_end)

        # Process items with badges
        t_process_start = time.monotonic()
        for item in result.items:
            item_dict = item.model_dump(mode="json")
            content_hash = compute_item_hash(item_dict)
            badge = classify_item(item.external_id, content_hash, seen_ids)
            seen_ids = update_seen_id(item.external_id, content_hash, seen_ids)

            item_dict["_badge"] = badge
            all_results.append(item_dict)

            if badge == "NEW":
                total_new += 1
            elif badge == "UPDATED":
                total_updated += 1

        t_process_end = time.monotonic()
        if profiler is not None:
            profiler.mark_phase("item_processing", t_process_start, t_process_end)

        total_items += len(result.items)

        # Files
        for f in result.files:
            all_files.append(f.model_dump(mode="json"))

        # Errors
        for err in result.errors:
            all_fetch_errors.append(err.model_dump(mode="json"))
            total_errors += 1

        # Show scope stats
        for scope_name, scope_stat in result.stats.per_scope_stats.items():
            print_status(
                SYM_OK,
                f"{scope_name:<16} {scope_stat.items_fetched} items "
                f"({format_duration(result.stats.duration_seconds)})",
            )

        if not result.stats.per_scope_stats:
            print_status(
                SYM_OK,
                f"{'fetch':<16} {len(result.items)} items "
                f"({format_duration(result.stats.duration_seconds)})",
            )

        # Save cursor only on full completion
        if result.cursor and not result.errors:
            from ingestion.cli.state import save_cursor

            save_cursor(platform, result.cursor, config_dir)

        if profiler is not None:
            all_profiles.append(profiler.to_dict(platform, result.stats))

        console.print()

    # Save seen_ids
    save_seen_ids(seen_ids, config_dir)

    duration = time.monotonic() - start_time
    output_data = _build_output_data(
        all_results,
        all_files,
        all_fetch_errors,
        total_items,
        total_new,
        total_updated,
        total_errors,
        duration,
        valid_platforms,
        scope_filter,
        incremental,
    )
    output_path = save_output(output_data, config_dir, state.output_dir)
    _record_history(
        output_path,
        valid_platforms,
        total_items,
        total_new,
        total_updated,
        total_errors,
        duration,
        config_dir,
    )
    _display_summary(
        output_data,
        output_path,
        total_items,
        total_new,
        total_updated,
        total_errors,
        duration,
    )

    # Write profile JSON
    if profile and all_profiles:
        t_write_end = time.monotonic()
        profile_data: dict[str, object] = {
            "profiles": all_profiles,
            "total_duration": round(duration, 4),
            "phases": {
                "credential_check": round(t_cred_end - t_cred_start, 4),
                "fetch_and_process": round(
                    t_write_end - start_time - (t_cred_end - t_cred_start), 4
                ),
            },
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
        out_dir = state.output_dir or config_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        profile_name = "profile-" + datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M") + ".json"
        profile_path = out_dir / profile_name
        profile_path.write_text(
            json.dumps(profile_data, indent=2, default=str) + "\n", encoding="utf-8"
        )
        console.print(f"  Profile:  [link=file://{profile_path}]{profile_path}[/link]")


def _build_output_data(
    items: list[dict],
    files: list[dict],
    errors: list[dict],
    total_items: int,
    total_new: int,
    total_updated: int,
    total_errors: int,
    duration: float,
    platforms: list[str],
    scope_filter: frozenset[FetchScope] | None,
    incremental: bool,
) -> dict:
    """Assemble the output JSON structure."""
    return {
        "items": items,
        "files": files,
        "errors": errors,
        "stats": {
            "items_fetched": total_items,
            "items_new": total_new,
            "items_updated": total_updated,
            "errors": total_errors,
            "duration_seconds": round(duration, 2),
        },
        "metadata": {
            "platforms": platforms,
            "scopes": [s.value for s in (scope_filter or frozenset())],
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "version": "2.0.0",
            "incremental": incremental,
        },
    }


def _record_history(
    output_path: Path,
    platforms: list[str],
    total_items: int,
    total_new: int,
    total_updated: int,
    total_errors: int,
    duration: float,
    config_dir: Path,
) -> None:
    """Append a run entry to history.jsonl."""
    append_history(
        {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "platforms": platforms,
            "items_fetched": total_items,
            "items_new": total_new,
            "items_updated": total_updated,
            "errors": total_errors,
            "duration_seconds": round(duration, 2),
            "output_file": str(output_path),
        },
        config_dir,
    )


def _display_summary(
    output_data: dict,
    output_path: Path,
    total_items: int,
    total_new: int,
    total_updated: int,
    total_errors: int,
    duration: float,
) -> None:
    """Print or emit JSON summary of the sync run."""
    if state.format == "json":
        from ingestion.cli.display import stdout_console

        stdout_console.print_json(json.dumps(output_data))
    else:
        console.print("  ─── Sync Complete ────────────────────")
        console.print(
            f"  Items:    {total_items} fetched ({total_new} new, {total_updated} updated)"
        )
        if total_errors:
            console.print(f"  Errors:   [yellow]{total_errors} recoverable[/yellow]")
        console.print(f"  Duration: {format_duration(duration)}")
        console.print(f"  Output:   [link=file://{output_path}]{output_path}[/link]")


def _print_dry_run(
    platforms: list[str],
    all_creds: dict[str, dict],
    scope_filter: frozenset[FetchScope] | None,
    incremental: bool,
    config_dir: Path,
) -> None:
    """Print dry-run execution plan."""
    console.print()
    console.print("  ─── Dry Run ──────────────────────")
    console.print()

    for platform in PLATFORM_NAMES:
        creds = all_creds.get(platform)
        if not creds:
            console.print(f"  {platform}")
            print_status(SYM_FAIL, "no credentials")
            console.print()
            continue

        if platform not in platforms:
            continue

        console.print(f"  {platform} (cred: ~/.config/ingest/credentials.json)")
        effective_scopes = _resolve_platform_scopes(platform, scope_filter)
        if effective_scopes:
            scope_names = sorted(s.value for s in effective_scopes)
        else:
            scope_names = sorted(s.value for s in PLATFORM_SCOPES.get(platform, frozenset()))
        console.print(f"    scopes: {', '.join(scope_names)}")

        cursor = load_cursor(platform, config_dir)
        if cursor and incremental:
            console.print(f"    cursor: {json.dumps(cursor)}")
        else:
            console.print("    cursor: none (full fetch)")

        if platform == "teams":
            team_ids = creds.get("team_ids", [])
            console.print(f"    teams: {len(team_ids)}")
            _print_token_expiry(creds.get("graph_token", ""))
        elif platform == "moodle":
            console.print(f"    user: {creds.get('user_id', 'unknown')}")
        elif platform == "telegram":
            console.print(f"    bot_token: {'set' if creds.get('bot_token') else 'missing'}")

        console.print()

    console.print("  No API calls were made.")


def _print_token_expiry(token: str) -> None:
    """Try to decode JWT and show expiry."""
    if not token:
        return
    from ingestion.cli.jwt_utils import get_expiry_remaining_minutes

    minutes = get_expiry_remaining_minutes(token)
    if minutes is None:
        return
    if minutes > 0:
        console.print(f"    token expires: in {minutes}m")
    else:
        console.print("    token: [red]EXPIRED[/red]")


async def _test_connection(platform: str, creds: dict, config: IngestionConfig) -> bool:
    """Test adapter connection."""
    adapter = _make_adapter(platform, config)
    try:
        return await adapter.test_connection(creds)
    except Exception:
        return False


def _make_adapter(platform: str, config: IngestionConfig) -> BaseAdapter:
    """Instantiate the appropriate adapter for a platform."""
    if platform == "teams":
        from ingestion.adapters.teams.adapter import TeamsAdapter

        return TeamsAdapter(config.teams)
    elif platform == "moodle":
        from ingestion.adapters.moodle.adapter import MoodleAdapter

        return MoodleAdapter(config.moodle)
    elif platform == "telegram":
        from ingestion.adapters.telegram.adapter import TelegramAdapter

        return TelegramAdapter(config.telegram)
    else:
        msg = f"Unknown platform: {platform}"
        raise ValueError(msg)


def _make_live_progress_callback(
    progress: _FetchProgress,
    live: object | None = None,
) -> ProgressCallback:
    """Create a progress callback, optionally updating a Rich Live display.

    Categories prefixed with '+' are stat increments, not display lines:
        +items, +files, +errors — increment counters only.
    """

    def callback(category: str, message: str) -> None:
        if category == "+items":
            progress.inc(items=int(message))
        elif category == "+files":
            progress.inc(files=int(message))
        elif category == "+assignments":
            progress.inc(assignments=int(message))
        elif category == "+errors":
            progress.inc(errors=int(message))
        else:
            progress.update(category, message)

        if live is not None:
            live.update(progress.render())  # type: ignore[attr-defined]

    return callback


class _ProfileCollector:
    """Lightweight timing collector that timestamps on_progress callbacks.

    Derives per-scope durations from callback boundaries and records
    every event for full-profile output.
    """

    def __init__(self) -> None:
        self._events: list[dict[str, object]] = []
        self._scope_starts: dict[str, float] = {}
        self._scope_ends: dict[str, float] = {}
        self._phase_times: dict[str, float] = {}
        self._start: float = time.monotonic()

    def record(self, category: str, message: str) -> None:
        """Record a single on_progress event with elapsed timestamp."""
        elapsed = time.monotonic() - self._start
        self._events.append(
            {
                "t": round(elapsed, 4),
                "category": category,
                "message": message,
            }
        )
        if not category.startswith("+"):
            if category not in self._scope_starts:
                self._scope_starts[category] = elapsed
            self._scope_ends[category] = elapsed

    def mark_phase(self, name: str, start: float, end: float) -> None:
        """Record a named phase with absolute monotonic timestamps."""
        self._phase_times[name] = round(end - start, 4)

    def to_dict(self, platform: str, stats: SyncStats | None = None) -> dict:
        """Build the profile output for one platform."""
        scope_durations = {}
        for scope, s_start in self._scope_starts.items():
            s_end = self._scope_ends.get(scope, s_start)
            scope_durations[scope] = round(s_end - s_start, 4)

        phase_times = dict(self._phase_times)
        benchmark = None
        if stats is not None:
            fetch_duration = stats.duration_seconds or phase_times.get("fetch", 0.0)
            run_duration = fetch_duration + phase_times.get("item_processing", 0.0)
            benchmark = {
                "items_fetched": stats.items_fetched,
                "errors": stats.errors,
                "fetch_duration_seconds": round(fetch_duration, 4),
                "run_duration_seconds": round(run_duration, 4),
                "fetch_throughput_items_per_second": (
                    round(stats.items_fetched / fetch_duration, 4) if fetch_duration > 0 else None
                ),
            }

        data: dict[str, object] = {
            "platform": platform,
            "phases": phase_times,
            "scopes": scope_durations,
            "events": list(self._events),
            "total_events": len(self._events),
        }
        if benchmark is not None:
            data["benchmark"] = benchmark
        return data


class _FetchProgress:
    """Live progress dashboard — rolling lines per scope + real-time stats."""

    _MAX_LINES = 3
    # Categories with more visible lines
    _EXPANDED_CATEGORIES = {"teams": 25, "errors": 8}

    def __init__(self, platform: str) -> None:
        self._platform = platform
        self._lines: dict[str, list[str]] = {}
        self._overflow: dict[str, int] = {}  # overflow count per category
        self._start = time.monotonic()
        self._items = 0
        self._files = 0
        self._assignments = 0
        self._errors = 0
        self._api_calls = 0

    def update(self, category: str, message: str) -> None:
        if category not in self._lines:
            self._lines[category] = []
            self._overflow[category] = 0
        max_lines = self._EXPANDED_CATEGORIES.get(category, self._MAX_LINES)
        self._lines[category].append(message)
        if len(self._lines[category]) > max_lines:
            self._overflow[category] += len(self._lines[category]) - max_lines
            self._lines[category] = self._lines[category][-max_lines:]
        self._api_calls += 1

    def inc(
        self,
        *,
        items: int = 0,
        files: int = 0,
        assignments: int = 0,
        errors: int = 0,
    ) -> None:
        self._items += items
        self._files += files
        self._assignments += assignments
        self._errors += errors

    def render(self) -> Group:
        from rich.console import Group
        from rich.text import Text

        elapsed = time.monotonic() - self._start

        # Stats bar
        stats = Text()
        stats.append("    ")
        stats.append(f"{format_duration(elapsed)}", style="bold cyan")
        stats.append("  ", style="dim")
        stats.append("items ", style="dim")
        stats.append(f"{self._items}", style="bold green")
        stats.append("  ", style="dim")
        stats.append("files ", style="dim")
        stats.append(f"{self._files}", style="bold blue")
        stats.append("  ", style="dim")
        stats.append("hw ", style="dim")
        stats.append(f"{self._assignments}", style="bold magenta")
        stats.append("  ", style="dim")
        stats.append("errors ", style="dim")
        if self._errors:
            stats.append(f"{self._errors}", style="bold red")
        else:
            stats.append("0", style="bold green")
        stats.append("  ", style="dim")
        stats.append("api ", style="dim")
        stats.append(f"{self._api_calls}", style="bold")
        stats.append("\n")

        # Separator
        sep = Text("    " + "─" * 44 + "\n", style="dim")

        # Category lines
        body = Text()
        for category, lines in self._lines.items():
            overflow = self._overflow.get(category, 0)
            if category == "errors":
                label = f"    {category}"
                if overflow:
                    label += f" (+{overflow} more in output)"
                body.append(f"{label}\n", style="bold red")
                for line in lines:
                    body.append(f"      {line}\n", style="red")
            else:
                body.append(f"    {category}\n", style="bold")
                for line in lines:
                    body.append(f"      {line}\n", style="dim")

        if not self._lines:
            body.append("    starting...\n", style="dim")

        return Group(stats, sep, body)


def _resolve_platform_scopes(
    platform: str,
    scope_filter: frozenset[FetchScope] | None,
) -> frozenset[FetchScope] | None:
    """Resolve scopes for a platform, respecting the scope filter."""
    if scope_filter is None:
        return None
    platform_scopes = PLATFORM_SCOPES.get(platform, frozenset())
    return scope_filter & platform_scopes or None


def _try_teams_refresh_if_expired(
    creds: dict,
    all_creds: dict[str, dict],
    config_dir: Path,
) -> tuple[dict, dict[str, dict]]:
    """Always refresh Teams tokens via saved SSO cookies.

    CSA tokens are server-side invalidated across sessions, so we always
    need fresh tokens even if the JWT exp claim says valid.
    Returns (updated_creds, updated_all_creds).
    """
    graph_token = creds.get("graph_token", "")
    if not graph_token:
        return creds, all_creds

    print_status(SYM_PENDING, "Refreshing Teams tokens (CSA tokens expire between sessions)...")

    try:
        from ingestion.cli.auth.teams_login import try_silent_refresh

        if try_silent_refresh():
            # Reload credentials after successful refresh
            refreshed_all = load_all_credentials(config_dir)
            refreshed_creds = refreshed_all.get("teams", creds)
            return refreshed_creds, refreshed_all
        else:
            print_status(SYM_FAIL, "Silent refresh failed — re-login with: ingest login teams")
    except Exception as exc:
        print_status(SYM_FAIL, f"Silent refresh error: {exc}")

    return creds, all_creds
