# pyright: reportAttributeAccessIssue=false, reportArgumentType=false

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from core.models import (
    Announcement as AnnouncementORM,
    Assignment as AssignmentORM,
    Attachment,
    BaseClassifiedItem,
    CanonicalEntity,
    ClassificationLog,
    DedupLog,
    Event as EventORM,
    IngestedItem,
    Link,
    Material as MaterialORM,
)
from processing.adapters.postgres import mapper
from processing.adapters.postgres.repository import PostgresEntityRepository
from processing.exceptions import RowScopedPersistenceError
from processing.models.classification import ClassificationLogEntry
from processing.models.dedup import CanonicalCandidate, DedupDecision
from processing.models.extracted import (
    ExtractedAttachment,
    ExtractedLink,
)
from processing.models.entities import RawMessage
from processing.service_metadata import PROCESSOR_VERSION, PROMPT_VERSION
from processing.tests.fixtures import (
    create_course,
    create_pending_moodle_announcement,
    create_pending_moodle_assignment,
    create_pending_moodle_event,
    create_pending_moodle_material,
    create_pending_teams_raw_message,
    create_processed_moodle_assignment,
)


class ExplodingLinksRepository(PostgresEntityRepository):
    def save_links(self, classified_item_id, links) -> None:
        raise RuntimeError("save_links failed")


@pytest.mark.django_db
def test_get_pending_batch_returns_pending_items_in_created_order():
    first = create_pending_moodle_assignment()
    create_processed_moodle_assignment()
    second = create_pending_moodle_event()

    repository = PostgresEntityRepository()

    batch = repository.get_pending_batch(limit=10)

    assert [envelope.ingested_item.id for envelope in batch] == [first.id, second.id]
    assert [envelope.entity.entity_type for envelope in batch] == [
        "assignment",
        "event",
    ]


@pytest.mark.django_db
def test_dev_repository_get_pending_batch_filters_by_service_start():
    old_item = create_pending_moodle_assignment(
        payload_overrides={"published_at": "2026-04-25T11:00:00+00:00"}
    )
    boundary_item = create_pending_moodle_event(
        payload_overrides={"published_at": "2026-04-25T11:30:00+00:00"}
    )
    stale_published_item = create_pending_moodle_announcement(
        payload_overrides={"published_at": "2026-04-25T10:30:00+00:00"}
    )
    missing_published_item = create_pending_moodle_material()
    new_item = create_pending_moodle_announcement(
        payload_overrides={"published_at": "2026-04-25T12:00:00+00:00"}
    )
    type(old_item).objects.filter(pk=old_item.pk).update(
        created_at=datetime(2026, 4, 25, 10, 0, tzinfo=timezone.utc)
    )
    type(boundary_item).objects.filter(pk=boundary_item.pk).update(
        created_at=datetime(2026, 4, 25, 10, 30, tzinfo=timezone.utc)
    )
    type(stale_published_item).objects.filter(pk=stale_published_item.pk).update(
        created_at=datetime(2026, 4, 25, 11, 0, tzinfo=timezone.utc)
    )
    type(missing_published_item).objects.filter(pk=missing_published_item.pk).update(
        created_at=datetime(2026, 4, 25, 11, 30, tzinfo=timezone.utc)
    )
    type(new_item).objects.filter(pk=new_item.pk).update(
        created_at=datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    )
    service_start = datetime(2026, 4, 25, 10, 30, tzinfo=timezone.utc)

    dev_repository_module = importlib.import_module(
        "processing.adapters.postgres.dev_repository"
    )
    repository = dev_repository_module.DevPostgresEntityRepository(service_start)

    batch = repository.get_pending_batch(limit=10)

    assert [envelope.ingested_item.id for envelope in batch] == [new_item.id]
    assert old_item.id not in [envelope.ingested_item.id for envelope in batch]
    assert boundary_item.id not in [envelope.ingested_item.id for envelope in batch]
    assert stale_published_item.id not in [
        envelope.ingested_item.id for envelope in batch
    ]
    assert missing_published_item.id not in [
        envelope.ingested_item.id for envelope in batch
    ]


