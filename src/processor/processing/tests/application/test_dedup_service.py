from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from processing.application.dedup import (
    FirstSeenResolutionStrategy,
    PlatformPriorityResolutionStrategy,
    WeightedMatchStrategy,
)
from processing.application.dedup.service import DedupService
from processing.models.dedup import CanonicalCandidate, CanonicalState, ResolutionAction
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import Announcement, Assignment, RawMessage


class FakeRepository:
    def __init__(
        self,
        *,
        candidates_by_group: dict[tuple[UUID, str], list[CanonicalCandidate]]
        | None = None,
        failing_groups: set[tuple[UUID, str]] | None = None,
    ):
        self.candidates_by_group = dict(candidates_by_group or {})
        self.failing_groups = set(failing_groups or set())
        self.calls: list[tuple[UUID, str]] = []

    def get_pending_batch(self, limit: int) -> list[ProcessingEnvelope]:
        raise NotImplementedError

    def persist_batch(self, envelopes: list[ProcessingEnvelope]) -> None:
        raise NotImplementedError

    def persist_unclassified_batch(self, envelopes: list[ProcessingEnvelope]) -> None:
        raise NotImplementedError

    def save_entity(self, envelope: ProcessingEnvelope) -> UUID:
        raise NotImplementedError

    def mark_processed(self, ingested_item_id: UUID) -> None:
        raise NotImplementedError

    def mark_failed(self, ingested_item_id: UUID, error: str) -> None:
        raise NotImplementedError

    def save_links(self, classified_item_id: UUID, links) -> None:
        raise NotImplementedError

    def save_attachments(self, classified_item_id: UUID, attachments) -> None:
        raise NotImplementedError

    def save_classification_log(self, log) -> None:
        raise NotImplementedError

    def get_all_courses(self):
        raise NotImplementedError

    def get_course_by_platform_mapping(self, platform: str, external_course_id: str):
        raise NotImplementedError

    def create_course(self, name: str, code: str | None = None):
        raise NotImplementedError

    def update_course_code(self, course_id: UUID, code: str) -> None:
        raise NotImplementedError

    def upsert_course_platform_mapping(
        self,
        course_id: UUID,
        platform: str,
        external_course_id: str,
        external_course_name: str | None,
    ) -> None:
        raise NotImplementedError

    def get_existing_canonicals(
        self, course_id: UUID, entity_type: str
    ) -> list[CanonicalCandidate]:
        self.calls.append((course_id, entity_type))
        if (course_id, entity_type) in self.failing_groups:
            raise RuntimeError("database read failed")
        return list(self.candidates_by_group.get((course_id, entity_type), []))


class InvalidAttachResolutionStrategy:
    def resolve(self, incoming_platform: str, matches):
        del incoming_platform, matches
        return ResolutionAction(kind="attach")


def _make_envelope(
    *,
    entity,
    course_id: UUID | None,
    platform: str = "moodle",
    external_id: str | None = None,
) -> ProcessingEnvelope:
    ingested = IngestedItemContext(
        id=uuid4(),
        external_id=external_id or f"{platform}_{uuid4().hex[:8]}",
        platform=platform,
        course_external_id="course-1",
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        payload={
            "content_type": entity.entity_type,
            "title": entity.title,
            "author": entity.author,
            "body": entity.body,
            "course_name": "SYNTHETIC EXAMPLE COURSE",
            "attachments": [],
            "links": [],
        },
    )
    return ProcessingEnvelope(
        id=ingested.id,
        ingested_item=ingested,
        entity=entity,
        resolved_course_id=course_id,
    )


def test_deduplicate_batch_marks_items_without_course_or_supported_type_as_skipped():
    course_id = uuid4()
    service = DedupService(
        FakeRepository(),
        WeightedMatchStrategy(),
        FirstSeenResolutionStrategy(),
    )

    result = service.deduplicate_batch(
        [
            _make_envelope(
                entity=Assignment(
                    title="Homework 1",
                    deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
                ),
                course_id=None,
            ),
            _make_envelope(
                entity=RawMessage(title="Raw note"),
                course_id=course_id,
            ),
        ]
    )

    assert [envelope.dedup_status for envelope in result] == ["skipped", "skipped"]
    assert [envelope.dedup_decision for envelope in result] == [None, None]


def test_deduplicate_batch_links_same_batch_duplicates_to_first_anchor():
    course_id = uuid4()
    service = DedupService(
        FakeRepository(),
        WeightedMatchStrategy(),
        FirstSeenResolutionStrategy(),
    )
    first = _make_envelope(
        entity=Assignment(
            title="Homework 1",
            deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
        ),
        course_id=course_id,
        platform="moodle",
    )
    second = _make_envelope(
        entity=Assignment(
            title=" Homework   1 ",
            deadline=datetime(2025, 9, 9, 22, 2, tzinfo=timezone.utc),
        ),
        course_id=course_id,
        platform="teams",
    )

    result = service.deduplicate_batch([first, second])

    assert result[0].dedup_status == "primary"
    assert result[0].dedup_decision is not None
    assert result[1].dedup_status == "linked"
    assert result[1].dedup_decision is not None
    assert (
        result[1].dedup_decision.canonical_entity_id
        == result[0].dedup_decision.canonical_entity_id
    )


