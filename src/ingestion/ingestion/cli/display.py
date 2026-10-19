"""Shared display utilities — Rich panels, spinners, prompts, formatters."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(stderr=True)
stdout_console = Console()

# Canonical platform ordering used across all CLI commands
PLATFORM_NAMES: tuple[str, ...] = ("teams", "moodle", "telegram")

# Status symbols
SYM_OK = "[green]✔[/green]"
SYM_FAIL = "[red]✖[/red]"
SYM_WARN = "[yellow]⚠[/yellow]"
SYM_PENDING = "[dim]○[/dim]"
SYM_RATE_LIMIT = "[yellow]⏳[/yellow]"


def print_status(symbol: str, message: str) -> None:
    """Print a status line to stderr."""
    console.print(f"  {symbol} {message}")


def print_error_panel(title: str, body: str) -> None:
    """Print a red error panel."""
    console.print(Panel(body, title=f"[red]{title}[/red]", border_style="red"))


def print_success_panel(title: str, body: str) -> None:
    """Print a green success panel."""
    console.print(Panel(body, title=f"[green]{title}[/green]", border_style="green"))


def format_duration(seconds: float) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remaining = seconds % 60
    return f"{minutes}m{remaining:.0f}s"


def format_token_redacted(token: str, visible: int = 8) -> str:
    """Redact a token, showing first N chars.

    For short tokens (< 20 chars), shows fewer characters to avoid
    leaking identifiable structure (e.g. Telegram bot token format).
    """
    if not token:
        return "(empty)"
    effective_visible = min(visible, max(4, len(token) // 4))
    return token[:effective_visible] + "...****"


def make_stats_table(stats: dict) -> Table:
    """Create a Rich table from stats dict."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value")
    for key, value in stats.items():
        table.add_row(str(key), str(value))
    return table


def format_badge(badge: str) -> Text:
    """Format a NEW/UPDATED/UNCHANGED badge with color."""
    if badge == "NEW":
        return Text("[NEW]", style="green bold")
    if badge == "UPDATED":
        return Text("[UPD]", style="yellow bold")
    return Text("[---]", style="dim")


def format_platform_header(platform: str, status: str = "") -> str:
    """Format a platform header line."""
    if status:
        return f"  [bold]{platform}[/bold]  {status}"
    return f"  [bold]{platform}[/bold]"
