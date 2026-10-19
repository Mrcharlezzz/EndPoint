# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import cast
from uuid import UUID, uuid4

import pytest

from core.models import (
    BaseClassifiedItem,
    CanonicalEntity,
    CoursePlatformMapping,
    IngestedItem,
)
from processing.application.dedup import (
    DedupService,
    FirstSeenResolutionStrategy,
    WeightedMatchStrategy,
)
from processing.adapters.postgres import mapper
from processing.adapters.postgres.repository import PostgresEntityRepository
from processing.config import ProcessingConfig
from processing.exceptions import RowScopedPersistenceError, TransientPersistenceError
from processing.models.classification import (
    AnnouncementPatch,
    AssignmentPatch,
    ClassificationLogEntry,
    ClassificationResult,
    MaterialPatch,
)
from processing.models.course import Course
from processing.models.dedup import DedupDecision
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import Assignment, Material, RawMessage
from processing.models.invariants import InvariantCode
from processing.models.extracted import (
    ExtractedAttachment,
    ExtractedLink,
)
from processing.pipeline import ProcessingPipeline
from processing.tests.fixtures import (
    create_course,
    create_pending_moodle_assignment,
    create_pending_moodle_material,
    create_pending_teams_raw_message,
)


class FakeRepository:
    def __init__(
        self,
        *,
        courses: list[Course] | None = None,
        mark_failed_failures_before_success: int = 0,
        batch_failures: list[Exception] | None = None,
        per_item_failures: dict[UUID, Exception] | None = None,
        unclassified_failure: Exception | None = None,
    ):
        self.courses = list(courses or [])
        self.mark_failed_failures_before_success = mark_failed_failures_before_success
        self.batch_failures = list(batch_failures or [])
        self.per_item_failures = dict(per_item_failures or {})
        self.unclassified_failure = unclassified_failure

        self.persist_batch_calls: list[list[ProcessingEnvelope]] = []
        self.saved_envelopes: list[ProcessingEnvelope] = []
        self.saved_links: list[tuple[UUID, list[ExtractedLink]]] = []
        self.saved_attachments: list[tuple[UUID, list[ExtractedAttachment]]] = []
        self.saved_logs: list[ClassificationLogEntry] = []
        self.mark_processed_calls: list[UUID] = []
        self.mark_failed_calls: list[tuple[UUID, str]] = []
        self.mark_failed_attempts = 0

    def get_pending_batch(self, limit: int) -> list[ProcessingEnvelope]:
        raise NotImplementedError

    def get_existing_canonicals(self, course_id: UUID, entity_type: str):
        return []

    def save_entity(self, envelope: ProcessingEnvelope) -> UUID:
        raise NotImplementedError

    def persist_unclassified_batch(self, envelopes: list[ProcessingEnvelope]) -> None:
        if self.unclassified_failure is not None:
            raise self.unclassified_failure
        for envelope in envelopes:
            if envelope.classification_log is not None:
                self.saved_logs.append(envelope.classification_log)
            self.mark_processed_calls.append(envelope.ingested_item.id)

    def persist_batch(self, envelopes: list[ProcessingEnvelope]) -> None:
        self.persist_batch_calls.append(list(envelopes))
        if self.batch_failures:
            raise self.batch_failures.pop(0)

        for envelope in envelopes:
            failure = self.per_item_failures.get(envelope.ingested_item.id)
            if failure is not None:
                raise failure

        for envelope in envelopes:
            classified_item_id = uuid4()
            self.saved_envelopes.append(envelope)
            self.saved_links.append(
                (classified_item_id, list(envelope.extracted_links))
            )
            self.saved_attachments.append(
                (classified_item_id, list(envelope.extracted_attachments))
            )
            if envelope.classification_log is not None:
                self.saved_logs.append(
                    envelope.classification_log.model_copy(
                        update={"classified_item_id": classified_item_id}
                    )
                )
            self.mark_processed_calls.append(envelope.ingested_item.id)

    def mark_processed(self, ingested_item_id: UUID) -> None:
        self.mark_processed_calls.append(ingested_item_id)

    def mark_failed(self, ingested_item_id: UUID, error: str) -> None:
        self.mark_failed_attempts += 1
        if self.mark_failed_failures_before_success > 0:
            self.mark_failed_failures_before_success -= 1
            raise RuntimeError("mark_failed unavailable")
        self.mark_failed_calls.append((ingested_item_id, error))

    def save_links(self, classified_item_id: UUID, links: list[ExtractedLink]) -> None:
        self.saved_links.append((classified_item_id, links))

    def save_attachments(
        self, classified_item_id: UUID, attachments: list[ExtractedAttachment]
    ) -> None:
        self.saved_attachments.append((classified_item_id, attachments))

    def save_classification_log(self, log: ClassificationLogEntry) -> None:
        self.saved_logs.append(log)

    def get_all_courses(self) -> list[Course]:
        return list(self.courses)

    def create_course(self, name: str, code: str | None = None) -> Course:
        course = Course(id=uuid4(), name=name, code=code)
        self.courses.append(course)
        return course

    def update_course_code(self, course_id: UUID, code: str) -> None:
        self.courses = [
            course.model_copy(update={"code": code})
            if course.id == course_id
            else course
            for course in self.courses
        ]

    def get_course_by_platform_mapping(
        self, platform: str, external_course_id: str
    ) -> Course | None:
        return None

    def upsert_course_platform_mapping(
        self,
        course_id: UUID,
        platform: str,
        external_course_id: str,
        external_course_name: str | None,
    ) -> None:
        return None


class FakeClassifier:
    def __init__(self):
        self.classify_calls = 0
        self.extract_calls = 0
        self.model = "fake-llm"

    async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult:
        self.classify_calls += 1
        return ClassificationResult(
            entity_type="assignment",
            confidence=0.93,
            reasoning="Contains a deadline and grading info.",
        )

    async def extract_fields(
        self, envelope: ProcessingEnvelope, classified_type: str
    ) -> AssignmentPatch:
        self.extract_calls += 1
        assert classified_type == "assignment"
        return AssignmentPatch(
            title="Homework 1",
            deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
            submission_format="file",
            confidence=0.88,
            reasoning="Clear assignment structure.",
        )


