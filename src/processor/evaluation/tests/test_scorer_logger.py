from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from evaluation.eval.loader import EvalRow
from evaluation.eval.logger import write_artifacts
from evaluation.eval.runner import PipelineRowResult, build_envelope_for_row
from evaluation.eval.scorer import TokenCostConfig, score_results
from processing.models.classification import (
    AssignmentPatch,
    ClassificationResult,
    EventPatch,
)
from processing.models.entities import Assignment, Event


def _row(
    *,
    dataset_name: str = "assignment",
    gold_type: str = "assignment",
    gold_fields: dict[str, str] | None = None,
) -> EvalRow:
    return EvalRow(
        row_id="row-1",
        dataset_name=dataset_name,
        source_file=f"{dataset_name}_extraction.csv",
        external_id="ext-1",
        platform="telegram",
        course_name="Algorithms",
        published_at="2026-03-01T09:00:00Z",
        body="Homework due tomorrow at 23:59",
        gold_entity_type=gold_type,
        gold_fields=gold_fields
        if gold_fields is not None
        else (
            {"expected_deadline": "2026-03-02T23:59:00+01:00"}
            if dataset_name == "assignment"
            else {}
        ),
        difficulty="easy",
        coverage_group="assignment_deadline",
        coverage_case="relative_deadline",
        slice_tags=["relative_date"],
        notes="",
        order_index=0,
    )


def _completed_result(row: EvalRow) -> PipelineRowResult:
    envelope = build_envelope_for_row(row).model_copy(
        update={
            "entity": Assignment(
                body=row.body,
                deadline=datetime.fromisoformat("2026-03-02T23:59:00+01:00"),
            )
        }
    )
    classification = ClassificationResult(
        entity_type="assignment",
        confidence=0.92,
        reasoning="assignment signal",
    )
    extraction = AssignmentPatch(
        deadline=datetime.fromisoformat("2026-03-02T23:59:00+01:00"),
        confidence=0.88,
        reasoning="deadline signal",
    )
    return PipelineRowResult(
        row=row,
        status="completed",
        envelope=envelope,
        classification_output=classification.model_dump(mode="json"),
        extraction_output=extraction.model_dump(mode="json"),
        processing_time_ms=12,
        retry_count=0,
        classification_time_ms=4,
        extract_time_ms=8,
        classification_usage={
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_tokens": 30,
        },
        extraction_usage={
            "prompt_tokens": 40,
            "completion_tokens": 15,
            "total_tokens": 55,
        },
        prompt_version="entity-inference-v2",
        prompt_fingerprint="abc123def456",
    )


def test_score_results_separates_success_mismatch_and_system_errors() -> None:
    success = _completed_result(_row())
    mismatch_row = _row(dataset_name="classification", gold_type="event")
    mismatch = _completed_result(mismatch_row)
    system_error = PipelineRowResult(
        row=_row(dataset_name="classification"),
        status="system_error",
        processing_time_ms=3,
        retry_count=1,
        error_type="RuntimeError",
        error_message="boom",
        traceback_short="RuntimeError: boom",
    )

    scored_rows, metrics = score_results([success, mismatch, system_error])

    assert [row.status for row in scored_rows] == [
        "success",
        "eval_mismatch",
        "system_error",
    ]
    assert scored_rows[0].classification_correct is None
    assert scored_rows[0].num_correct_fields == 1
    assert scored_rows[0].item_completeness_score == 1.0
    assert metrics["run"]["total_rows"] == 3
    assert metrics["run"]["system_error_rows"] == 1
    assert metrics["classification"]["overall_accuracy"] == 0.0
    assert (
        metrics["extraction"]["assignment"]["end_to_end"]["item_completeness_mean"]
        == 1.0
    )
    assert metrics["errors"]["counts_by_type"] == {"RuntimeError": 1}
    assert metrics["performance"]["row_latency_ms"]["p50"] == 12
    assert metrics["performance"]["classification_latency_ms"]["p50"] == 4
    assert metrics["performance"]["extraction_latency_ms"]["p50"] == 8


def test_extraction_scoring_does_not_require_classification_output() -> None:
    row = _row()
    envelope = build_envelope_for_row(row).model_copy(
        update={
            "entity": Assignment(
                body=row.body,
                deadline=datetime.fromisoformat("2026-03-02T23:59:00+01:00"),
            )
        }
    )
    extraction = AssignmentPatch(
        deadline=datetime.fromisoformat("2026-03-02T23:59:00+01:00"),
        confidence=0.88,
        reasoning="deadline signal",
    )

    scored_rows, metrics = score_results(
        [
            PipelineRowResult(
                row=row,
                status="completed",
                envelope=envelope,
                classification_output=None,
                extraction_output=extraction.model_dump(mode="json"),
                processing_time_ms=12,
                retry_count=0,
            )
        ]
    )

    assert scored_rows[0].status == "success"
    assert scored_rows[0].classification_correct is None
    assert scored_rows[0].num_correct_fields == 1
    assert scored_rows[0].item_completeness_score == 1.0
    assert (
        metrics["extraction"]["assignment"]["end_to_end"]["item_completeness_mean"]
        == 1.0
    )


