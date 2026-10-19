from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from processing.application.extraction_resolution import (
    ExtractionResolutionService,
    choose_scalar,
    parse_payload_evidence,
)
from processing.exceptions import ClassificationError
from processing.models.classification import (
    AnnouncementPatch,
    AssignmentPatch,
    EventPatch,
    MaterialPatch,
)
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import (
    Announcement,
    Assignment,
    Event,
    Material,
    RawMessage,
)
from processing.models.extracted import (
    ExtractedAttachment,
    ExtractedLink,
)


def _make_raw_envelope(
    *,
    title: str | None = "Original title",
    body: str | None = "Original body",
    author: str | None = "Ada Lovelace",
    course_name: str | None = "Programming in C and C++",
    payload_overrides: dict | None = None,
    extracted_links: list[ExtractedLink] | None = None,
    extracted_attachments: list[ExtractedAttachment] | None = None,
) -> ProcessingEnvelope:
    payload = {
        "content_type": "raw_message",
        "title": title,
        "author": author,
        "body": body,
        "course_name": course_name,
        "published_at": None,
        "deadline": None,
        "submission_format": None,
        "start_time": None,
        "end_time": None,
        "location": None,
        "attachments": [],
        "links": [],
    }
    if payload_overrides is not None:
        payload.update(payload_overrides)

    ingested = IngestedItemContext(
        id=uuid4(),
        external_id="teams_msg_1",
        platform="teams",
        course_external_id=None,
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        source_url=None,
        payload=payload,
    )
    return ProcessingEnvelope(
        id=ingested.id,
        ingested_item=ingested,
        entity=RawMessage(title=title, body=body, author=author),
        extracted_links=extracted_links or [],
        extracted_attachments=extracted_attachments or [],
    )


def test_choose_scalar_prefers_current_when_present():
    assert choose_scalar("payload_val", "extracted_val") == "payload_val"


def test_choose_scalar_falls_back_to_extracted_when_current_is_none():
    assert choose_scalar(None, "extracted_val") == "extracted_val"


def test_choose_scalar_returns_none_when_both_none():
    assert choose_scalar(None, None) is None


def test_parse_payload_evidence_parses_datetime_string():
    evidence = parse_payload_evidence(
        {"deadline": "2025-09-01T21:59:00+00:00"},
        Assignment,
    )
    assert evidence["deadline"] == datetime(2025, 9, 1, 21, 59, tzinfo=timezone.utc)


def test_parse_payload_evidence_ignores_common_fields():
    evidence = parse_payload_evidence(
        {"title": "HW1", "body": "text", "deadline": "2025-09-01T21:59:00+00:00"},
        Assignment,
    )
    assert "title" not in evidence
    assert "body" not in evidence
    assert "deadline" in evidence


def test_parse_payload_evidence_ignores_unknown_fields():
    evidence = parse_payload_evidence(
        {"unknown_field": "value", "deadline": None},
        Assignment,
    )
    assert "unknown_field" not in evidence
    assert "deadline" not in evidence


def test_resolve_assignment_prefers_payload_scalars_and_fills_missing_values():
    service = ExtractionResolutionService()
    envelope = _make_raw_envelope(
        payload_overrides={
            "deadline": "2025-09-01T21:59:00+00:00",
            "submission_format": None,
            "published_at": "2025-08-30T10:00:00+00:00",
        }
    )

    resolved = service.resolve(
        envelope,
        "assignment",
        AssignmentPatch(
            title="Homework 2",
            deadline=datetime(2025, 9, 8, 21, 59, tzinfo=timezone.utc),
            submission_format="upload a pdf",
            confidence=0.85,
            reasoning="Mixed sparse extraction.",
        ),
    )

    assert isinstance(resolved.entity, Assignment)
    assert resolved.entity.title == "Homework 2"
    assert resolved.entity.body == "Original body"
    assert resolved.entity.published_at == datetime(
        2025, 8, 30, 10, 0, tzinfo=timezone.utc
    )
    assert resolved.entity.deadline == datetime(2025, 9, 1, 21, 59, tzinfo=timezone.utc)
    assert resolved.entity.submission_format == "upload a pdf"


def test_resolve_event_prefers_payload_scalars():
    service = ExtractionResolutionService()
    envelope = _make_raw_envelope(
        payload_overrides={
            "start_time": "2025-10-01T09:00:00+00:00",
            "end_time": None,
            "location": "Room 101",
        }
    )

    resolved = service.resolve(
        envelope,
        "event",
        EventPatch(
            title="Office hours",
            start_time=datetime(2025, 10, 2, 9, 0, tzinfo=timezone.utc),
            end_time=datetime(2025, 10, 1, 10, 0, tzinfo=timezone.utc),
            location="Room 202",
            confidence=0.87,
            reasoning="Scheduled occurrence.",
        ),
    )

    assert isinstance(resolved.entity, Event)
    assert resolved.entity.start_time == datetime(
        2025, 10, 1, 9, 0, tzinfo=timezone.utc
    )
    assert resolved.entity.end_time == datetime.fromisoformat(
        "2025-10-01T10:00:00+02:00"
    )
    assert resolved.entity.location == "Room 101"