def test_deduplicate_batch_promotes_higher_priority_platform_in_same_batch():
    course_id = uuid4()
    service = DedupService(
        FakeRepository(),
        WeightedMatchStrategy(),
        PlatformPriorityResolutionStrategy(["moodle", "teams", "telegram"]),
    )
    first = _make_envelope(
        entity=Assignment(
            title="Homework 1",
            deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
        ),
        course_id=course_id,
        platform="teams",
    )
    second = _make_envelope(
        entity=Assignment(
            title="Homework 1",
            deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
        ),
        course_id=course_id,
        platform="moodle",
    )

    result = service.deduplicate_batch([first, second])

    assert result[0].dedup_status == "linked"
    assert result[1].dedup_status == "primary"
    assert result[0].dedup_decision is not None
    assert result[1].dedup_decision is not None
    assert (
        result[0].dedup_decision.canonical_entity_id
        == result[1].dedup_decision.canonical_entity_id
    )


def test_deduplicate_batch_marks_group_failed_when_candidate_load_fails(
    caplog: pytest.LogCaptureFixture,
):
    course_id = uuid4()
    service = DedupService(
        FakeRepository(failing_groups={(course_id, "assignment")}),
        WeightedMatchStrategy(),
        FirstSeenResolutionStrategy(),
    )
    envelopes = [
        _make_envelope(
            entity=Assignment(
                title="Homework 1",
                deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
            ),
            course_id=course_id,
        ),
        _make_envelope(
            entity=Assignment(
                title="Homework 2",
                deadline=datetime(2025, 9, 16, 21, 59, tzinfo=timezone.utc),
            ),
            course_id=course_id,
        ),
    ]

    with caplog.at_level(logging.ERROR, logger="processing.application.dedup.service"):
        result = service.deduplicate_batch(envelopes)

    assert [envelope.dedup_status for envelope in result] == ["failed", "failed"]
    assert [envelope.dedup_decision for envelope in result] == [None, None]
    error_record = next(
        record
        for record in caplog.records
        if "Dedup candidate load failed" in record.getMessage()
    )
    assert error_record.exc_info is not None
    assert error_record.exc_info[0] is RuntimeError
    assert "database read failed" in error_record.getMessage()


def test_deduplicate_batch_uses_normalized_announcement_published_at():
    course_id = uuid4()
    service = DedupService(
        FakeRepository(),
        WeightedMatchStrategy(),
        FirstSeenResolutionStrategy(),
    )
    first = _make_envelope(
        entity=Announcement(
            title="Updates",
            body="Homework 2 deadline was extended and quiz grading was updated.",
            published_at=datetime(2025, 9, 24, 13, 16, 35, tzinfo=timezone.utc),
        ),
        course_id=course_id,
        platform="telegram",
    )
    second = _make_envelope(
        entity=Announcement(
            title="Course updates",
            body="Homework 2 deadline is extended and quiz grading has been updated.",
            published_at=datetime(2025, 9, 24, 13, 28, 0, tzinfo=timezone.utc),
        ),
        course_id=course_id,
        platform="telegram",
    )

    result = service.deduplicate_batch([first, second])

    assert result[0].dedup_status == "primary"
    assert result[1].dedup_status == "linked"
    assert result[1].dedup_decision is not None
    assert result[1].dedup_decision.matched_by == "weighted"


def test_deduplicate_batch_marks_item_failed_when_resolution_action_has_no_canonical(
    caplog: pytest.LogCaptureFixture,
):
    course_id = uuid4()
    service = DedupService(
        FakeRepository(),
        WeightedMatchStrategy(),
        InvalidAttachResolutionStrategy(),
    )

    with caplog.at_level(logging.ERROR, logger="processing.application.dedup.service"):
        result = service.deduplicate_batch(
            [
                _make_envelope(
                    entity=Assignment(
                        title="Homework 1",
                        deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
                    ),
                    course_id=course_id,
                )
            ]
        )

    assert result[0].dedup_status == "failed"
    assert result[0].dedup_decision is None
    error_record = next(
        record
        for record in caplog.records
        if "Dedup evaluation failed" in record.getMessage()
    )
    assert error_record.exc_info is not None
    assert "attach actions require a canonical_id" in error_record.getMessage()


def test_finalize_group_rejects_overlapping_canonical_memberships():
    service = DedupService(
        FakeRepository(),
        WeightedMatchStrategy(),
        FirstSeenResolutionStrategy(),
    )
    entity = Assignment(
        title="Homework 1",
        deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
    )
    envelope = _make_envelope(
        entity=entity,
        course_id=uuid4(),
    )
    first_canonical_id = uuid4()
    second_canonical_id = uuid4()
    states = {
        first_canonical_id: CanonicalState(
            canonical_id=first_canonical_id,
            representative=entity,
            representative_platform="moodle",
            pending_primary_batch_index=0,
            member_batch_indexes=[0],
        ),
        second_canonical_id: CanonicalState(
            canonical_id=second_canonical_id,
            representative=entity,
            representative_platform="teams",
            pending_primary_batch_index=None,
            member_batch_indexes=[0],
        ),
    }

    with pytest.raises(RuntimeError, match="multiple canonical states"):
        service._finalize_group(
            entries=[(0, envelope)],
            states=states,
            item_assignments={0: first_canonical_id},
            item_scores={0: 1.0},
            item_matched_by={0: "weighted"},
            updated=[envelope],
        )
