#!/usr/bin/env python3
"""Load offline ingested-item fixture JSON files into Postgres.

The loader accepts a directory containing JSON exports. It loads files that
contain an ingested item list and skips unrelated JSON files such as metrics or
reports. Supported shapes are:

- {"items": [...]}
- {"ingested_items": [...]}
- [...]

Rows are inserted into the current `ingested_items` schema. Source items can be
current DB-row-shaped fixtures with a `payload`, or normalized ingestion items
where the payload must be built from top-level fields.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PLATFORMS = {"moodle", "teams", "telegram"}
PAYLOAD_SCHEMA_VERSION = "4.0"
PAYLOAD_FIELDS = (
    "content_type",
    "title",
    "author",
    "body",
    "course_name",
    "course_external_id",
    "published_at",
    "deadline",
    "submission_format",
    "start_time",
    "end_time",
    "location",
    "attachments",
    "links",
)


@dataclass(frozen=True)
class SkippedFixture:
    path: Path
    reason: str


@dataclass(frozen=True)
class CollectedFixtures:
    items: list[dict[str, Any]]
    loaded_files: int
    skipped_files: list[SkippedFixture]


def collect_ingested_items(directory: Path) -> CollectedFixtures:
    """Collect ingested items from JSON files directly under `directory`."""
    items: list[dict[str, Any]] = []
    loaded_files = 0
    skipped_files: list[SkippedFixture] = []

    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            skipped_files.append(SkippedFixture(path=path, reason=str(exc)))
            continue

        extracted = extract_ingested_item_list(document)
        if extracted is None:
            skipped_files.append(
                SkippedFixture(path=path, reason="not an ingested item list")
            )
            continue

        loaded_files += 1
        items.extend(extracted)

    return CollectedFixtures(
        items=items,
        loaded_files=loaded_files,
        skipped_files=skipped_files,
    )


def extract_ingested_item_list(document: Any) -> list[dict[str, Any]] | None:
    """Return an ingested item list from a supported JSON document shape."""
    candidate: Any
    if isinstance(document, list):
        candidate = document
    elif isinstance(document, dict) and isinstance(document.get("items"), list):
        candidate = document["items"]
    elif isinstance(document, dict) and isinstance(
        document.get("ingested_items"), list
    ):
        candidate = document["ingested_items"]
    else:
        return None

    if not candidate:
        return None
    if not all(_looks_like_ingested_item(item) for item in candidate):
        return None
    return list(candidate)


def _looks_like_ingested_item(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    platform = value.get("platform")
    if platform not in EXPECTED_PLATFORMS:
        return False
    if not isinstance(value.get("external_id"), str) or not value["external_id"]:
        return False
    if isinstance(value.get("payload"), dict):
        return isinstance(value["payload"].get("content_type"), str)
    return isinstance(value.get("content_type"), str) and isinstance(
        value.get("body"), str
    )


def item_to_row(item: dict[str, Any]) -> dict[str, Any]:
    """Map a fixture item into the current `ingested_items` row shape."""
    payload = _payload_from_item(item)
    raw_payload = item.get("raw_payload")
    if not isinstance(raw_payload, dict):
        raw_payload = {}

    return {
        "id": _resolve_row_id(item),
        "external_id": item["external_id"],
        "platform": item["platform"],
        "course_external_id": item.get("course_external_id")
        or payload.get("course_external_id"),
        "source_url": item.get("source_url"),
        "schema_version": item.get("schema_version") or PAYLOAD_SCHEMA_VERSION,
        "adapter_version": item.get("adapter_version")
        or raw_payload.get("adapter_version")
        or "",
        "ingestion_mode": item.get("ingestion_mode")
        or raw_payload.get("ingestion_mode")
        or "server_poll",
        # Offline fixture loads are pre-process baselines, so reset work to pending.
        "processing_status": "pending",
        "error_message": None,
        "payload": payload,
        "raw_payload": raw_payload,
    }


def _resolve_row_id(item: dict[str, Any]) -> str:
    raw_id = item.get("id")
    if isinstance(raw_id, str):
        try:
            return str(uuid.UUID(raw_id))
        except ValueError:
            pass
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"endpoint-ingested:{item['external_id']}")
    )


def _payload_from_item(item: dict[str, Any]) -> dict[str, Any]:
    existing_payload = item.get("payload")
    if isinstance(existing_payload, dict):
        return dict(existing_payload)

    payload = {field: item.get(field) for field in PAYLOAD_FIELDS}
    payload["attachments"] = _list_or_empty(item.get("attachments"))
    payload["links"] = _list_or_empty(item.get("links"))
    return payload


def _list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def build_sql_script(rows: list[dict[str, Any]], *, replace: bool) -> str:
    statements = ["BEGIN;"]
    if replace:
        statements.append("TRUNCATE TABLE ingested_items RESTART IDENTITY CASCADE;")

    for row in rows:
        statements.append(_insert_statement(row))

    statements.append("COMMIT;")
    statements.append("")
    return "\n".join(statements)


def _insert_statement(row: dict[str, Any]) -> str:
    values = [
        _sql_literal(row["id"]),
        _sql_literal(row["external_id"]),
        _sql_literal(row["platform"]),
        _sql_literal(row["course_external_id"]),
        _sql_literal(row["source_url"]),
        _sql_literal(row["schema_version"]),
        _sql_literal(row["adapter_version"]),
        _sql_literal(row["ingestion_mode"]),
        _sql_literal(row["processing_status"]),
        _sql_literal(row["error_message"]),
        _jsonb_literal(row["payload"]),
        _jsonb_literal(row["raw_payload"]),
    ]
    return (
        "INSERT INTO ingested_items\n"
        "    (id, external_id, platform, course_external_id, source_url,\n"
        "     schema_version, adapter_version, ingestion_mode,\n"
        "     processing_status, error_message, payload, raw_payload,\n"
        "     created_at, updated_at)\n"
        f"VALUES ({', '.join(values)}, NOW(), NOW())\n"
        "ON CONFLICT (external_id) DO UPDATE SET\n"
        "    course_external_id = EXCLUDED.course_external_id,\n"
        "    source_url = EXCLUDED.source_url,\n"
        "    schema_version = EXCLUDED.schema_version,\n"
        "    adapter_version = EXCLUDED.adapter_version,\n"
        "    ingestion_mode = EXCLUDED.ingestion_mode,\n"
        "    processing_status = 'pending',\n"
        "    error_message = NULL,\n"
        "    payload = EXCLUDED.payload,\n"
        "    raw_payload = EXCLUDED.raw_payload,\n"
        "    updated_at = EXCLUDED.updated_at;"
    )


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("'", "''")
    return f"'{text}'"


def _jsonb_literal(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True).replace("'", "''")
    return f"'{text}'::jsonb"


def run_psql(sql: str) -> None:
    postgres_db = os.environ.get("POSTGRES_DB") or _read_env_var("POSTGRES_DB")
    postgres_user = os.environ.get("POSTGRES_USER") or _read_env_var("POSTGRES_USER")
    if not postgres_db or not postgres_user:
        raise RuntimeError("POSTGRES_DB and POSTGRES_USER must be set in env or .env")

    command = [
        "docker",
        "compose",
        "-f",
        str(REPO_ROOT / "docker-compose.yml"),
        "exec",
        "-T",
        "db",
        "psql",
        f"--username={postgres_user}",
        f"--dbname={postgres_db}",
        "--set=ON_ERROR_STOP=on",
    ]
    subprocess.run(command, input=sql, text=True, check=True, cwd=REPO_ROOT)


def _read_env_var(key: str) -> str:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return ""
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        env_key, value = line.split("=", 1)
        if env_key.strip() == key:
            return value.strip()
    return ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load ingested_items fixture JSON files from a directory."
    )
    parser.add_argument("directory", type=Path, help="Directory containing JSON files")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Truncate ingested_items and dependent processor output before loading.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and validate fixtures without writing to Postgres.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    directory = args.directory
    if not directory.is_dir():
        print(f"Fixture directory not found: {directory}", file=sys.stderr)
        return 2

    collected = collect_ingested_items(directory)
    if not collected.items:
        print(f"No ingested item fixtures found in {directory}", file=sys.stderr)
        return 1

    rows = [item_to_row(item) for item in collected.items]
    action = "Would load" if args.dry_run else "Loading"
    print(
        f"{action} {len(rows)} ingested_items rows from "
        f"{collected.loaded_files} JSON file(s)."
    )
    for skipped in collected.skipped_files:
        print(f"Skipping {skipped.path.name}: {skipped.reason}")

    if args.dry_run:
        return 0

    try:
        run_psql(build_sql_script(rows, replace=args.replace))
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Load failed: {exc}", file=sys.stderr)
        return 2

    print(f"Loaded {len(rows)} ingested_items rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
