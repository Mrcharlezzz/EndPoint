# pyright: reportAttributeAccessIssue=false, reportIndexIssue=false

"""Verify that each fixture factory creates valid DB rows and the polling query works."""

import pytest

from core.models.ingested import IngestedItem

from processing.tests.fixtures import (
    create_course,
    create_pending_moodle_announcement,
    create_pending_moodle_assignment,
    create_pending_moodle_event,
    create_pending_moodle_material,
    create_pending_teams_raw_message,
    create_processed_moodle_assignment,
)

REQUIRED_PAYLOAD_KEYS = {
    "content_type",
    "title",
    "author",
    "body",
    "course_name",
    "published_at",
    "deadline",
    "submission_format",
    "start_time",
    "end_time",
    "location",
    "attachments",
    "links",
}


def _assert_required_payload_keys(item: IngestedItem) -> None:
    assert REQUIRED_PAYLOAD_KEYS.issubset(item.payload.keys())


@pytest.mark.django_db
def test_create_course():
    course = create_course("Matrix Algebra I", code="EX-102")
    assert course.pk is not None
    assert course.name == "Matrix Algebra I"
    assert course.code == "EX-102"


@pytest.mark.django_db
def test_create_course_without_code():
    course = create_course("Unnamed Course")
    assert course.pk is not None
    assert course.code is None


@pytest.mark.django_db
def test_create_pending_moodle_assignment():
    item = create_pending_moodle_assignment()
    assert item.pk is not None
    assert item.processing_status == "pending"
    assert item.platform == "moodle"
    assert item.schema_version == "4.0"
    assert item.adapter_version == "v0"
    assert item.payload["content_type"] == "assignment"
    assert item.payload["title"] == "Homework 1"
    assert item.payload["deadline"] is not None
    assert item.payload["submission_format"] == "file"
    assert item.payload["published_at"] is not None
    _assert_required_payload_keys(item)


@pytest.mark.django_db
def test_create_pending_moodle_event():
    item = create_pending_moodle_event()
    assert item.pk is not None
    assert item.processing_status == "pending"
    assert item.platform == "moodle"
    assert item.source_url == (
        "https://elearning.example.edu/mod/assign/view.php?id=40436"
    )
    assert item.payload["content_type"] == "event"
    assert item.payload["title"] == "Assignment 13 is due"
    assert item.payload["start_time"] is not None
    _assert_required_payload_keys(item)


@pytest.mark.django_db
def test_create_pending_teams_raw_message():
    item = create_pending_teams_raw_message()
    assert item.pk is not None
    assert item.processing_status == "pending"
    assert item.platform == "teams"
    assert item.payload["content_type"] == "raw_message"
    assert item.payload["body"] is not None
    assert item.payload["author"] == "Example Teacher"
    _assert_required_payload_keys(item)


@pytest.mark.django_db
def test_create_pending_moodle_announcement():
    item = create_pending_moodle_announcement()
    assert item.pk is not None
    assert item.processing_status == "pending"
    assert item.platform == "moodle"
    assert item.payload["content_type"] == "announcement"
    assert item.payload["title"] == "Extra online classes"
    _assert_required_payload_keys(item)


@pytest.mark.django_db
def test_create_pending_moodle_material():
    item = create_pending_moodle_material()
    assert item.pk is not None
    assert item.processing_status == "pending"
    assert item.platform == "moodle"
    assert item.source_url == (
        "https://elearning.example.edu/mod/resource/view.php?id=37533"
    )
    assert item.payload["content_type"] == "material"
    assert item.payload["title"] == "Lecture 1 (Intro)"
    assert len(item.payload["attachments"]) == 1
    _assert_required_payload_keys(item)


@pytest.mark.django_db
def test_create_processed_moodle_assignment():
    item = create_processed_moodle_assignment()
    assert item.processing_status == "processed"
    assert item.payload["content_type"] == "assignment"


@pytest.mark.django_db
def test_polling_query_returns_only_pending():
    """Pending items appear in the polling query; processed items do not."""
    pending = create_pending_moodle_assignment()
    processed = create_processed_moodle_assignment()

    pending_ids = set(
        IngestedItem.objects.filter(processing_status="pending").values_list(
            "id", flat=True
        )
    )
    assert pending.pk in pending_ids
    assert processed.pk not in pending_ids


@pytest.mark.django_db
def test_polling_query_mixed_types():
    """Polling query returns all pending entity types together."""
    assignment = create_pending_moodle_assignment()
    event = create_pending_moodle_event()
    raw = create_pending_teams_raw_message()
    announcement = create_pending_moodle_announcement()
    material = create_pending_moodle_material()

    pending_ids = set(
        IngestedItem.objects.filter(processing_status="pending").values_list(
            "id", flat=True
        )
    )
    assert assignment.pk in pending_ids
    assert event.pk in pending_ids
    assert raw.pk in pending_ids
    assert announcement.pk in pending_ids
    assert material.pk in pending_ids


@pytest.mark.django_db
def test_fixture_override():
    """Overrides are applied correctly."""
    item = create_pending_moodle_assignment(
        payload_overrides={"title": "Custom Title", "published_at": None}
    )
    assert item.payload["title"] == "Custom Title"
    assert item.payload["published_at"] is None
