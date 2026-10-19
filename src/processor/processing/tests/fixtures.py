# pyright: reportAttributeAccessIssue=false

"""Test fixture factories.

Each factory creates ORM rows from SYNTHETIC EXAMPLE values. Call within @pytest.mark.django_db tests.

All factories accept top-level IngestedItem overrides plus `payload_overrides`
to customize the payload per test scenario. `external_id` gets a unique suffix
by default to prevent unique-constraint conflicts if a factory is called
multiple times within one test.
"""

from __future__ import annotations

import uuid

from core.models import Course
from core.models.ingested import IngestedItem


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def create_course(name: str, code: str | None = None, **overrides) -> Course:
    """Create a Course row."""
    fields = {"name": name, "code": code}
    fields.update(overrides)
    return Course.objects.create(**fields)


def _payload(**overrides) -> dict:
    payload = {
        "content_type": "raw_message",
        "title": None,
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
    }
    payload.update(overrides)
    return payload


def _create_ingested_item(
    *,
    external_id: str,
    platform: str,
    course_external_id: str | None,
    payload: dict,
    **overrides,
) -> IngestedItem:
    fields = dict(
        external_id=external_id,
        platform=platform,
        course_external_id=course_external_id,
        source_url=overrides.pop("source_url", None),
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        payload=payload,
        raw_payload={},
    )
    fields.update(overrides)
    return IngestedItem.objects.create(**fields)


# SYNTHETIC EXAMPLE fixtures; no captured platform messages are included.
def create_pending_moodle_assignment(**overrides) -> IngestedItem:
    external_id = overrides.pop("external_id", f"moodle_assign_synthetic_{_uid()}")
    payload_overrides = overrides.pop("payload_overrides", {})
    payload = _payload(
        content_type="assignment", title="Example assignment",
        body="Submit the example worksheet.", author="Example Teacher",
        course_name="SYNTHETIC EXAMPLE COURSE",
        published_at="2025-01-01T09:00:00+00:00",
        deadline="2025-01-08T17:00:00+00:00",
    )
    payload.update(payload_overrides)
    return _create_ingested_item(external_id=external_id, platform="moodle",
                                 course_external_id="synthetic-course", payload=payload, **overrides)


def create_pending_moodle_event(**overrides) -> IngestedItem:
    external_id = overrides.pop("external_id", f"moodle_event_synthetic_{_uid()}")
    payload_overrides = overrides.pop("payload_overrides", {})
    payload = _payload(
        content_type="event", title="Example study session",
        body="An example session for testing.", author="Example Teacher",
        course_name="SYNTHETIC EXAMPLE COURSE",
        published_at="2025-01-01T09:00:00+00:00",
        start_time="2025-01-10T10:00:00+00:00",
        end_time="2025-01-10T11:00:00+00:00",
    )
    payload.update(payload_overrides)
    return _create_ingested_item(external_id=external_id, platform="moodle",
                                 course_external_id="synthetic-course", payload=payload, **overrides)


def create_pending_teams_raw_message(**overrides) -> IngestedItem:
    external_id = overrides.pop("external_id", f"teams_csa_synthetic_{_uid()}")
    payload_overrides = overrides.pop("payload_overrides", {})
    payload = _payload(
        content_type="raw_message", author="Example Teacher",
        body="The example worksheet is due next week.",
        course_name="SYNTHETIC EXAMPLE COURSE",
        published_at="2025-01-01T09:00:00+00:00",
    )
    payload.update(payload_overrides)
    return _create_ingested_item(external_id=external_id, platform="teams",
                                 course_external_id="synthetic-course", payload=payload, **overrides)


def create_pending_moodle_announcement(**overrides) -> IngestedItem:
    external_id = overrides.pop("external_id", f"moodle_forum_synthetic_{_uid()}")
    payload_overrides = overrides.pop("payload_overrides", {})
    payload = _payload(
        content_type="announcement", title="Example notice",
        body="This is a synthetic example announcement.", author="Example Teacher",
        course_name="SYNTHETIC EXAMPLE COURSE",
        published_at="2025-01-01T09:00:00+00:00",
    )
    payload.update(payload_overrides)
    return _create_ingested_item(external_id=external_id, platform="moodle",
                                 course_external_id="synthetic-course", payload=payload, **overrides)


def create_pending_moodle_material(**overrides) -> IngestedItem:
    external_id = overrides.pop("external_id", f"moodle_material_synthetic_{_uid()}")
    payload_overrides = overrides.pop("payload_overrides", {})
    payload = _payload(
        content_type="material", title="Example slides",
        body="Synthetic course material.", course_name="SYNTHETIC EXAMPLE COURSE",
        published_at="2025-01-01T09:00:00+00:00",
        attachments=[{"filename": "example.pdf", "mime_type": "application/pdf",
                      "url": "https://example.test/example.pdf"}],
    )
    payload.update(payload_overrides)
    overrides.setdefault("source_url", "https://example.test/example.pdf")
    return _create_ingested_item(external_id=external_id, platform="moodle",
                                 course_external_id="synthetic-course", payload=payload, **overrides)


def create_processed_moodle_assignment(**overrides) -> IngestedItem:
    """Same data as pending moodle assignment but processing_status=processed.

    Used to verify processed items are excluded from the polling query.
    """
    overrides.setdefault("processing_status", "processed")
    overrides.setdefault("external_id", f"moodle_assign_1001_processed_{_uid()}")
    return create_pending_moodle_assignment(**overrides)
