"""Creds command — interactive credential manager."""

from __future__ import annotations


def creds_command() -> None:
    """Manage stored credentials."""
    from ingestion.cli._creds_impl import run_creds_menu

    run_creds_menu()
