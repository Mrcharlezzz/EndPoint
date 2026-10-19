"""Inspect command — browse fetched results."""

from __future__ import annotations

from typing import Annotated

import typer


def inspect_command(
    run: Annotated[
        str | None,
        typer.Option("--run", help="Select run by timestamp prefix, index, or file path"),
    ] = None,
) -> None:
    """Browse fetched results."""
    from ingestion.cli._inspect_impl import run_inspector

    run_inspector(run_selector=run)