class ExplodingClassifier(FakeClassifier):
    async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult:
        raise RuntimeError("boom")


class ScenarioClassifier:
    def __init__(self):
        self.model = "fake-llm"

    async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult:
        external_id = envelope.ingested_item.external_id
        if "fail" in external_id:
            raise RuntimeError("classification boom")
        if "announce" in external_id:
            return ClassificationResult(
                entity_type="announcement",
                confidence=0.89,
                reasoning="General course information.",
            )
        if "material" in external_id:
            return ClassificationResult(
                entity_type="material",
                confidence=0.87,
                reasoning="Message shares course resources.",
            )
        return ClassificationResult(
            entity_type="assignment",
            confidence=0.93,
            reasoning="Contains a deadline and task instructions.",
        )

    async def extract_fields(
        self, envelope: ProcessingEnvelope, classified_type: str
    ) -> AssignmentPatch | AnnouncementPatch | MaterialPatch:
        if classified_type == "announcement":
            return AnnouncementPatch(
                title="Exam room changed",
                confidence=0.82,
                reasoning="Informational announcement.",
            )
        if classified_type == "material":
            return MaterialPatch(
                title="Lecture 1 slides",
                confidence=0.84,
                reasoning="Resource shared for course reference.",
            )
        return AssignmentPatch(
            title="Homework 1",
            deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
            submission_format="file",
            confidence=0.88,
            reasoning="Clear assignment structure.",
        )


class DedupIntegrationClassifier(ScenarioClassifier):
    async def extract_fields(
        self, envelope: ProcessingEnvelope, classified_type: str
    ) -> AssignmentPatch | AnnouncementPatch | MaterialPatch:
        external_id = envelope.ingested_item.external_id
        if external_id == "telegram_announce_anchor":
            return AnnouncementPatch(
                title="Updates",
                confidence=0.86,
                reasoning="Announcement about course updates.",
            )
        if external_id == "telegram_announce_duplicate":
            return AnnouncementPatch(
                title="Course updates",
                confidence=0.85,
                reasoning="Announcement restating the same update.",
            )
        return await super().extract_fields(envelope, classified_type)


class FakeDedupService:
    def __init__(self):
        self.calls: list[list[ProcessingEnvelope]] = []

    def deduplicate_batch(
        self, envelopes: list[ProcessingEnvelope]
    ) -> list[ProcessingEnvelope]:
        self.calls.append(list(envelopes))
        canonical_id = uuid4()
        return [
            envelope.model_copy(
                update={
                    "dedup_decision": DedupDecision(
                        decision="new_canonical",
                        canonical_entity_id=canonical_id,
                        matched_by="no_match",
                        score=0.0,
                    ),
                    "dedup_status": "primary",
                }
            )
            for envelope in envelopes
        ]


class ExplodingDedupService:
    def deduplicate_batch(self, envelopes: list[ProcessingEnvelope]):
        del envelopes
        raise RuntimeError("dedup service unavailable")


class ExplodingLinksRepository(PostgresEntityRepository):
    def save_links(self, classified_item_id: UUID, links: list[ExtractedLink]) -> None:
        raise RuntimeError("save_links failed")


class PollingSequenceRepository(FakeRepository):
    def __init__(
        self,
        batches: list[list[ProcessingEnvelope] | Exception],
    ):
        super().__init__()
        self.batches = list(batches)
        self.get_pending_batch_calls = 0

    def get_pending_batch(self, limit: int) -> list[ProcessingEnvelope]:
        self.get_pending_batch_calls += 1
        if not self.batches:
            return []
        next_item = self.batches.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return cast(list[ProcessingEnvelope], next_item)


def _make_ingested_item_context(
    *,
    course_name: str,
    title: str | None = None,
    author: str | None = "Ada Lovelace",
    body: str | None = None,
) -> IngestedItemContext:
    return IngestedItemContext(
        id=uuid4(),
        external_id="teams_msg_1",
        platform="teams",
        course_external_id=None,
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        source_url=None,
        payload={
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
        },
    )


def _make_raw_message_envelope() -> ProcessingEnvelope:
    ingested = _make_ingested_item_context(
        course_name="SYNTHETIC EXAMPLE COURSE",
        title="Reminder",
        body=(
            "The homework is due next Tuesday. "
            "See https://example.com/spec and https://example.com/extra."
        ),
    )
    return ProcessingEnvelope(
        id=ingested.id,
        ingested_item=ingested,
        entity=RawMessage(
            title="Reminder",
            body=ingested.payload["body"],
            author="Ada Lovelace",
        ),
    )


def _make_structured_assignment_envelope() -> ProcessingEnvelope:
    ingested = _make_ingested_item_context(
        course_name="Programming in C and C++",
        title="  Homework   2  ",
        author="  Ada   Lovelace  ",
        body="Body with https://example.com/material.\r\n\r\n",
    )
    ingested.payload["content_type"] = "assignment"
    envelope = ProcessingEnvelope(
        id=ingested.id,
        ingested_item=ingested,
        entity=Assignment(
            title="  Homework   2  ",
            body=ingested.payload["body"],
            author="  Ada   Lovelace  ",
            deadline=datetime(2025, 9, 10, 21, 59, tzinfo=timezone.utc),
        ),
    )
    return envelope.model_copy(
        update={
            "extracted_attachments": [
                ExtractedAttachment(
                    filename="slides.pdf",
                    mime_type="application/pdf",
                    url="https://example.com/slides.pdf",
                ),
            ]
        }
    )