@pytest.mark.django_db
def test_get_existing_canonicals_projects_primary_item_fields():
    item = create_pending_moodle_assignment()
    course = create_course(name="Normalized course", code="EX-101-A")
    envelope = mapper.orm_to_envelope(item)
    envelope = envelope.model_copy(update={"resolved_course_id": course.id})
    repository = PostgresEntityRepository()
    classified_item_id = repository.save_entity(envelope)
    canonical_id = uuid4()
    CanonicalEntity.objects.create(
        id=canonical_id,
        entity_type="assignment",
        primary_item_id=classified_item_id,
        course_id=course.id,
    )

    candidates = repository.get_existing_canonicals(course.id, "assignment")

    assert len(candidates) == 1
    assert candidates[0].canonical_id == canonical_id
    assert isinstance(candidates[0], CanonicalCandidate)
    assert candidates[0].entity.title == "Homework 1"
    assert candidates[0].entity.deadline == datetime(
        2025, 9, 9, 21, 59, tzinfo=timezone.utc
    )
    assert candidates[0].persisted_primary_item_id == classified_item_id


@pytest.mark.django_db
def test_get_existing_canonicals_projects_announcement_body_and_published_at():
    item = create_pending_moodle_announcement()
    course = create_course(name="Normalized course", code="SAT-02")
    envelope = mapper.orm_to_envelope(item).model_copy(
        update={"resolved_course_id": course.id}
    )
    repository = PostgresEntityRepository()
    classified_item_id = repository.save_entity(envelope)
    canonical_id = uuid4()
    CanonicalEntity.objects.create(
        id=canonical_id,
        entity_type="announcement",
        primary_item_id=classified_item_id,
        course_id=course.id,
    )

    candidates = repository.get_existing_canonicals(course.id, "announcement")

    assert len(candidates) == 1
    assert candidates[0].canonical_id == canonical_id
    assert isinstance(candidates[0], CanonicalCandidate)
    assert candidates[0].entity.title == "Extra online classes"
    assert "meet online" in (candidates[0].entity.body or "")
    assert candidates[0].entity.published_at == datetime(
        2025, 11, 4, 12, 52, 28, tzinfo=timezone.utc
    )


@pytest.mark.django_db
def test_get_existing_canonicals_skips_missing_child_row(caplog):
    item = create_pending_moodle_assignment(
        external_id=f"moodle_orphan_{uuid4().hex[:8]}"
    )
    course = create_course(name="Normalized course", code="EX-101-A")
    orphan = BaseClassifiedItem.objects.create(
        ingested_item_id=item.id,
        entity_type="assignment",
        course_id=course.id,
        title="Homework 1",
    )
    canonical_id = uuid4()
    CanonicalEntity.objects.create(
        id=canonical_id,
        entity_type="assignment",
        primary_item_id=orphan.id,
        course_id=course.id,
    )
    repository = PostgresEntityRepository()

    with caplog.at_level("WARNING"):
        candidates = repository.get_existing_canonicals(course.id, "assignment")

    assert candidates == []
    assert f"Skipping canonical {canonical_id}" in caplog.text


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("factory", "orm_model", "field_name", "expected_value"),
    [
        (
            create_pending_moodle_assignment,
            AssignmentORM,
            "deadline",
            datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
        ),
        (
            create_pending_moodle_announcement,
            AnnouncementORM,
            "title",
            "Extra online classes",
        ),
        (
            create_pending_moodle_event,
            EventORM,
            "start_time",
            datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc),
        ),
        (
            create_pending_moodle_material,
            MaterialORM,
            "title",
            "Lecture 1 (Intro)",
        ),
    ],
)
def test_save_entity_creates_base_and_typed_child_rows(
    factory, orm_model, field_name, expected_value
):
    item = factory()
    course = create_course(name="Normalized course", code="EX-101-A")
    envelope = mapper.orm_to_envelope(item)
    envelope = envelope.model_copy(update={"resolved_course_id": course.id})

    repository = PostgresEntityRepository()

    classified_item_id = repository.save_entity(envelope)

    base_item = BaseClassifiedItem.objects.get(pk=classified_item_id)
    typed_child = orm_model.objects.get(pk=classified_item_id)

    assert base_item.ingested_item_id == item.id
    assert base_item.course_id == course.id
    assert base_item.entity_type == envelope.entity.entity_type
    assert getattr(typed_child, field_name) == expected_value


