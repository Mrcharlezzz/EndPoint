from __future__ import annotations

from uuid import uuid4

import pytest

from processing.config import ProcessingConfig
from processing.models.classification import ClassificationResult, LLMUsage
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import RawMessage
from processing.observability.llm_usage import LLMUsageCollector


def _make_envelope() -> ProcessingEnvelope:
    ingested = IngestedItemContext(
        id=uuid4(),
        external_id="teams_msg_1",
        platform="teams",
        course_external_id="thread-123",
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        source_url="https://teams.microsoft.com/l/message/1",
        payload={
            "content_type": "raw_message",
            "title": "Reminder",
            "author": "Example",
            "body": "The homework is due next Tuesday.",
            "course_name": "SYNTHETIC EXAMPLE COURSE",
            "published_at": "2025-10-02T07:47:19+00:00",
            "attachments": [],
            "links": [],
        },
    )
    return ProcessingEnvelope(
        id=ingested.id,
        ingested_item=ingested,
        entity=RawMessage(
            title="Reminder",
            body="The homework is due next Tuesday.",
            author="Example",
        ),
    )


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _ParsedMessage:
    def __init__(self, parsed):
        self.parsed = parsed


class _Choice:
    def __init__(self, parsed):
        self.message = _ParsedMessage(parsed)


class _Completion:
    def __init__(self, parsed, prompt_tokens: int, completion_tokens: int):
        self.choices = [_Choice(parsed)]
        self.usage = _Usage(prompt_tokens, completion_tokens)


class _FakeCompletions:
    def __init__(self, completion):
        self.completion = completion

    async def parse(self, **kwargs):
        return self.completion


class _FakeClient:
    def __init__(self, completion):
        self.completions = _FakeCompletions(completion)
        self.beta = type(
            "_Beta",
            (),
            {"chat": type("_Chat", (), {"completions": self.completions})()},
        )()


@pytest.mark.anyio
async def test_llm_classifier_records_usage_from_completion(monkeypatch):
    from processing.adapters.llm import classifier as classifier_module

    completion = _Completion(
        ClassificationResult(
            entity_type="raw_message",
            confidence=0.9,
            reasoning="Greeting",
        ),
        prompt_tokens=11,
        completion_tokens=7,
    )
    fake_client = _FakeClient(completion)

    monkeypatch.setattr(classifier_module, "AsyncOpenAI", lambda **_: fake_client)
    monkeypatch.setattr(
        classifier_module,
        "maybe_wrap_openai_client",
        lambda client: client,
    )

    collector = LLMUsageCollector()
    classifier = classifier_module.LLMClassifier(
        ProcessingConfig(
            llm_base_url="https://api.openai.com/v1",
            llm_api_key="not-needed",
            llm_max_retries=1,
            llm_model="gpt-5-nano",
            llm_timeout_seconds=30.0,
            llm_prompt_version="entity-inference-v2",
        ),
        usage_collector=collector,
    )

    with collector.capture():
        result = await classifier.classify(_make_envelope())
        usage = collector.current_usage()

    assert result.entity_type == "raw_message"
    assert usage == LLMUsage(input_tokens=11, output_tokens=7)
