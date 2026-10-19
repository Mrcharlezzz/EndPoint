from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from processing.models.entities import Announcement, Assignment, Event, Material


DedupItemStatus = Literal["primary", "linked", "promoted", "skipped", "failed"]
DeduplicatedEntity = Assignment | Announcement | Event | Material


class CanonicalCandidate(BaseModel):
    """Typed representative of an existing canonical cluster."""

    model_config = ConfigDict(frozen=True)

    canonical_id: UUID
    entity: DeduplicatedEntity
    platform: str
    persisted_primary_item_id: UUID | None = None


class AssignmentComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate: CanonicalCandidate
    title_similarity: float | None = None
    deadline_similarity: float | None = None


class AnnouncementComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate: CanonicalCandidate
    title_similarity: float | None = None
    body_similarity: float | None = None
    published_at_similarity: float | None = None


class EventComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate: CanonicalCandidate
    title_similarity: float | None = None
    start_time_similarity: float | None = None


class MaterialComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate: CanonicalCandidate
    title_similarity: float | None = None
    body_similarity: float | None = None


class CandidateMatch(BaseModel):
    """A canonical candidate accepted by a match strategy."""

    model_config = ConfigDict(frozen=True)

    canonical_id: UUID
    candidate: CanonicalCandidate
    score: float = Field(ge=0.0, le=1.0)
    matched_by: str


class ResolutionAction(BaseModel):
    """Generic action returned by a resolution strategy."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["create_new", "attach", "promote", "skip", "fail"]
    canonical_id: UUID | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_canonical_id(self) -> ResolutionAction:
        if self.kind in {"attach", "promote"} and self.canonical_id is None:
            raise ValueError(f"{self.kind} actions require a canonical_id")
        return self


class CanonicalState(BaseModel):
    """Mutable batch-time state for one canonical cluster."""

    canonical_id: UUID
    representative: DeduplicatedEntity
    representative_platform: str
    persisted_primary_item_id: UUID | None = None
    pending_primary_batch_index: int | None = None
    member_batch_indexes: list[int] = Field(default_factory=list)


class DedupDecision(BaseModel):
    """Dedup output consumed by later pipeline and persistence phases."""

    model_config = ConfigDict(frozen=True)

    decision: Literal["new_canonical", "link_existing"]
    canonical_entity_id: UUID | None = None
    matched_by: str
    score: float
    match_strategy: str | None = None
    resolution_strategy: str | None = None
    error_message: str | None = None
