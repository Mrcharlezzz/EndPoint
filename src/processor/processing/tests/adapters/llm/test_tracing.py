from __future__ import annotations

from uuid import uuid4

import pytest

from processing.config import ProcessingConfig
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import RawMessage


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


def test_build_langsmith_extra_for_classify_contains_processor_context():
    from processing.adapters.llm.tracing import build_langsmith_extra

    extra = build_langsmith_extra(
        _make_envelope(),
        operation="classify",
        prompt_version="entity-inference-v2",
        prompt_fingerprint="abc123def456",
    )

    assert extra["metadata"]["component"] == "processor"
    assert extra["metadata"]["operation"] == "classify"
    assert extra["metadata"]["ingested_item_id"] is not None
    assert extra["metadata"]["external_id"] == "teams_msg_1"
    assert extra["metadata"]["platform"] == "teams"
    assert extra["metadata"]["schema_version"] == "4.0"
    assert extra["metadata"]["adapter_version"] == "v0"
    assert extra["metadata"]["entity_type"] == "raw_message"
    assert extra["metadata"]["prompt_version"] == "entity-inference-v2"
    assert extra["metadata"]["prompt_fingerprint"] == "abc123def456"
    assert "processor" in extra["tags"]
    assert "llm" in extra["tags"]
    assert "classify" in extra["tags"]
    assert "platform:teams" in extra["tags"]
    assert "schema:4.0" in extra["tags"]
    assert "prompt:entity-inference-v2" in extra["tags"]


def test_build_langsmith_extra_for_extract_includes_classified_type():
    from processing.adapters.llm.tracing import build_langsmith_extra

    extra = build_langsmith_extra(
        _make_envelope(),
        operation="extract",
        classified_type="assignment",
    )

    assert extra["metadata"]["operation"] == "extract"
    assert extra["metadata"]["classified_type"] == "assignment"
    assert "extract" in extra["tags"]


def test_wrap_openai_client_always_wraps_client():
    from processing.adapters.llm.tracing import maybe_wrap_openai_client

    client = object()
    wrapped_client = object()

    wrapped = maybe_wrap_openai_client(
        client,
        wrap_client=lambda received: wrapped_client if received is client else None,
    )

    assert wrapped is wrapped_client


class _ParsedMessage:
    def __init__(self, parsed):
        self.parsed = parsed


class _Choice:
    def __init__(self, parsed):
        self.message = _ParsedMessage(parsed)


class _Completion:
    def __init__(self, parsed):
        self.choices = [_Choice(parsed)]


class _FakeCompletions:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return _Completion(self.parsed)


class _FakeClient:
    def __init__(self, parsed):
        self.completions = _FakeCompletions(parsed)
        self.beta = type(
            "_Beta",
            (),
            {"chat": type("_Chat", (), {"completions": self.completions})()},
        )()


@pytest.mark.anyio
async def test_classifier_passes_langsmith_extra_to_classify_call(monkeypatch):
    from processing.adapters.llm import classifier as classifier_module
    from processing.models.classification import ClassificationResult

    base_client = _FakeClient(
        ClassificationResult(
            entity_type="assignment",
            confidence=0.9,
            reasoning="Contains a due date.",
        )
    )
    wrapped_client = _FakeClient(
        ClassificationResult(
            entity_type="assignment",
            confidence=0.9,
            reasoning="Contains a due date.",
        )
    )

    monkeypatch.setattr(classifier_module, "AsyncOpenAI", lambda **_: base_client)
    monkeypatch.setattr(
        classifier_module,
        "maybe_wrap_openai_client",
        lambda client: wrapped_client if client is base_client else client,
    )

    classifier = classifier_module.LLMClassifier(
        ProcessingConfig(
            llm_base_url="https://api.openai.com/v1",
            llm_api_key="not-needed",
            llm_max_retries=1,
            llm_model="gpt-5-nano",
            llm_timeout_seconds=30.0,
            llm_prompt_version="entity-inference-v2",
        )
    )

    await classifier.classify(_make_envelope())

    assert wrapped_client.completions.calls
    call = wrapped_client.completions.calls[0]
    system_message = call["messages"][0]
    assert (
        system_message["content"] == classifier.prompt_set.classification_system_prompt
    )
    assert call["langsmith_extra"]["metadata"]["operation"] == "classify"
    assert (
        call["langsmith_extra"]["metadata"]["prompt_version"] == "entity-inference-v2"
    )
    assert (
        call["langsmith_extra"]["metadata"]["prompt_fingerprint"]
        == classifier.prompt_fingerprint
    )
    assert "classify" in call["langsmith_extra"]["tags"]
    assert "prompt:entity-inference-v2" in call["langsmith_extra"]["tags"]


@pytest.mark.anyio
async def test_classifier_passes_langsmith_extra_to_extract_call(monkeypatch):
    from processing.adapters.llm import classifier as classifier_module
    from processing.models.classification import AssignmentPatch

    base_client = _FakeClient(
        AssignmentPatch(confidence=0.9, reasoning="Assignment fields.")
    )
    wrapped_client = _FakeClient(
        AssignmentPatch(confidence=0.9, reasoning="Assignment fields.")
    )

    monkeypatch.setattr(classifier_module, "AsyncOpenAI", lambda **_: base_client)
    monkeypatch.setattr(
        classifier_module,
        "maybe_wrap_openai_client",
        lambda client: wrapped_client if client is base_client else client,
    )

    classifier = classifier_module.LLMClassifier(
        ProcessingConfig(
            llm_base_url="https://api.openai.com/v1",
            llm_api_key="not-needed",
            llm_max_retries=1,
            llm_model="gpt-5-nano",
            llm_timeout_seconds=30.0,
            llm_prompt_version="entity-inference-v2",
        )
    )

    await classifier.extract_fields(_make_envelope(), "assignment")

    assert wrapped_client.completions.calls
    call = wrapped_client.completions.calls[0]
    assert call["langsmith_extra"]["metadata"]["operation"] == "extract"
    assert call["langsmith_extra"]["metadata"]["classified_type"] == "assignment"
    assert (
        call["langsmith_extra"]["metadata"]["prompt_version"] == "entity-inference-v2"
    )
    assert "extract" in call["langsmith_extra"]["tags"]
    assert "prompt:entity-inference-v2" in call["langsmith_extra"]["tags"]