def test_write_artifacts_persists_jsonl_csv_errors_metrics_and_summary(
    tmp_path: Path,
) -> None:
    scored_rows, metrics = score_results(
        [
            _completed_result(_row()),
            PipelineRowResult(
                row=_row(dataset_name="classification"),
                status="system_error",
                processing_time_ms=3,
                retry_count=1,
                error_type="RuntimeError",
                error_message="boom",
                traceback_short="RuntimeError: boom",
            ),
        ]
    )

    artifact_dir = write_artifacts(
        scored_rows,
        metrics,
        output_dir=tmp_path,
        eval_run_id="eval-test",
    )

    assert (artifact_dir / "rows.jsonl").exists()
    assert (artifact_dir / "rows.csv").exists()
    assert (artifact_dir / "system_errors.jsonl").exists()
    assert (artifact_dir / "metrics.json").exists()
    assert (artifact_dir / "run_summary.txt").exists()
    assert len((artifact_dir / "rows.jsonl").read_text().splitlines()) == 2
    assert len((artifact_dir / "system_errors.jsonl").read_text().splitlines()) == 1
    assert (
        json.loads((artifact_dir / "metrics.json").read_text())["run"]["eval_run_id"]
        == "eval-test"
    )
    first_row = json.loads((artifact_dir / "rows.jsonl").read_text().splitlines()[0])
    assert first_row["prompt_version"] == "entity-inference-v2"
    assert first_row["prompt_fingerprint"] == "abc123def456"


def test_score_results_aggregates_token_usage_and_estimates_cost() -> None:
    scored_rows, metrics = score_results(
        [_completed_result(_row())],
        cost_config=TokenCostConfig(
            input_price_usd_per_1m_tokens=0.5,
            output_price_usd_per_1m_tokens=1.5,
        ),
    )

    del scored_rows

    assert (
        metrics["performance"]["token_usage"]["classification"]["prompt_tokens"] == 20
    )
    assert (
        metrics["performance"]["token_usage"]["extraction"]["completion_tokens"] == 15
    )
    assert metrics["performance"]["token_usage"]["total"]["total_tokens"] == 85
    assert metrics["performance"]["cost"]["estimated_total_usd"] == 0.0000675
    assert metrics["performance"]["cost"]["pricing"] == {
        "input_price_usd_per_1m_tokens": 0.5,
        "output_price_usd_per_1m_tokens": 1.5,
    }


def test_location_tolerant_match_accepts_lowercase_containment() -> None:
    row = _row(
        dataset_name="event",
        gold_type="event",
        gold_fields={"expected_location": "google"},
    )
    envelope = build_envelope_for_row(row).model_copy(
        update={"entity": Event(body=row.body, location="Google Meet")}
    )
    classification = ClassificationResult(
        entity_type="event",
        confidence=0.92,
        reasoning="event signal",
    )
    extraction = EventPatch(
        location="Google Meet",
        confidence=0.88,
        reasoning="location signal",
    )

    scored_rows, metrics = score_results(
        [
            PipelineRowResult(
                row=row,
                status="completed",
                envelope=envelope,
                classification_output=classification.model_dump(mode="json"),
                extraction_output=extraction.model_dump(mode="json"),
                processing_time_ms=12,
                retry_count=0,
            )
        ]
    )

    comparison = scored_rows[0].field_comparisons[0]
    assert comparison.exact_match is False
    assert comparison.tolerant_match is True
    assert (
        metrics["extraction"]["event"]["end_to_end"]["per_field"]["expected_location"][
            "tolerant_accuracy"
        ]
        == 1.0
    )


def test_datetime_scoring_flags_timezone_offset_shift() -> None:
    row = _row(
        dataset_name="event",
        gold_type="event",
        gold_fields={"expected_start_time": "2026-04-16T15:15:39+00:00"},
    )
    envelope = build_envelope_for_row(row).model_copy(
        update={
            "entity": Event(
                body=row.body,
                start_time=datetime.fromisoformat("2026-04-16T13:15:39+00:00"),
            )
        }
    )

    scored_rows, _ = score_results(
        [
            PipelineRowResult(
                row=row,
                status="completed",
                envelope=envelope,
                classification_output=None,
                extraction_output=None,
                processing_time_ms=12,
                retry_count=0,
            )
        ]
    )

    comparison = scored_rows[0].field_comparisons[0]
    assert comparison.exact_match is False
    assert comparison.temporal_anomaly == "timezone_offset_shift"
