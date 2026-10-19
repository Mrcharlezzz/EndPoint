"""Run command — fetch data from authenticated platforms."""

from __future__ import annotations

from typing import Annotated

import typer


def run_command(
    platform: Annotated[
        list[str] | None,
        typer.Option("--platform", "-p", help="Filter to specific platforms"),
    ] = None,
    scope: Annotated[
        list[str] | None,
        typer.Option("--scope", "-s", help="Filter to specific scopes"),
    ] = None,
    incremental: Annotated[
        bool, typer.Option("--incremental", help="Use saved cursor for incremental fetch")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show plan, zero API calls")] = False,
    profile: Annotated[
        bool, typer.Option("--profile", help="Emit detailed timing profile to output dir")
    ] = False,
) -> None:
    """Fetch data from authenticated platforms."""
    from ingestion.cli._run_impl import execute_run_sync, pre_run_check

    # Interactive menu (sync — questionary needs sync context)
    should_continue, platforms, scopes = pre_run_check(
        cli_platforms=platform or [],
        cli_scopes=scope or [],
    )
    if not should_continue:
        return

    # Async fetch
    import asyncio

    asyncio.run(
        execute_run_sync(
            platforms=platforms,
            scopes=scopes,
            incremental=incremental,
            dry_run=dry_run,
            profile=profile,
        )
    )