def test_resolve_localizes_naive_deadline_to_university_timezone():
    service = ExtractionResolutionService(university_timezone="Europe/Amsterdam")

    resolved = service.resolve(
        _make_raw_envelope(),
        "assignment",
        AssignmentPatch(
            deadline=datetime(2026, 3, 13, 21, 0),
            confidence=0.9,
            reasoning="Deadline from body.",
        ),
    )

    assert isinstance(resolved.entity, Assignment)
    assert resolved.entity.deadline == datetime.fromisoformat(
        "2026-03-13T21:00:00+01:00"
    )


def test_resolve_localizes_naive_summer_event_to_dst_offset():
    service = ExtractionResolutionService(university_timezone="Europe/Amsterdam")

    resolved = service.resolve(
        _make_raw_envelope(),
        "event",
        EventPatch(
            start_time=datetime(2026, 4, 11, 9, 30),
            confidence=0.9,
            reasoning="Start time from body.",
        ),
    )

    assert isinstance(resolved.entity, Event)
    assert resolved.entity.start_time == datetime.fromisoformat(
        "2026-04-11T09:30:00+02:00"
    )


def test_resolve_published_at_offset_keeps_naive_event_time_on_utc_clock():
    service = ExtractionResolutionService(university_timezone="Europe/Amsterdam")

    resolved = service.resolve(
        _make_raw_envelope(),
        "event",
        EventPatch(
            start_time=datetime(2026, 4, 16, 15, 15, 39),
            start_time_source_kind="published_at_offset_utc",
            confidence=0.9,
            reasoning="Start time computed from published_at plus five minutes.",
        ),
    )

    assert isinstance(resolved.entity, Event)
    assert resolved.entity.start_time is not None
    assert resolved.entity.start_time == datetime.fromisoformat(
        "2026-04-16T15:15:39+00:00"
    )
    assert resolved.entity.start_time.tzinfo is timezone.utc


def test_resolve_published_at_offset_normalizes_aware_event_time_to_utc():
    service = ExtractionResolutionService(university_timezone="Europe/Amsterdam")

    resolved = service.resolve(
        _make_raw_envelope(),
        "event",
        EventPatch(
            start_time=datetime.fromisoformat("2026-04-16T17:15:39+02:00"),
            start_time_source_kind="published_at_offset_utc",
            confidence=0.9,
            reasoning="Start time computed from published_at plus five minutes.",
        ),
    )

    assert isinstance(resolved.entity, Event)
    assert resolved.entity.start_time is not None
    assert resolved.entity.start_time == datetime.fromisoformat(
        "2026-04-16T15:15:39+00:00"
    )
    assert resolved.entity.start_time.tzinfo is timezone.utc


def test_resolve_treats_extracted_aware_datetime_as_local_wall_time():
    llm_utc_wall_time = datetime.fromisoformat("2026-03-13T21:00:00+00:00")
    service = ExtractionResolutionService(university_timezone="Europe/Amsterdam")

    resolved = service.resolve(
        _make_raw_envelope(),
        "assignment",
        AssignmentPatch(
            deadline=llm_utc_wall_time,
            confidence=0.9,
            reasoning="LLM included an untrusted timezone offset.",
        ),
    )

    assert isinstance(resolved.entity, Assignment)
    assert resolved.entity.deadline == datetime.fromisoformat(
        "2026-03-13T21:00:00+01:00"
    )


def test_resolve_preserves_links_and_attachments():
    service = ExtractionResolutionService()
    existing_link = ExtractedLink(url="https://example.com/a", label="A")
    existing_attachment = ExtractedAttachment(
        filename="slides.pdf",
        mime_type="application/pdf",
        url="https://example.com/slides.pdf",
    )
    envelope = _make_raw_envelope(
        extracted_links=[existing_link],
        extracted_attachments=[existing_attachment],
    )

    resolved = service.resolve(
        envelope,
        "material",
        MaterialPatch(
            title="Lecture resources",
            confidence=0.83,
            reasoning="Adds new supporting data.",
        ),
    )

    assert resolved.extracted_links == [existing_link]
    assert resolved.extracted_attachments == [existing_attachment]


@pytest.mark.parametrize(
    ("classified_type", "patch", "expected_type"),
    [
        (
            "announcement",
            AnnouncementPatch(
                title="Exam room changed",
                confidence=0.82,
                reasoning="Informational notice.",
            ),
            Announcement,
        ),
        (
            "event",
            EventPatch(
                title="Office hours",
                start_time=datetime(2025, 10, 1, 9, 0, tzinfo=timezone.utc),
                end_time=datetime(2025, 10, 1, 10, 0, tzinfo=timezone.utc),
                location="Room A",
                confidence=0.87,
                reasoning="Scheduled occurrence.",
            ),
            Event,
        ),
        (
            "material",
            MaterialPatch(
                title="Lecture 1 slides",
                confidence=0.84,
                reasoning="Shared resource.",
            ),
            Material,
        ),
    ],
)
def test_resolve_dispatches_to_supported_types(classified_type, patch, expected_type):
    service = ExtractionResolutionService()

    resolved = service.resolve(
        _make_raw_envelope(),
        classified_type,
        patch,
    )

    assert isinstance(resolved.entity, expected_type)
    assert resolved.entity.author == "Ada Lovelace"
    assert resolved.entity.body == "Original body"


def test_resolve_raises_for_unsupported_type():
    service = ExtractionResolutionService()

    with pytest.raises(ClassificationError):
        service.resolve(
            _make_raw_envelope(),
            "unknown_type",
            AnnouncementPatch(confidence=0.8, reasoning="x"),
        )