@pytest.mark.django_db
def test_save_links_and_attachments_replace_existing_rows():
    item = create_pending_moodle_assignment()
    envelope = mapper.orm_to_envelope(item)
    repository = PostgresEntityRepository()
    classified_item_id = repository.save_entity(envelope)

    repository.save_links(
        classified_item_id,
        [ExtractedLink(url="https://example.com/old", label="Old")],
    )
    repository.save_links(
        classified_item_id,
        [ExtractedLink(url="https://example.com/new", label="New")],
    )
    repository.save_attachments(
        classified_item_id,
        [
            ExtractedAttachment(
                filename="old.pdf",
                mime_type="application/pdf",
                url="https://example.com/old.pdf",
            )
        ],
    )
    repository.save_attachments(
        classified_item_id,
        [
            ExtractedAttachment(
                filename="new.pdf",
                mime_type="application/pdf",
                url="https://example.com/new.pdf",
            )
        ],
    )
    assert list(
        Link.objects.filter(classified_item_id=classified_item_id).values_list(
            "url", "label"
        )
    ) == [("https://example.com/new", "New")]
    assert list(
        Attachment.objects.filter(classified_item_id=classified_item_id).values_list(
            "filename", "mime_type", "url"
        )
    ) == [("new.pdf", "application/pdf", "https://example.com/new.pdf")]


@pytest.mark.django_db
def test_mark_processed_and_failed_update_ingested_status():
    processed_item = create_pending_moodle_assignment()
    failed_item = create_pending_moodle_event()
    repository = PostgresEntityRepository()

    repository.mark_processed(processed_item.id)
    repository.mark_failed(
        failed_item.id,
        "OpenAI request timed out while extracting fields.",
    )

    processed = IngestedItem.objects.get(pk=processed_item.id)
    assert processed.processing_status == "processed"
    assert processed.processing_completed_at is not None
    failed = IngestedItem.objects.get(pk=failed_item.id)
    assert failed.processing_status == "failed"
    assert failed.processing_completed_at is not None
    assert failed.error_message == "OpenAI request timed out while extracting fields."


@pytest.mark.django_db
def test_save_classification_log_persists_row():
    item = create_pending_moodle_assignment()
    envelope = mapper.orm_to_envelope(item)
    repository = PostgresEntityRepository()
    classified_item_id = repository.save_entity(envelope)

    repository.save_classification_log(
        ClassificationLogEntry(
            ingested_item_id=item.id,
            classified_item_id=classified_item_id,
            entity_type="assignment",
            classification_method="llm",
            llm_model="gpt-5",
            llm_raw_output="{...}",
            confidence=0.91,
            processing_time_ms=842,
            input_tokens=140,
            output_tokens=29,
        )
    )

    log = ClassificationLog.objects.get(ingested_item_id=item.id)
    assert log.classified_item_id == classified_item_id
    assert log.entity_type == "assignment"
    assert log.classification_method == "llm"
    assert log.confidence == 0.91
    assert log.input_tokens == 140
    assert log.output_tokens == 29
    assert log.processor_version == PROCESSOR_VERSION
    assert log.prompt_version == PROMPT_VERSION


