from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
import hashlib
import json

from processing.adapters.llm.field_instructions import (
    ENTITY_EXTRACTION_FIELDS,
    FieldInstruction,
    build_extraction_system_prompt as _build_extraction_system_prompt,
)
from processing.models.envelope import ProcessingEnvelope
from processing.models.registry import ENTITY_REGISTRY

DEFAULT_PROMPT_VERSION = "entity-inference-v1"


@dataclass(frozen=True)
class PromptSet:
    version: str
    classification_system_prompt: str
    extraction_preamble: str
    field_instructions: Mapping[str, FieldInstruction]
    extraction_fields: Mapping[str, tuple[str, ...]] | None = None
    user_prompt_version: str = "user-prompt-v1"

    @cached_property
    def fingerprint(self) -> str:
        payload = {
            "version": self.version,
            "classification_system_prompt": self.classification_system_prompt,
            "extraction_preamble": self.extraction_preamble,
            "field_instructions": {
                name: instruction.render(name)
                for name, instruction in sorted(self.field_instructions.items())
            },
            "extraction_fields": {
                name: list(fields)
                for name, fields in sorted((self.extraction_fields or {}).items())
            },
            "user_prompt_version": self.user_prompt_version,
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:12]

    def build_extraction_system_prompt(self, entity_type: str) -> str:
        return _build_extraction_system_prompt(
            entity_type,
            preamble=self.extraction_preamble,
            field_instructions=self.field_instructions,
            fields=self.extraction_fields_for(entity_type),
        )

    def extraction_fields_for(self, entity_type: str) -> tuple[str, ...]:
        if self.extraction_fields is not None and entity_type in self.extraction_fields:
            return self.extraction_fields[entity_type]
        fields = ENTITY_EXTRACTION_FIELDS.get(entity_type)
        if fields is None:
            raise ValueError(f"Unsupported extraction type: {entity_type}")
        _, patch_cls = ENTITY_REGISTRY[entity_type]
        default_fields = set(fields)
        return tuple(
            name
            for name in patch_cls.model_fields
            if name in default_fields or name in self.field_instructions
        )


def get_prompt_set(version: str) -> PromptSet:
    from processing.adapters.llm.prompt_versions import PROMPT_SETS

    try:
        return PROMPT_SETS[version]
    except KeyError as exc:
        supported = ", ".join(sorted(PROMPT_SETS))
        raise ValueError(
            f"Unknown prompt version {version!r}. Supported versions: {supported}"
        ) from exc


def build_extraction_system_prompt(entity_type: str) -> str:
    return get_prompt_set(DEFAULT_PROMPT_VERSION).build_extraction_system_prompt(
        entity_type
    )


def build_user_prompt(envelope: ProcessingEnvelope) -> str:
    payload = envelope.ingested_item.payload
    # Keep the full payload available so extraction can fall back to original
    # platform context even when title/body are sparse or partially normalized.
    payload_json = json.dumps(payload, indent=2, sort_keys=True, default=str)
    body = envelope.entity.body or ""

    return (
        f"Platform: {envelope.ingested_item.platform}\n"
        f"Adapter Version: {envelope.ingested_item.adapter_version}\n"
        f"Course External ID: {envelope.ingested_item.course_external_id}\n"
        f"Course Name: {payload.get('course_name')}\n"
        f"Published At: {payload.get('published_at')}\n"
        f"Source URL: {envelope.ingested_item.source_url}\n"
        f"Author: {envelope.entity.author}\n"
        f"Title: {envelope.entity.title}\n"
        f"Body:\n{body}\n\n"
        f"Payload JSON:\n{payload_json}"
    )