def _create_pending_telegram_raw_message(
    *,
    external_id: str,
    course_name: str,
    body: str,
    published_at: str,
) -> IngestedItem:
    return IngestedItem.objects.create(
        external_id=external_id,
        platform="telegram",
        course_external_id="telegram-course-1",
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        source_url=None,
        payload={
            "content_type": "raw_message",
            "title": None,
            "author": "Channel Admin",
            "body": body,
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
        raw_payload={},
    )


def test_process_item_classifies_raw_message_normalizes_and_persists():
    course = Course(id=uuid4(), name="Programming in C and C++", code="EX-101-A")
    repository = FakeRepository(courses=[course])
    classifier = FakeClassifier()
    pipeline = ProcessingPipeline(repository, classifier, ProcessingConfig())
    envelope = _make_raw_message_envelope()

    pipeline.process_item(envelope)

    processed = repository.saved_envelopes[0]
    assert classifier.classify_calls == 1
    assert classifier.extract_calls == 1
    assert isinstance(processed.entity, Assignment)
    assert processed.resolved_course_id == course.id
    assert len(repository.saved_envelopes) == 1
    assert len(repository.persist_batch_calls) == 1
    assert repository.saved_links == [
        (
            repository.saved_links[0][0],
            [],
        )
    ]
    assert repository.saved_attachments[0][0] == repository.saved_links[0][0]
    assert repository.saved_logs[0].classified_item_id == repository.saved_links[0][0]
    assert repository.mark_processed_calls == [envelope.ingested_item.id]
    assert repository.mark_failed_calls == []


def test_process_item_skips_classifier_for_structured_entities():
    course = Course(id=uuid4(), name="Programming in C and C++", code=None)
    repository = FakeRepository(courses=[course])
    classifier = FakeClassifier()
    pipeline = ProcessingPipeline(repository, classifier, ProcessingConfig())
    envelope = _make_structured_assignment_envelope()

    pipeline.process_item(envelope)

    processed = repository.saved_envelopes[0]
    assert classifier.classify_calls == 0
    assert classifier.extract_calls == 0
    assert processed.resolved_course_id == course.id
    assert processed.entity.title == "Homework 2"
    assert processed.entity.author == "Ada Lovelace"
    assert len(repository.persist_batch_calls) == 1
    assert repository.saved_links == [
        (
            repository.saved_links[0][0],
            [],
        )
    ]
    assert repository.saved_attachments == [
        (
            repository.saved_attachments[0][0],
            [
                ExtractedAttachment(
                    filename="slides.pdf",
                    mime_type="application/pdf",
                    url="https://example.com/slides.pdf",
                )
            ],
        )
    ]
    assert repository.saved_logs[0].classification_method == "deterministic"
    assert repository.saved_logs[0].entity_type == "assignment"
    assert repository.saved_logs[0].confidence == 1.0
    assert repository.mark_processed_calls == [envelope.ingested_item.id]


def test_deduplicate_batch_returns_input_when_service_is_missing():
    repository = FakeRepository()
    classifier = FakeClassifier()
    pipeline = ProcessingPipeline(repository, classifier, ProcessingConfig())
    envelope = _make_structured_assignment_envelope()

    result = asyncio.run(pipeline._deduplicate_batch([envelope]))

    assert result == [envelope]


def test_deduplicate_batch_logs_service_failure_traceback(
    caplog: pytest.LogCaptureFixture,
):
    pipeline = ProcessingPipeline(
        FakeRepository(),
        FakeClassifier(),
        ProcessingConfig(),
        dedup_service=ExplodingDedupService(),
    )
    envelope = _make_structured_assignment_envelope()

    with caplog.at_level(logging.ERROR, logger="processing.pipeline"):
        result = asyncio.run(pipeline._deduplicate_batch([envelope]))

    assert result == [envelope]
    error_record = next(
        record
        for record in caplog.records
        if "Dedup failed for batch" in record.getMessage()
    )
    assert error_record.exc_info is not None
    assert error_record.exc_info[0] is RuntimeError
    assert "dedup service unavailable" in error_record.getMessage()


def test_process_item_uses_dedup_service_before_persist():
    course = Course(id=uuid4(), name="Programming in C and C++", code=None)
    repository = FakeRepository(courses=[course])
    classifier = FakeClassifier()
    dedup_service = FakeDedupService()
    pipeline = ProcessingPipeline(
        repository,
        classifier,
        ProcessingConfig(),
        dedup_service=dedup_service,
    )
    envelope = _make_structured_assignment_envelope()

    pipeline.process_item(envelope)

    assert len(dedup_service.calls) == 1
    assert dedup_service.calls[0][0].resolved_course_id == course.id
    assert repository.persist_batch_calls[0][0].dedup_decision is not None
    assert repository.persist_batch_calls[0][0].dedup_decision.decision == (
        "new_canonical"
    )


@pytest.mark.django_db(transaction=True)
def test_batch_pipeline_with_real_db_deduplicates_moodle_and_teams_assignment():
    create_course(name="Matrix Algebra and Advanced Calculus I", code="EX-102")
    moodle_item = create_pending_moodle_assignment(external_id="moodle_assign_anchor")
    teams_item = create_pending_teams_raw_message(
        external_id="teams_assign_duplicate",
        payload_overrides={
            "course_name": "SYNTHETIC EXAMPLE COURSE",
        },
    )

    repository = PostgresEntityRepository()
    dedup_service = DedupService(
        repository,
        WeightedMatchStrategy(),
        FirstSeenResolutionStrategy(),
    )
    pipeline = ProcessingPipeline(
        repository,
        DedupIntegrationClassifier(),
        ProcessingConfig(),
        dedup_service=dedup_service,
    )

    batch = repository.get_pending_batch(limit=10)
    results = asyncio.run(pipeline._process_batch(batch))
    successes = [envelope for envelope, error in results if error is None]
    failures = [(envelope, error) for envelope, error in results if error is not None]

    assert failures == []

    deduplicated = asyncio.run(pipeline._deduplicate_batch(successes))
    persisted_ids = asyncio.run(pipeline._persist_successes(deduplicated))

    moodle_id = cast(UUID, moodle_item.id)
    teams_id = cast(UUID, teams_item.id)

    assert set(persisted_ids) == {moodle_id, teams_id}

    decisions_by_id: dict[UUID, DedupDecision] = {}
    for envelope in deduplicated:
        decision = envelope.dedup_decision
        assert decision is not None
        decisions_by_id[envelope.ingested_item.id] = decision

    assert decisions_by_id[moodle_id].decision == "new_canonical"
    assert decisions_by_id[teams_id].decision == "link_existing"

    canonical = CanonicalEntity.objects.select_related("primary_item").get()
    classified_items = list(
        BaseClassifiedItem.objects.filter(
            ingested_item_id__in=[moodle_id, teams_id]
        ).order_by("created_at")
    )

    assert canonical.primary_item.ingested_item_id == moodle_item.id
    assert [item.canonical_entity_id for item in classified_items] == [
        canonical.id,
        canonical.id,
    ]


@pytest.mark.django_db(transaction=True)
def test_process_item_with_real_db_deduplicates_fuzzy_telegram_announcement():
    create_course(name="Industrial Programming", code="EX-101-A")
    first_item = _create_pending_telegram_raw_message(
        external_id="telegram_announce_anchor",
        course_name="Industrial Programming",
        body="Updates: homework 2 deadline was extended and quiz grading changed.",
        published_at="2025-09-24T13:16:35+00:00",
    )
    second_item = _create_pending_telegram_raw_message(
        external_id="telegram_announce_duplicate",
        course_name="Industrial Programming",
        body="Course updates: homework 2 deadline is extended and quiz grading changed.",
        published_at="2025-09-24T13:28:00+00:00",
    )

    repository = PostgresEntityRepository()
    dedup_service = DedupService(
        repository,
        WeightedMatchStrategy(),
        FirstSeenResolutionStrategy(),
    )
    pipeline = ProcessingPipeline(
        repository,
        DedupIntegrationClassifier(),
        ProcessingConfig(),
        dedup_service=dedup_service,
    )

    pipeline.process_item(mapper.orm_to_envelope(first_item))
    pipeline.process_item(mapper.orm_to_envelope(second_item))

    canonical = CanonicalEntity.objects.select_related("primary_item").get()
    classified_items = list(
        BaseClassifiedItem.objects.filter(
            ingested_item_id__in=[first_item.id, second_item.id]
        ).order_by("created_at")
    )

    assert len(classified_items) == 2
    assert canonical.primary_item.ingested_item_id == first_item.id
    assert [item.canonical_entity_id for item in classified_items] == [
        canonical.id,
        canonical.id,
    ]


def test_process_item_classifies_raw_message_into_material():
    course = Course(id=uuid4(), name="Programming in C and C++", code="EX-101-A")
    repository = FakeRepository(courses=[course])
    classifier = ScenarioClassifier()
    pipeline = ProcessingPipeline(repository, classifier, ProcessingConfig())
    envelope = _make_raw_message_envelope()
    envelope = envelope.model_copy(
        update={
            "ingested_item": envelope.ingested_item.model_copy(
                update={"external_id": "teams_material_message"}
            )
        }
    )

    pipeline.process_item(envelope)

    processed = repository.saved_envelopes[0]
    assert isinstance(processed.entity, Material)
    assert processed.resolved_course_id == course.id
    assert len(repository.persist_batch_calls) == 1
    assert repository.saved_attachments == [
        (
            repository.saved_attachments[0][0],
            [],
        )
    ]
    assert repository.saved_logs[0].entity_type == "material"
    assert repository.mark_processed_calls == [envelope.ingested_item.id]


class RawMessageClassifier:
    """Classifier that always returns raw_message (e.g., greetings, off-topic)."""

    def __init__(self):
        self.classify_calls = 0
        self.extract_calls = 0
        self.model = "fake-llm"

    async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult:
        self.classify_calls += 1
        return ClassificationResult(
            entity_type="raw_message",
            confidence=0.97,
            reasoning="Greeting message with no academic content.",
        )

    async def extract_fields(self, envelope: ProcessingEnvelope, classified_type: str):
        self.extract_calls += 1
        raise AssertionError("extract_fields must not be called for raw_message")


class CapturingRawMessageClassifier(RawMessageClassifier):
    def __init__(self):
        super().__init__()
        self.classified_envelopes: list[ProcessingEnvelope] = []

    async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult:
        self.classified_envelopes.append(envelope)
        return await super().classify(envelope)


def test_process_item_raw_message_routes_to_persist_unclassified_batch():
    repository = FakeRepository()
    classifier = RawMessageClassifier()
    pipeline = ProcessingPipeline(repository, classifier, ProcessingConfig())
    envelope = _make_raw_message_envelope()

    pipeline.process_item(envelope)

    # classification called once, extraction never called
    assert classifier.classify_calls == 1
    assert classifier.extract_calls == 0
    # no persist_batch — raw envelopes go to persist_unclassified_batch path
    assert repository.persist_batch_calls == []
    # item marked processed via raw path
    assert repository.mark_processed_calls == [envelope.ingested_item.id]
    assert repository.mark_failed_calls == []
    # log written with raw_message type
    assert len(repository.saved_logs) == 1
    assert repository.saved_logs[0].entity_type == "raw_message"
    assert repository.saved_logs[0].classification_method == "llm"


def test_process_item_short_raw_message_heuristic_routes_to_unclassified_success():
    repository = FakeRepository()
    classifier = FakeClassifier()
    pipeline = ProcessingPipeline(repository, classifier, ProcessingConfig())
    envelope = _make_raw_message_envelope().model_copy(
        update={"entity": RawMessage(title="Reminder", body="Quiz is out")}
    )

    pipeline.process_item(envelope)

    assert classifier.classify_calls == 0
    assert classifier.extract_calls == 0
    assert repository.persist_batch_calls == []
    assert repository.mark_processed_calls == [envelope.ingested_item.id]
    assert repository.mark_failed_calls == []
    assert len(repository.saved_logs) == 1
    assert repository.saved_logs[0].entity_type == "raw_message"
    assert repository.saved_logs[0].classification_method == "rule_based"


def test_process_item_normalizes_raw_message_before_llm_classification():
    repository = FakeRepository()
    classifier = CapturingRawMessageClassifier()
    pipeline = ProcessingPipeline(repository, classifier, ProcessingConfig())
    envelope = _make_raw_message_envelope().model_copy(
        update={
            "entity": RawMessage(
                title="  Homework   1  ",
                body=(
                    "Hello\r\n\r\n\r\nWorld\u00a0"
                    "This message is long enough for classifier capture."
                ),
                author="  Ada   Lovelace  ",
            )
        }
    )

    pipeline.process_item(envelope)

    seen = classifier.classified_envelopes[0]
    assert seen.entity.title == "Homework 1"
    assert (
        seen.entity.body
        == "Hello\n\nWorld This message is long enough for classifier capture."
    )
    assert seen.entity.author == "Ada Lovelace"


@pytest.mark.django_db(transaction=True)
def test_process_item_persists_structured_material_with_course_name():
    create_course(name="SAT Training - English", code="SAT-02")
    item = create_pending_moodle_material(
        payload_overrides={
            "course_name": "SYNTHETIC EXAMPLE COURSE",
            "body": "Slides for lecture 1.",
        }
    )

    repository = PostgresEntityRepository()
    pipeline = ProcessingPipeline(repository, FakeClassifier(), ProcessingConfig())
    envelope = mapper.orm_to_envelope(item)

    pipeline.process_item(envelope)

    item.refresh_from_db()
    assert item.processing_status == "processed"
    classified = BaseClassifiedItem.objects.get(ingested_item_id=item.id)
    assert classified.entity_type == "material"


@pytest.mark.django_db(transaction=True)
def test_process_item_resolves_material_via_existing_course_platform_mapping():
    create_course(name="SAT Training - English", code="SAT-02")
    first_item = create_pending_moodle_material(
        payload_overrides={
            "course_name": "SYNTHETIC EXAMPLE COURSE",
            "body": "Slides for lecture 1.",
        }
    )
    second_item = create_pending_moodle_material(
        external_id=f"moodle_material_mapping_{uuid4().hex[:8]}"
    )

    repository = PostgresEntityRepository()
    pipeline = ProcessingPipeline(repository, FakeClassifier(), ProcessingConfig())

    pipeline.process_item(mapper.orm_to_envelope(first_item))
    pipeline.process_item(mapper.orm_to_envelope(second_item))

    first_item.refresh_from_db()
    second_item.refresh_from_db()

    assert first_item.processing_status == "processed"
    assert second_item.processing_status == "processed"
    assert CoursePlatformMapping.objects.filter(
        platform="moodle",
        external_course_id=first_item.course_external_id,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_process_item_fails_structured_material_without_course_name():
    item = create_pending_moodle_material()

    repository = PostgresEntityRepository()
    pipeline = ProcessingPipeline(repository, FakeClassifier(), ProcessingConfig())
    envelope = mapper.orm_to_envelope(item)

    pipeline.process_item(envelope)

    item.refresh_from_db()
    assert item.processing_status == "failed"
    assert item.error_message == "Cannot resolve course without payload.course_name"
    assert not BaseClassifiedItem.objects.filter(ingested_item_id=item.id).exists()


def test_process_item_logs_invariant_warnings(caplog: pytest.LogCaptureFixture):
    course = Course(id=uuid4(), name="Programming in C and C++", code=None)
    repository = FakeRepository(courses=[course])
    classifier = FakeClassifier()
    pipeline = ProcessingPipeline(repository, classifier, ProcessingConfig())
    envelope = _make_structured_assignment_envelope()
    envelope = envelope.model_copy(
        update={
            "entity": envelope.entity.model_copy(update={"title": None, "body": None})
        }
    )

    with caplog.at_level(logging.WARNING):
        pipeline.process_item(envelope)

    assert InvariantCode.NO_TITLE_OR_BODY.value in caplog.text
    assert str(envelope.ingested_item.id) in caplog.text


def test_process_item_marks_failed_when_processing_raises(
    caplog: pytest.LogCaptureFixture,
):
    repository = FakeRepository()
    classifier = ExplodingClassifier()
    pipeline = ProcessingPipeline(repository, classifier, ProcessingConfig())
    envelope = _make_raw_message_envelope()

    with caplog.at_level(logging.ERROR, logger="processing.pipeline"):
        pipeline.process_item(envelope)

    assert repository.mark_processed_calls == []
    assert repository.mark_failed_calls == [(envelope.ingested_item.id, "boom")]
    error_record = next(
        record
        for record in caplog.records
        if "Processing failed for ingested item" in record.getMessage()
    )
    assert error_record.exc_info is not None
    assert error_record.exc_info[0] is RuntimeError
    assert "boom" in error_record.getMessage()


def test_process_item_retries_mark_failed_until_it_succeeds():
    repository = FakeRepository(
        mark_failed_failures_before_success=1,
    )
    classifier = ExplodingClassifier()
    config = ProcessingConfig()
    config.failure_mark_retries = 2
    config.failure_mark_retry_delay_seconds = 0.0
    pipeline = ProcessingPipeline(repository, classifier, config)
    envelope = _make_raw_message_envelope()

    pipeline.process_item(envelope)

    assert repository.mark_processed_calls == []
    assert repository.mark_failed_attempts == 2
    assert repository.mark_failed_calls == [(envelope.ingested_item.id, "boom")]


def test_mark_failed_retry_logs_traceback(caplog: pytest.LogCaptureFixture):
    repository = FakeRepository(
        mark_failed_failures_before_success=1,
    )
    config = ProcessingConfig()
    config.failure_mark_retries = 1
    config.failure_mark_retry_delay_seconds = 0.0
    pipeline = ProcessingPipeline(repository, FakeClassifier(), config)
    envelope = _make_raw_message_envelope()

    with caplog.at_level(logging.WARNING, logger="processing.pipeline"):
        asyncio.run(pipeline._mark_failed_safely(envelope.ingested_item.id, "boom"))

    retry_record = next(
        record for record in caplog.records if "retrying" in record.getMessage()
    )
    assert retry_record.exc_info is not None
    assert retry_record.exc_info[0] is RuntimeError
    assert "mark_failed unavailable" in retry_record.getMessage()


def test_mark_failed_safely_skips_retry_delay_when_shutdown_is_set():
    repository = FakeRepository(mark_failed_failures_before_success=1)
    config = ProcessingConfig()
    config.failure_mark_retries = 1
    config.failure_mark_retry_delay_seconds = 60.0
    pipeline = ProcessingPipeline(repository, FakeClassifier(), config)
    envelope = _make_raw_message_envelope()
    shutdown = asyncio.Event()
    shutdown.set()

    asyncio.run(
        pipeline._mark_failed_safely(envelope.ingested_item.id, "boom", shutdown)
    )

    assert repository.mark_failed_attempts == 1
    assert repository.mark_failed_calls == []


def test_process_item_swallow_mark_failed_errors_after_retries_exhausted(
    caplog: pytest.LogCaptureFixture,
):
    repository = FakeRepository(
        mark_failed_failures_before_success=3,
    )
    classifier = ExplodingClassifier()
    config = ProcessingConfig()
    config.failure_mark_retries = 2
    config.failure_mark_retry_delay_seconds = 0.0
    pipeline = ProcessingPipeline(repository, classifier, config)
    envelope = _make_raw_message_envelope()

    with caplog.at_level(logging.ERROR, logger="processing.pipeline"):
        pipeline.process_item(envelope)

    assert repository.mark_processed_calls == []
    assert repository.mark_failed_attempts == 3
    assert repository.mark_failed_calls == []
    error_record = next(
        record
        for record in caplog.records
        if "Failed to mark ingested item" in record.getMessage()
    )
    assert error_record.exc_info is not None
    assert error_record.exc_info[0] is RuntimeError
    assert "mark_failed unavailable" in error_record.getMessage()


def test_run_loop_exits_from_idle_wait_when_shutdown_is_set():
    repository = PollingSequenceRepository(batches=[[]])
    pipeline = ProcessingPipeline(repository, FakeClassifier(), ProcessingConfig())
    shutdown = asyncio.Event()

    async def run_until_idle_then_shutdown() -> None:
        task = asyncio.create_task(pipeline.run_until_shutdown(shutdown))
        while repository.get_pending_batch_calls == 0:
            await asyncio.sleep(0)
        shutdown.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run_until_idle_then_shutdown())

    assert repository.get_pending_batch_calls == 1


def test_run_loop_drains_active_batch_after_shutdown_then_stops():
    envelope = _make_structured_assignment_envelope()
    repository = PollingSequenceRepository(batches=[[envelope], []])
    pipeline = ProcessingPipeline(repository, FakeClassifier(), ProcessingConfig())
    shutdown = asyncio.Event()
    batch_started = asyncio.Event()
    allow_batch_to_finish = asyncio.Event()
    batch_finalized = False

    async def fake_process_batch(batch: list[ProcessingEnvelope]):
        batch_started.set()
        shutdown.set()
        await allow_batch_to_finish.wait()
        return [(item, None) for item in batch]

    async def fake_persist_successes(
        batch: list[ProcessingEnvelope], shutdown_arg: asyncio.Event | None = None
    ):
        nonlocal batch_finalized
        assert shutdown_arg is shutdown
        batch_finalized = True
        return [item.ingested_item.id for item in batch]

    async def run_until_batch_started_then_release() -> None:
        pipeline._process_batch = fake_process_batch
        pipeline._persist_successes = fake_persist_successes
        task = asyncio.create_task(pipeline.run_until_shutdown(shutdown))
        await asyncio.wait_for(batch_started.wait(), timeout=1.0)
        allow_batch_to_finish.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run_until_batch_started_then_release())

    assert batch_finalized is True
    assert repository.get_pending_batch_calls == 1


