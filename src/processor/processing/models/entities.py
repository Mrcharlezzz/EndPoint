from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class ClassifiedBase(BaseModel):
    """Shared fields across all classified entity types.
    Maps to base_classified_items columns."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    title: str | None = None
    body: str | None = None
    author: str | None = None
    published_at: datetime | None = None


class Assignment(ClassifiedBase):
    """An assignment with deadline and submission details.
    Type-specific fields map to the assignments child table."""

    entity_type: Literal["assignment"] = "assignment"

    deadline: datetime | None = None
    submission_format: str | None = None


class Announcement(ClassifiedBase):
    """An informational announcement."""

    entity_type: Literal["announcement"] = "announcement"


class Material(ClassifiedBase):
    """A durable course resource such as slides, files, or reference links.
    No type-specific fields in v1 — child table has only the MTI pointer."""

    entity_type: Literal["material"] = "material"


class Event(ClassifiedBase):
    """A scheduled occurrence (lecture, office hours, exam).
    Type-specific fields map to the events child table."""

    entity_type: Literal["event"] = "event"

    start_time: datetime | None = None
    end_time: datetime | None = None
    location: str | None = None


class RawMessage(ClassifiedBase):
    """An unclassified message awaiting LLM classification.
    Placeholder entity type written by adapters for unstructured content."""

    entity_type: Literal["raw_message"] = "raw_message"


# The discriminated union — the central type for the pipeline
ClassifiedEntity = Annotated[
    Union[Assignment, Announcement, Material, Event, RawMessage],
    Field(discriminator="entity_type"),
]
