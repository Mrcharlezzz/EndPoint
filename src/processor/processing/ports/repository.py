from __future__ import annotations

from typing import Protocol
from uuid import UUID

from processing.models.classification import ClassificationLogEntry
from processing.models.course import Course
from processing.models.dedup import CanonicalCandidate
from processing.models.envelope import ProcessingEnvelope
from processing.models.extracted import (
    ExtractedAttachment,
    ExtractedLink,
)


class EntityRepositoryPort(Protocol):
    def get_pending_batch(self, limit: int) -> list[ProcessingEnvelope]: ...

    def get_existing_canonicals(
        self, course_id: UUID, entity_type: str
    ) -> list[CanonicalCandidate]: ...

    def persist_batch(self, envelopes: list[ProcessingEnvelope]) -> None: ...

    def persist_unclassified_batch(
        self, envelopes: list[ProcessingEnvelope]
    ) -> None: ...

    def save_entity(self, envelope: ProcessingEnvelope) -> UUID: ...

    def mark_processed(self, ingested_item_id: UUID) -> None: ...

    def mark_failed(self, ingested_item_id: UUID, error: str) -> None: ...

    def save_links(
        self, classified_item_id: UUID, links: list[ExtractedLink]
    ) -> None: ...

    def save_attachments(
        self,
        classified_item_id: UUID,
        attachments: list[ExtractedAttachment],
    ) -> None: ...

    def save_classification_log(self, log: ClassificationLogEntry) -> None: ...

    def get_all_courses(self) -> list[Course]: ...

    def get_course_by_platform_mapping(
        self, platform: str, external_course_id: str
    ) -> Course | None: ...

    def create_course(self, name: str, code: str | None = None) -> Course: ...

    def update_course_code(self, course_id: UUID, code: str) -> None: ...

    def upsert_course_platform_mapping(
        self,
        course_id: UUID,
        platform: str,
        external_course_id: str,
        external_course_name: str | None,
    ) -> None: ...
