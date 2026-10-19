# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from openai import AsyncOpenAI

from processing.adapters.llm.prompts import (
    build_user_prompt,
    get_prompt_set,
)
from processing.adapters.llm.tracing import (
    build_langsmith_extra,
    maybe_wrap_openai_client,
)
from processing.config import ProcessingConfig
from processing.exceptions import ClassificationError, ExtractionError
from processing.models.classification import (
    ClassificationResult,
    ExtractionPatch,
    LLMUsage,
)
from processing.models.envelope import ProcessingEnvelope
from processing.models.registry import ENTITY_REGISTRY
from processing.observability.llm_usage import LLMUsageCollector
from processing.ports.classifier import EntityInferencePort


def _usage_from_completion(completion: object) -> LLMUsage | None:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return None
    return LLMUsage(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
    )


class LLMClassifier(EntityInferencePort):
    """OpenAI-compatible classifier using structured outputs."""

    def __init__(
        self,
        config: ProcessingConfig,
        usage_collector: LLMUsageCollector | None = None,
    ):
        client = AsyncOpenAI(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
            max_retries=config.llm_max_retries,
        )
        self.client = maybe_wrap_openai_client(client)
        self.model = config.llm_model
        self.timeout = config.llm_timeout_seconds
        self.prompt_set = get_prompt_set(config.llm_prompt_version)
        self.prompt_version = self.prompt_set.version
        self.prompt_fingerprint = self.prompt_set.fingerprint
        self.usage_collector = usage_collector or LLMUsageCollector()

    async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult:
        parse = self.client.beta.chat.completions.parse  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        try:
            completion = await parse(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": self.prompt_set.classification_system_prompt,
                    },
                    {"role": "user", "content": build_user_prompt(envelope)},
                ],
                response_format=ClassificationResult,
                timeout=self.timeout,
                langsmith_extra=build_langsmith_extra(
                    envelope,
                    operation="classify",
                    prompt_version=self.prompt_version,
                    prompt_fingerprint=self.prompt_fingerprint,
                ),
            )
        except Exception as exc:
            raise ClassificationError(f"Classification call failed: {exc}") from exc

        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise ClassificationError("Classification call returned no parsed result")
        self._record_usage(completion)
        return parsed

    async def extract_fields(
        self, envelope: ProcessingEnvelope, classified_type: str
    ) -> ExtractionPatch:
        parse = self.client.beta.chat.completions.parse  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        entry = ENTITY_REGISTRY.get(classified_type)
        if entry is None:
            raise ExtractionError(f"Unsupported extraction type: {classified_type}")
        _, response_model = entry
        try:
            system_prompt = self.prompt_set.build_extraction_system_prompt(
                classified_type
            )
        except ValueError as exc:
            raise ExtractionError(str(exc)) from exc

        try:
            completion = await parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": build_user_prompt(envelope)},
                ],
                response_format=response_model,
                timeout=self.timeout,
                langsmith_extra=build_langsmith_extra(
                    envelope,
                    operation="extract",
                    classified_type=classified_type,
                    prompt_version=self.prompt_version,
                    prompt_fingerprint=self.prompt_fingerprint,
                ),
            )
        except Exception as exc:
            raise ExtractionError(f"Field extraction call failed: {exc}") from exc

        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise ExtractionError("Field extraction returned no parsed result")
        self._record_usage(completion)
        return parsed

    def _record_usage(self, completion: object) -> None:
        usage = _usage_from_completion(completion)
        if usage is None:
            return
        self.usage_collector.record_call(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
