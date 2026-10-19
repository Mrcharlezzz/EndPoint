from __future__ import annotations

from dataclasses import replace

from processing.adapters.llm.field_instructions import (
    FIELD_INSTRUCTIONS,
    SPARSE_EXTRACTION_PREAMBLE,
    FieldInstruction,
)
from processing.adapters.llm.prompts import PromptSet

CLASSIFICATION_SYSTEM_PROMPT = """
You classify one academic raw message into exactly one type.

General rules:
- Use only the supplied title, body, platform context, and payload context.
- Do not invent missing facts.
- Analyze the publishable entity signals in the message. If the signals are ambiguous, weak, or not clear enough, default to raw_message.
- If uncertain between raw_message and a publishable type, default to raw_message.
- If uncertain between two publishable types, choose the type matching the primary student action.

General publishable entity signals:
- The message provides an instruction, request, or directive. Signal example: "Please send your Sprint II reports in the chat by tomorrow." Not this signal: team reports, status updates, replies, acknowledgements.
- The message addresses a group of students, the course channel, or the cohort. Signal example: "Everyone should register on LMS before Friday." Not this signal: a directed question, one-person answer, thread reply, or "thanks/yes/exactly" chatter.
- The message gives new or materially changed course information. Signal example: "The lab deadline moved to Monday 23:59." Not this signal: restating, confirming, or commenting on information already established elsewhere.

Type criteria / Type-specific signals:
- assignment: introduces, reminds about, or materially updates student work such as homework, lab, contest, task, submission,  deadline.
- event: schedules or updates an attendable course occurrence such as class, lecture, quiz session, lab, office hour, presentation, exam, defence, consultation, or meeting, with time/date evidence in the body.
- material: shares or points to a durable course resource such as slides, recording, reading, worksheet, repository, file, or reference link.it should not be short lived.
- announcement: gives standalone course information or instruction that is not primarily an assignment, event, material or noise. it should be relevnt and carry signal.
- raw_message: greeting, acknowledgement, reaction, short conversational note, off-topic chat, empty content, or ambiguous message with no stable publishable course entity.

Tie-breakers:
- assignment vs announcement: choose assignment when the message creates, reminds about, or substantially updates required student work.
- event vs announcement: choose event when attending at a time/place is the main point.
- material vs announcement: choose material when the durable resource is the main point.
- assignment vs material: choose assignment when the resource supports submitted work; choose material when the resource itself is what students should keep or review.
- raw_message vs anything: choose raw_message when the message does not produce student-facing item, or the signal is not strong enough.

In reasoning, write one short sentence naming the decision criteria.
""".strip()

TEMPORAL_EXTRACTION_RULES = """
Temporal date/time rules:
- For each extracted deadline, start_time, or end_time, also return its matching *_source_kind field.
- Use `explicit_local_time` when the message states a date or clock time, including today, tomorrow, next Monday, or March 19 at 09:45. Resolve partial dates with published_at when needed, but return a course-local wall-clock datetime without timezone.
- Use `published_at_offset_utc` only when the message gives a relative date that you determine from published_at, such as in 5 minutes, in 2 hours. Compute from UTC published_at and return the resulting UTC datetime.
""".strip()

EXTRACTION_PREAMBLE_V2 = f"{SPARSE_EXTRACTION_PREAMBLE}\n\n{TEMPORAL_EXTRACTION_RULES}"

FIELD_INSTRUCTIONS_V2: dict[str, FieldInstruction] = {
    **FIELD_INSTRUCTIONS,
    "deadline": replace(
        FIELD_INSTRUCTIONS["deadline"],
        return_spec="datetime or null",
        normalize="Follow the Temporal date/time rules above. If only a deadline date is present, use 23:59 local academic time for that date.",
    ),
    "start_time": replace(
        FIELD_INSTRUCTIONS["start_time"],
        return_spec="datetime or null",
        normalize="Follow the Temporal date/time rules above. If only an event date is present, use the default event start time policy.",
    ),
    "end_time": replace(
        FIELD_INSTRUCTIONS["end_time"],
        return_spec="datetime or null",
        normalize="Follow the Temporal date/time rules above. If only an event date is present, use the default event end time policy.",
    ),
    "deadline_source_kind": FieldInstruction(
        return_spec="explicit_local_time, published_at_offset_utc, or null",
        extract_from="the same message-body evidence used for deadline",
        normalize="Follow the Temporal date/time rules above.",
        empty_rule="deadline is null",
        never="invent a source kind when deadline is null",
    ),
    "start_time_source_kind": FieldInstruction(
        return_spec="explicit_local_time, published_at_offset_utc, or null",
        extract_from="the same message-body evidence used for start_time",
        normalize="Follow the Temporal date/time rules above.",
        empty_rule="start_time is null",
        never="invent a source kind when start_time is null",
    ),
    "end_time_source_kind": FieldInstruction(
        return_spec="explicit_local_time, published_at_offset_utc, or null",
        extract_from="the same message-body evidence used for end_time",
        normalize="Follow the Temporal date/time rules above.",
        empty_rule="end_time is null",
        never="invent a source kind when end_time is null",
    ),
}

PROMPT_SET_V2 = PromptSet(
    version="entity-inference-v2",
    classification_system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
    extraction_preamble=EXTRACTION_PREAMBLE_V2,
    field_instructions=FIELD_INSTRUCTIONS_V2,
)