def test_run_loop_continues_after_batch_fetch_error(
    caplog: pytest.LogCaptureFixture,
):
    repository = PollingSequenceRepository(
        batches=[RuntimeError("db temporarily unavailable"), []]
    )
    classifier = FakeClassifier()
    config = ProcessingConfig()
    config.poll_interval_seconds = 0.01
    pipeline = ProcessingPipeline(repository, classifier, config)
    shutdown = asyncio.Event()

    async def run_until_second_fetch_then_shutdown() -> None:
        task = asyncio.create_task(pipeline.run_until_shutdown(shutdown))
        while repository.get_pending_batch_calls < 2:
            await asyncio.sleep(0)
        shutdown.set()
        await asyncio.wait_for(task, timeout=1.0)

    with caplog.at_level(logging.ERROR, logger="processing.pipeline"):
        asyncio.run(run_until_second_fetch_then_shutdown())

    assert repository.get_pending_batch_calls == 2
    error_record = next(
        record
        for record in caplog.records
        if "Failed to fetch pending batch" in record.getMessage()
    )
    assert error_record.exc_info is not None
    assert error_record.exc_info[0] is RuntimeError
    assert "db temporarily unavailable" in error_record.getMessage()


def test_run_loop_logs_idle_poll_at_info(
    caplog: pytest.LogCaptureFixture,
):
    repository = PollingSequenceRepository(batches=[[]])
    pipeline = ProcessingPipeline(repository, FakeClassifier(), ProcessingConfig())
    shutdown = asyncio.Event()

    async def run_until_idle_then_shutdown() -> None:
        task = asyncio.create_task(pipeline.run_until_shutdown(shutdown))
        while repository.get_pending_batch_calls == 0:
            await asyncio.sleep(0)
        shutdown.set()
        await asyncio.wait_for(task, timeout=1.0)

    with caplog.at_level(logging.INFO, logger="processing.pipeline"):
        asyncio.run(run_until_idle_then_shutdown())

    idle_record = next(
        record
        for record in caplog.records
        if "No pending items found" in record.getMessage()
    )
    assert idle_record.levelno == logging.INFO


