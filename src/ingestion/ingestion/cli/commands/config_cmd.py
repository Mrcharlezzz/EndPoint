"""Config command — interactive config editor."""

from __future__ import annotations


def config_command() -> None:
    """View and edit settings."""
    from ingestion.cli._config_impl import run_config_editor

    run_config_editor()
