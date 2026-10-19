from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from processing.exceptions import ClassificationError
from processing.models.classification import ExtractionPatch, TemporalSourceKind
from processing.models.entities import ClassifiedBase, ClassifiedEntity
from processing.models.envelope import ProcessingEnvelope
from processing.models.extracted import (
    ExtractedAttachment,
    ExtractedLink,
)
from processing.models.registry import COMMON_FIELDS, ENTITY_REGISTRY

_CLASSIFIED_ENTITY_ADAPTER = TypeAdapter(ClassifiedEntity)
_DATETIME_FIELDS = frozenset({"deadline", "start_time", "end_time"})


@dataclass(frozen=True)
class ResolvedExtraction:
    entity: ClassifiedEntity
    extracted_links: list[ExtractedLink]
    extracted_attachments: list[ExtractedAttachment]


def choose_scalar(current: Any, extracted: Any) -> Any:
    """Prefer a deterministic payload value over an LLM-extracted one."""
    return current if current is not None else extracted


def _apply_university_timezone(
    value: Any,
    tz: ZoneInfo,
    *,
    trust_existing_timezone: bool,
    source_kind: TemporalSourceKind | None = None,
) -> Any:
    if not isinstance(value, datetime):
        return value
    if trust_existing_timezone:
        if value.tzinfo is not None:
            return value
        return value.replace(tzinfo=tz)
    if source_kind == "published_at_offset_utc":
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=None).replace(tzinfo=tz)


def parse_payload_evidence(
    payload: dict[str, Any],
    entity_cls: type[ClassifiedBase],
) -> dict[str, Any]:
    """Parse non-common payload fields once through the target entity model."""
    typed_field_names = set(entity_cls.model_fields) - COMMON_FIELDS
    raw = {k: v for k, v in payload.items() if k in typed_field_names and v is not None}
    if not raw:
        return {}

    adapter = TypeAdapter(entity_cls)
    coerced = adapter.validate_python(raw)
    return {
        name: getattr(coerced, name)
        for name in typed_field_names
        if getattr(coerced, name, None) is not None
    }


class ExtractionResolutionService:
    def __init__(self, university_timezone: str = "Europe/Amsterdam") -> None:
        self._timezone = ZoneInfo(university_timezone)

    def resolve(
        self,
        envelope: ProcessingEnvelope,
        classified_type: str,
        extraction: ExtractionPatch,
    ) -> ResolvedExtraction:
        entry = ENTITY_REGISTRY.get(classified_type)
        if entry is None:
            raise ClassificationError(f"Unsupported classified type: {classified_type}")

        entity_cls, _ = entry
        payload_evidence = parse_payload_evidence(
            envelope.ingested_item.payload, entity_cls
        )

        fields: dict[str, Any] = {
            "title": extraction.title or envelope.entity.title,
            "body": envelope.entity.body,
            "author": envelope.entity.author,
            "published_at": envelope.ingested_item.payload.get("published_at"),
        }
        for name in entity_cls.model_fields:
            if name in COMMON_FIELDS:
                continue
            payload_value = payload_evidence.get(name)
            resolved_value = choose_scalar(
                payload_value,
                getattr(extraction, name, None),
            )
            if name in _DATETIME_FIELDS:
                resolved_value = _apply_university_timezone(
                    resolved_value,
                    self._timezone,
                    trust_existing_timezone=payload_value is not None,
                    source_kind=getattr(extraction, f"{name}_source_kind", None),
                )
            fields[name] = resolved_value

        entity = _CLASSIFIED_ENTITY_ADAPTER.validate_python(
            {"entity_type": classified_type, **fields}
        )

        return ResolvedExtraction(
            entity=entity,
            extracted_links=list(envelope.extracted_links),
            extracted_attachments=list(envelope.extracted_attachments),
        )
