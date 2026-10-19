from __future__ import annotations

from processing.adapters.llm.field_instructions import (
    FIELD_INSTRUCTIONS,
    SPARSE_EXTRACTION_PREAMBLE,
)
from processing.adapters.llm.prompts import PromptSet

CLASSIFICATION_SYSTEM_PROMPT = """
You classify academic messages into one of five types:
- assignment
- announcement
- event
- material
- raw_message (use when the content doesn't match any other type: greetings, chit-chat, off-topic, or empty messages)

Choose the single best type and explain your reasoning briefly.
""".strip()

PROMPT_SET_V1 = PromptSet(
    version="entity-inference-v1",
    classification_system_prompt=CLASSIFICATION_SYSTEM_PROMPT,
    extraction_preamble=SPARSE_EXTRACTION_PREAMBLE,
    field_instructions=FIELD_INSTRUCTIONS,
)
