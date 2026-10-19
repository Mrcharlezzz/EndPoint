from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from processing.application.classification import ClassificationOrchestrator
from processing.models.classification import (
    AssignmentPatch,
    ClassificationResult,
    LLMUsage,
    ExtractionPatch,
    MaterialPatch,
)
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import Assignment, Material, RawMessage
from processing.models.extracted import (
    ExtractedAttachment,
    ExtractedLink,
)
from processing.observability.llm_usage import LLMUsageCollector


class FakeClassifier:
    def __init__(self, classification_result, extraction_result):
        self.classification_result = classification_result
        self.extraction_result = extraction_result
        self.calls: list[tuple] = []
        self.model = "fake-llm"
        self.prompt_version = "entity-inference-v2"
        self.prompt_fingerprint = "abc123def456"
        self.classify_usage: LLMUsage | None = None
        self.extract_usage: LLMUsage | None = None
        self.usage_collector: LLMUsageCollector | None = None

    async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult:
        self.calls.append(("classify", envelope.entity.entity_type))
        if self.usage_collector is not None and self.classify_usage is not None:
            self.usage_collector.record_call(
                input_tokens=self.classify_usage.input_tokens,
                output_tokens=self.classify_usage.output_tokens,
            )
        return self.classification_result

    async def extract_fields(
        self, envelope: ProcessingEnvelope, classified_type: str
    ) -> ExtractionPatch:
        self.calls.append(("extract_fields", classified_type))
        assert self.extraction_result is not None, (
            "extract_fields called but no extraction_result configured"
        )
        if self.usage_collector is not None and self.extract_usage is not None:
            self.usage_collector.record_call(
                input_tokens=self.extract_usage.input_tokens,
                output_tokens=self.extract_usage.output_tokens,
            )
        return self.extraction_result


def _make_raw_envelope(
    *,
    title: str | None = "Original title",
    body: str | None = "Original body with enough detail to require classification.",
    author: str | None = "Ada Lovelace",
    course_name: str | None = "Programming in C and C++",
    payload_overrides: dict | None = None,
    extracted_links: list[ExtractedLink] | None = None,
    extracted_attachments: list[ExtractedAttachment] | None = None,
) -> ProcessingEnvelope:
    payload = {
        "content_type": "raw_message",
        "title": title,
        "author": author,
        "body": body,
        "course_name": course_name,
        "published_at": None,
        "deadline": None,
        "submission_format": None,
        "start_time": None,
        "end_time": None,
        "location": None,
        "attachments": [],
        "links": [],
    }
    if payload_overrides is not None:
        payload.update(payload_overrides)

    ingested = IngestedItemContext(
        id=uuid4(),
        external_id="teams_msg_1",
        platform="teams",
        course_external_id=None,
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        source_url=None,
        payload=payload,
    )
    return ProcessingEnvelope(
        id=ingested.id,
        ingested_item=ingested,
        entity=RawMessage(title=title, body=body, author=author),
        extracted_links=extracted_links or [],
        extracted_attachments=extracted_attachments or [],
    )


@pytest.mark.anyio
async def test_classify_and_extract_raw_message_skips_extraction_and_preserves_entity():
    classifier = FakeClassifier(
        classification_result=ClassificationResult(
            entity_type="raw_message",
            confidence=0.95,
            reasoning="Greeting message, no academic content.",
        ),
        extraction_result=None,  # should never be called
    )
    envelope = _make_raw_envelope()

    result = await ClassificationOrchestrator(classifier).classify_and_extract(envelope)

    # extract_fields must not have been called
    assert classifier.calls == [("classify", "raw_message")]
    # entity unchanged — still a RawMessage
    assert isinstance(result.entity, RawMessage)
    assert result.entity.title == envelope.entity.title
    assert result.entity.body == envelope.entity.body
    assert result.entity.author == envelope.entity.author
    # supporting data untouched
    assert result.extracted_links == []
    assert result.extracted_attachments == []
    # log written with raw_message type
    assert result.classification_log is not None
    assert result.classification_log.entity_type == "raw_message"
    assert result.classification_log.classification_method == "llm"
    assert result.classification_log.llm_model == "fake-llm"
    assert result.classification_log.prompt_version == "entity-inference-v2"
    assert result.classification_log.confidence == 0.95
    assert result.classification_log.processing_time_ms is not None
    assert result.classification_log.processing_time_ms >= 0
    assert result.classification_log.classified_item_id is None


@pytest.mark.anyio
async def test_classify_and_extract_short_raw_message_without_links_skips_classifier():
    classifier = FakeClassifier(
        classification_result=ClassificationResult(
            entity_type="assignment",
            confidence=0.93,
            reasoning="Would classify if called.",
        ),
        extraction_result=None,
    )
    envelope = _make_raw_envelope(body="Quiz is out")

    result = await ClassificationOrchestrator(classifier).classify_and_extract(envelope)

    assert classifier.calls == []
    assert isinstance(result.entity, RawMessage)
    assert result.entity == envelope.entity
    assert result.classification_log is not None
    assert result.classification_log.entity_type == "raw_message"
    assert result.classification_log.classification_method == "rule_based"
    assert result.classification_log.llm_model is None
    assert result.classification_log.confidence == 1.0
    assert result.classification_log.processing_time_ms is not None
    assert result.classification_log.input_tokens is None
    assert result.classification_log.output_tokens is None


