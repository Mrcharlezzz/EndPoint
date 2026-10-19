from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.load_ingested_fixtures import (
    build_sql_script,
    collect_ingested_items,
    item_to_row,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
LOADER_SCRIPT = REPO_ROOT / "scripts" / "load_ingested_fixtures.py"


def _normalized_item(external_id: str = "tg_1_10") -> dict[str, object]:
    return {
        "external_id": external_id,
        "platform": "telegram",
        "content_type": "raw_message",
        "ingestion_mode": "desktop_export",
        "raw_payload": {
            "schema_version": "2",
            "source": "telegram_desktop_export",
            "ingestion_mode": "desktop_export",
            "adapter_version": "",
            "original": {"id": 10},
        },
        "title": None,
        "author": "Alice",
        "body": "Class moved to Friday",
        "source_url": None,
        "course_name": "Software Engineering and Design CUB Spring 2026",
        "course_external_id": "3709134149",
        "published_at": "2026-03-27T19:08:04Z",
        "deadline": None,
        "submission_format": None,
        "start_time": None,
        "end_time": None,
        "location": None,
        "attachments": [],
        "links": [],
    }


def test_collects_items_from_supported_json_shapes(tmp_path: Path) -> None:
    item_a = _normalized_item("tg_1_10")
    item_b = _normalized_item("tg_1_11")
    item_c = _normalized_item("tg_1_12")
    (tmp_path / "01-object-items.json").write_text(
        json.dumps({"items": [item_a]}), encoding="utf-8"
    )
    (tmp_path / "02-object-ingested-items.json").write_text(
        json.dumps({"ingested_items": [item_b]}), encoding="utf-8"
    )
    (tmp_path / "03-root-list.json").write_text(json.dumps([item_c]), encoding="utf-8")

    result = collect_ingested_items(tmp_path)

    assert [item["external_id"] for item in result.items] == [
        "tg_1_10",
        "tg_1_11",
        "tg_1_12",
    ]
    assert result.loaded_files == 3
    assert result.skipped_files == []


def test_skips_json_files_without_ingested_item_lists(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text(
        json.dumps({"total_items": 10, "platforms": {"telegram": 10}}),
        encoding="utf-8",
    )
    (tmp_path / "bad-list.json").write_text(
        json.dumps([{"not": "an ingested item"}]),
        encoding="utf-8",
    )
    (tmp_path / "valid.json").write_text(
        json.dumps({"items": [_normalized_item()]}), encoding="utf-8"
    )

    result = collect_ingested_items(tmp_path)

    assert [item["external_id"] for item in result.items] == ["tg_1_10"]
    assert result.loaded_files == 1
    assert [skipped.path.name for skipped in result.skipped_files] == [
        "bad-list.json",
        "metrics.json",
    ]


def test_item_to_row_maps_normalized_item_to_current_ingested_schema() -> None:
    row = item_to_row(_normalized_item())

    assert row["external_id"] == "tg_1_10"
    assert row["platform"] == "telegram"
    assert row["course_external_id"] == "3709134149"
    assert row["source_url"] is None
    assert row["schema_version"] == "4.0"
    assert row["adapter_version"] == ""
    assert row["ingestion_mode"] == "desktop_export"
    assert row["processing_status"] == "pending"
    assert row["error_message"] is None
    assert row["payload"] == {
        "content_type": "raw_message",
        "title": None,
        "author": "Alice",
        "body": "Class moved to Friday",
        "course_name": "Software Engineering and Design CUB Spring 2026",
        "course_external_id": "3709134149",
        "published_at": "2026-03-27T19:08:04Z",
        "deadline": None,
        "submission_format": None,
        "start_time": None,
        "end_time": None,
        "location": None,
        "attachments": [],
        "links": [],
    }
    assert row["raw_payload"]["source"] == "telegram_desktop_export"


def test_item_to_row_preserves_existing_payload_shape() -> None:
    item = {
        "id": "11111111-1111-1111-1111-111111111111",
        "external_id": "teams_csa_1",
        "platform": "teams",
        "course_external_id": "team-1",
        "source_url": "https://example.test/source",
        "schema_version": "4.0",
        "adapter_version": "2.1.0",
        "ingestion_mode": "server_poll",
        "processing_status": "processed",
        "payload": {
            "content_type": "raw_message",
            "body": "hello",
            "course_name": "Course",
            "published_at": "2026-04-01T00:00:00Z",
        },
        "raw_payload": {"source": "teams_csa"},
    }

    row = item_to_row(item)

    assert row["id"] == "11111111-1111-1111-1111-111111111111"
    assert row["payload"] == item["payload"]
    assert row["processing_status"] == "pending"
    assert row["error_message"] is None


def test_build_sql_script_truncates_and_upserts_rows() -> None:
    rows = [item_to_row(_normalized_item("tg_1_10"))]

    sql = build_sql_script(rows, replace=True)

    assert "TRUNCATE TABLE ingested_items RESTART IDENTITY CASCADE;" in sql
    assert "INSERT INTO ingested_items" in sql
    assert "'tg_1_10'" in sql
    assert "ON CONFLICT (external_id) DO UPDATE SET" in sql
    assert "'pending'" in sql
    assert "::jsonb" in sql


def test_cli_dry_run_does_not_call_docker(tmp_path: Path) -> None:
    (tmp_path / "items.json").write_text(
        json.dumps({"items": [_normalized_item()]}), encoding="utf-8"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(LOADER_SCRIPT),
            str(tmp_path),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0
    assert "Would load 1 ingested_items rows from 1 JSON file(s)." in result.stdout


def test_cli_executes_psql_with_generated_sql(tmp_path: Path) -> None:
    (tmp_path / "items.json").write_text(
        json.dumps({"items": [_normalized_item()]}), encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    stdin_log = tmp_path / "stdin.sql"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{docker_log}"
if ! [ -t 0 ]; then
    cat >> "{stdin_log}"
fi
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "POSTGRES_DB": "shared_db_test",
        "POSTGRES_USER": "postgres_test",
    }

    result = subprocess.run(
        [
            sys.executable,
            str(LOADER_SCRIPT),
            str(tmp_path),
            "--replace",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    docker_call = docker_log.read_text(encoding="utf-8")
    assert "compose -f" in docker_call
    assert "psql --username=postgres_test --dbname=shared_db_test" in docker_call
    stdin = stdin_log.read_text(encoding="utf-8")
    assert "TRUNCATE TABLE ingested_items RESTART IDENTITY CASCADE;" in stdin
    assert "'tg_1_10'" in stdin
