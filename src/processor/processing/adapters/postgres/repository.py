# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import logging
import threading
from typing import Callable, TypeVar
from typing_extensions import ParamSpec
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from processing.adapters.postgres import mapper
from processing.adapters.postgres.db_errors import translate_persistence_error
from processing.exceptions import RowScopedPersistenceError
from processing.models.classification import ClassificationLogEntry
from processing.models.course import Course
from processing.models.dedup import CanonicalCandidate
from processing.models.envelope import ProcessingEnvelope
from processing.models.entities import Announcement, Assignment, Event, Material
from processing.models.extracted import (
    ExtractedAttachment,
    ExtractedLink,
)
from processing.ports.repository import EntityRepositoryPort

_P = ParamSpec("_P")
_R = TypeVar("_R")
logger = logging.getLogger(__name__)


class PostgresEntityRepository(EntityRepositoryPort):
    """Repository implementation backed by the shared Django ORM models."""

    def get_pending_batch(self, limit: int) -> list[ProcessingEnvelope]:
        return self._run_entrypoint(self._get_pending_batch, limit)

    def get_existing_canonicals(
        self, course_id: UUID, entity_type: str
    ) -> list[CanonicalCandidate]:
        return self._run_entrypoint(
            self._get_existing_canonicals, course_id, entity_type
        )

    def _get_pending_batch(self, limit: int) -> list[ProcessingEnvelope]:
        from core.models.ingested import IngestedItem

        queryset = IngestedItem.objects.filter(processing_status="pending").order_by(
            "created_at"
        )[:limit]
        return [mapper.orm_to_envelope(item) for item in queryset]

    def _get_existing_canonicals(
        self, course_id: UUID, entity_type: str
    ) -> list[CanonicalCandidate]:
        from core.models import (
            Announcement as AnnouncementORM,
            Assignment as AssignmentORM,
            CanonicalEntity,
            Event as EventORM,
            Material as MaterialORM,
        )

        canonicals = (
            CanonicalEntity.objects.select_related(
                "primary_item", "primary_item__ingested_item"
            )
            .filter(course_id=course_id, entity_type=entity_type)
            .order_by("created_at")
        )

        candidates: list[CanonicalCandidate] = []
        model_by_entity_type = {
            "assignment": (AssignmentORM, Assignment),
            "announcement": (AnnouncementORM, Announcement),
            "event": (EventORM, Event),
            "material": (MaterialORM, Material),
        }
        mapping = model_by_entity_type.get(entity_type)
        if mapping is None:
            return candidates
        orm_model, entity_model = mapping

        for canonical in canonicals:
            primary_item = canonical.primary_item
            typed_item = orm_model.objects.filter(pk=primary_item.pk).first()
            if typed_item is None:
                logger.warning(
                    "Skipping canonical %s for course=%s entity_type=%s because primary item %s has no typed child row",
                    canonical.id,
                    course_id,
                    entity_type,
                    primary_item.pk,
                )
                continue
            entity = entity_model.model_validate(typed_item)
            candidates.append(
                CanonicalCandidate(
                    canonical_id=canonical.id,
                    entity=entity,
                    platform=primary_item.ingested_item.platform,
                    persisted_primary_item_id=canonical.primary_item_id,
                )
            )
        return candidates

    def persist_batch(self, envelopes: list[ProcessingEnvelope]) -> None:
        return self._run_entrypoint(self._persist_batch, envelopes)

    def persist_unclassified_batch(self, envelopes: list[ProcessingEnvelope]) -> None:
        return self._run_entrypoint(self._persist_unclassified_batch_impl, envelopes)

    def _persist_unclassified_batch_impl(
        self, envelopes: list[ProcessingEnvelope]
    ) -> None:
        from core.models.ingested import IngestedItem

        try:
            with transaction.atomic():  # pyright: ignore[reportGeneralTypeIssues]
                for envelope in envelopes:
                    if envelope.classification_log is not None:
                        self.save_classification_log(envelope.classification_log)
                ingested_ids = [envelope.ingested_item.id for envelope in envelopes]
                completed_at = timezone.now()
                IngestedItem.objects.filter(pk__in=ingested_ids).update(
                    processing_status="processed",
                    processing_completed_at=completed_at,
                )
        except Exception as exc:
            raise translate_persistence_error(exc) from exc

    def _persist_batch(self, envelopes: list[ProcessingEnvelope]) -> None:
        from core.models.ingested import IngestedItem

        try:
            with transaction.atomic():  # pyright: ignore[reportGeneralTypeIssues]
                classified_item_ids: dict[int, UUID] = {}
                for index, envelope in enumerate(envelopes):
                    classified_item_id = self.save_entity(envelope)
                    classified_item_ids[index] = classified_item_id
                    self.save_links(classified_item_id, envelope.extracted_links)
                    self.save_attachments(
                        classified_item_id, envelope.extracted_attachments
                    )

                    if envelope.classification_log is not None:
                        self.save_classification_log(
                            envelope.classification_log.model_copy(
                                update={"classified_item_id": classified_item_id}
                            )
                        )

                self._persist_canonical_assignments(classified_item_ids, envelopes)
                self._persist_dedup_logs(classified_item_ids, envelopes)

                ingested_ids = [envelope.ingested_item.id for envelope in envelopes]
                completed_at = timezone.now()
                IngestedItem.objects.filter(pk__in=ingested_ids).update(
                    processing_status="processed",
                    processing_completed_at=completed_at,
                )
        except Exception as exc:
            raise translate_persistence_error(exc) from exc

    def save_entity(self, envelope: ProcessingEnvelope) -> UUID:
        from core.models import (
            Announcement as AnnouncementORM,
            Assignment as AssignmentORM,
            BaseClassifiedItem,
            Event as EventORM,
            Material as MaterialORM,
        )

        if envelope.entity.entity_type == "raw_message":
            raise ValueError(
                "save_entity called with an unclassified entity; "
                "use persist_unclassified_batch for unclassified items"
            )

        with transaction.atomic():  # pyright: ignore[reportGeneralTypeIssues]
            create_fields = {
                "ingested_item_id": envelope.ingested_item.id,
                "entity_type": envelope.entity.entity_type,
                "course_id": envelope.resolved_course_id,
                "dedup_status": envelope.dedup_status or "skipped",
                **mapper.entity_to_base_dict(envelope.entity),
            }
            child_fields = mapper.entity_to_child_dict(envelope.entity)

            if isinstance(envelope.entity, Assignment):
                item = AssignmentORM.objects.create(**create_fields, **child_fields)
                return item.id
            if isinstance(envelope.entity, Announcement):
                item = AnnouncementORM.objects.create(**create_fields, **child_fields)
                return item.id
            if isinstance(envelope.entity, Material):
                item = MaterialORM.objects.create(**create_fields, **child_fields)
                return item.id
            if isinstance(envelope.entity, Event):
                item = EventORM.objects.create(**create_fields, **child_fields)
                return item.id

            base_item = BaseClassifiedItem.objects.create(**create_fields)
            return base_item.id

    def _persist_canonical_assignments(
        self, classified_item_ids: dict[int, UUID], envelopes: list[ProcessingEnvelope]
    ) -> None:
        from core.models import BaseClassifiedItem, CanonicalEntity

        grouped: dict[UUID, list[int]] = {}
        for index, envelope in enumerate(envelopes):
            decision = envelope.dedup_decision
            canonical_id = None if decision is None else decision.canonical_entity_id
            if canonical_id is None:
                continue
            grouped.setdefault(canonical_id, []).append(index)

        existing = CanonicalEntity.objects.in_bulk(grouped.keys())
        for canonical_id, indexes in grouped.items():
            primary_index = next(
                (
                    index
                    for index in indexes
                    if envelopes[index].dedup_status in {"primary", "promoted"}
                ),
                None,
            )
            canonical = existing.get(canonical_id)
            if canonical is None:
                if primary_index is None:
                    raise RowScopedPersistenceError(
                        f"Canonical {canonical_id} missing primary item in batch"
                    )
                primary_envelope = envelopes[primary_index]
                canonical = CanonicalEntity.objects.create(
                    id=canonical_id,
                    entity_type=primary_envelope.entity.entity_type,
                    primary_item_id=classified_item_ids[primary_index],
                    course_id=primary_envelope.resolved_course_id,
                )
                existing[canonical_id] = canonical
            elif primary_index is not None:
                CanonicalEntity.objects.filter(pk=canonical_id).update(
                    primary_item_id=classified_item_ids[primary_index]
                )

            BaseClassifiedItem.objects.filter(
                pk__in=[classified_item_ids[index] for index in indexes]
            ).update(canonical_entity_id=canonical_id)

    def _persist_dedup_logs(
        self, classified_item_ids: dict[int, UUID], envelopes: list[ProcessingEnvelope]
    ) -> None:
        from core.models import DedupLog

        for index, envelope in enumerate(envelopes):
            decision = envelope.dedup_decision
            DedupLog.objects.create(
                classified_item_id=classified_item_ids[index],
                canonical_entity_id=(
                    None if decision is None else decision.canonical_entity_id
                ),
                status=envelope.dedup_status or "skipped",
                match_strategy=None if decision is None else decision.match_strategy,
                resolution_strategy=(
                    None if decision is None else decision.resolution_strategy
                ),
                score=None if decision is None else decision.score,
                error_message=None if decision is None else decision.error_message,
            )

    def mark_processed(self, ingested_item_id: UUID) -> None:
        self._run_entrypoint(self._mark_processed, ingested_item_id)

    def mark_failed(self, ingested_item_id: UUID, error: str) -> None:
        self._run_entrypoint(self._mark_failed, ingested_item_id, error)

    def _mark_failed(self, ingested_item_id: UUID, error: str) -> None:
        from core.models.ingested import IngestedItem

        IngestedItem.objects.filter(pk=ingested_item_id).update(
            processing_status="failed",
            error_message=error[:2000],
            processing_completed_at=timezone.now(),
        )

    def _mark_processed(self, ingested_item_id: UUID) -> None:
        from core.models.ingested import IngestedItem

        IngestedItem.objects.filter(pk=ingested_item_id).update(
            processing_status="processed",
            processing_completed_at=timezone.now(),
        )

    def save_links(self, classified_item_id: UUID, links: list[ExtractedLink]) -> None:
        from core.models import Link

        self._replace_supporting_rows(
            model=Link,
            classified_item_id=classified_item_id,
            orm_dicts=mapper.links_to_orm_dicts(classified_item_id, links),
        )

    def save_attachments(
        self,
        classified_item_id: UUID,
        attachments: list[ExtractedAttachment],
    ) -> None:
        from core.models import Attachment

        self._replace_supporting_rows(
            model=Attachment,
            classified_item_id=classified_item_id,
            orm_dicts=mapper.attachments_to_orm_dicts(classified_item_id, attachments),
        )

    def _replace_supporting_rows(
        self,
        *,
        model,
        classified_item_id: UUID,
        orm_dicts: list[dict],
    ) -> None:
        with transaction.atomic():  # pyright: ignore[reportGeneralTypeIssues]
            model.objects.filter(classified_item_id=classified_item_id).delete()
            model.objects.bulk_create([model(**item) for item in orm_dicts])

    def save_classification_log(self, log: ClassificationLogEntry) -> None:
        from core.models import ClassificationLog

        ClassificationLog.objects.create(**mapper.classification_log_to_orm_dict(log))

    def get_all_courses(self) -> list[Course]:
        return self._run_entrypoint(self._get_all_courses)

    def _get_all_courses(self) -> list[Course]:
        from core.models import Course as CourseORM

        return [mapper.orm_to_course(course) for course in CourseORM.objects.all()]

    def create_course(self, name: str, code: str | None = None) -> Course:
        return self._run_entrypoint(self._create_course, name, code)

    def _create_course(self, name: str, code: str | None = None) -> Course:
        from core.models import Course as CourseORM

        orm_course = CourseORM.objects.create(name=name, code=code)
        return mapper.orm_to_course(orm_course)

    def update_course_code(self, course_id: UUID, code: str) -> None:
        self._run_entrypoint(self._update_course_code, course_id, code)

    def _update_course_code(self, course_id: UUID, code: str) -> None:
        from core.models import Course as CourseORM

        CourseORM.objects.filter(pk=course_id).update(code=code)

    def get_course_by_platform_mapping(
        self, platform: str, external_course_id: str
    ) -> Course | None:
        return self._run_entrypoint(
            self._get_course_by_platform_mapping, platform, external_course_id
        )

    def _get_course_by_platform_mapping(
        self, platform: str, external_course_id: str
    ) -> Course | None:
        from core.models import CoursePlatformMapping

        mapping = (
            CoursePlatformMapping.objects.select_related("course")
            .filter(platform=platform, external_course_id=external_course_id)
            .first()
        )
        if mapping is None:
            return None
        return mapper.orm_to_course(mapping.course)

    def upsert_course_platform_mapping(
        self,
        course_id: UUID,
        platform: str,
        external_course_id: str,
        external_course_name: str | None,
    ) -> None:
        self._run_entrypoint(
            self._upsert_course_platform_mapping,
            course_id,
            platform,
            external_course_id,
            external_course_name,
        )

    def _upsert_course_platform_mapping(
        self,
        course_id: UUID,
        platform: str,
        external_course_id: str,
        external_course_name: str | None,
    ) -> None:
        from core.models import CoursePlatformMapping

        CoursePlatformMapping.objects.update_or_create(
            platform=platform,
            external_course_id=external_course_id,
            defaults={
                "course_id": course_id,
                "external_course_name": external_course_name,
            },
        )

    def _run_entrypoint(
        self, operation: Callable[_P, _R], *args: _P.args, **kwargs: _P.kwargs
    ) -> _R:
        from django.db import close_old_connections

        should_cleanup = threading.current_thread() is not threading.main_thread()
        if should_cleanup:
            close_old_connections()
        try:
            return operation(*args, **kwargs)
        finally:
            if should_cleanup:
                close_old_connections()