@pytest.mark.anyio
async def test_classify_and_extract_short_raw_message_with_link_uses_classifier():
    classifier = FakeClassifier(
        classification_result=ClassificationResult(
            entity_type="raw_message",
            confidence=0.86,
            reasoning="Short link share still needs classification.",
        ),
        extraction_result=None,
    )
    envelope = _make_raw_envelope(
        body="Slides",
        extracted_links=[ExtractedLink(url="https://example.test/slides.pdf")],
    )

    result = await ClassificationOrchestrator(classifier).classify_and_extract(envelope)

    assert classifier.calls == [("classify", "raw_message")]
    assert result.classification_log is not None
    assert result.classification_log.classification_method == "llm"


@pytest.mark.anyio
async def test_classify_returns_logged_envelope_and_classification_result():
    classifier = FakeClassifier(
        classification_result=ClassificationResult(
            entity_type="assignment",
            confidence=0.92,
            reasoning="Contains assignment wording.",
        ),
        extraction_result=None,
    )
    envelope = _make_raw_envelope()

    result, classification = await ClassificationOrchestrator(classifier).classify(
        envelope
    )

    assert classifier.calls == [("classify", "raw_message")]
    assert result is not envelope
    assert result.entity == envelope.entity
    assert classification.entity_type == "assignment"
    assert result.classification_log is not None
    assert result.classification_log.entity_type == "assignment"
    assert result.classification_log.classification_method == "llm"
    assert result.classification_log.llm_model == "fake-llm"
    assert result.classification_log.prompt_version == "entity-inference-v2"
    assert result.classification_log.confidence == 0.92
    assert result.classification_log.processing_time_ms is None


@pytest.mark.anyio
async def test_extract_uses_supplied_entity_type_and_resolves_without_classifying():
    classifier = FakeClassifier(
        classification_result=ClassificationResult(
            entity_type="event",
            confidence=0.2,
            reasoning="Should not be used.",
        ),
        extraction_result=AssignmentPatch(
            title="Homework 1",
            deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
            confidence=0.88,
            reasoning="Clear assignment deadline.",
        ),
    )
    envelope = _make_raw_envelope()

    result, extraction = await ClassificationOrchestrator(classifier).extract(
        envelope, "assignment"
    )

    assert classifier.calls == [("extract_fields", "assignment")]
    assert result is not envelope
    assert extraction is classifier.extraction_result
    assert isinstance(result.entity, Assignment)
    assert result.entity.title == "Homework 1"
    assert result.entity.deadline == datetime(
        2025, 9, 9, 21, 59, tzinfo=ZoneInfo("Europe/Amsterdam")
    )
    assert result.classification_log is None


@pytest.mark.anyio
async def test_classify_and_extract_builds_assignment_entity_and_log():
    classifier = FakeClassifier(
        classification_result=ClassificationResult(
            entity_type="assignment",
            confidence=0.92,
            reasoning="Contains a due date and grading details.",
        ),
        extraction_result=AssignmentPatch(
            title="Homework 1",
            deadline=datetime(2025, 9, 9, 21, 59, tzinfo=timezone.utc),
            submission_format="file",
            confidence=0.88,
            reasoning="Clear assignment structure.",
        ),
    )
    envelope = _make_raw_envelope()

    result = await ClassificationOrchestrator(classifier).classify_and_extract(envelope)

    assert result is not envelope  # immutable — new copy returned
    assert classifier.calls == [
        ("classify", "raw_message"),
        ("extract_fields", "assignment"),
    ]
    assert isinstance(result.entity, Assignment)
    assert result.entity.title == "Homework 1"
    assert (
        result.entity.body
        == "Original body with enough detail to require classification."
    )
    assert result.entity.author == "Ada Lovelace"
    assert result.entity.deadline == datetime(
        2025, 9, 9, 21, 59, tzinfo=ZoneInfo("Europe/Amsterdam")
    )
    assert result.extracted_links == []
    assert result.extracted_attachments == []
    assert result.classification_log is not None
    assert result.classification_log.ingested_item_id == result.ingested_item.id
    assert result.classification_log.classified_item_id is None
    assert result.classification_log.entity_type == "assignment"
    assert result.classification_log.classification_method == "llm"
    assert result.classification_log.llm_model == "fake-llm"
    assert result.classification_log.confidence == 0.92
    assert result.classification_log.processing_time_ms is not None
    assert result.classification_log.processing_time_ms >= 0


