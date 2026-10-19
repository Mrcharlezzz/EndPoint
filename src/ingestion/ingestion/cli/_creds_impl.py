"""Creds command implementation — interactive credential manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

import questionary

if TYPE_CHECKING:
    from pathlib import Path
from rich.console import Console
from rich.panel import Panel

from ingestion.cli.app import state
from ingestion.cli.display import (
    PLATFORM_NAMES,
    SYM_FAIL,
    SYM_OK,
    format_token_redacted,
    print_status,
)
from ingestion.cli.state import (
    clear_all_credentials,
    clear_credentials,
    load_all_credentials,
)

console = Console(stderr=True)


def run_creds_menu() -> None:
    """Interactive credential manager."""
    config_dir = state.config_dir
    all_creds = load_all_credentials(config_dir)

    choices = ["View all credentials"]
    for p in PLATFORM_NAMES:
        if p in all_creds:
            choices.append(f"Show {p} details")
    choices.extend(["Clear credentials", "Re-login platform"])

    action = questionary.select("Credential actions:", choices=choices).ask()
    if action is None:
        return

    if action == "View all credentials":
        _view_all(all_creds)
    elif action.startswith("Show "):
        platform = action.split()[1]
        _show_details(platform, all_creds.get(platform, {}))
    elif action == "Clear credentials":
        _clear_creds(all_creds, config_dir)
    elif action == "Re-login platform":
        _relogin()


def _view_all(all_creds: dict[str, dict]) -> None:
    """Show status of all platform credentials."""
    console.print()
    for p in PLATFORM_NAMES:
        creds = all_creds.get(p)
        if not creds:
            print_status(SYM_FAIL, f"{p:<12} no credentials")
            continue

        status = _get_cred_status(p, creds)
        print_status(SYM_OK, f"{p:<12} {status}")
    console.print()


def _get_cred_status(platform: str, creds: dict) -> str:
    """Get a human-readable status for platform credentials."""
    if platform == "teams":
        from ingestion.cli.jwt_utils import get_expiry_remaining_minutes

        token = creds.get("graph_token", "")
        minutes = get_expiry_remaining_minutes(token)
        if minutes is not None:
            if minutes > 0:
                return f"valid (expires in {minutes}m)"
            return "valid (EXPIRED)"
        return "valid (token set)"
    elif platform == "moodle":
        token = creds.get("token", "")
        return f"valid (wstoken: {format_token_redacted(token)})"
    elif platform == "telegram":
        return "valid (bot_token set)"
    return "unknown"


def _show_details(platform: str, creds: dict) -> None:
    """Show detailed credential info for a platform."""
    console.print()

    if platform == "teams":
        _show_teams_details(creds)
    elif platform == "moodle":
        _show_moodle_details(creds)
    elif platform == "telegram":
        _show_telegram_details(creds)


def _show_teams_details(creds: dict) -> None:
    """Show Teams credential details with JWT decode."""
    from ingestion.cli.jwt_utils import get_token_info

    lines = []

    graph_token = creds.get("graph_token", "")
    if graph_token:
        lines.append("Graph Token:")
        jwt_info = get_token_info(graph_token)
        if jwt_info:
            for k, v in jwt_info.items():
                lines.append(f"  {k}: {v}")
        lines.append(f"  Token: {format_token_redacted(graph_token)}")

    csa_token = creds.get("csa_token", "")
    if csa_token:
        lines.append("")
        lines.append("CSA Token:")
        lines.append(f"  Token: {format_token_redacted(csa_token)}")

    team_ids = creds.get("team_ids", [])
    if team_ids:
        lines.append("")
        lines.append(f"Teams ({len(team_ids)}):")
        for tid in team_ids:
            lines.append(f"  • {tid}")

    console.print(Panel("\n".join(lines), title="Teams Credentials", border_style="blue"))


def _show_moodle_details(creds: dict) -> None:
    """Show Moodle credential details."""
    lines = [
        f"User ID:   {creds.get('user_id', 'unknown')}",
        f"Token:     {format_token_redacted(creds.get('token', ''))}",
    ]
    console.print(Panel("\n".join(lines), title="Moodle Credentials", border_style="blue"))


def _show_telegram_details(creds: dict) -> None:
    """Show Telegram credential details."""
    lines = [
        f"Token: {format_token_redacted(creds.get('bot_token', ''))}",
    ]
    console.print(Panel("\n".join(lines), title="Telegram Credentials", border_style="blue"))


def _clear_creds(all_creds: dict[str, dict], config_dir: Path) -> None:
    """Interactive credential clearing."""
    choices = [p for p in PLATFORM_NAMES if p in all_creds]
    choices.append("All platforms")

    if not choices[:-1]:
        console.print("  [dim]No credentials to clear[/dim]")
        return

    target = questionary.select("Clear credentials for:", choices=choices).ask()
    if target is None:
        return

    if target == "All platforms":
        clear_all_credentials(config_dir)
        print_status(SYM_OK, "Cleared all credentials")
    else:
        clear_credentials(target, config_dir)
        print_status(SYM_OK, f"Cleared {target} credentials")


def _relogin() -> None:
    """Shortcut to re-login a platform."""
    target = questionary.select("Re-login to:", choices=list(PLATFORM_NAMES)).ask()
    if target is None:
        return

    if target == "teams":
        from ingestion.cli.auth.teams_login import run_teams_login

        run_teams_login()
    elif target == "moodle":
        from ingestion.cli.auth.moodle_login import run_moodle_login

        run_moodle_login()
    elif target == "telegram":
        from ingestion.cli.auth.telegram_login import run_telegram_login

        run_telegram_login()
