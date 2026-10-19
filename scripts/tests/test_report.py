from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.report import build_report


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_SCRIPT = REPO_ROOT / "scripts" / "report.py"


NOW = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)


def _published_at(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def raw_db_payload() -> dict[str, list[dict[str, object]]]:
    return {
        "ingested_items": [
            {
                "id": "ingested-1",
                "platform": "moodle",
                "processing_status": "processed",
                "error_message": None,
                "payload": {
                    "published_at": _published_at(3),
                    "content_type": "assignment",
                },
            },
            {
                "id": "ingested-2",
                "platform": "teams",
                "processing_status": "failed",
                "error_message": "LLM timeout",
                "payload": {
                    "published_at": _published_at(90),
                    "content_type": "raw_message",
                },
            },
            {
                "id": "ingested-3",
                "platform": "telegram",
                "processing_status": "processed",
                "error_message": None,
                "payload": {"published_at": None, "content_type": "raw_message"},
            },
        ],
        "classified_items": [
            {
                "id": "classified-1",
                "ingested_item_id": "ingested-1",
                "platform": "moodle",
                "entity_type": "assignment",
                "title": "Homework 1",
                "dedup_status": "primary",
                "published_at": _published_at(3),
            }
        ],
        "classification_logs": [
            {
                "ingested_item_id": "ingested-3",
                "platform": "telegram",
                "entity_type": "raw_message",
                "classified_item_id": None,
            }
        ],
        "dedup_logs": [
            {
                "status": "primary",
                "platform": "moodle",
                "entity_type": "assignment",
                "error_message": None,
            }
        ],
    }


def test_build_report_summarizes_processor_run() -> None:
    report = build_report(
        raw_db_payload(),
        run_dir=Path("data/runs/example"),
        now=NOW,
        termination_cause="idle",
        time_window_days=60,
    )

    assert report.metrics["termination_cause"] == "idle"
    assert report.metrics["window"]["platforms"]["moodle"]["in_window"] == 1
    assert report.metrics["window"]["platforms"]["teams"]["out_of_window"] == 1
    assert (
        report.metrics["window"]["platforms"]["telegram"]["missing_published_at"] == 1
    )
    assert report.metrics["processing"]["by_status"]["failed"] == 1
    assert report.metrics["processing"]["raw_message_unclassified"]["telegram"] == 1
    assert report.metrics["failures"]["top_reasons"] == [
        {"reason": "LLM timeout", "count": 1}
    ]
    assert report.metrics["dedup"]["by_status"] == {"primary": 1}
    assert report.metrics["samples"]["assignment"][0]["title"] == "Homework 1"
    assert "Termination cause: `idle`" in report.markdown
    assert "| teams | 1 | 0 | 1 | 0 |" in report.markdown


def test_metrics_output_is_json_serializable() -> None:
    report = build_report(
        raw_db_payload(),
        run_dir=Path("data/runs/example"),
        now=NOW,
        termination_cause="timeout",
        time_window_days=60,
    )

    json.dumps(report.metrics)


def test_report_cli_writes_report_and_metrics_with_mocked_db(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "kind").write_text("processor\n", encoding="utf-8")
    db_payload = json.dumps(raw_db_payload())

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{docker_log}"
printf '%s\\n' '{db_payload}'
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
            "python",
            str(REPORT_SCRIPT),
            str(run_dir),
            "--termination-cause",
            "idle",
            "--time-window-days",
            "60",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert (run_dir / "report.md").exists()
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["termination_cause"] == "idle"
    assert metrics["window"]["platforms"]["moodle"]["in_window"] == 1
    docker_call = docker_log.read_text(encoding="utf-8")
    assert "psql --username=shared_db_test" not in docker_call
    assert "psql --username=postgres_test --dbname=shared_db_test" in docker_call