def test_run_loop_swallow_unexpected_worker_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    repository = PollingSequenceRepository(
        batches=[[_make_structured_assignment_envelope()], []]
    )
    classifier = FakeClassifier()
    config = ProcessingConfig()
    config.poll_interval_seconds = 0.01
    pipeline = ProcessingPipeline(repository, classifier, config)
    shutdown = asyncio.Event()

    async def exploding_process_batch(batch: list[ProcessingEnvelope]):
        raise RuntimeError("unexpected worker crash")

    monkeypatch.setattr(pipeline, "_process_batch", exploding_process_batch)

    async def run_until_second_fetch_then_shutdown() -> None:
        task = asyncio.create_task(pipeline.run_until_shutdown(shutdown))
        while repository.get_pending_batch_calls < 2:
            await asyncio.sleep(0)
        shutdown.set()
        await asyncio.wait_for(task, timeout=1.0)

    with caplog.at_level(logging.ERROR, logger="processing.pipeline"):
        asyncio.run(run_until_second_fetch_then_shutdown())

    assert repository.get_pending_batch_calls == 2
    error_record = next(
        record
        for record in caplog.records
        if "Batch crashed unexpectedly" in record.getMessage()
    )
    assert error_record.exc_info is not None
    assert error_record.exc_info[0] is RuntimeError
    assert "unexpected worker crash" in error_record.getMessage()


