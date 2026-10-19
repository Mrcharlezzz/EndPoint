from __future__ import annotations

from processing.adapters.llm.prompts import PromptSet
from processing.adapters.llm.prompt_versions.entity_inference_v2 import (
    EXTRACTION_PREAMBLE_V2,
    FIELD_INSTRUCTIONS_V2,
)

CLASSIFICATION_SYSTEM_PROMPT = """
You classify academic messages into one of five types:
- assignment
- announcement
- event
- material
- raw_message

Choose the single best type and explain your reasoning briefly.

Use raw_message when the message should not become a standalone student-facing course item.
This includes greetings, acknowledgements, off-topic chat, empty messages,
student/team status reports or retrospectives, replies to prior chat context,
a reply to a specific person, student questions about homework or grading, answers to those questions, and
operational chatter that only makes sense inside the thread.
Use raw_message for non-durable content, for example near-term reminders.

Do not classify a message as announcement only because it mentions course work,
deadlines, recordings, grades, repositories, LMS, or a course resource. An
announcement must be a standalone notice intended for the course cohort.

Type guide:
- assignment: a standalone required task, homework, lab, quiz, contest, or
  submission for students, a publication of such content. They usually contain a deadline (not required).
- event: a standalone scheduled course occurrence students can attend, with
  date or time evidence. It should reference the future. They should not be short time reminders(lecture in 15 mins).
- material: a standalone durable course resource students should open, keep, or
  review, or a message sharing such content.
- announcement: standalone course information or instruction intended for the
  cohort, when it is not primarily an assignment, event, material, or raw chat.
- raw_message: anything conversational, contextual, ambiguous, or not stable as
  a publishable course item, not clear for any of the other categories. This includes context dependent
  operational status updates, troubleshooting, or lightweight polls or sign ups. Short time event reminders
  (e.g lecture in 15 mins).
""".strip()

PROMPT_SET_V3 = PromptSet(
    version="entity-inference-v3",
    classification_system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
    extraction_preamble=EXTRACTION_PREAMBLE_V2,
    field_instructions=FIELD_INSTRUCTIONS_V2,
)
