"""History command — view past sync runs."""

from __future__ import annotations


def history_command() -> None:
    """View past sync runs."""
    from ingestion.cli._history_impl import run_history

    run_history()
