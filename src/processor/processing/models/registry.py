from __future__ import annotations

from processing.models.classification import (
    AnnouncementPatch,
    AssignmentPatch,
    EventPatch,
    ExtractionPatch,
    MaterialPatch,
)
from processing.models.entities import (
    Announcement,
    Assignment,
    ClassifiedBase,
    Event,
    Material,
)

COMMON_FIELDS: frozenset[str] = frozenset(
    {"title", "body", "author", "published_at", "entity_type"}
)

ENTITY_REGISTRY: dict[str, tuple[type[ClassifiedBase], type[ExtractionPatch]]] = {
    "assignment": (Assignment, AssignmentPatch),
    "announcement": (Announcement, AnnouncementPatch),
    "event": (Event, EventPatch),
    "material": (Material, MaterialPatch),
}
