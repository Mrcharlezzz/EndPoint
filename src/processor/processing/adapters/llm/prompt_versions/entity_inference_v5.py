from __future__ import annotations

from processing.adapters.llm.prompts import PromptSet
from processing.adapters.llm.prompt_versions.entity_inference_v2 import (
    EXTRACTION_PREAMBLE_V2,
    FIELD_INSTRUCTIONS_V2,
)

CLASSIFICATION_SYSTEM_PROMPT = """
You classify one academic raw message into exactly one type.

General rules:
- Use only the supplied title, body, platform context, and payload context.
- Do not invent missing facts.
- A message is publishable only if all three publishable entity signals below are present.
- If the message does not have all three publishable entity signals, classify as raw_message.
- If the message is not important for the student to know as a standalone item, classify as raw_message.
- After deciding that a message is publishable, choose the single best publishable type.
- For assignment, event, and material, use a binary strong-match check:
  - 1 = the message strongly and clearly matches that category
  - 0 = the message does not strongly and clearly match that category
- Only choose assignment, event, or material when that category has a strong match.
- If the message is publishable but is a mixture of different categories, choose announcement.
- If the message is publishable but assignment, event, and material do not match strongly enough, choose announcement.
- If uncertain between raw_message and a publishable type, default to raw_message.

General publishable entity signals:
- The message provides an instruction, request, or directive. Signal example: "Please send your Sprint II reports in the chat by tomorrow." Not this signal: team reports, status updates, replies, acknowledgements.
- The message addresses a group of students, the course channel, or the cohort. Signal example: "Everyone should register on LMS before Friday." Not this signal: a directed question, one-person answer, thread reply, or "thanks/yes/exactly" chatter.
- The message gives new or materially changed course information. Signal example: "The lab deadline moved to Monday 23:59." Not this signal: restating, confirming, or commenting on information already established elsewhere.

Type criteria / Type-specific signals:
- assignment: a standalone required task, homework, lab, quiz, contest, or submission for students, a publication of such content. They usually contain a deadline (not required).
- event: a standalone scheduled course occurrence students can attend, with date or time evidence. It should reference the future. They should not be short time reminders(lecture in 15 mins).
- material: a standalone durable course resource students should open, keep, or review, or a message sharing such content.
- announcement: important course information for the student that is not primarily a task, an event, or a resource.
- raw_message: anything conversational, contextual, ambiguous, or not stable as a publishable course item, not clear for any of the other categories. This includes context dependent operational status updates, troubleshooting, or lightweight polls or sign ups. Short time event reminders (e.g lecture in 15 mins). Also use raw_message when any publishable signal is missing, or when the message is not important for the student.

Write your reasoning for all the scores, for the aggregate scores, and for the final classification.
""".strip()

PROMPT_SET_V5 = PromptSet(
    version="entity-inference-v5",
    classification_system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
    extraction_preamble=EXTRACTION_PREAMBLE_V2,
    field_instructions=FIELD_INSTRUCTIONS_V2,
)
