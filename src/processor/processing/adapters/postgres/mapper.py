from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from processing.models.classification import ClassificationLogEntry
from processing.models.course import Course
from processing.models.entities import (
    Announcement,
    Assignment,
    ClassifiedEntity,
    Event,
    Material,
    RawMessage,
)
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.extracted import (
    ExtractedAttachment,
    ExtractedLink,
)

BASE_FIELDS: set[str] = {"title", "body", "author", "published_at"}
ASSIGNMENT_FIELDS: set[str] = {
    "deadline",
    "submission_format",
}
EVENT_FIELDS: set[str] = {"start_time", "end_time", "location"}

_ModelT = TypeVar("_ModelT", bound=BaseModel)

if TYPE_CHECKING:
    from core.models.courses import Course as CourseORM
    from core.models.ingested import IngestedItem as IngestedItemORM


def orm_to_envelope(orm_item: "IngestedItemORM") -> ProcessingEnvelope:
    """Convert an IngestedItem ORM row into the processor's unit of work."""
    ctx = IngestedItemContext.model_validate(orm_item)
    payload = ctx.payload

    return ProcessingEnvelope(
        id=ctx.id,
        ingested_item=ctx,
        entity=payload_to_entity(payload),
        extracted_links=_payload_list_to_models(
            payload.get("links", []), ExtractedLink
        ),
        extracted_attachments=_payload_list_to_models(
            payload.get("attachments", []), ExtractedAttachment
        ),
    )


def payload_to_entity(payload: dict) -> ClassifiedEntity:
    content_type = payload.get("content_type")
    base_fields = {field: payload.get(field) for field in BASE_FIELDS}

    if content_type == "assignment":
        return Assignment.model_validate(
            {
                **base_fields,
                "entity_type": "assignment",
                **{field: payload.get(field) for field in ASSIGNMENT_FIELDS},
            }
        )
    if content_type == "event":
        return Event.model_validate(
            {
                **base_fields,
                "entity_type": "event",
                **{field: payload.get(field) for field in EVENT_FIELDS},
            }
        )
    if content_type == "announcement":
        return Announcement.model_validate(
            {
                **base_fields,
                "entity_type": "announcement",
            }
        )
    if content_type == "material":
        return Material.model_validate(
            {
                **base_fields,
                "entity_type": "material",
            }
        )
    if content_type == "raw_message":
        return RawMessage.model_validate(
            {
                **base_fields,
                "entity_type": "raw_message",
            }
        )
    raise ValueError(f"Unsupported content_type in ingested payload: {content_type!r}")


def _payload_list_to_models(
    raw_items: list[dict], model: type[_ModelT]
) -> list[_ModelT]:
    return [model.model_validate(item) for item in raw_items]


def orm_to_course(orm_course: "CourseORM") -> Course:
    return Course.model_validate(orm_course)


def entity_to_base_dict(entity: ClassifiedEntity) -> dict:
    return entity.model_dump(include=BASE_FIELDS)


def entity_to_child_dict(entity: ClassifiedEntity) -> dict:
    if isinstance(entity, Assignment):
        return entity.model_dump(include=ASSIGNMENT_FIELDS)
    if isinstance(entity, Event):
        return entity.model_dump(include=EVENT_FIELDS)
    if isinstance(entity, Announcement):
        return {}
    return {}


def links_to_orm_dicts(classified_item_id, links: list[ExtractedLink]) -> list[dict]:
    return [
        {"classified_item_id": classified_item_id, **link.model_dump()}
        for link in links
    ]


def attachments_to_orm_dicts(
    classified_item_id, attachments: list[ExtractedAttachment]
) -> list[dict]:
    return [
        {"classified_item_id": classified_item_id, **attachment.model_dump()}
        for attachment in attachments
    ]


def classification_log_to_orm_dict(log: ClassificationLogEntry) -> dict:
    return log.model_dump()
