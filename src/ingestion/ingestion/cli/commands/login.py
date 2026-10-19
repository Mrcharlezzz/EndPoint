"""Login commands — authenticate with platforms."""

from __future__ import annotations

import typer
from rich.console import Console

login_app = typer.Typer(no_args_is_help=True)
console = Console(stderr=True)


@login_app.command(name="teams")
def login_teams() -> None:
    """Authenticate with Microsoft Teams via Playwright browser."""
    from ingestion.cli.auth.teams_login import run_teams_login

    run_teams_login()


@login_app.command(name="moodle")
def login_moodle() -> None:
    """Authenticate with Moodle (username + password)."""
    from ingestion.cli.auth.moodle_login import run_moodle_login

    run_moodle_login()


@login_app.command(name="telegram")
def login_telegram() -> None:
    """Authenticate with Telegram (bot token)."""
    from ingestion.cli.auth.telegram_login import run_telegram_login

    run_telegram_login()
