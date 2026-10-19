from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluation.eval.scorer import ScoredRow


def write_artifacts(
    scored_rows: list[ScoredRow],
    metrics: dict[str, Any],
    *,
    output_dir: str | Path,
    eval_run_id: str,
) -> Path:
    artifact_dir = Path(output_dir) / eval_run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    enriched_metrics = dict(metrics)
    enriched_run = dict(enriched_metrics.get("run", {}))
    enriched_run["eval_run_id"] = eval_run_id
    enriched_run.setdefault("eval_timestamp", datetime.now(timezone.utc).isoformat())
    enriched_metrics["run"] = enriched_run

    row_objects = [_row_json(row, eval_run_id) for row in scored_rows]
    _write_jsonl(artifact_dir / "rows.jsonl", row_objects)
    _write_rows_csv(artifact_dir / "rows.csv", row_objects)
    _write_jsonl(
        artifact_dir / "system_errors.jsonl",
        [row for row in row_objects if row["status"] == "system_error"],
    )
    (artifact_dir / "metrics.json").write_text(
        json.dumps(enriched_metrics, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "run_summary.txt").write_text(
        _summary_text(enriched_metrics, artifact_dir),
        encoding="utf-8",
    )
    return artifact_dir


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "eval_run_id",
        "row_id",
        "dataset_name",
        "platform",
        "gold_entity_type",
        "predicted_entity_type",
        "classification_correct",
        "status",
        "num_expected_fields",
        "num_correct_fields",
        "item_completeness_score",
        "processing_time_ms",
        "classification_time_ms",
        "extract_time_ms",
        "prompt_version",
        "prompt_fingerprint",
        "retry_count",
        "error_type",
        "error_message",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})


def _row_json(scored_row: ScoredRow, eval_run_id: str) -> dict[str, Any]:
    row = scored_row.row
    result = scored_row.pipeline_result
    classification_output = result.classification_output or {}
    extraction_output = result.extraction_output or {}
    return {
        "eval_run_id": eval_run_id,
        "row_id": row.row_id,
        "dataset_name": row.dataset_name,
        "source_file": row.source_file,
        "external_id": row.external_id,
        "platform": row.platform,
        "course_name": row.course_name,
        "published_at": row.published_at,
        "body": row.body,
        "difficulty": row.difficulty,
        "coverage_group": row.coverage_group,
        "coverage_case": row.coverage_case,
        "slice_tags": row.slice_tags,
        "notes": row.notes,
        "gold_entity_type": row.gold_entity_type,
        "gold_fields": row.gold_fields,
        "predicted_entity_type": scored_row.predicted_entity_type,
        "classification_confidence": classification_output.get("confidence"),
        "classification_reasoning": classification_output.get("reasoning"),
        "classification_output": classification_output,
        "extraction_output": extraction_output,
        "extraction_confidence": extraction_output.get("confidence"),
        "extraction_reasoning": extraction_output.get("reasoning"),
        "status": scored_row.status,
        "classification_correct": scored_row.classification_correct,
        "extraction_scored": scored_row.extraction_scored,
        "extraction_skipped": scored_row.extraction_skipped,
        "field_comparisons": [
            asdict(comparison) for comparison in scored_row.field_comparisons
        ],
        "num_expected_fields": scored_row.num_expected_fields,
        "num_predicted_fields": scored_row.num_predicted_fields,
        "num_correct_fields": scored_row.num_correct_fields,
        "item_completeness_score": scored_row.item_completeness_score,
        "full_row_exact_match": scored_row.full_row_exact_match,
        "processing_time_ms": result.processing_time_ms,
        "classification_time_ms": result.classification_time_ms,
        "extract_time_ms": result.extract_time_ms,
        "classification_usage": result.classification_usage,
        "extraction_usage": result.extraction_usage,
        "retry_count": result.retry_count,
        "llm_model": _llm_model(result),
        "prompt_version": result.prompt_version,
        "prompt_fingerprint": result.prompt_fingerprint,
        "eval_timestamp": datetime.now(timezone.utc).isoformat(),
        "error_type": result.error_type,
        "error_message": result.error_message,
        "traceback_short": result.traceback_short,
    }


def _llm_model(result) -> str | None:
    if result.envelope is None or result.envelope.classification_log is None:
        return None
    return result.envelope.classification_log.llm_model


def _summary_text(metrics: dict[str, Any], artifact_dir: Path) -> str:
    run = metrics.get("run", {})
    classification = metrics.get("classification", {})
    errors = metrics.get("errors", {})
    performance = metrics.get("performance", {})
    row_latency = performance.get("row_latency_ms", {})
    usage = performance.get("token_usage", {}).get("total", {})
    cost = performance.get("cost", {})
    lines = [
        "Evaluation Run Summary",
        f"Run ID: {run.get('eval_run_id')}",
        f"Timestamp: {run.get('eval_timestamp')}",
        f"Rows: {run.get('total_rows', 0)}",
        f"Wall clock seconds: {run.get('wall_clock_seconds')}",
        f"Rows/sec: {run.get('throughput_rows_per_second')}",
        f"Call debug log: {run.get('call_debug_log_path')}",
        f"Prompt version: {run.get('prompt_version')}",
        f"Prompt fingerprint: {run.get('prompt_fingerprint')}",
        f"Success: {run.get('successful_rows', 0)}",
        f"Eval mismatches: {run.get('eval_mismatch_rows', 0)}",
        f"System errors: {run.get('system_error_rows', 0)}",
        f"Skipped: {run.get('skipped_rows', 0)}",
        f"Classification macro F1: {classification.get('macro_f1', 0.0):.4f}",
        f"Row latency p50/p95/p99/max ms: {row_latency.get('p50')}/{row_latency.get('p95')}/{row_latency.get('p99')}/{row_latency.get('max')}",
        f"Total tokens prompt/completion/total: {usage.get('prompt_tokens')}/{usage.get('completion_tokens')}/{usage.get('total_tokens')}",
        f"Estimated cost USD: {cost.get('estimated_total_usd')}",
        f"Error counts: {errors.get('counts_by_type', {})}",
        f"Artifacts: {artifact_dir}",
        "",
    ]
    return "\n".join(lines)