@pytest.mark.django_db
def test_persist_batch_creates_records_and_marks_items_processed():
    item = create_pending_moodle_assignment()
    course = create_course(name="Normalized course", code="EX-101-A")
    envelope = mapper.orm_to_envelope(item)
    envelope = envelope.model_copy(
        update={
            "resolved_course_id": course.id,
            "extracted_links": [
                ExtractedLink(url="https://example.com/spec", label="Spec")
            ],
            "classification_log": ClassificationLogEntry(
                ingested_item_id=item.id,
                classified_item_id=item.id,
                entity_type="assignment",
                classification_method="deterministic",
                confidence=1.0,
            ),
        }
    )

    repository = PostgresEntityRepository()

    repository.persist_batch([envelope])

    assert BaseClassifiedItem.objects.filter(ingested_item_id=item.id).count() == 1
    assert Link.objects.filter(classified_item__ingested_item_id=item.id).count() == 1
    assert ClassificationLog.objects.filter(ingested_item_id=item.id).count() == 1
    persisted = IngestedItem.objects.get(pk=item.id)
    assert persisted.processing_status == "processed"
    assert persisted.processing_completed_at is not None


@pytest.mark.django_db
def test_persist_batch_without_dedup_decision_preserves_null_canonical():
    item = create_pending_moodle_assignment()
    course = create_course(name="Normalized course", code="EX-101-A")
    envelope = mapper.orm_to_envelope(item).model_copy(
        update={"resolved_course_id": course.id}
    )
    repository = PostgresEntityRepository()

    repository.persist_batch([envelope])

    classified = BaseClassifiedItem.objects.get(ingested_item_id=item.id)
    assert classified.canonical_entity_id is None
    assert classified.dedup_status == "skipped"
    assert CanonicalEntity.objects.count() == 0
    assert (
        DedupLog.objects.filter(classified_item=classified, status="skipped").count()
        == 1
    )


@pytest.mark.django_db
def test_persist_batch_creates_new_canonical_and_links_item():
    item = create_pending_moodle_assignment()
    course = create_course(name="Normalized course", code="EX-101-A")
    canonical_id = uuid4()
    envelope = mapper.orm_to_envelope(item).model_copy(
        update={
            "resolved_course_id": course.id,
            "dedup_decision": DedupDecision(
                decision="new_canonical",
                canonical_entity_id=canonical_id,
                matched_by="no_match",
                score=0.0,
            ),
            "dedup_status": "primary",
        }
    )
    repository = PostgresEntityRepository()

    repository.persist_batch([envelope])

    classified = BaseClassifiedItem.objects.get(ingested_item_id=item.id)
    canonical = CanonicalEntity.objects.get(pk=canonical_id)
    assert classified.canonical_entity_id == canonical_id
    assert classified.dedup_status == "primary"
    assert canonical.primary_item_id == classified.id
    assert canonical.course_id == course.id
    assert canonical.entity_type == "assignment"
    assert (
        DedupLog.objects.filter(classified_item=classified, status="primary").count()
        == 1
    )


@pytest.mark.django_db
def test_persist_batch_links_same_batch_duplicate_to_first_anchor_canonical():
    first_item = create_pending_moodle_assignment()
    second_item = create_pending_moodle_assignment(
        external_id=f"moodle_assign_dupe_{uuid4().hex[:8]}"
    )
    course = create_course(name="Normalized course", code="EX-101-A")
    canonical_id = uuid4()
    first = mapper.orm_to_envelope(first_item).model_copy(
        update={
            "resolved_course_id": course.id,
            "dedup_decision": DedupDecision(
                decision="new_canonical",
                canonical_entity_id=canonical_id,
                matched_by="no_match",
                score=0.0,
            ),
            "dedup_status": "primary",
        }
    )
    second = mapper.orm_to_envelope(second_item).model_copy(
        update={
            "resolved_course_id": course.id,
            "dedup_decision": DedupDecision(
                decision="link_existing",
                canonical_entity_id=canonical_id,
                matched_by="weighted",
                score=1.0,
            ),
            "dedup_status": "linked",
        }
    )
    repository = PostgresEntityRepository()

    repository.persist_batch([first, second])

    assert CanonicalEntity.objects.filter(pk=canonical_id).count() == 1
    classified_items = list(
        BaseClassifiedItem.objects.filter(
            ingested_item_id__in=[first_item.id, second_item.id]
        ).order_by("created_at")
    )
    assert [item.canonical_entity_id for item in classified_items] == [
        canonical_id,
        canonical_id,
    ]
    assert [item.dedup_status for item in classified_items] == ["primary", "linked"]