@pytest.mark.anyio
async def test_classify_and_extract_accumulates_usage_into_classification_log():
    usage_collector = LLMUsageCollector()
    classifier = FakeClassifier(
        classification_result=ClassificationResult(
            entity_type="assignment",
            confidence=0.92,
            reasoning="Contains assignment wording.",
        ),
        extraction_result=AssignmentPatch(
            title="Homework 1",
            confidence=0.88,
            reasoning="Clear assignment structure.",
        ),
    )
    classifier.classify_usage = LLMUsage(input_tokens=100, output_tokens=20)
    classifier.extract_usage = LLMUsage(input_tokens=40, output_tokens=9)
    classifier.usage_collector = usage_collector

    result = await ClassificationOrchestrator(
        classifier,
        usage_collector=usage_collector,
    ).classify_and_extract(_make_raw_envelope())

    assert result.classification_log is not None
    assert result.classification_log.input_tokens == 140
    assert result.classification_log.output_tokens == 29


@pytest.mark.anyio
async def test_classify_and_extract_builds_material_entity():
    classifier = FakeClassifier(
        classification_result=ClassificationResult(
            entity_type="material",
            confidence=0.88,
            reasoning="Message shares course slides.",
        ),
        extraction_result=MaterialPatch(
            title="Lecture 1 slides",
            confidence=0.83,
            reasoning="Resource shared for later reference.",
        ),
    )

    envelope = _make_raw_envelope()
    result = await ClassificationOrchestrator(classifier).classify_and_extract(envelope)

    assert result is not envelope  # immutable — new copy returned
    assert isinstance(result.entity, Material)
    assert result.entity.title == "Lecture 1 slides"
    assert (
        result.entity.body
        == "Original body with enough detail to require classification."
    )
    assert result.extracted_attachments == []


@pytest.mark.anyio
async def test_classify_and_extract_prefers_deterministic_scalar_payload_values_and_fills_missing_ones():
    classifier = FakeClassifier(
        classification_result=ClassificationResult(
            entity_type="assignment",
            confidence=0.9,
            reasoning="Assignment with mixed deterministic and extracted fields.",
        ),
        extraction_result=AssignmentPatch(
            title="Homework 2",
            deadline=datetime(2025, 9, 8, 21, 59, tzinfo=timezone.utc),
            submission_format="upload a pdf",
            confidence=0.85,
            reasoning="Mixed sparse extraction.",
        ),
    )
    envelope = _make_raw_envelope(
        payload_overrides={
            "deadline": "2025-09-01T21:59:00+00:00",
            "submission_format": None,
            "published_at": "2025-08-30T10:00:00+00:00",
        }
    )

    result = await ClassificationOrchestrator(classifier).classify_and_extract(envelope)

    assert isinstance(result.entity, Assignment)
    assert result.entity.deadline == datetime(2025, 9, 1, 21, 59, tzinfo=timezone.utc)
    assert result.entity.submission_format == "upload a pdf"
    assert result.entity.published_at == datetime(
        2025, 8, 30, 10, 0, tzinfo=timezone.utc
    )


@pytest.mark.anyio
async def test_classify_and_extract_preserves_existing_links_and_attachments():
    existing_link = ExtractedLink(url="https://example.com/a", label="A")
    existing_attachment = ExtractedAttachment(
        filename="slides.pdf",
        mime_type="application/pdf",
        url="https://example.com/slides.pdf",
    )
    classifier = FakeClassifier(
        classification_result=ClassificationResult(
            entity_type="material",
            confidence=0.87,
            reasoning="Shared resource.",
        ),
        extraction_result=MaterialPatch(
            title="Lecture resources",
            confidence=0.83,
            reasoning="Adds one new supporting item of each type.",
        ),
    )
    envelope = _make_raw_envelope(
        extracted_links=[existing_link],
        extracted_attachments=[existing_attachment],
    )

    result = await ClassificationOrchestrator(classifier).classify_and_extract(envelope)

    assert result.extracted_links == [existing_link]
    assert result.extracted_attachments == [existing_attachment]


@pytest.mark.anyio
async def test_classify_and_extract_preserves_existing_supporting_data():
    existing_link = ExtractedLink(url="https://example.com/material", label="Deck")
    existing_attachment = ExtractedAttachment(
        filename="lecture.pdf",
        mime_type="application/pdf",
        url="https://example.com/lecture.pdf",
    )
    classifier = FakeClassifier(
        classification_result=ClassificationResult(
            entity_type="material",
            confidence=0.86,
            reasoning="Still material.",
        ),
        extraction_result=MaterialPatch(
            title="Lecture deck",
            confidence=0.82,
            reasoning="Sparse extraction leaves supporting data untouched.",
        ),
    )
    envelope = _make_raw_envelope(
        extracted_links=[existing_link],
        extracted_attachments=[existing_attachment],
    )

    result = await ClassificationOrchestrator(classifier).classify_and_extract(envelope)

    assert result.extracted_links == [existing_link]
    assert result.extracted_attachments == [existing_attachment]