def test_persist_successes_retries_transient_batch_errors():
    course = Course(id=uuid4(), name="Programming in C and C++", code="EX-101-A")
    repository = FakeRepository(
        courses=[course],
        batch_failures=[TransientPersistenceError("db temporarily unavailable")],
    )
    classifier = FakeClassifier()
    config = ProcessingConfig()
    config.batch_persist_retries = 1
    config.batch_persist_retry_delay_seconds = 0.0
    pipeline = ProcessingPipeline(repository, classifier, config)
    envelope = _make_structured_assignment_envelope()
    envelope = envelope.model_copy(update={"resolved_course_id": course.id})

    persisted_ids = asyncio.run(pipeline._persist_successes([envelope]))

    assert persisted_ids == [envelope.ingested_item.id]
    assert len(repository.persist_batch_calls) == 2
    assert repository.mark_processed_calls == [envelope.ingested_item.id]


def test_persist_successes_skips_retry_delay_when_shutdown_is_set():
    repository = FakeRepository(
        batch_failures=[TransientPersistenceError("db temporarily unavailable")],
    )
    config = ProcessingConfig()
    config.batch_persist_retries = 1
    config.batch_persist_retry_delay_seconds = 60.0
    pipeline = ProcessingPipeline(repository, FakeClassifier(), config)
    envelope = _make_structured_assignment_envelope()
    shutdown = asyncio.Event()
    shutdown.set()

    persisted_ids = asyncio.run(pipeline._persist_successes([envelope], shutdown))

    assert persisted_ids == []
    assert len(repository.persist_batch_calls) == 1


