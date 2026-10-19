"""File-based state management for the CLI.

Handles all JSON file I/O: credentials, seen_ids, cursors, history.
All paths live under ~/.config/ingest/ by default.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "ingest"
SEEN_IDS_CAP = 10_000
SEEN_IDS_EVICT_RATIO = 0.2
OUTPUT_RETENTION = 20


def _ensure_dir(path: Path) -> None:
    """Create directory and parents if needed."""
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict | list:
    """Read a JSON file, returning empty dict if missing/corrupt."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: Any, *, chmod_600: bool = False) -> None:
    """Write data as JSON to path, creating parent dirs."""
    _ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    if chmod_600:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


# --- Credentials ---


def credentials_path(config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    return config_dir / "credentials.json"


def load_all_credentials(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, dict]:
    """Load all platform credentials. Returns {platform: creds_dict}."""
    data = _read_json(credentials_path(config_dir))
    if not isinstance(data, dict):
        return {}
    return data


def load_credentials(platform: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> dict | None:
    """Load credentials for a single platform."""
    all_creds = load_all_credentials(config_dir)
    return all_creds.get(platform)


def save_credentials(platform: str, creds: dict, config_dir: Path = DEFAULT_CONFIG_DIR) -> None:
    """Save credentials for a platform (overwrites existing)."""
    all_creds = load_all_credentials(config_dir)
    all_creds[platform] = creds
    _write_json(credentials_path(config_dir), all_creds, chmod_600=True)


def clear_credentials(platform: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> bool:
    """Clear credentials for a platform. Returns True if found and removed."""
    all_creds = load_all_credentials(config_dir)
    if platform not in all_creds:
        return False
    del all_creds[platform]
    _write_json(credentials_path(config_dir), all_creds, chmod_600=True)
    return True


def clear_all_credentials(config_dir: Path = DEFAULT_CONFIG_DIR) -> None:
    """Clear all credentials."""
    _write_json(credentials_path(config_dir), {}, chmod_600=True)


# --- Seen IDs (change tracking) ---


def seen_ids_path(config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    return config_dir / "seen_ids.json"


def load_seen_ids(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, dict]:
    """Load seen_ids tracking data. Returns {external_id: {hash, last_seen}}."""
    data = _read_json(seen_ids_path(config_dir))
    if not isinstance(data, dict):
        return {}
    return data


def save_seen_ids(data: dict[str, dict], config_dir: Path = DEFAULT_CONFIG_DIR) -> None:
    """Save seen_ids, applying LRU eviction if over cap."""
    if len(data) > SEEN_IDS_CAP:

        def last_seen_sort_key(item: tuple[str, dict]) -> str:
            last_seen = item[1].get("last_seen")
            return last_seen if isinstance(last_seen, str) else ""

        sorted_items = sorted(data.items(), key=last_seen_sort_key)
        evict_count = int(len(data) * SEEN_IDS_EVICT_RATIO)
        data = dict(sorted_items[evict_count:])
    _write_json(seen_ids_path(config_dir), data)


def compute_item_hash(item_dict: dict) -> str:
    """Compute a content hash for a normalized item dict."""
    body = item_dict.get("body", "")
    title = item_dict.get("title", "")
    raw = f"{title}|{body}"
    return f"sha256:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def classify_item(
    external_id: str,
    content_hash: str | None,
    seen_ids: dict[str, dict],
) -> str:
    """Classify item as NEW, UPDATED, or UNCHANGED."""
    if external_id not in seen_ids:
        return "NEW"
    existing_hash = seen_ids[external_id].get("hash", "")
    if content_hash is not None and content_hash != existing_hash:
        return "UPDATED"
    if content_hash is None:
        return "NEW"
    return "UNCHANGED"


def update_seen_id(
    external_id: str,
    content_hash: str | None,
    seen_ids: dict[str, dict],
) -> dict[str, dict]:
    """Record an item in seen_ids (immutable — returns new dict)."""
    now = datetime.now(tz=UTC).isoformat()
    entry = {"hash": content_hash or "", "last_seen": now}
    return {**seen_ids, external_id: entry}


# --- Cursors ---


def cursor_path(platform: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    return config_dir / "cursors" / f"{platform}.json"


def load_cursor(platform: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> dict | None:
    """Load cursor for a platform."""
    path = cursor_path(platform, config_dir)
    data = _read_json(path)
    if isinstance(data, dict) and data:
        return data
    return None


def save_cursor(platform: str, cursor: dict, config_dir: Path = DEFAULT_CONFIG_DIR) -> None:
    """Save cursor for a platform."""
    _write_json(cursor_path(platform, config_dir), cursor)


# --- History ---


def history_path(config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    return config_dir / "history.jsonl"


def append_history(entry: dict, config_dir: Path = DEFAULT_CONFIG_DIR) -> None:
    """Append a run entry to history.jsonl."""
    path = history_path(config_dir)
    _ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def load_history(config_dir: Path = DEFAULT_CONFIG_DIR) -> list[dict]:
    """Load all history entries, most recent first."""
    path = history_path(config_dir)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.reverse()
    return entries


# --- Output files ---


def output_dir_path(config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    return config_dir / "output"


def generate_output_filename() -> str:
    """Generate a timestamped output filename."""
    now = datetime.now(tz=UTC)
    return now.strftime("%Y-%m-%dT%H-%M") + ".json"


def save_output(
    data: dict,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    output_dir: Path | None = None,
) -> Path:
    """Save a run's output to a timestamped JSON file. Returns the file path."""
    out_dir = output_dir or output_dir_path(config_dir)
    _ensure_dir(out_dir)
    filename = generate_output_filename()
    path = out_dir / filename
    _write_json(path, data)
    enforce_output_retention(out_dir)
    return path


def enforce_output_retention(
    out_dir: Path,
    max_files: int = OUTPUT_RETENTION,
) -> None:
    """Delete old output files beyond retention limit."""
    files = sorted(out_dir.glob("*.json"), key=lambda f: f.name)
    excess = len(files) - max_files
    if excess > 0:
        for f in files[:excess]:
            f.unlink(missing_ok=True)


def list_output_files(
    config_dir: Path = DEFAULT_CONFIG_DIR,
    output_dir: Path | None = None,
) -> list[Path]:
    """List output files, newest first."""
    out_dir = output_dir or output_dir_path(config_dir)
    if not out_dir.exists():
        return []
    return sorted(out_dir.glob("*.json"), key=lambda f: f.name, reverse=True)


def load_output(path: Path) -> dict:
    """Load an output file."""
    data = _read_json(path)
    if not isinstance(data, dict):
        return {}
    return data


# --- Config ---


def config_yaml_path(config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    return config_dir / "config.yaml"


# --- Session Cookies (for silent token refresh) ---


def _session_cookies_path(platform: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    return config_dir / "sessions" / f"{platform}_cookies.json"


def save_session_cookies(
    platform: str,
    cookies: list[dict],
    config_dir: Path = DEFAULT_CONFIG_DIR,
) -> None:
    """Save browser session cookies for silent token refresh."""
    _write_json(_session_cookies_path(platform, config_dir), cookies, chmod_600=True)


def load_session_cookies(
    platform: str,
    config_dir: Path = DEFAULT_CONFIG_DIR,
) -> list[dict]:
    """Load saved browser session cookies. Returns empty list if none."""
    data = _read_json(_session_cookies_path(platform, config_dir))
    if isinstance(data, list):
        return data
    return []


# --- Token Expiry ---


def is_token_expired(token: str, buffer_minutes: int = 5) -> bool:
    """Check if a JWT token is expired (or will expire within buffer_minutes).

    Returns True if expired/expiring, False if still valid.
    Returns True on any decode failure (treat as expired -> trigger refresh).
    """
    from ingestion.cli.jwt_utils import is_expired

    return is_expired(token, buffer_minutes)
