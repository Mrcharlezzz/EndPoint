from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.static_run_inspector import (  # noqa: E402
    build_inspector_document,
    build_inspector_rows,
    render_html,
    summarize_flags,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
INSPECTOR_SCRIPT = REPO_ROOT / "scripts" / "static_run_inspector.py"


def test_build_inspector_rows_flags_publishable_quality_issues() -> None:
    raw_data = {
        "classified_rows": [
            {
                "classified_item_id": "classified-empty",
                "ingested_item_id": "ingested-empty",
                "external_id": "tg_3736855178_352",
                "platform": "telegram",
                "entity_type": "material",
                "course_id": "course-jvm",
                "course_name": "Development in JVM Languages CUB Spring 2026",
                "course_code": None,
                "title": None,
                "body": "",
                "published_at": "2026-04-02T14:55:21Z",
                "source_url": None,
                "dedup_status": "primary",
                "canonical_entity_id": "canonical-empty",
                "source_content_type": "raw_message",
                "source_title": None,
                "source_body": "",
                "source_published_at": "2026-04-02T14:55:21Z",
                "source_payload": {},
                "deadline": None,
                "submission_format": None,
                "start_time": None,
                "end_time": None,
                "location": None,
                "classification_entity_type": "material",
                "classification_method": "llm",
                "llm_model": "gpt-5-nano",
                "confidence": 0.62,
                "processor_version": "processor-v1",
                "prompt_version": "classification-prompts-v1",
                "processing_time_ms": 19343,
                "llm_raw_output": "",
                "dedup_log_status": "primary",
                "match_strategy": "WeightedMatchStrategy",
                "resolution_strategy": "FirstSeenResolutionStrategy",
                "dedup_score": 0.0,
                "link_count": 0,
                "attachment_count": 0,
            },
            {
                "classified_item_id": "classified-json",
                "ingested_item_id": "ingested-json",
                "external_id": "teams_csa_1764767858077",
                "platform": "teams",
                "entity_type": "event",
                "course_id": "course-maac",
                "course_name": "Matrix Algebra and Advanced Calculus I",
                "course_code": None,
                "title": None,
                "body": '{"scopeId":"e1884960","storageId":"abc"}',
                "published_at": "2025-12-03T13:17:38.077Z",
                "source_url": None,
                "dedup_status": "primary",
                "canonical_entity_id": "canonical-json",
                "source_content_type": "raw_message",
                "source_title": None,
                "source_body": '{"scopeId":"e1884960","storageId":"abc"}',
                "source_published_at": "2025-12-03T13:17:38.077Z",
                "source_payload": {"body": '{"scopeId":"e1884960"}'},
                "deadline": None,
                "submission_format": None,
                "start_time": None,
                "end_time": None,
                "location": None,
                "classification_entity_type": "event",
                "classification_method": "llm",
                "llm_model": "gpt-5-nano",
                "confidence": 0.65,
                "processor_version": "processor-v1",
                "prompt_version": "classification-prompts-v1",
                "processing_time_ms": 23000,
                "llm_raw_output": "",
                "dedup_log_status": "primary",
                "match_strategy": "WeightedMatchStrategy",
                "resolution_strategy": "FirstSeenResolutionStrategy",
                "dedup_score": 0.0,
                "link_count": 0,
                "attachment_count": 0,
            },
        ]
    }

    rows = build_inspector_rows(raw_data)

    assert rows[0]["classified"]["title"] is None
    assert rows[0]["flags"] == [
        "empty_publishable",
        "missing_title",
        "missing_body",
        "missing_source_url",
        "missing_llm_raw_output",
        "generic_title",
    ]
    assert rows[1]["event"]["start_time"] is None
    assert rows[1]["flags"] == [
        "missing_title",
        "json_body",
        "missing_start_time",
        "missing_source_url",
        "missing_llm_raw_output",
        "generic_title",
    ]


def test_summarize_flags_counts_rows_and_flags() -> None:
    rows = [
        {"flags": ["missing_title", "missing_source_url"]},
        {"flags": ["missing_source_url"]},
        {"flags": []},
    ]

    summary = summarize_flags(rows)

    assert summary == {
        "rows": 3,
        "flagged_rows": 2,
        "flags": {"missing_source_url": 2, "missing_title": 1},
    }


def test_build_inspector_document_adds_summary_counts() -> None:
    raw_data = {
        "classified_rows": [
            {
                "classified_item_id": "classified-1",
                "ingested_item_id": "ingested-1",
                "external_id": "teams-1",
                "platform": "teams",
                "entity_type": "event",
                "course_id": "course-1",
                "course_name": "Course 1",
                "course_code": "C1",
                "title": "Meeting in GeneralPlay",
                "body": "Meeting in GeneralPlay",
                "published_at": "2025-11-24T14:36:37.855Z",
                "source_url": None,
                "dedup_status": "primary",
                "canonical_entity_id": "canonical-1",
                "source_content_type": "raw_message",
                "source_title": None,
                "source_body": "Meeting in GeneralPlay",
                "source_published_at": "2025-11-24T14:36:37.855Z",
                "source_payload": {},
                "deadline": None,
                "submission_format": None,
                "start_time": None,
                "end_time": None,
                "location": "GeneralPlay",
                "classification_entity_type": "event",
                "classification_method": "llm",
                "llm_model": "gpt-5-nano",
                "confidence": 0.65,
                "processor_version": "processor-v1",
                "prompt_version": "classification-prompts-v1",
                "processing_time_ms": 23000,
                "llm_raw_output": "",
                "dedup_log_status": "primary",
                "match_strategy": "WeightedMatchStrategy",
                "resolution_strategy": "FirstSeenResolutionStrategy",
                "dedup_score": 0.0,
                "link_count": 0,
                "attachment_count": 0,
            }
        ]
    }

    document = build_inspector_document(
        raw_data,
        run_dir=Path("data/runs/example"),
        generated_at=datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
    )

    assert document["generated_at"] == "2026-04-21T12:00:00Z"
    assert document["run_dir"] == "data/runs/example"
    assert document["summary"]["rows"] == 1
    assert document["summary"]["flagged_rows"] == 1
    assert document["summary"]["flags"]["missing_start_time"] == 1
    assert document["summary"]["by_platform"] == {"teams": 1}
    assert document["summary"]["by_entity_type"] == {"event": 1}


def test_render_html_contains_filters_content_and_escaped_data() -> None:
    document = {
        "generated_at": "2026-04-21T12:00:00Z",
        "run_dir": "data/runs/example",
        "summary": {
            "rows": 1,
            "flagged_rows": 1,
            "flags": {"missing_source_url": 1},
            "by_platform": {"teams": 1},
            "by_entity_type": {"announcement": 1},
        },
        "rows": [
            {
                "classified_item_id": "classified-1",
                "external_id": "teams-1",
                "platform": "teams",
                "entity_type": "announcement",
                "course": {"name": "Course 1"},
                "classified": {
                    "title": "Visible <script>alert(1)</script>",
                    "body": 'Visible body <img src=x onerror="alert(1)">',
                    "published_at": "2026-04-01T10:00:00Z",
                    "source_url": "https://teams.example/source?x=<unsafe>",
                    "dedup_status": "primary",
                },
                "source": {
                    "body": "Raw source",
                    "payload": {"html": "<b>not markup</b>"},
                },
                "classification": {
                    "model": "gpt-5-nano",
                    "confidence": 0.72,
                    "processor_version": "processor-v1",
                    "prompt_version": "classification-prompts-v1",
                },
                "dedup": {"status": "primary", "score": 0.0},
                "flags": ["missing_source_url"],
                "counts": {"links": 0, "attachments": 0},
            }
        ],
    }

    html = render_html(document)

    assert "<title>Endpoint Static Run Inspector</title>" in html
    assert "Only flagged" in html
    assert '<label>Course<select id="course">' in html
    assert "missing_source_url" in html
    assert "Visible &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert "Source URL" in html
    assert "https://teams.example/source?x=&lt;unsafe&gt;" in html
    assert "<h3>Body</h3>" in html
    assert "Classified body" not in html
    assert "Source body" not in html
    assert '<script type="application/json" id="inspector-data">' in html
    assert "<script>alert(1)</script>" not in html
    assert '<img src=x onerror="alert(1)">' not in html


def test_cli_writes_inspector_artifacts_with_fake_docker(tmp_path: Path) -> None:
    db_payload = {"classified_rows": []}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{docker_log}"
printf '%s\\n' '{json.dumps(db_payload)}'
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

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = subprocess.run(
        ["python", str(INSPECTOR_SCRIPT), str(run_dir)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    docker_call = docker_log.read_text(encoding="utf-8")
    assert "psql --username=postgres_test --dbname=shared_db_test" in docker_call
    assert (run_dir / "inspection" / "inspector-data.json").exists()
    assert (run_dir / "inspection" / "index.html").exists()
    assert "Inspector written:" in result.stdout
