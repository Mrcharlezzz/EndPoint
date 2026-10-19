from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from processing.service_metadata import PROCESSOR_VERSION, PROMPT_VERSION

TemporalSourceKind = Literal["explicit_local_time", "published_at_offset_utc"]


class ClassificationResult(BaseModel):
    """Output model for the LLM classification call (step 3a).
    Passed as structured output schema to the LLM."""

    # Constrained to valid types — structured outputs enforce this at generation time.
    # If a single classify+extract call is implemented in the future, consider
    # instructor to enforce output formats across the union of extraction models.
    entity_type: Literal[
        "assignment", "announcement", "event", "material", "raw_message"
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class ExtractionPatch(BaseModel):
    """Base structured output model for sparse LLM extraction."""

    title: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class AssignmentPatch(ExtractionPatch):
    """Structured output model for LLM field extraction of assignments."""

    deadline: datetime | None = None
    deadline_source_kind: TemporalSourceKind | None = Field(
        default=None,
        description=(
            "How deadline was derived: explicit_local_time for message-local clock "
            "times, published_at_offset_utc for offsets computed from published_at."
        ),
    )
    submission_format: str | None = None


class AnnouncementPatch(ExtractionPatch):
    """Structured output model for LLM field extraction of announcements."""

    pass


class MaterialPatch(ExtractionPatch):
    """Structured output model for LLM field extraction of materials."""

    pass


class EventPatch(ExtractionPatch):
    """Structured output model for LLM field extraction of events."""

    start_time: datetime | None = None
    start_time_source_kind: TemporalSourceKind | None = Field(
        default=None,
        description=(
            "How start_time was derived: explicit_local_time for message-local "
            "clock times, published_at_offset_utc for offsets computed from "
            "published_at."
        ),
    )
    end_time: datetime | None = None
    end_time_source_kind: TemporalSourceKind | None = Field(
        default=None,
        description=(
            "How end_time was derived: explicit_local_time for message-local "
            "clock times, published_at_offset_utc for offsets computed from "
            "published_at."
        ),
    )
    location: str | None = None


class LLMUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None


class ClassificationLogEntry(BaseModel):
    """All information needed to write a classification_logs row.
    Field names match classification_logs ORM columns."""

    model_config = ConfigDict(from_attributes=True)

    ingested_item_id: UUID
    classified_item_id: UUID | None = None
    entity_type: str | None = None
    classification_method: str  # 'field_mapping' | 'rule_based' | 'llm'
    llm_model: str | None = None
    llm_raw_output: str | None = None
    confidence: float | None = None
    processing_time_ms: int | None = None
    processor_version: str | None = PROCESSOR_VERSION
    prompt_version: str | None = PROMPT_VERSION
    input_tokens: int | None = None
    output_tokens: int | None = None
