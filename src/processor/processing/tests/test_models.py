"""Tests for Pydantic domain models — no DB required."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from processing.models.entities import (
    Assignment,
    Announcement,
    Event,
    Material,
    RawMessage,
    ClassifiedEntity,
)
from processing.models.envelope import ProcessingEnvelope, IngestedItemContext
from processing.models.extracted import (
    ExtractedLink,
    ExtractedAttachment,
)
from processing.models.classification import (
    ClassificationResult,
    ExtractionPatch,
    AssignmentPatch,
    MaterialPatch,
    EventPatch,
    ClassificationLogEntry,
)
from processing.models.course import Course, CourseMatch
from processing.models.registry import ENTITY_REGISTRY, COMMON_FIELDS
from processing.service_metadata import PROCESSOR_VERSION, PROMPT_VERSION
from processing.adapters.llm.field_instructions import (
    ENTITY_EXTRACTION_FIELDS,
    FIELD_INSTRUCTIONS,
)


# ── Entity construction ───────────────────────────────────────────────────────


def test_assignment_constructs():
    a = Assignment(
        title="Homework 1",
        published_at=datetime(2025, 9, 2, 10, tzinfo=timezone.utc),
        deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
    )
    assert a.entity_type == "assignment"
    assert a.title == "Homework 1"
    assert a.published_at == datetime(2025, 9, 2, 10, tzinfo=timezone.utc)
    assert a.deadline is not None
    assert a.deadline.year == 2025


def test_announcement_constructs():
    a = Announcement(
        title="Extra online classes",
        body="Join tonight at 7.15",
        published_at=datetime(2025, 10, 2, 7, 47, 19, tzinfo=timezone.utc),
    )
    assert a.entity_type == "announcement"
    assert a.title == "Extra online classes"
    assert a.published_at == datetime(2025, 10, 2, 7, 47, 19, tzinfo=timezone.utc)


def test_event_constructs():
    e = Event(
        title="Assignment 13 is due",
        start_time=datetime(2026, 1, 15, 21, tzinfo=timezone.utc),
    )
    assert e.entity_type == "event"
    assert e.start_time is not None
    assert e.end_time is None
    assert e.location is None


def test_material_constructs():
    material = Material(title="Lecture 1 slides", body="See attachment.")
    assert material.entity_type == "material"
    assert material.title == "Lecture 1 slides"


def test_raw_message_constructs():
    r = RawMessage(body="Dear students, reminder about grading.")
    assert r.entity_type == "raw_message"
    assert r.title is None


# ── Discriminated union ───────────────────────────────────────────────────────


def test_discriminated_union_resolves_assignment():
    from pydantic import TypeAdapter

    ta = TypeAdapter(ClassifiedEntity)
    entity = ta.validate_python({"entity_type": "assignment", "title": "HW1"})
    assert isinstance(entity, Assignment)


def test_discriminated_union_resolves_event():
    from pydantic import TypeAdapter

    ta = TypeAdapter(ClassifiedEntity)
    entity = ta.validate_python({"entity_type": "event"})
    assert isinstance(entity, Event)


def test_discriminated_union_resolves_material():
    from pydantic import TypeAdapter

    ta = TypeAdapter(ClassifiedEntity)
    entity = ta.validate_python({"entity_type": "material", "title": "Slides"})
    assert isinstance(entity, Material)


def test_discriminated_union_resolves_raw_message():
    from pydantic import TypeAdapter

    ta = TypeAdapter(ClassifiedEntity)
    entity = ta.validate_python({"entity_type": "raw_message", "body": "text"})
    assert isinstance(entity, RawMessage)


# ── from_attributes (ORM → domain) ───────────────────────────────────────────


def test_assignment_from_attributes():
    orm = SimpleNamespace(
        title="Homework 1",
        body=None,
        author=None,
        published_at=datetime(2025, 9, 2, 10, tzinfo=timezone.utc),
        entity_type="assignment",
        deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
        submission_format="file",
    )
    a = Assignment.model_validate(orm)
    assert a.title == "Homework 1"
    assert a.published_at == datetime(2025, 9, 2, 10, tzinfo=timezone.utc)
    assert a.submission_format == "file"


def test_event_from_attributes():
    orm = SimpleNamespace(
        title="Assignment 13 is due",
        body="Sheet 13",
        author=None,
        entity_type="event",
        start_time=datetime(2026, 1, 15, 21, tzinfo=timezone.utc),
        end_time=None,
        location=None,
    )
    e = Event.model_validate(orm)
    assert e.start_time is not None
    assert e.start_time.year == 2026


# ── model_dump ────────────────────────────────────────────────────────────────


def test_assignment_model_dump_excludes_removed_fields():
    a = Assignment(
        title="HW1", published_at=datetime(2025, 9, 2, 10, tzinfo=timezone.utc)
    )
    d = a.model_dump(exclude_none=True)
    assert "title" in d
    assert "published_at" in d
    assert "deadline" not in d
    assert "max_points" not in d
    assert "points" not in d
    assert "status" not in d
    assert d["entity_type"] == "assignment"


def test_announcement_model_dump():
    a = Announcement(
        title="Notice",
        published_at=datetime(2025, 10, 2, 7, 47, 19, tzinfo=timezone.utc),
    )
    d = a.model_dump()
    assert d["entity_type"] == "announcement"
    assert d["title"] == "Notice"
    assert d["published_at"] == datetime(2025, 10, 2, 7, 47, 19, tzinfo=timezone.utc)


def test_material_model_dump():
    material = Material(title="Slides")
    dumped = material.model_dump()
    assert dumped["entity_type"] == "material"
    assert dumped["title"] == "Slides"


# ── Envelope ──────────────────────────────────────────────────────────────────


def _make_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content_type": "assignment",
        "title": "Homework 1",
        "author": None,
        "body": None,
        "course_name": "SYNTHETIC EXAMPLE COURSE",
        "published_at": None,
        "deadline": "2025-09-09T21:59:00+00:00",
        "submission_format": "file",
        "start_time": None,
        "end_time": None,
        "location": None,
        "attachments": [],
        "links": [],
    }
    payload.update(overrides)
    return payload


def _make_ingested_context(**overrides) -> IngestedItemContext:
    defaults: dict[str, Any] = dict(
        id=uuid4(),
        external_id="moodle_assign_1001",
        platform="moodle",
        course_external_id="8413",
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        source_url=None,
        payload=_make_payload(),
    )
    defaults.update(overrides)
    return IngestedItemContext.model_validate(defaults)


def test_ingested_item_context_from_attributes():
    orm = SimpleNamespace(
        id=uuid4(),
        external_id="moodle_assign_1001",
        platform="moodle",
        course_external_id="8413",
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        source_url="https://moodle.test/mod/assign/view.php?id=41249",
        payload=_make_payload(content_type="assignment", title="Homework 1"),
    )
    context = IngestedItemContext.model_validate(orm)
    assert context.schema_version == "4.0"
    assert context.adapter_version == "v0"
    assert context.processing_status == "pending"
    assert context.source_url == "https://moodle.test/mod/assign/view.php?id=41249"
    assert context.payload["content_type"] == "assignment"
    assert context.payload["title"] == "Homework 1"


def test_envelope_wraps_entity():
    item_id = uuid4()
    entity = Assignment(title="HW1")
    envelope = ProcessingEnvelope(
        id=item_id,
        ingested_item=_make_ingested_context(),
        entity=entity,
    )
    assert envelope.id == item_id
    assert isinstance(envelope.entity, Assignment)
    assert envelope.resolved_course_id is None
    assert envelope.extracted_links == []
    assert envelope.classification_log is None
    assert envelope.ingested_item.payload["content_type"] == "assignment"


def test_envelope_entity_replacement():
    """Entity can be replaced (RawMessage → Assignment after LLM classification)."""
    envelope = ProcessingEnvelope(
        id=uuid4(),
        ingested_item=_make_ingested_context(
            payload=_make_payload(content_type="raw_message")
        ),
        entity=RawMessage(body="some text"),
    )
    assert isinstance(envelope.entity, RawMessage)

    # Replace with typed entity after classification (immutable — use model_copy)
    envelope = envelope.model_copy(
        update={
            "entity": Assignment(
                title="Classified HW",
                deadline=datetime(2025, 12, 1, tzinfo=timezone.utc),
            )
        }
    )
    assert isinstance(envelope.entity, Assignment)
    assert envelope.entity.title == "Classified HW"


def test_envelope_adds_deterministic_classification_log():
    item_id = uuid4()
    envelope = ProcessingEnvelope(
        id=item_id,
        ingested_item=_make_ingested_context(id=item_id),
        entity=Assignment(title="Homework 1"),
    )

    updated = envelope.with_deterministic_classification_log()

    assert envelope.classification_log is None
    assert updated.classification_log is not None
    assert updated.classification_log.ingested_item_id == item_id
    assert updated.classification_log.entity_type == "assignment"
    assert updated.classification_log.classification_method == "deterministic"
    assert updated.classification_log.confidence == 1.0


def test_envelope_preserves_existing_classification_log():
    item_id = uuid4()
    existing_log = ClassificationLogEntry(
        ingested_item_id=item_id,
        entity_type="assignment",
        classification_method="llm",
        confidence=0.87,
    )
    envelope = ProcessingEnvelope(
        id=item_id,
        ingested_item=_make_ingested_context(id=item_id),
        entity=Assignment(title="Homework 1"),
        classification_log=existing_log,
    )

    updated = envelope.with_deterministic_classification_log()

    assert updated.classification_log == existing_log


# ── Extraction result models ──────────────────────────────────────────────────


def test_classification_result_validates():
    r = ClassificationResult(
        entity_type="assignment", confidence=0.95, reasoning="Has deadline"
    )
    assert r.confidence == 0.95


def test_classification_result_confidence_bounds():
    with pytest.raises(Exception):
        ClassificationResult(entity_type="assignment", confidence=1.5, reasoning="x")


def test_extraction_patch_base_has_common_defaults():
    patch = ExtractionPatch(confidence=0.9, reasoning="test")
    assert patch.title is None
    assert "links" not in ExtractionPatch.model_fields
    assert "dates" not in ExtractionPatch.model_fields


def test_assignment_patch_inherits_base_fields():
    patch = AssignmentPatch(
        title="HW1",
        deadline=datetime(2025, 9, 9, tzinfo=timezone.utc),
        confidence=0.9,
        reasoning="clear deadline",
    )
    assert patch.title == "HW1"
    assert patch.deadline is not None
    assert "dates" not in AssignmentPatch.model_fields
    assert "max_points" not in AssignmentPatch.model_fields
    assert "status" not in AssignmentPatch.model_fields
    assert isinstance(patch, ExtractionPatch)


def test_assignment_patch():
    r = AssignmentPatch(
        title="HW1",
        deadline=datetime(2025, 9, 9, tzinfo=timezone.utc),
        confidence=0.9,
        reasoning="clear deadline",
    )
    assert r.title == "HW1"
    assert "dates" not in AssignmentPatch.model_fields


def test_event_patch():
    r = EventPatch(
        title="Lecture",
        start_time=datetime(2025, 11, 1, 10, tzinfo=timezone.utc),
        confidence=0.85,
        reasoning="has time",
    )
    assert r.start_time is not None
    assert r.end_time is None


def test_material_patch():
    result = MaterialPatch(
        title="Lecture 1 slides",
        confidence=0.85,
        reasoning="Resource shared for course material.",
    )
    assert result.title == "Lecture 1 slides"
    assert "links" not in MaterialPatch.model_fields


def test_registry_has_all_four_entity_types():
    assert set(ENTITY_REGISTRY.keys()) == {
        "assignment",
        "announcement",
        "event",
        "material",
    }


def test_registry_entries_are_entity_patch_pairs():
    from processing.models.entities import ClassifiedBase

    for entity_type, (entity_cls, patch_cls) in ENTITY_REGISTRY.items():
        assert issubclass(entity_cls, ClassifiedBase), f"{entity_type} entity"
        assert issubclass(patch_cls, ExtractionPatch), f"{entity_type} patch"


def test_common_fields_constant():
    assert "title" in COMMON_FIELDS
    assert "body" in COMMON_FIELDS
    assert "author" in COMMON_FIELDS
    assert "published_at" in COMMON_FIELDS
    assert "entity_type" in COMMON_FIELDS
    assert "deadline" not in COMMON_FIELDS


def test_entity_extraction_fields_derived_from_registry():
    for entity_type, (_, patch_cls) in ENTITY_REGISTRY.items():
        expected_fields = {
            name
            for name in patch_cls.model_fields
            if name
            not in {
                "confidence",
                "reasoning",
                "deadline_source_kind",
                "start_time_source_kind",
                "end_time_source_kind",
            }
        }
        actual_fields = set(ENTITY_EXTRACTION_FIELDS[entity_type])
        assert actual_fields == expected_fields, f"Mismatch for {entity_type}"


def test_all_extraction_fields_have_instructions():
    all_fields: set[str] = set()
    for fields in ENTITY_EXTRACTION_FIELDS.values():
        all_fields.update(fields)
    for field_name in all_fields:
        assert field_name in FIELD_INSTRUCTIONS, f"Missing instruction for {field_name}"


# ── Classification log ────────────────────────────────────────────────────────


def test_classification_log_entry():
    log = ClassificationLogEntry(
        ingested_item_id=uuid4(),
        entity_type="assignment",
        classification_method="llm",
        llm_model="gpt-4o",
        confidence=0.92,
        processing_time_ms=1230,
    )
    assert log.classified_item_id is None
    assert log.classification_method == "llm"
    assert log.processing_time_ms == 1230
    assert log.llm_raw_output is None
    assert log.processor_version == PROCESSOR_VERSION
    assert log.prompt_version == PROMPT_VERSION


# ── Course models ─────────────────────────────────────────────────────────────


def test_course_from_attributes():
    orm = SimpleNamespace(
        id=uuid4(), name="Matrix Algebra I", code="EX-102", major=None, semester=None
    )
    c = Course.model_validate(orm)
    assert c.name == "Matrix Algebra I"
    assert c.code == "EX-102"


def test_course_match():
    cid = uuid4()
    m = CourseMatch(
        course_id=cid,
        matched_by="code_exact",
        course_name="Matrix Algebra I",
        course_code="EX-102",
    )
    assert m.matched_by == "code_exact"
    assert m.course_id == cid


# ── Extracted models ──────────────────────────────────────────────────────────


def test_extracted_link():
    link = ExtractedLink(url="https://example.com", label="Click here")
    assert link.url == "https://example.com"
    assert link.label == "Click here"


def test_extracted_link_no_label():
    link = ExtractedLink(url="https://example.com")
    assert link.label is None


def test_extracted_attachment():
    att = ExtractedAttachment(filename="report.pdf", mime_type="application/pdf")
    assert att.filename == "report.pdf"
    assert att.url is None
