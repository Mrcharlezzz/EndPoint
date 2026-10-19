"""History command implementation — view past sync runs."""

from __future__ import annotations

import questionary
from rich.console import Console
from rich.panel import Panel

from ingestion.cli.app import state
from ingestion.cli.display import format_duration
from ingestion.cli.state import load_history

console = Console(stderr=True)


def run_history() -> None:
    """Display scrollable list of past sync runs."""
    config_dir = state.config_dir
    entries = load_history(config_dir)

    if not entries:
        console.print("  [dim]No history yet. Run [bold]ingest run[/bold] first.[/dim]")
        return

    choices = []
    for entry in entries:
        ts = entry.get("timestamp", entry.get("started_at", "unknown"))[:16]
        platforms = ", ".join(entry.get("platforms", [entry.get("platform", "?")]))
        items = entry.get("items_fetched", 0)
        errors = entry.get("errors", 0)
        duration = entry.get("duration_seconds", 0)

        label = (
            f"{ts}  {platforms:<20} {items:>4} items  "
            f"{errors} {'error' if errors == 1 else 'errors'}  "
            f"{format_duration(duration)}"
        )
        choices.append(questionary.Choice(title=label, value=entry))

    while True:
        selected = questionary.select(
            "Past runs:",
            choices=choices,
            use_shortcuts=False,
        ).ask()

        if selected is None:
            break

        _show_run_details(selected)

        action = questionary.select("", choices=["Back to list", "Quit"]).ask()
        if action == "Quit" or action is None:
            break


def _show_run_details(entry: dict) -> None:
    """Show detailed stats for a single run."""
    lines = []
    for key, value in entry.items():
        if key == "output_file":
            lines.append(f"Output: {value}")
        elif isinstance(value, list):
            lines.append(f"{key}: {', '.join(str(v) for v in value)}")
        else:
            lines.append(f"{key}: {value}")

    console.print(
        Panel(
            "\n".join(lines),
            title="Run Details",
            border_style="cyan",
        )
    )