@pytest.mark.django_db
def test_persist_batch_promotes_existing_canonical_primary():
    existing_item = create_pending_moodle_assignment(
        external_id=f"moodle_existing_{uuid4().hex[:8]}"
    )
    promoted_item = create_pending_moodle_assignment(
        external_id=f"moodle_promoted_{uuid4().hex[:8]}"
    )
    course = create_course(name="Normalized course", code="EX-101-A")
    repository = PostgresEntityRepository()

    existing_envelope = mapper.orm_to_envelope(existing_item).model_copy(
        update={"resolved_course_id": course.id}
    )
    existing_classified_id = repository.save_entity(existing_envelope)
    canonical_id = uuid4()
    CanonicalEntity.objects.create(
        id=canonical_id,
        entity_type="assignment",
        primary_item_id=existing_classified_id,
        course_id=course.id,
    )

    promoted = mapper.orm_to_envelope(promoted_item).model_copy(
        update={
            "resolved_course_id": course.id,
            "dedup_decision": DedupDecision(
                decision="link_existing",
                canonical_entity_id=canonical_id,
                matched_by="weighted",
                score=0.98,
            ),
            "dedup_status": "promoted",
        }
    )

    repository.persist_batch([promoted])

    promoted_classified = BaseClassifiedItem.objects.get(
        ingested_item_id=promoted_item.id
    )
    canonical = CanonicalEntity.objects.get(pk=canonical_id)
    assert canonical.primary_item_id == promoted_classified.id
    assert promoted_classified.canonical_entity_id == canonical_id
    assert promoted_classified.dedup_status == "promoted"
    assert (
        DedupLog.objects.filter(
            classified_item=promoted_classified,
            status="promoted",
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_persist_canonical_assignments_raises_row_scoped_error_when_new_canonical_has_no_primary():
    item = create_pending_moodle_assignment()
    course = create_course(name="Normalized course", code="EX-101-A")
    canonical_id = uuid4()
    envelope = mapper.orm_to_envelope(item).model_copy(
        update={
            "resolved_course_id": course.id,
            "dedup_decision": DedupDecision(
                decision="link_existing",
                canonical_entity_id=canonical_id,
                matched_by="weighted",
                score=0.95,
            ),
            "dedup_status": "linked",
        }
    )
    repository = PostgresEntityRepository()
    classified_item_id = repository.save_entity(envelope)

    with pytest.raises(
        RowScopedPersistenceError,
        match="missing primary item in batch",
    ):
        repository._persist_canonical_assignments({0: classified_item_id}, [envelope])


@pytest.mark.django_db
def test_repository_persists_normalized_entity_and_supporting_values():
    item = create_pending_moodle_assignment()
    course = create_course(name="Normalized course", code="EX-101-A")
    envelope = mapper.orm_to_envelope(item)
    envelope = envelope.model_copy(
        update={
            "resolved_course_id": course.id,
            "entity": envelope.entity.model_copy(
                update={
                    "title": "Homework 2",
                    "body": "Paragraph one\n\nParagraph two",
                    "author": None,
                }
            ),
        }
    )

    repository = PostgresEntityRepository()

    classified_item_id = repository.save_entity(envelope)
    repository.save_links(
        classified_item_id,
        [
            ExtractedLink(
                url="https://example.edu/material", label="Material"
            )
        ],
    )
    repository.save_attachments(
        classified_item_id,
        [
            ExtractedAttachment(
                filename="slides.pdf",
                mime_type="application/pdf",
                url="https://example.com/slides.pdf",
            )
        ],
    )

    base_item = BaseClassifiedItem.objects.get(pk=classified_item_id)

    assert base_item.title == "Homework 2"
    assert base_item.body == "Paragraph one\n\nParagraph two"
    assert base_item.author is None
    assert list(
        Link.objects.filter(classified_item_id=classified_item_id).values_list(
            "url", "label"
        )
    ) == [("https://example.edu/material", "Material")]
    assert list(
        Attachment.objects.filter(classified_item_id=classified_item_id).values_list(
            "filename", "mime_type", "url"
        )
    ) == [("slides.pdf", "application/pdf", "https://example.com/slides.pdf")]


@pytest.mark.django_db(transaction=True)
def test_persist_batch_rolls_back_and_translates_row_scoped_failures():
    item = create_pending_moodle_assignment()
    create_course(name="Matrix Algebra and Advanced Calculus I", code="EX-102")
    envelope = mapper.orm_to_envelope(item)

    repository = ExplodingLinksRepository()

    with pytest.raises(RowScopedPersistenceError):
        repository.persist_batch([envelope])

    assert BaseClassifiedItem.objects.filter(ingested_item_id=item.id).count() == 0
    assert IngestedItem.objects.get(pk=item.id).processing_status == "pending"


@pytest.mark.django_db
def test_persist_unclassified_batch_writes_log_and_marks_processed_without_classified_item():
    item = create_pending_teams_raw_message()
    repository = PostgresEntityRepository()
    envelope = mapper.orm_to_envelope(item)

    assert isinstance(envelope.entity, RawMessage)

    envelope = envelope.model_copy(
        update={
            "classification_log": ClassificationLogEntry(
                ingested_item_id=item.id,
                entity_type="raw_message",
                classification_method="llm",
                llm_model="gpt-5",
                confidence=0.95,
                processing_time_ms=120,
                input_tokens=140,
                output_tokens=29,
            )
        }
    )

    repository.persist_unclassified_batch([envelope])

    persisted = IngestedItem.objects.get(pk=item.id)
    assert persisted.processing_status == "processed"
    assert persisted.processing_completed_at is not None
    assert BaseClassifiedItem.objects.filter(ingested_item_id=item.id).count() == 0
    log = ClassificationLog.objects.get(ingested_item_id=item.id)
    assert log.entity_type == "raw_message"
    assert log.classification_method == "llm"
    assert log.classified_item_id is None
    assert log.confidence == 0.95
    assert log.input_tokens == 140
    assert log.output_tokens == 29


@pytest.mark.django_db
def test_save_entity_raises_for_raw_message_envelope():
    item = create_pending_teams_raw_message()
    envelope = mapper.orm_to_envelope(item)

    assert isinstance(envelope.entity, RawMessage)

    repository = PostgresEntityRepository()

    with pytest.raises(ValueError, match="unclassified"):
        repository.save_entity(envelope)


@pytest.mark.django_db
def test_course_operations_return_domain_models_and_update_code():
    create_course(name="Matrix Algebra", code="MA-101")
    repository = PostgresEntityRepository()

    created = repository.create_course(name="Programming in C and C++")
    repository.update_course_code(created.id, "EX-101-A")
    courses = repository.get_all_courses()

    created_course = next(course for course in courses if course.id == created.id)
    assert created_course.name == "Programming in C and C++"
    assert created_course.code == "EX-101-A"
    assert {course.code for course in courses} == {"MA-101", "EX-101-A"}


@pytest.mark.django_db
def test_course_platform_mapping_operations_lookup_and_upsert():
    course = create_course(name="SAT Training - English", code="SAT-02")
    repository = PostgresEntityRepository()

    assert repository.get_course_by_platform_mapping("moodle", "1000") is None

    repository.upsert_course_platform_mapping(
        course.id,
        "moodle",
        "1000",
        "SYNTHETIC EXAMPLE COURSE",
    )

    mapped = repository.get_course_by_platform_mapping("moodle", "1000")
    assert mapped is not None
    assert mapped.id == course.id
    assert mapped.name == "SAT Training - English"
    assert mapped.code == "SAT-02"
