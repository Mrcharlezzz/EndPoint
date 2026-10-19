from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from processing.models.invariants import InvariantCode, Severity, check_envelope
from processing.models.classification import ClassificationLogEntry
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import Assignment, Event, RawMessage
from processing.models.extracted import (
    ExtractedAttachment,
    ExtractedLink,
)


def _context(
    *,
    content_type: str,
    course_name: str | None = "SYNTHETIC EXAMPLE COURSE",
    published_at: str | None = "2025-10-02T07:47:19+00:00",
) -> IngestedItemContext:
    return IngestedItemContext(
        id=uuid4(),
        external_id="item-1",
        platform="teams",
        course_external_id=None,
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        source_url=None,
        payload={
            "content_type": content_type,
            "title": "Homework 1",
            "author": "Ada Lovelace",
            "body": "Solve the problems.",
            "course_name": course_name,
            "published_at": published_at,
            "deadline": None,
            "submission_format": None,
            "start_time": None,
            "end_time": None,
            "location": None,
            "attachments": [],
            "links": [],
        },
    )


def _classification_log(entity_type: str) -> ClassificationLogEntry:
    return ClassificationLogEntry(
        ingested_item_id=uuid4(),
        classified_item_id=uuid4(),
        entity_type=entity_type,
        classification_method="deterministic",
        confidence=1.0,
    )


def _assignment_envelope() -> ProcessingEnvelope:
    ctx = _context(content_type="assignment")
    return ProcessingEnvelope(
        id=ctx.id,
        ingested_item=ctx,
        entity=Assignment(
            title="Homework 1",
            body="Solve the problems.",
            author="Ada Lovelace",
            published_at=datetime(2025, 10, 2, 7, 47, 19, tzinfo=timezone.utc),
            deadline=datetime(2025, 10, 9, 21, 59, tzinfo=timezone.utc),
            submission_format="file",
        ),
        resolved_course_id=uuid4(),
        classification_log=_classification_log("assignment"),
    )


def _event_envelope() -> ProcessingEnvelope:
    ctx = _context(content_type="event")
    return ProcessingEnvelope(
        id=ctx.id,
        ingested_item=ctx,
        entity=Event(
            title="Lecture",
            body="Join us tomorrow.",
            author="Ada Lovelace",
            start_time=datetime(2025, 10, 9, 8, 0, tzinfo=timezone.utc),
            end_time=datetime(2025, 10, 9, 10, 0, tzinfo=timezone.utc),
            location="Room 1",
        ),
        resolved_course_id=uuid4(),
        classification_log=_classification_log("event"),
    )


def _raw_message_envelope() -> ProcessingEnvelope:
    ctx = _context(content_type="raw_message", course_name=None, published_at=None)
    return ProcessingEnvelope(
        id=ctx.id,
        ingested_item=ctx,
        entity=RawMessage(
            title="General reminder",
            body="Check https://example.com/info",
            author=None,
        ),
        extracted_links=[ExtractedLink(url="https://example.com/info")],
    )


def _codes(warnings: list) -> set[InvariantCode]:
    return {warning.code for warning in warnings}


def test_check_envelope_returns_no_findings_for_clean_assignment():
    warnings = check_envelope(_assignment_envelope())

    assert warnings == []


def _e(envelope, **entity_kwargs):
    """Return a new envelope with entity fields updated."""
    return envelope.model_copy(
        update={"entity": envelope.entity.model_copy(update=entity_kwargs)}
    )


