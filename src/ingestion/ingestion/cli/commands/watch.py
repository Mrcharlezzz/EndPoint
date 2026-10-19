"""Telegram watch command — live message streaming dashboard."""

from __future__ import annotations

import typer

watch_app = typer.Typer(no_args_is_help=True)


@watch_app.command(name="watch")
def watch_command() -> None:
    """Live message streaming dashboard for Telegram."""
    import asyncio

    from ingestion.cli._watch_impl import run_watch

    asyncio.run(run_watch())
