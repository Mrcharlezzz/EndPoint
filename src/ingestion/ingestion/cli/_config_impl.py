"""Config command implementation — interactive config editor."""

from __future__ import annotations

import os

import questionary
from rich.console import Console

from ingestion.cli.app import state
from ingestion.cli.display import SYM_OK, SYM_WARN, print_status
from ingestion.config import IngestionConfig, MoodleConfig

console = Console(stderr=True)


# Config keys with their types, defaults, and env var names
def _key(k: str, t: str, d: object, e: str) -> dict:
    return {"key": k, "type": t, "default": d, "env": e}


_CONFIG_KEYS: list[dict] = [
    _key("teams.csa_page_size", "int", 50, "ENDPOINT_TEAMS__CSA_PAGE_SIZE"),
    _key("teams.semester_start", "date", None, "ENDPOINT_TEAMS__SEMESTER_START"),
    _key("teams.requests_per_second", "float", 10.0, "ENDPOINT_TEAMS__REQUESTS_PER_SECOND"),
    _key("teams.max_retries", "int", 3, "ENDPOINT_TEAMS__MAX_RETRIES"),
    _key("moodle.base_url", "str", MoodleConfig().base_url, "ENDPOINT_MOODLE__BASE_URL"),
    _key("moodle.forum_scope", "str", "announcements", "ENDPOINT_MOODLE__FORUM_SCOPE"),
    _key("moodle.requests_per_second", "float", 10.0, "ENDPOINT_MOODLE__REQUESTS_PER_SECOND"),
    _key("telegram.poll_timeout", "int", 0, "ENDPOINT_TELEGRAM__POLL_TIMEOUT"),
    _key("telegram.max_updates", "int", 100, "ENDPOINT_TELEGRAM__MAX_UPDATES"),
    _key("telegram.requests_per_second", "float", 1.0, "ENDPOINT_TELEGRAM__REQUESTS_PER_SECOND"),
]


def run_config_editor() -> None:
    """Interactive config editor."""
    config = IngestionConfig()

    choices = []
    for entry in _CONFIG_KEYS:
        key = entry["key"]
        current_value = _get_current_value(config, key)
        source = _get_source(entry)
        label = f"{key:<35} {str(current_value):<15} ({source})"
        choices.append(questionary.Choice(title=label, value=entry))

    while True:
        selected = questionary.select(
            "Select setting to edit:",
            choices=choices,
            use_shortcuts=False,
        ).ask()

        if selected is None:
            break

        key = selected["key"]
        env_var = selected["env"]
        current = _get_current_value(config, key)

        # Warn about env var override
        if os.environ.get(env_var):
            console.print(
                f"  {SYM_WARN} {key} is set via env var {env_var}\n"
                f"    File edit would be overridden."
            )

        new_val = questionary.text(f"New value ({selected['type']}, current: {current}):").ask()

        if new_val is None or new_val == "":
            continue

        # Validate and save to config.yaml
        try:
            _save_config_value(key, new_val, selected["type"])
            print_status(SYM_OK, f"{key} = {new_val} (saved to config.yaml)")
        except Exception as exc:
            console.print(f"  [red]Invalid value: {exc}[/red]")

        # Ask to continue
        if not questionary.confirm("Edit another setting?", default=True).ask():
            break


def _get_current_value(config: IngestionConfig, key: str) -> object:
    """Get current value from config by dotted key."""
    parts = key.split(".")
    obj: object = config
    for part in parts:
        obj = getattr(obj, part, None)
    return obj


def _get_source(entry: dict) -> str:
    """Determine where the current value comes from."""
    env_var = entry["env"]
    if os.environ.get(env_var):
        return "env var"

    # Check config.yaml
    config_path = state.config_dir / "config.yaml"
    if config_path.exists():
        try:
            import yaml  # noqa: F811

            with config_path.open() as f:
                data = yaml.safe_load(f) or {}
            parts = entry["key"].split(".")
            node = data
            for part in parts:
                if isinstance(node, dict):
                    node = node.get(part)
                else:
                    node = None
                    break
            if node is not None:
                return "config.yaml"
        except Exception:
            pass

    return "default"


def _save_config_value(key: str, value: str, value_type: str) -> None:
    """Save a config value to config.yaml."""
    # Parse value
    if value_type == "int":
        parsed = int(value)
    elif value_type == "float":
        parsed = float(value)
    elif value_type == "date":
        from datetime import date

        parsed = str(date.fromisoformat(value))
    else:
        parsed = value

    config_path = state.config_dir / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if config_path.exists():
        try:
            import yaml

            with config_path.open() as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {}

    # Set nested key
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = parsed

    # Write back
    try:
        import yaml

        with config_path.open("w") as f:
            yaml.dump(data, f, default_flow_style=False)
    except ImportError:
        # Fallback: write as simple key=value if no yaml
        import json

        config_path.write_text(json.dumps(data, indent=2) + "\n")
