from __future__ import annotations

import json
from pathlib import Path

from processing.models.envelope import ProcessingEnvelope
from processing.models.invariants import InvariantCode, InvariantWarning, Severity
from scripts.scan_data_quality import (
    SIMULATED_RESOLVED_COURSE_ID,
    format_report,
    load_from_fixtures,
)


def test_format_report_summarizes_findings():
    report = format_report(
        total_items=3,
        findings_by_item={
            "item-1": [
                InvariantWarning(
                    code=InvariantCode.GHOST_ATTACHMENT,
                    severity=Severity.WARNING,
                    field="filename",
                    message="ghost attachment",
                )
            ],
            "item-2": [
                InvariantWarning(
                    code=InvariantCode.NO_TITLE_OR_BODY,
                    severity=Severity.ERROR,
                    field="title,body",
                    message="missing both title and body",
                )
            ],
        },
        verbose=False,
    )

    assert "Scanned: 3 items" in report
    assert "Warnings: 1 (across 1 items)" in report
    assert "Errors: 1 (across 1 items)" in report
    assert "Clean: 1 items" in report
    assert "ghost_attachment" in report
    assert "no_title_or_body" in report


def test_load_from_fixtures_normalizes_text(tmp_path: Path):
    fixture_path = tmp_path / "fixtures.json"
    fixture_path.write_text(
        json.dumps(
            {
                "ingested_items": [
                    {
                        "id": "f25ee41d-a942-4d06-b10d-2857368ed8dc",
                        "external_id": "moodle_assign_1",
                        "platform": "moodle",
                        "course_external_id": "8413",
                        "schema_version": "4.0",
                        "adapter_version": "v0",
                        "ingestion_mode": "server_poll",
                        "source_url": None,
                        "processing_status": "pending",
                        "payload": {
                            "content_type": "assignment",
                            "title": "  Homework   1  ",
                            "author": "  Ada   Lovelace  ",
                            "body": "Line 1\r\n\r\nLine\u00a02",
                            "course_name": "SYNTHETIC EXAMPLE COURSE",
                            "published_at": "2025-10-02T07:47:19+00:00",
                            "deadline": "2025-10-09T21:59:00+00:00",
                            "submission_format": "file",
                            "start_time": None,
                            "end_time": None,
                            "location": None,
                            "attachments": [],
                            "links": [],
                        },
                        "raw_payload": {},
                    }
                ]
            }
        )
    )

    envelopes = load_from_fixtures(fixture_path)

    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert isinstance(envelope, ProcessingEnvelope)
    assert envelope.entity.title == "Homework 1"
    assert envelope.entity.author == "Ada Lovelace"
    assert envelope.entity.body == "Line 1\n\nLine 2"
    assert envelope.resolved_course_id == SIMULATED_RESOLVED_COURSE_ID


def test_load_from_fixtures_keeps_unsupported_structured_content_scannable(
    tmp_path: Path,
):
    fixture_path = tmp_path / "fixtures.json"
    fixture_path.write_text(
        json.dumps(
            {
                "ingested_items": [
                    {
                        "id": "f25ee41d-a942-4d06-b10d-2857368ed8dc",
                        "external_id": "moodle_unknown_1",
                        "platform": "moodle",
                        "course_external_id": None,
                        "schema_version": "4.0",
                        "adapter_version": "v0",
                        "ingestion_mode": "server_poll",
                        "source_url": None,
                        "processing_status": "pending",
                        "payload": {
                            "content_type": "unknown_type",
                            "title": "Lecture 1",
                            "author": None,
                            "body": None,
                            "course_name": None,
                            "published_at": None,
                            "deadline": None,
                            "submission_format": None,
                            "start_time": None,
                            "end_time": None,
                            "location": None,
                            "attachments": [],
                            "links": [],
                        },
                        "raw_payload": {},
                    }
                ]
            }
        )
    )

    envelopes = load_from_fixtures(fixture_path)

    assert len(envelopes) == 1
    assert envelopes[0].entity.entity_type == "announcement"