def test_persist_successes_logs_transient_retry_traceback(
    caplog: pytest.LogCaptureFixture,
):
    course = Course(id=uuid4(), name="Programming in C and C++", code="EX-101-A")
    repository = FakeRepository(
        courses=[course],
        batch_failures=[TransientPersistenceError("db temporarily unavailable")],
    )
    classifier = FakeClassifier()
    config = ProcessingConfig()
    config.batch_persist_retries = 1
    config.batch_persist_retry_delay_seconds = 0.0
    pipeline = ProcessingPipeline(repository, classifier, config)
    envelope = _make_structured_assignment_envelope()
    envelope = envelope.model_copy(update={"resolved_course_id": course.id})

    with caplog.at_level(logging.WARNING, logger="processing.pipeline"):
        asyncio.run(pipeline._persist_successes([envelope]))

    retry_record = next(
        record for record in caplog.records if "retrying" in record.getMessage()
    )
    assert retry_record.exc_info is not None
    assert retry_record.exc_info[0] is TransientPersistenceError
    assert "db temporarily unavailable" in retry_record.getMessage()


def test_persist_successes_logs_transient_exhaustion_traceback(
    caplog: pytest.LogCaptureFixture,
):
    envelope = _make_structured_assignment_envelope()
    repository = FakeRepository(
        batch_failures=[TransientPersistenceError("db still unavailable")],
    )
    config = ProcessingConfig()
    config.batch_persist_retries = 0
    pipeline = ProcessingPipeline(repository, FakeClassifier(), config)

    with caplog.at_level(logging.ERROR, logger="processing.pipeline"):
        persisted_ids = asyncio.run(pipeline._persist_successes([envelope]))

    assert persisted_ids == []
    error_record = next(
        record
        for record in caplog.records
        if "Batch persistence failed after" in record.getMessage()
    )
    assert error_record.exc_info is not None
    assert error_record.exc_info[0] is TransientPersistenceError
    assert "db still unavailable" in error_record.getMessage()


def test_persist_successes_falls_back_to_per_item_persistence_and_leaves_failures_pending():
    good = _make_structured_assignment_envelope()
    bad = _make_structured_assignment_envelope()
    repository = FakeRepository(
        batch_failures=[RowScopedPersistenceError("batch row failure")],
        per_item_failures={
            bad.ingested_item.id: RowScopedPersistenceError("single row failure")
        },
    )
    pipeline = ProcessingPipeline(repository, FakeClassifier(), ProcessingConfig())

    persisted_ids = asyncio.run(pipeline._persist_successes([good, bad]))

    assert persisted_ids == [good.ingested_item.id]
    assert repository.persist_batch_calls == [[good, bad], [good], [bad]]
    assert repository.mark_processed_calls == [good.ingested_item.id]
    assert repository.mark_failed_calls == []


