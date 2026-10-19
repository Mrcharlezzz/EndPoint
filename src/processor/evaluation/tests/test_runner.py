from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.eval.call_debug_logger import CallDebugLogger
from evaluation.eval.loader import EvalRow
from evaluation.eval.runner import EvalRunner, build_envelope_for_row
from processing.models.classification import (
    AssignmentPatch,
    ClassificationResult,
)
from processing.models.envelope import ProcessingEnvelope


class FakeClassifier:
    model = "fake-model"

    def __init__(
        self,
        *,
        fail_ids: set[str] | None = None,
        fail_classification: bool = False,
    ) -> None:
        self.fail_ids = fail_ids or set()
        self.fail_classification = fail_classification
        self.envelopes: list[ProcessingEnvelope] = []

    async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult:
        self.envelopes.append(envelope)
        if self.fail_classification:
            raise RuntimeError("classification should not run")
        if envelope.ingested_item.external_id in self.fail_ids:
            raise RuntimeError("classification boom")
        return ClassificationResult(
            entity_type="assignment",
            confidence=0.91,
            reasoning="contains homework deadline",
        )

    async def extract_fields(
        self, envelope: ProcessingEnvelope, classified_type: str
    ) -> AssignmentPatch:
        if envelope.ingested_item.external_id in self.fail_ids:
            raise RuntimeError("extraction boom")
        return AssignmentPatch(
            deadline=datetime.fromisoformat("2026-03-02T23:59:00+01:00"),
            confidence=0.87,
            reasoning="explicit deadline",
        )


class _FakeUsage:
    def __init__(self, *, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens

    def model_dump(self, *, mode: str = "json", exclude_none: bool = False) -> dict:
        del mode, exclude_none
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class FakeInstrumentedLLMClassifier:
    model = "fake-model"
    prompt_version = "entity-inference-v2"
    prompt_fingerprint = "abc123def456"

    def __init__(self) -> None:
        async def parse(*, response_format, **kwargs):
            del kwargs
            if response_format is ClassificationResult:
                parsed = ClassificationResult(
                    entity_type="assignment",
                    confidence=0.91,
                    reasoning="contains homework deadline",
                )
                usage = _FakeUsage(prompt_tokens=11, completion_tokens=7)
            else:
                parsed = AssignmentPatch(
                    deadline=datetime.fromisoformat("2026-03-02T23:59:00+01:00"),
                    confidence=0.87,
                    reasoning="explicit deadline",
                )
                usage = _FakeUsage(prompt_tokens=13, completion_tokens=5)
            return SimpleNamespace(
                usage=usage,
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
            )

        self.client = SimpleNamespace(
            beta=SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))
            )
        )

    async def classify(self, envelope: ProcessingEnvelope) -> ClassificationResult:
        del envelope
        completion = await self.client.beta.chat.completions.parse(
            response_format=ClassificationResult
        )
        return completion.choices[0].message.parsed

    async def extract_fields(
        self, envelope: ProcessingEnvelope, classified_type: str
    ) -> AssignmentPatch:
        del envelope, classified_type
        completion = await self.client.beta.chat.completions.parse(
            response_format=AssignmentPatch
        )
        return completion.choices[0].message.parsed


def _row(row_id: str) -> EvalRow:
    return EvalRow(
        row_id=row_id,
        dataset_name="assignment",
        source_file="assignment_extraction.csv",
        external_id=row_id,
        platform="telegram",
        course_name="Algorithms",
        published_at="2026-03-01T09:00:00Z",
        body="Homework due tomorrow at 23:59",
        gold_entity_type="assignment",
        gold_fields={"expected_deadline": "2026-03-02T23:59:00+01:00"},
        difficulty="easy",
        coverage_group="assignment_deadline",
        coverage_case="relative_deadline",
        slice_tags=["relative_date"],
        notes="",
        order_index=0,
    )


def test_build_envelope_for_row_does_not_include_author_metadata() -> None:
    envelope = build_envelope_for_row(_row("row-ok"))

    assert envelope.entity.author is None
    assert "author" not in envelope.ingested_item.payload
    assert envelope.ingested_item.payload["course_name"] == "Algorithms"


