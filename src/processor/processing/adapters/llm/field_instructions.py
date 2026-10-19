from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from processing.models.classification import ExtractionPatch
from processing.models.registry import ENTITY_REGISTRY


@dataclass(frozen=True)
class FieldInstruction:
    return_spec: str
    extract_from: str
    normalize: str
    empty_rule: str
    never: str

    def render(self, field_name: str) -> str:
        return (
            f"{field_name}\n"
            f"- Return: {self.return_spec}\n"
            f"- Extract from: {self.extract_from}\n"
            f"- Normalize: {self.normalize}\n"
            f"- Return null/[] when: {self.empty_rule}\n"
            f"- Never: {self.never}"
        )


FIELD_INSTRUCTIONS: dict[str, FieldInstruction] = {
    "title": FieldInstruction(
        return_spec="short plain-text heading, or null",
        extract_from="only words explicitly supported by the message body",
        normalize="2-10 words when possible; no emoji; no trailing punctuation; trim edges; replace repeated internal whitespace with single spaces",
        empty_rule=(
            "the message has no stable subject phrase and is only a short comment, acknowledgement, reaction request, poll, or other ephemeral note"
        ),
        never=(
            "invent context, summarize the whole message into a sentence, or "
            "use generic labels when the message contains a more specific subject phrase"
        ),
    ),
    "deadline": FieldInstruction(
        return_spec="local academic datetime without timezone, or null",
        extract_from="explicit deadline wording in the message body",
        normalize="Return local date/time only; do not set a timezone. Use payload context (e.g. published_at) to resolve partial or relative date references from the body (e.g. infer the year for '13.03', resolve 'next Friday'). If only a date is present, use the default deadline time policy.",
        empty_rule="there are no dates, or dates are present but they are not deadlines for this message",
        never="return published_at itself as the deadline value, or ignore the deadline most related to the message; if none is clearly primary among plausible deadlines, choose the earliest one",
    ),
    "submission_format": FieldInstruction(
        return_spec="short plain-text submission rule, or null",
        extract_from="explicit submission instructions in the message body",
        normalize="short factual phrase; trim edges; replace repeated internal whitespace with single spaces",
        empty_rule="the submission format is not explicitly described",
        never="infer a format from the platform alone",
    ),
    "start_time": FieldInstruction(
        return_spec="local academic datetime without timezone, or null",
        extract_from="explicit event timing in the message body",
        normalize="Return local date/time only; do not set a timezone. Use payload context (e.g. published_at) to resolve partial or relative date references from the body (e.g. infer the year for '13.03', resolve 'today', 'tomorrow', 'next Friday'). If only a date is present, use the default event start time policy.",
        empty_rule="the start time is not explicitly grounded in the message body",
        never="return published_at itself as the start time value, or infer from an end time",
    ),
    "end_time": FieldInstruction(
        return_spec="local academic datetime without timezone, or null",
        extract_from="explicit event end time in the message body",
        normalize="Return local date/time only; do not set a timezone. Use payload context (e.g. published_at) to resolve partial or relative date references from the body. If only a date is present, use the default event end time policy.",
        empty_rule="the end time is not explicitly grounded in the message body",
        never="invent a duration or infer from the start time",
    ),
    "location": FieldInstruction(
        return_spec="plain-text location, or null",
        extract_from="explicit location wording in the message body, including room names, building shorthand, and explicit platform names such as Google Meet, Zoom, and East Hall 5",
        normalize="short factual phrase; trim edges; replace repeated internal whitespace with single spaces",
        empty_rule="the location is not explicitly stated in the message body",
        never="infer location from course metadata or unrelated payload fields",
    ),
}


_METADATA_FIELDS: frozenset[str] = frozenset({"confidence", "reasoning"})
_AUXILIARY_FIELDS: frozenset[str] = frozenset(
    {"deadline_source_kind", "start_time_source_kind", "end_time_source_kind"}
)


def extraction_fields_for_patch_model(
    patch_cls: type[ExtractionPatch],
) -> tuple[str, ...]:
    return tuple(
        name
        for name in patch_cls.model_fields
        if name not in _METADATA_FIELDS and name not in _AUXILIARY_FIELDS
    )


ENTITY_EXTRACTION_FIELDS: dict[str, tuple[str, ...]] = {
    entity_type: extraction_fields_for_patch_model(patch_cls)
    for entity_type, (_, patch_cls) in ENTITY_REGISTRY.items()
}


SUPPORTED_EXTRACTION_TYPES = tuple(ENTITY_REGISTRY)

SPARSE_EXTRACTION_PREAMBLE = (
    "Extract semantic fields from the message body only.\n"
    "Use published_at only to resolve relative date phrases.\n"
    "For every field below, use the message body as the only extraction evidence.\n"
    "The only exception is published_at, which may be used solely to resolve relative date phrases.\n"
    "Only extract deadline, start_time, or end_time values when they are tied to new actionable information: assigned, due, scheduled, changed, or becoming available.\n"
    "Do not extract incidental conversational references like materials we used today.\n"
    "Create a new extraction object containing only the fields covered by these instructions.\n"
    "For fields that are not clearly supported, return null for scalars and [] for list fields.\n"
    "Do not rewrite, restate, or normalize fields outside the instructed set.\n"
    "Do not invent values or infer unsupported details from structured payload fields."
)


def build_extraction_system_prompt(
    entity_type: str,
    *,
    preamble: str = SPARSE_EXTRACTION_PREAMBLE,
    field_instructions: Mapping[str, FieldInstruction] = FIELD_INSTRUCTIONS,
    fields: tuple[str, ...] | None = None,
) -> str:
    selected_fields = fields
    if selected_fields is None:
        selected_fields = ENTITY_EXTRACTION_FIELDS.get(entity_type)
    if selected_fields is None:
        raise ValueError(f"Unsupported extraction type: {entity_type}")

    field_list = "\n".join(f"- {field_name}" for field_name in selected_fields)
    field_blocks = "\n\n".join(
        field_instructions[field_name].render(field_name)
        for field_name in selected_fields
    )
    return (
        f"You extract {entity_type} fields from academic messages.\n\n"
        f"{preamble}\n\n"
        f"Fields for this entity type:\n{field_list}\n\n"
        f"{field_blocks}"
    )
