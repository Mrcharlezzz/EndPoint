"""Inspect command implementation — browse fetched results."""

from __future__ import annotations

import json
from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel

from ingestion.cli.app import state
from ingestion.cli.state import list_output_files, load_output

console = Console(stderr=True)


def run_inspector(run_selector: str | None = None) -> None:
    """Browse fetched results from output JSON files."""
    config_dir = state.config_dir
    output_dir = state.output_dir

    # Resolve which output file to inspect
    path = _resolve_output_file(run_selector, config_dir, output_dir)
    if path is None:
        console.print("  [dim]No output files found. Run [bold]ingest run[/bold] first.[/dim]")
        return

    data = load_output(path)
    items = data.get("items", [])
    if not items:
        console.print(f"  [dim]No items in {path.name}[/dim]")
        return

    console.print(f"  Loading: {path.name} ({len(items)} items)")
    console.print()

    # Group by content type
    grouped: dict[str, list[dict]] = {}
    for item in items:
        ct = item.get("content_type", "unknown")
        grouped.setdefault(ct, []).append(item)

    # Build choice list
    choices: list[questionary.Choice] = []
    for ct, ct_items in sorted(grouped.items()):
        # Section header
        choices.append(
            questionary.Choice(
                title=f"── {ct.replace('_', ' ').title()} ({len(ct_items)}) ──",
                disabled="section",
            )
        )
        for item in ct_items:
            label = _build_item_label(item)
            choices.append(questionary.Choice(title=label, value=item))

    while True:
        selected = questionary.select(
            "Select item:",
            choices=choices,
            use_shortcuts=False,
        ).ask()

        if selected is None:
            break

        _show_item_detail(selected)

        action = questionary.select("", choices=["Back to list", "Show raw JSON", "Quit"]).ask()

        if action == "Show raw JSON":
            console.print_json(json.dumps(selected, indent=2, default=str))
            questionary.press_any_key_to_continue().ask()
        elif action == "Quit" or action is None:
            break


def _resolve_output_file(
    selector: str | None,
    config_dir: Path,
    output_dir: Path,
) -> Path | None:
    """Resolve the output file to inspect."""
    if selector:
        # Direct file path
        path = Path(selector)
        if path.exists():
            return path
        # Timestamp prefix search
        files = list_output_files(config_dir, output_dir)
        for f in files:
            if f.stem.startswith(selector):
                return f
        # Index
        try:
            idx = int(selector)
            if 0 <= idx < len(files):
                return files[idx]
        except ValueError:
            pass
        return None

    # Default: latest
    files = list_output_files(config_dir, output_dir)
    return files[0] if files else None


def _build_item_label(item: dict) -> str:
    """Build a display label for an item in the list."""
    badge = item.get("_badge", "")
    badge_str = f"[{badge}]" if badge and badge != "UNCHANGED" else ""

    title = item.get("title", "")
    if not title:
        body = item.get("body", "")
        course = item.get("course_name", "")
        prefix = f"[{course}] " if course else ""
        title = prefix + (body[:80] + "..." if len(body) > 80 else body)

    due = item.get("deadline", "")
    due_str = f"  due: {due[:10]}" if due else ""

    parts = [title]
    if badge_str:
        parts.append(badge_str)
    if due_str:
        parts.append(due_str)

    return "  ".join(parts)


def _show_item_detail(item: dict) -> None:
    """Show a Rich panel with item details."""
    title = item.get("title", "(no title)")
    badge = item.get("_badge", "")
    platform = item.get("platform", "")
    ct = item.get("content_type", "")
    course = item.get("course_name", "")
    author = item.get("author", "")
    due = item.get("deadline", "")
    published = item.get("published_at", "")

    lines = []
    lines.append(f"Platform: {platform}    Type: {ct}    [{badge}]")
    if course:
        lines.append(f"Course:   {course}")
    if author:
        lines.append(f"Author:   {author}")
    if due:
        lines.append(f"Due:      {due}")
    if published:
        lines.append(f"Posted:   {published}")

    body = item.get("body", "")
    if body:
        lines.append("")
        lines.append("Body:")
        # Truncate long bodies
        if len(body) > 500:
            lines.append(body[:500] + "...")
        else:
            lines.append(body)

    attachments = item.get("attachments", [])
    if attachments:
        lines.append("")
        lines.append(f"Attachments ({len(attachments)}):")
        for att in attachments[:10]:
            name = att.get("filename", "unnamed")
            size = att.get("size_bytes")
            size_str = f" ({size // 1024} KB)" if size else ""
            lines.append(f"• {name}{size_str}")

    console.print(
        Panel(
            "\n".join(lines),
            title=title,
            border_style="cyan",
        )
    )
