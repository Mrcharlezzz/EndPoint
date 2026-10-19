from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .classification import ClassificationLogEntry
from .dedup import DedupDecision, DedupItemStatus
from .entities import ClassifiedEntity
from .extracted import ExtractedAttachment, ExtractedLink


class IngestedItemContext(BaseModel):
    """Read-only context from the ingested_item row.
    The processor never writes to ingested_items.
    Carried along for course resolution and LLM prompting.

    Field names match ingested_items ORM columns exactly."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    external_id: str
    platform: str  # 'moodle', 'teams', 'telegram'
    course_external_id: str | None = None  # platform's course/group ID
    schema_version: str
    adapter_version: str
    ingestion_mode: str
    processing_status: str
    source_url: str | None = None
    payload: dict[str, Any]


class ProcessingEnvelope(BaseModel):
    """The unit of work flowing through the pipeline.

    Wraps a typed domain entity with processing context.
    The entity is a discriminated union — its concrete type
    determines which fields are available and which DB child
    table it maps to.

    Processing steps produce new envelope copies via model_copy —
    data flow is immutable. Use model_copy(update={...}) to advance
    state (e.g. set resolved_course_id, replace entity after LLM
    classification).
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    # Identity
    id: UUID  # ingested_items.id until the classified row is created
    ingested_item: IngestedItemContext  # the source ingested row

    # The domain entity (discriminated union)
    entity: ClassifiedEntity

    # Processing state (populated by pipeline steps)
    resolved_course_id: UUID | None = None

    # Supporting data (populated during normalization/extraction)
    extracted_links: list[ExtractedLink] = Field(default_factory=list)
    extracted_attachments: list[ExtractedAttachment] = Field(default_factory=list)

    # Classification audit (populated by LLM or deterministic path)
    classification_log: ClassificationLogEntry | None = None

    # Dedup outcome (populated after normalization, before persistence)
    dedup_decision: DedupDecision | None = None
    dedup_status: DedupItemStatus | None = None

    def with_deterministic_classification_log(self) -> ProcessingEnvelope:
        if self.classification_log is not None:
            return self
        return self.model_copy(
            update={
                "classification_log": ClassificationLogEntry(
                    ingested_item_id=self.ingested_item.id,
                    entity_type=self.entity.entity_type,
                    classification_method="deterministic",
                    confidence=1.0,
                )
            }
        )