@pytest.mark.anyio
async def test_runner_returns_one_result_per_row_and_preserves_system_errors() -> None:
    classifier = FakeClassifier(fail_ids={"row-fail"})
    runner = EvalRunner(
        classifier,
        concurrency=2,
        timeout_seconds=5.0,
        retry_count=0,
        max_errors=None,
        continue_on_error=True,
    )

    results = await runner.run_rows([_row("row-ok"), _row("row-fail")])

    assert [result.row.row_id for result in results] == ["row-ok", "row-fail"]
    ok, failed = results
    assert ok.status == "completed"
    assert ok.classification_output is None
    assert ok.extraction_output is not None
    assert failed.status == "system_error"
    assert failed.error_type == "RuntimeError"
    assert "extraction boom" in (failed.error_message or "")
    assert failed.traceback_short


@pytest.mark.anyio
async def test_runner_extraction_dataset_uses_gold_type_without_classification() -> (
    None
):
    classifier = FakeClassifier(fail_classification=True)
    runner = EvalRunner(
        classifier,
        concurrency=1,
        timeout_seconds=5.0,
        retry_count=0,
        max_errors=None,
        continue_on_error=True,
    )

    results = await runner.run_rows([_row("row-ok")])

    assert results[0].status == "completed"
    assert results[0].classification_output is None
    assert results[0].extraction_output is not None
    assert results[0].extraction_output["deadline"] == "2026-03-02T23:59:00+01:00"


@pytest.mark.anyio
async def test_runner_captures_call_timing_and_usage_for_classify_and_extract() -> None:
    classifier = FakeInstrumentedLLMClassifier()
    runner = EvalRunner(
        classifier,
        concurrency=1,
        timeout_seconds=5.0,
        retry_count=0,
        max_errors=None,
        continue_on_error=True,
    )

    classification_row = _row("row-classification")
    classification_row = replace(
        classification_row,
        dataset_name="classification",
        source_file="message_index.csv",
        gold_fields={},
    )
    extraction_row = _row("row-extraction")

    classification_result, extraction_result = await runner.run_rows(
        [classification_row, extraction_row]
    )

    assert classification_result.classification_time_ms is not None
    assert classification_result.classification_usage == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    assert classification_result.extract_time_ms is None
    assert classification_result.extraction_usage is None

    assert extraction_result.classification_time_ms is None
    assert extraction_result.classification_usage is None
    assert extraction_result.extract_time_ms is not None
    assert extraction_result.extraction_usage == {
        "prompt_tokens": 13,
        "completion_tokens": 5,
        "total_tokens": 18,
    }


@pytest.mark.anyio
async def test_runner_persists_call_debug_log_events(tmp_path: Path) -> None:
    classifier = FakeInstrumentedLLMClassifier()
    call_log_path = tmp_path / "call_debug.jsonl"
    runner = EvalRunner(
        classifier,
        concurrency=2,
        timeout_seconds=5.0,
        retry_count=0,
        max_errors=None,
        continue_on_error=True,
        eval_run_id="eval-test",
        call_debug_logger=CallDebugLogger(call_log_path),
    )

    classification_row = replace(
        _row("row-classification"),
        dataset_name="classification",
        source_file="message_index.csv",
        gold_fields={},
    )
    extraction_row = _row("row-extraction")

    await runner.run_rows([classification_row, extraction_row])

    events = [
        json.loads(line)
        for line in call_log_path.read_text(encoding="utf-8").splitlines()
    ]

    assert len(events) == 4
    assert [event["event"] for event in events] == [
        "start",
        "finish",
        "start",
        "finish",
    ]
    assert {event["operation"] for event in events} == {"classify", "extract"}
    assert all(event["eval_run_id"] == "eval-test" for event in events)
    assert all("call_id" in event for event in events)
    assert all(event["prompt_version"] == "entity-inference-v2" for event in events)
    assert all(event["prompt_fingerprint"] == "abc123def456" for event in events)
    assert {event["row_id"] for event in events} == {
        "row-classification",
        "row-extraction",
    }