def test_persist_individually_logs_row_error_traceback(
    caplog: pytest.LogCaptureFixture,
):
    envelope = _make_structured_assignment_envelope()
    repository = FakeRepository(
        per_item_failures={
            envelope.ingested_item.id: RowScopedPersistenceError("single row failure")
        },
    )
    pipeline = ProcessingPipeline(repository, FakeClassifier(), ProcessingConfig())

    with caplog.at_level(logging.ERROR, logger="processing.pipeline"):
        persisted_ids = asyncio.run(pipeline._persist_individually([envelope]))

    assert persisted_ids == []
    error_record = next(
        record
        for record in caplog.records
        if "Failed to persist ingested item" in record.getMessage()
    )
    assert error_record.exc_info is not None
    assert error_record.exc_info[0] is RowScopedPersistenceError
    assert "single row failure" in error_record.getMessage()


def test_persist_successes_logs_row_scoped_fallback_traceback(
    caplog: pytest.LogCaptureFixture,
):
    envelope = _make_structured_assignment_envelope()
    repository = FakeRepository(
        batch_failures=[RowScopedPersistenceError("batch row failure")],
    )
    pipeline = ProcessingPipeline(repository, FakeClassifier(), ProcessingConfig())

    with caplog.at_level(logging.WARNING, logger="processing.pipeline"):
        asyncio.run(pipeline._persist_successes([envelope]))

    fallback_record = next(
        record
        for record in caplog.records
        if "falling back to per-item persistence" in record.getMessage()
    )
    assert fallback_record.exc_info is not None
    assert fallback_record.exc_info[0] is RowScopedPersistenceError
    assert "batch row failure" in fallback_record.getMessage()


def test_persist_unclassified_batch_logs_exception_message_and_traceback(
    caplog: pytest.LogCaptureFixture,
):
    envelope = _make_raw_message_envelope()
    repository = FakeRepository(
        unclassified_failure=RuntimeError("unclassified write failed"),
    )
    pipeline = ProcessingPipeline(repository, FakeClassifier(), ProcessingConfig())

    with caplog.at_level(logging.ERROR, logger="processing.pipeline"):
        persisted_ids = asyncio.run(pipeline._persist_unclassified_batch([envelope]))

    assert persisted_ids == []
    record = next(
        record
        for record in caplog.records
        if "Failed to persist unclassified batch" in record.getMessage()
    )
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
    assert "unclassified write failed" in record.getMessage()


@pytest.mark.django_db(transaction=True)
def test_process_item_rolls_back_partial_persist_and_leaves_item_pending_when_storage_fails():
    item = create_pending_moodle_assignment()
    create_course(name="Matrix Algebra and Advanced Calculus I", code="EX-102")

    repository = ExplodingLinksRepository()
    classifier = FakeClassifier()
    pipeline = ProcessingPipeline(repository, classifier, ProcessingConfig())
    envelope = mapper.orm_to_envelope(item)

    pipeline.process_item(envelope)

    assert BaseClassifiedItem.objects.filter(ingested_item_id=item.id).count() == 0
    pending_item = IngestedItem.objects.get(pk=item.id)
    assert pending_item.processing_status == "pending"
    assert pending_item.error_message is None
    assert classifier.classify_calls == 0


@pytest.mark.django_db(transaction=True)
def test_batch_pipeline_with_real_db_persists_successes_and_marks_failed_items(
    monkeypatch: pytest.MonkeyPatch,
):
    create_course(name="Programming in C and C++", code="EX-101-A")

    success_assignment = create_pending_teams_raw_message(
        external_id="teams_success_assign",
    )
    success_announcement = create_pending_teams_raw_message(
        external_id="teams_success_announce",
        payload_overrides={
            "body": "General:\r\nThe exam room has changed for tomorrow morning.",
        },
    )
    failed_message = create_pending_teams_raw_message(
        external_id="teams_fail_classify",
        payload_overrides={
            "body": "General:\r\nThis message will fail classification.",
        },
    )

    repository = PostgresEntityRepository()
    classifier = ScenarioClassifier()
    pipeline = ProcessingPipeline(repository, classifier, ProcessingConfig())
    persist_calls: list[list[ProcessingEnvelope]] = []
    original_persist_batch = repository.persist_batch

    def tracked_persist_batch(envelopes: list[ProcessingEnvelope]) -> None:
        persist_calls.append(list(envelopes))
        original_persist_batch(envelopes)

    monkeypatch.setattr(repository, "persist_batch", tracked_persist_batch)

    batch = repository.get_pending_batch(limit=10)

    results = asyncio.run(pipeline._process_batch(batch))
    successes = [envelope for envelope, error in results if error is None]
    failures = [(envelope, error) for envelope, error in results if error is not None]

    persisted_ids = asyncio.run(pipeline._persist_successes(successes))
    for envelope, error in failures:
        assert error is not None
        asyncio.run(pipeline._mark_failed_safely(envelope.ingested_item.id, str(error)))

    success_assignment.refresh_from_db()
    success_announcement.refresh_from_db()
    failed_message.refresh_from_db()

    assert len(persist_calls) == 1
    assert {envelope.ingested_item.id for envelope in persist_calls[0]} == {
        success_assignment.id,
        success_announcement.id,
    }
    assert set(persisted_ids) == {success_assignment.id, success_announcement.id}

    assert success_assignment.processing_status == "processed"
    assert success_announcement.processing_status == "processed"
    assert failed_message.processing_status == "failed"
    assert failed_message.error_message == "classification boom"

    assert (
        BaseClassifiedItem.objects.filter(
            ingested_item_id__in=[success_assignment.id, success_announcement.id]
        ).count()
        == 2
    )
    assert (
        BaseClassifiedItem.objects.filter(ingested_item_id=failed_message.id).count()
        == 0
    )
    assert IngestedItem.objects.filter(processing_status="processed").count() >= 2
    assert (
        success_assignment.classification_logs.filter(
            classification_method="llm"
        ).count()
        == 1
    )
    assert (
        success_announcement.classification_logs.filter(
            classification_method="llm"
        ).count()
        == 1
    )
    assert failed_message.classification_logs.count() == 0
