from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.check_ingested_data import IngestedRow, build_report


NOW = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)


def make_row(
    *,
    external_id: str,
    platform: str,
    published_at: str | None = "2026-04-10T09:00:00Z",
    course_external_id: str | None = "course-1",
    course_name: str | None = "Course 1",
    payload: dict[str, object] | None = None,
) -> IngestedRow:
    row_payload = {
        "published_at": published_at,
        "title": f"Title {external_id}",
        "content_type": "announcement",
        "course_name": course_name,
        "body": "Body",
    }
    if payload is not None:
        row_payload = payload
    return IngestedRow(
        external_id=external_id,
        platform=platform,
        course_external_id=course_external_id,
        payload=row_payload,
        created_at="2026-04-10T09:01:00+00:00",
        updated_at="2026-04-10T09:02:00+00:00",
        processing_status="pending",
    )


def valid_rows() -> list[IngestedRow]:
    return [
        make_row(external_id="teams-1", platform="teams"),
        make_row(external_id="teams-2", platform="teams"),
        make_row(external_id="moodle-1", platform="moodle"),
        make_row(external_id="moodle-2", platform="moodle"),
        make_row(
            external_id="telegram-1",
            platform="telegram",
            course_external_id=None,
            course_name=None,
        ),
        make_row(
            external_id="telegram-2",
            platform="telegram",
            course_external_id=None,
            course_name=None,
        ),
    ]


def test_empty_dataset_fails() -> None:
    report = build_report([], now=NOW, window_days=60, allowed_empty_platforms=set())

    assert report.passed is False
    assert "No ingested_items rows found." in report.critical_failures


def test_missing_telegram_fails_unless_allowed() -> None:
    rows = [row for row in valid_rows() if row.platform != "telegram"]

    failing_report = build_report(
        rows,
        now=NOW,
        window_days=60,
        allowed_empty_platforms=set(),
    )
    allowed_report = build_report(
        rows,
        now=NOW,
        window_days=60,
        allowed_empty_platforms={"telegram"},
    )

    assert failing_report.passed is False
    assert "Platform telegram has 0 items." in failing_report.critical_failures
    assert allowed_report.passed is True


def test_published_at_coverage_below_90_percent_fails() -> None:
    rows = valid_rows()
    rows.extend(
        make_row(
            external_id=f"teams-missing-{index}",
            platform="teams",
            published_at=None,
        )
        for index in range(1, 3)
    )

    report = build_report(rows, now=NOW, window_days=60, allowed_empty_platforms=set())

    assert report.passed is False
    assert (
        "Platform teams published_at coverage 50.0% is below required 90.0%."
        in report.critical_failures
    )


def test_moodle_and_teams_missing_course_data_fails() -> None:
    rows = valid_rows()
    rows.append(
        make_row(
            external_id="moodle-missing-course",
            platform="moodle",
            course_external_id=None,
            course_name=None,
        )
    )

    report = build_report(rows, now=NOW, window_days=60, allowed_empty_platforms=set())

    assert report.passed is False
    assert (
        "Platform moodle course resolution 66.7% is below required 90.0%."
        in report.critical_failures
    )


def test_valid_mixed_dataset_passes() -> None:
    report = build_report(
        valid_rows(), now=NOW, window_days=60, allowed_empty_platforms=set()
    )

    assert report.passed is True
    assert report.critical_failures == []
    assert report.metrics["platforms"]["teams"]["window"]["in_window"] == 2
    assert (
        report.metrics["platforms"]["telegram"]["course_resolution"]["required"]
        is False
    )


def test_metrics_output_is_json_serializable() -> None:
    report = build_report(
        valid_rows(), now=NOW, window_days=60, allowed_empty_platforms=set()
    )

    json.dumps(report.metrics)
