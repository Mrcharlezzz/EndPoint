"""Typer application — entry point for the `ingest` CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ingestion import __version__

console = Console(stderr=True)
err_console = Console(stderr=True)

# Default state directory
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "ingest"

app = typer.Typer(
    name="ingest",
    help="Data ingestion CLI for Constructor University",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# --- Global state shared across commands ---


class GlobalState:
    """Mutable global state populated by the top-level callback."""

    verbose: bool = False
    output_dir: Path = DEFAULT_CONFIG_DIR / "output"
    config_dir: Path = DEFAULT_CONFIG_DIR
    format: str = "table"


state = GlobalState()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show debug output")] = False,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", "-o", help="Override output directory"),
    ] = None,
    fmt: Annotated[str, typer.Option("--format", help="Output format (table|json)")] = "table",
    version: Annotated[bool, typer.Option("--version", help="Show version", is_eager=True)] = False,
) -> None:
    """ingest — Data ingestion CLI for Constructor University."""
    if version:
        console.print(f"ingest {__version__}")
        raise typer.Exit()

    state.verbose = verbose
    if output_dir is not None:
        state.output_dir = output_dir
    state.format = fmt

    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


# --- Import and register subcommands ---

from ingestion.cli.commands.config_cmd import config_command  # noqa: E402
from ingestion.cli.commands.creds import creds_command  # noqa: E402
from ingestion.cli.commands.history import history_command  # noqa: E402
from ingestion.cli.commands.inspect_cmd import inspect_command  # noqa: E402
from ingestion.cli.commands.login import login_app  # noqa: E402
from ingestion.cli.commands.run import run_command  # noqa: E402
from ingestion.cli.commands.watch import watch_app  # noqa: E402

app.add_typer(login_app, name="login", help="Authenticate with a platform")
app.command(name="run")(run_command)
app.command(name="creds")(creds_command)
app.command(name="inspect")(inspect_command)
app.command(name="history")(history_command)
app.command(name="config")(config_command)
app.add_typer(watch_app, name="telegram", help="Telegram commands")
