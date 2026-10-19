"""SYNTHETIC EXAMPLE manual LLM smoke test.

Run explicitly with local credentials and database.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from core.models import (
    Assignment as AssignmentORM,
    BaseClassifiedItem,
    ClassificationLog,
    Event as EventORM,
    Link,
    Material as MaterialORM,
)
from core.models.ingested import IngestedItem
from processing.adapters.llm.classifier import LLMClassifier
from processing.adapters.postgres import mapper
from processing.adapters.postgres.repository import PostgresEntityRepository
from processing.config import ProcessingConfig
from processing.pipeline import ProcessingPipeline

UNIVERSITY_TIMEZONE = ZoneInfo("Europe/Amsterdam")

REQUIRED_ENV_VARS = (
    "RUN_LLM_LIVE_TESTS",
    "ENDPOINT_LLM_API_KEY",
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
)

# SYNTHETIC EXAMPLE: one safe sample for optional manual LLM smoke testing.
LIVE_DATASET_SMOKE_CASES: list[dict[str, Any]] = [
    {
        "dataset_id": "synthetic-example-1",
        "expected_entity_type": "assignment",
        "source_url": None,
        "payload": {
            "content_type": "raw_message", "title": None,
            "author": "Example Teacher",
            "body": "Please submit the example worksheet by Friday at 17:00.",
            "course_name": "SYNTHETIC EXAMPLE COURSE",
            "published_at": "2025-01-01T09:00:00+00:00",
            "deadline": None, "submission_format": None,
            "start_time": None, "end_time": None, "location": None,
            "attachments": [], "links": [],
        },
        "expected": {"entity_type": "assignment", "deadline": "2025-01-03T17:00:00+00:00", "links": []},
    },
]


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _local_wall_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.isoformat(timespec="seconds")
    return (
        value.astimezone(UNIVERSITY_TIMEZONE)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


def _serialized_links(classified_item: BaseClassifiedItem) -> list[str]:
    return [
        link.url
        for link in Link.objects.filter(classified_item=classified_item).order_by(
            "url", "label"
        )
    ]


def _result_summary(
    classified_item: BaseClassifiedItem,
    log: ClassificationLog,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "classified_item_id": str(classified_item.id),
        "entity_type": classified_item.entity_type,
        "title": classified_item.title,
        "body": classified_item.body,
        "course_id": str(classified_item.course_id),
        "links": _serialized_links(classified_item),
        "log": {
            "entity_type": log.entity_type,
            "llm_model": log.llm_model,
            "confidence": log.confidence,
            "processing_time_ms": log.processing_time_ms,
        },
    }

    if classified_item.entity_type == "assignment":
        assignment = AssignmentORM.objects.get(pk=classified_item.pk)
        summary["entity_fields"] = {
            "deadline": assignment.deadline.isoformat()
            if assignment.deadline
            else None,
            "submission_format": assignment.submission_format,
        }
    elif classified_item.entity_type == "event":
        event = EventORM.objects.get(pk=classified_item.pk)
        summary["entity_fields"] = {
            "start_time": event.start_time.isoformat() if event.start_time else None,
            "end_time": event.end_time.isoformat() if event.end_time else None,
            "location": event.location,
        }
    elif classified_item.entity_type == "material":
        material = MaterialORM.objects.get(pk=classified_item.pk)
        summary["entity_fields"] = {
            "title": material.title,
        }

    return summary


def _append_mismatch(
    mismatches: list[str],
    field: str,
    actual: Any,
    expected: Any,
) -> None:
    if actual != expected:
        mismatches.append(f"{field}: expected {expected!r}, got {actual!r}")


def _collect_expected_output_mismatches(
    classified_item: BaseClassifiedItem,
    expected: dict[str, Any],
) -> list[str]:
    mismatches: list[str] = []
    _append_mismatch(
        mismatches,
        "entity_type",
        classified_item.entity_type,
        expected["entity_type"],
    )
    if classified_item.entity_type != expected["entity_type"]:
        return mismatches

    if classified_item.entity_type == "assignment":
        assignment = AssignmentORM.objects.get(pk=classified_item.pk)
        _append_mismatch(
            mismatches,
            "assignment.deadline",
            _local_wall_datetime(assignment.deadline),
            _local_wall_datetime(_parse_datetime(expected["deadline"])),
        )
    elif classified_item.entity_type == "event":
        event = EventORM.objects.get(pk=classified_item.pk)
        _append_mismatch(
            mismatches,
            "event.start_time",
            _local_wall_datetime(event.start_time),
            _local_wall_datetime(_parse_datetime(expected["start_time"])),
        )
        location = event.location or ""
        if "google" not in location.lower():
            mismatches.append(
                f"event.location: expected 'google' in location, got {location!r}"
            )
    elif classified_item.entity_type == "material":
        material = MaterialORM.objects.get(pk=classified_item.pk)
        if "title" in expected:
            _append_mismatch(
                mismatches,
                "material.title",
                material.title,
                expected["title"],
            )

    if "links" in expected:
        _append_mismatch(
            mismatches,
            "links",
            _serialized_links(classified_item),
            sorted(expected["links"]),
        )

    return mismatches


def _live_env_skip_reason() -> str | None:
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        return "Manual live LLM test requires env vars: " + ", ".join(sorted(missing))
    if os.getenv("RUN_LLM_LIVE_TESTS") != "1":
        return "Set RUN_LLM_LIVE_TESTS=1 to execute the live LLM smoke test"
    if os.getenv("LANGSMITH_TRACING", "").lower() not in {"1", "true", "yes", "on"}:
        return "Set LANGSMITH_TRACING=true to verify LangSmith tracing manually"
    return None


SKIP_REASON = _live_env_skip_reason()

pytestmark = [
    pytest.mark.llm_live,
    pytest.mark.skipif(SKIP_REASON is not None, reason=SKIP_REASON or ""),
]


@pytest.mark.django_db(transaction=True)
def test_processes_dataset_backed_raw_messages_with_live_llm() -> None:
    failures: list[str] = []
    for sample in LIVE_DATASET_SMOKE_CASES:
        failures.extend(_process_dataset_backed_raw_message(sample))

    if failures:
        pytest.fail("Live LLM smoke mismatches:\n" + "\n".join(failures))


def _process_dataset_backed_raw_message(
    sample: dict[str, Any],
) -> list[str]:
    ingested_item = IngestedItem.objects.create(
        external_id=f"{sample['dataset_id']}_{uuid.uuid4().hex[:8]}",
        platform="telegram",
        course_external_id=f"manual-live-{sample['dataset_id']}",
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        payload=sample["payload"],
        raw_payload={"source": "telegram_export", "dataset_id": sample["dataset_id"]},
    )
    config = ProcessingConfig()
    repository = PostgresEntityRepository()
    classifier = LLMClassifier(config)
    pipeline = ProcessingPipeline(repository, classifier, config)

    pipeline.process_item(mapper.orm_to_envelope(ingested_item))

    ingested_item.refresh_from_db()
    if ingested_item.processing_status != "processed":
        return [
            f"{sample['dataset_id']}: processing_status expected 'processed', "
            f"got {ingested_item.processing_status!r}"
        ]

    classified_item, log, failures = _load_persisted_output(
        ingested_item.id,
        sample["dataset_id"],
    )
    if failures:
        return failures

    assert classified_item is not None
    assert log is not None
    assert classified_item.course_id is not None
    assert log.classification_method == "llm"
    assert log.llm_model == config.llm_model
    assert log.confidence is not None

    summary = _result_summary(classified_item, log)
    print(
        json.dumps(
            {
                "dataset_id": sample["dataset_id"],
                "summary": summary,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    mismatches = _collect_expected_output_mismatches(
        classified_item,
        sample["expected"],
    )
    return [f"{sample['dataset_id']}: {mismatch}" for mismatch in mismatches]


def _load_persisted_output(
    ingested_item_id,
    dataset_id: str,
) -> tuple[
    BaseClassifiedItem | None,
    ClassificationLog | None,
    list[str],
]:
    log = ClassificationLog.objects.filter(ingested_item_id=ingested_item_id).first()
    if log is None:
        return (
            None,
            None,
            [f"{dataset_id}: expected classification log after processing"],
        )

    classified_item = (
        BaseClassifiedItem.objects.select_related("course")
        .filter(ingested_item_id=ingested_item_id)
        .first()
    )
    if classified_item is None:
        return (
            None,
            log,
            [
                f"{dataset_id}: processed without classified item; "
                f"classification log entity_type={log.entity_type!r}"
            ],
        )

    return classified_item, log, []
