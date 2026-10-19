from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from processing.adapters.postgres import mapper
from processing.models.classification import ClassificationLogEntry
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
from processing.service_metadata import PROCESSOR_VERSION, PROMPT_VERSION
from processing.tests.fixtures import (
    create_course,
    create_pending_moodle_announcement,
    create_pending_moodle_assignment,
    create_pending_moodle_event,
    create_pending_moodle_material,
    create_pending_teams_raw_message,
)


@pytest.mark.django_db
def test_orm_to_envelope_maps_assignment_payload_and_supporting_data():
    item = create_pending_moodle_assignment(
        payload_overrides={
            "author": "Ada Lovelace",
            "body": "Solve all problems.",
            "attachments": [
                {
                    "filename": "homework-1.pdf",
                    "mime_type": "application/pdf",
                    "url": "https://example.com/homework-1.pdf",
                }
            ],
            "links": [{"url": "https://example.com/spec", "label": "Spec"}],
        }
    )

    envelope = mapper.orm_to_envelope(item)

    assert envelope.id == item.id
    assert envelope.ingested_item.id == item.id
    assert isinstance(envelope.entity, Assignment)
    assert envelope.entity.title == "Homework 1"
    assert envelope.entity.body == "Solve all problems."
    assert envelope.entity.author == "Ada Lovelace"
    assert envelope.entity.published_at == datetime(
        2025, 9, 2, 10, 0, tzinfo=timezone.utc
    )
    assert envelope.entity.deadline == datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc)
    assert envelope.entity.submission_format == "file"
    assert envelope.extracted_links == [
        ExtractedLink(url="https://example.com/spec", label="Spec")
    ]
    assert envelope.extracted_attachments == [
        ExtractedAttachment(
            filename="homework-1.pdf",
            mime_type="application/pdf",
            url="https://example.com/homework-1.pdf",
        )
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("factory", "expected_type", "field_name", "expected_value"),
    [
        (
            create_pending_moodle_announcement,
            Announcement,
            "title",
            "Extra online classes",
        ),
        (
            create_pending_moodle_event,
            Event,
            "start_time",
            datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc),
        ),
        (
            create_pending_moodle_material,
            Material,
            "title",
            "Lecture 1 (Intro)",
        ),
        (
            create_pending_teams_raw_message,
            RawMessage,
            "author",
            "Example Teacher",
        ),
    ],
)
def test_orm_to_envelope_dispatches_by_payload_content_type(
    factory, expected_type, field_name, expected_value
):
    item = factory()

    envelope = mapper.orm_to_envelope(item)

    assert isinstance(envelope.entity, expected_type)
    assert getattr(envelope.entity, field_name) == expected_value


@pytest.mark.django_db
def test_orm_to_envelope_maps_base_published_at():
    item = create_pending_moodle_assignment(
        payload_overrides={"published_at": "2025-10-02T07:47:19+00:00"}
    )

    envelope = mapper.orm_to_envelope(item)

    assert isinstance(envelope.entity, Assignment)
    assert envelope.entity.published_at == datetime(
        2025, 10, 2, 7, 47, 19, tzinfo=timezone.utc
    )


@pytest.mark.django_db
def test_orm_to_course_maps_course_model():
    orm_course = create_course(name="Programming in C and C++", code="EX-101-A")

    course = mapper.orm_to_course(orm_course)

    assert course.id == orm_course.id
    assert course.name == "Programming in C and C++"
    assert course.code == "EX-101-A"


def test_entity_to_base_dict_preserves_explicit_nulls():
    entity = Assignment(title="Homework 1")

    assert mapper.entity_to_base_dict(entity) == {
        "title": "Homework 1",
        "body": None,
        "author": None,
        "published_at": None,
    }


def test_entity_to_child_dict_splits_assignment_and_event_fields():
    assignment = Assignment(
        deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
        submission_format="file",
    )
    event = Event(
        start_time=datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc),
        location="Room A",
    )

    assert mapper.entity_to_child_dict(assignment) == {
        "deadline": datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
        "submission_format": "file",
    }
    assert mapper.entity_to_child_dict(event) == {
        "start_time": datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc),
        "end_time": datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc),
        "location": "Room A",
    }
    assert (
        mapper.entity_to_child_dict(
            Announcement(
                title="Heads up",
                published_at=datetime(2025, 10, 2, 7, 47, 19, tzinfo=timezone.utc),
            )
        )
        == {}
    )
    assert mapper.entity_to_child_dict(Material(title="Slides")) == {}
    assert mapper.entity_to_child_dict(RawMessage(body="unclassified")) == {}


def test_entity_to_child_dict_preserves_explicit_nulls():
    assignment = Assignment(
        deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
    )

    assert mapper.entity_to_child_dict(assignment) == {
        "deadline": datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
        "submission_format": None,
    }


def test_supporting_models_map_to_bulk_create_dicts():
    classified_item_id = uuid4()

    assert mapper.links_to_orm_dicts(
        classified_item_id,
        [ExtractedLink(url="https://example.com/spec", label="Spec")],
    ) == [
        {
            "classified_item_id": classified_item_id,
            "url": "https://example.com/spec",
            "label": "Spec",
        }
    ]
    assert mapper.attachments_to_orm_dicts(
        classified_item_id,
        [
            ExtractedAttachment(
                filename="homework-1.pdf",
                mime_type="application/pdf",
                url="https://example.com/homework-1.pdf",
            )
        ],
    ) == [
        {
            "classified_item_id": classified_item_id,
            "filename": "homework-1.pdf",
            "mime_type": "application/pdf",
            "url": "https://example.com/homework-1.pdf",
        }
    ]


def test_classification_log_to_orm_dict_preserves_model_shape():
    log = ClassificationLogEntry(
        ingested_item_id=uuid4(),
        classified_item_id=uuid4(),
        entity_type="assignment",
        classification_method="llm",
        llm_model="gpt-5",
        llm_raw_output="{...}",
        confidence=0.91,
        processing_time_ms=842,
        input_tokens=140,
        output_tokens=29,
    )

    assert mapper.classification_log_to_orm_dict(log) == {
        "ingested_item_id": log.ingested_item_id,
        "classified_item_id": log.classified_item_id,
        "entity_type": "assignment",
        "classification_method": "llm",
        "llm_model": "gpt-5",
        "llm_raw_output": "{...}",
        "confidence": 0.91,
        "processing_time_ms": 842,
        "input_tokens": 140,
        "output_tokens": 29,
        "processor_version": PROCESSOR_VERSION,
        "prompt_version": PROMPT_VERSION,
    }