@pytest.mark.parametrize(
    ("mutate", "expected_code", "expected_field", "expected_severity"),
    [
        (
            lambda e: e.model_copy(
                update={
                    "entity": e.entity.model_copy(update={"title": None, "body": None})
                }
            ),
            InvariantCode.NO_TITLE_OR_BODY,
            "title,body",
            Severity.ERROR,
        ),
        (
            lambda e: _e(e, title="   "),
            InvariantCode.EMPTY_TITLE,
            "title",
            Severity.WARNING,
        ),
        (
            lambda e: _e(e, body="   "),
            InvariantCode.EMPTY_BODY,
            "body",
            Severity.WARNING,
        ),
        (
            lambda e: _e(e, author="   "),
            InvariantCode.EMPTY_AUTHOR,
            "author",
            Severity.WARNING,
        ),
        (
            lambda e: _e(e, title="x" * 501),
            InvariantCode.TITLE_TOO_LONG,
            "title",
            Severity.ERROR,
        ),
        (
            lambda e: _e(e, author="x" * 256),
            InvariantCode.AUTHOR_TOO_LONG,
            "author",
            Severity.ERROR,
        ),
        (
            lambda e: _e(e, deadline=datetime(2025, 10, 9, 21, 59)),
            InvariantCode.DEADLINE_NOT_TIMEZONE_AWARE,
            "deadline",
            Severity.ERROR,
        ),
        (
            lambda e: _e(e, deadline=datetime(2025, 9, 1, 12, 0, tzinfo=timezone.utc)),
            InvariantCode.DEADLINE_BEFORE_PUBLISHED_AT,
            "deadline",
            Severity.WARNING,
        ),
        (
            lambda e: e.model_copy(update={"resolved_course_id": None}),
            InvariantCode.NO_COURSE_RESOLVED,
            "resolved_course_id",
            Severity.ERROR,
        ),
        (
            lambda e: e.model_copy(update={"classification_log": None}),
            InvariantCode.NO_CLASSIFICATION_LOG,
            "classification_log",
            Severity.WARNING,
        ),
    ],
)
def test_assignment_invariants_flag_invalid_states(
    mutate,
    expected_code: InvariantCode,
    expected_field: str,
    expected_severity: Severity,
):
    envelope = mutate(_assignment_envelope())

    warnings = check_envelope(envelope)

    assert any(
        warning.code == expected_code
        and warning.field == expected_field
        and warning.severity == expected_severity
        for warning in warnings
    )


@pytest.mark.parametrize(
    ("mutate", "expected_code", "expected_field", "expected_severity"),
    [
        (
            lambda e: _e(e, start_time=None),
            InvariantCode.NO_START_TIME,
            "start_time",
            Severity.WARNING,
        ),
        (
            lambda e: _e(e, end_time=datetime(2025, 10, 9, 7, 0, tzinfo=timezone.utc)),
            InvariantCode.END_BEFORE_START,
            "end_time",
            Severity.ERROR,
        ),
        (
            lambda e: _e(e, start_time=datetime(2025, 10, 9, 8, 0)),
            InvariantCode.START_TIME_NOT_TIMEZONE_AWARE,
            "start_time",
            Severity.ERROR,
        ),
        (
            lambda e: _e(e, end_time=datetime(2025, 10, 9, 10, 0)),
            InvariantCode.END_TIME_NOT_TIMEZONE_AWARE,
            "end_time",
            Severity.ERROR,
        ),
    ],
)
def test_event_invariants_flag_invalid_states(
    mutate,
    expected_code: InvariantCode,
    expected_field: str,
    expected_severity: Severity,
):
    envelope = mutate(_event_envelope())

    warnings = check_envelope(envelope)

    assert any(
        warning.code == expected_code
        and warning.field == expected_field
        and warning.severity == expected_severity
        for warning in warnings
    )


def test_supporting_data_invariants_flag_invalid_rows():
    envelope = _assignment_envelope()
    envelope = envelope.model_copy(
        update={
            "extracted_links": [
                ExtractedLink(url="ftp://example.com/file"),
                ExtractedLink(url="https://example.com/dup"),
                ExtractedLink(url="https://example.com/dup"),
            ],
            "extracted_attachments": [
                ExtractedAttachment(filename="attachment", mime_type=None, url=None),
                ExtractedAttachment(
                    filename="   ", mime_type="bad", url="https://example.com"
                ),
            ],
        }
    )

    warnings = check_envelope(envelope)

    assert _codes(warnings) >= {
        InvariantCode.URL_NOT_HTTP,
        InvariantCode.DUPLICATE_URL,
        InvariantCode.GHOST_ATTACHMENT,
        InvariantCode.INVALID_MIME_TYPE,
        InvariantCode.EMPTY_FILENAME,
    }


def test_raw_message_skips_processed_state_checks():
    warnings = check_envelope(_raw_message_envelope())

    assert InvariantCode.NO_COURSE_RESOLVED not in _codes(warnings)
    assert InvariantCode.NO_CLASSIFICATION_LOG not in _codes(warnings)
