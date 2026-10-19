from __future__ import annotations

from processing.adapters.llm.prompts import PromptSet
from processing.adapters.llm.prompt_versions.entity_inference_v1 import PROMPT_SET_V1
from processing.adapters.llm.prompt_versions.entity_inference_v2 import PROMPT_SET_V2
from processing.adapters.llm.prompt_versions.entity_inference_v3 import PROMPT_SET_V3
from processing.adapters.llm.prompt_versions.entity_inference_v4 import PROMPT_SET_V4
from processing.adapters.llm.prompt_versions.entity_inference_v5 import PROMPT_SET_V5

PROMPT_SETS: dict[str, PromptSet] = {
    PROMPT_SET_V1.version: PROMPT_SET_V1,
    PROMPT_SET_V2.version: PROMPT_SET_V2,
    PROMPT_SET_V3.version: PROMPT_SET_V3,
    PROMPT_SET_V4.version: PROMPT_SET_V4,
    PROMPT_SET_V5.version: PROMPT_SET_V5,
}
