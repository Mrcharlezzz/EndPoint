from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from evaluation.eval.loader import EvalRow
from evaluation.eval.runner import PipelineRowResult


@dataclass(frozen=True, slots=True)
class FieldComparison:
    field_name: str
    expected_value: Any
    predicted_value: Any
    normalized_expected_value: Any
    normalized_predicted_value: Any
    exact_match: bool
    tolerant_match: bool | None = None
    temporal_anomaly: str | None = None


@dataclass(slots=True)
class ScoredRow:
    row: EvalRow
    pipeline_result: PipelineRowResult
    status: str
    predicted_entity_type: str | None
    classification_correct: bool | None
    extraction_scored: bool
    extraction_skipped: bool
    field_comparisons: list[FieldComparison] = field(default_factory=list)
    num_expected_fields: int = 0
    num_predicted_fields: int = 0
    num_correct_fields: int = 0
    item_completeness_score: float | None = None
    full_row_exact_match: bool | None = None


@dataclass(frozen=True, slots=True)
class TokenCostConfig:
    input_price_usd_per_1m_tokens: float
    output_price_usd_per_1m_tokens: float


_FIELD_MAP = {
    "expected_deadline": ("deadline", "datetime"),
    "expected_start_time": ("start_time", "datetime"),
    "expected_end_time": ("end_time", "datetime"),
    "expected_location": ("location", "string"),
    "expected_title": ("title", "string"),
}


def score_results(
    results: list[PipelineRowResult],
    *,
    cost_config: TokenCostConfig | None = None,
) -> tuple[list[ScoredRow], dict[str, Any]]:
    scored_rows = [_score_row(result) for result in results]
    return scored_rows, _build_metrics(scored_rows, cost_config=cost_config)


def _score_row(result: PipelineRowResult) -> ScoredRow:
    row = result.row
    predicted_entity_type = _predicted_entity_type(result)

    if result.status == "system_error":
        return ScoredRow(
            row=row,
            pipeline_result=result,
            status="system_error",
            predicted_entity_type=predicted_entity_type,
            classification_correct=None,
            extraction_scored=False,
            extraction_skipped=True,
        )
    if result.status == "skipped":
        return ScoredRow(
            row=row,
            pipeline_result=result,
            status="skipped",
            predicted_entity_type=predicted_entity_type,
            classification_correct=None,
            extraction_scored=False,
            extraction_skipped=True,
        )

    if row.dataset_name == "classification":
        classification_correct = predicted_entity_type == row.gold_entity_type
        return ScoredRow(
            row=row,
            pipeline_result=result,
            status="success" if classification_correct else "eval_mismatch",
            predicted_entity_type=predicted_entity_type,
            classification_correct=classification_correct,
            extraction_scored=False,
            extraction_skipped=True,
        )

    classification_correct = None
    expected_fields = row.gold_fields
    if not expected_fields:
        return ScoredRow(
            row=row,
            pipeline_result=result,
            status="skipped",
            predicted_entity_type=predicted_entity_type,
            classification_correct=classification_correct,
            extraction_scored=False,
            extraction_skipped=True,
        )

    comparisons = [
        _compare_field(result, expected_field, expected_value)
        for expected_field, expected_value in expected_fields.items()
    ]
    num_expected = len(comparisons)
    num_predicted = sum(
        comparison.normalized_predicted_value not in (None, "", [], {})
        for comparison in comparisons
    )
    num_correct = sum(comparison.exact_match for comparison in comparisons)
    completeness = num_correct / num_expected if num_expected else None
    full_row_exact = bool(num_expected and num_correct == num_expected)
    status = "success" if full_row_exact else "eval_mismatch"
    return ScoredRow(
        row=row,
        pipeline_result=result,
        status=status,
        predicted_entity_type=predicted_entity_type,
        classification_correct=classification_correct,
        extraction_scored=True,
        extraction_skipped=False,
        field_comparisons=comparisons,
        num_expected_fields=num_expected,
        num_predicted_fields=num_predicted,
        num_correct_fields=num_correct,
        item_completeness_score=completeness,
        full_row_exact_match=full_row_exact,
    )


def _predicted_entity_type(result: PipelineRowResult) -> str | None:
    if result.classification_output is not None:
        value = result.classification_output.get("entity_type")
        if isinstance(value, str):
            return value
    if result.envelope is not None and result.envelope.classification_log is not None:
        return result.envelope.classification_log.entity_type
    return None


def _compare_field(
    result: PipelineRowResult, expected_field: str, expected_value: str
) -> FieldComparison:
    model_field, comparison_type = _FIELD_MAP[expected_field]
    predicted_value = _predicted_value(result, model_field)
    if comparison_type == "datetime":
        normalized_expected = _parse_datetime(expected_value)
        normalized_predicted = _parse_datetime(predicted_value)
        exact_match = (
            normalized_expected is not None
            and normalized_predicted is not None
            and normalized_expected == normalized_predicted
        )
        tolerant_match = _same_day(normalized_expected, normalized_predicted)
        temporal_anomaly = _temporal_anomaly(
            normalized_expected, normalized_predicted, exact_match=exact_match
        )
    elif comparison_type == "json":
        normalized_expected = _normalize_json(expected_value)
        normalized_predicted = _normalize_json(predicted_value)
        exact_match = normalized_expected == normalized_predicted
        tolerant_match = None
        temporal_anomaly = None
    else:
        normalized_expected = _normalize_string(expected_value)
        normalized_predicted = _normalize_string(predicted_value)
        exact_match = normalized_expected == normalized_predicted
        if expected_field == "expected_location":
            tolerant_match = _contains_match(normalized_expected, normalized_predicted)
        else:
            tolerant_match = exact_match
        temporal_anomaly = None
    return FieldComparison(
        field_name=expected_field,
        expected_value=expected_value,
        predicted_value=predicted_value,
        normalized_expected_value=_jsonable(normalized_expected),
        normalized_predicted_value=_jsonable(normalized_predicted),
        exact_match=exact_match,
        tolerant_match=tolerant_match,
        temporal_anomaly=temporal_anomaly,
    )


def _predicted_value(result: PipelineRowResult, field_name: str) -> Any:
    if result.envelope is not None and hasattr(result.envelope.entity, field_name):
        return getattr(result.envelope.entity, field_name)
    if result.extraction_output is not None:
        return result.extraction_output.get(field_name)
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        candidate = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            return None
    return None


def _same_day(left: datetime | None, right: datetime | None) -> bool | None:
    if left is None or right is None:
        return False
    if left.tzinfo is not None and right.tzinfo is not None:
        right = right.astimezone(left.tzinfo)
    return left.date() == right.date()


def _temporal_anomaly(
    expected: datetime | None,
    predicted: datetime | None,
    *,
    exact_match: bool,
) -> str | None:
    if exact_match or expected is None or predicted is None:
        return None
    if expected.tzinfo is None or predicted.tzinfo is None:
        return None

    expected_utc = expected.astimezone(timezone.utc)
    predicted_utc = predicted.astimezone(timezone.utc)
    delta_seconds = round(abs((predicted_utc - expected_utc).total_seconds()))
    if delta_seconds in {3600, 7200}:
        return "timezone_offset_shift"
    return None


def _normalize_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return " ".join(str(value).split()).casefold()


def _contains_match(expected: str | None, predicted: str | None) -> bool:
    if expected is None or predicted is None:
        return False
    return expected in predicted or predicted in expected


def _normalize_json(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value.strip()
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _build_metrics(
    scored_rows: list[ScoredRow],
    *,
    cost_config: TokenCostConfig | None,
) -> dict[str, Any]:
    statuses = Counter(row.status for row in scored_rows)
    metrics = {
        "run": {
            "total_rows": len(scored_rows),
            "successful_rows": statuses["success"],
            "eval_mismatch_rows": statuses["eval_mismatch"],
            "system_error_rows": statuses["system_error"],
            "skipped_rows": statuses["skipped"],
        },
        "classification": _classification_metrics(scored_rows),
        "extraction": _extraction_metrics(scored_rows),
        "slices": _slice_metrics(scored_rows),
        "errors": _error_metrics(scored_rows),
        "performance": _performance_metrics(scored_rows, cost_config=cost_config),
    }
    return metrics


def _classification_metrics(scored_rows: list[ScoredRow]) -> dict[str, Any]:
    rows = [row for row in scored_rows if row.classification_correct is not None]
    total = len(rows)
    correct = sum(row.classification_correct is True for row in rows)
    labels = sorted(
        {
            label
            for row in rows
            for label in (row.row.gold_entity_type, row.predicted_entity_type)
            if label is not None
        }
    )
    per_class: dict[str, Any] = {}
    for label in labels:
        tp = sum(
            row.row.gold_entity_type == label and row.predicted_entity_type == label
            for row in rows
        )
        fp = sum(
            row.row.gold_entity_type != label and row.predicted_entity_type == label
            for row in rows
        )
        fn = sum(
            row.row.gold_entity_type == label and row.predicted_entity_type != label
            for row in rows
        )
        support = sum(row.row.gold_entity_type == label for row in rows)
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        f1 = _f1(precision, recall)
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }

    macro_precision = (
        statistics.mean([entry["precision"] for entry in per_class.values()])
        if per_class
        else 0.0
    )
    macro_recall = (
        statistics.mean([entry["recall"] for entry in per_class.values()])
        if per_class
        else 0.0
    )
    macro_f1 = (
        statistics.mean([entry["f1"] for entry in per_class.values()])
        if per_class
        else 0.0
    )
    support_total = sum(entry["support"] for entry in per_class.values())
    weighted_f1 = _weighted_average(per_class, "f1", support_total)
    weighted_precision = _weighted_average(per_class, "precision", support_total)
    weighted_recall = _weighted_average(per_class, "recall", support_total)

    return {
        "overall_accuracy": _safe_div(correct, total),
        "support": total,
        "per_class": per_class,
        "macro_precision": macro_precision,
        "macro_recall": macro_recall,
        "macro_f1": macro_f1,
        "weighted_precision": weighted_precision,
        "weighted_recall": weighted_recall,
        "weighted_f1": weighted_f1,
        "confusion_matrix": _confusion_matrix(rows),
    }


def _extraction_metrics(scored_rows: list[ScoredRow]) -> dict[str, Any]:
    by_dataset: dict[str, list[ScoredRow]] = defaultdict(list)
    for row in scored_rows:
        if row.num_expected_fields:
            by_dataset[row.row.dataset_name].append(row)

    metrics: dict[str, Any] = {}
    for dataset_name, rows in sorted(by_dataset.items()):
        metrics[dataset_name] = {
            "end_to_end": _extraction_mode_metrics(rows),
            "conditional": _extraction_mode_metrics(
                [row for row in rows if row.classification_correct is True]
            ),
        }
    return metrics


def _extraction_mode_metrics(rows: list[ScoredRow]) -> dict[str, Any]:
    completeness_scores = [
        row.item_completeness_score
        for row in rows
        if row.item_completeness_score is not None
    ]
    all_comparisons = [
        comparison for row in rows for comparison in row.field_comparisons
    ]
    field_names = sorted({comparison.field_name for comparison in all_comparisons})
    per_field = {}
    for field_name in field_names:
        field_comparisons = [
            comparison
            for comparison in all_comparisons
            if comparison.field_name == field_name
        ]
        per_field[field_name] = {
            "exact_accuracy": _safe_div(
                sum(comparison.exact_match for comparison in field_comparisons),
                len(field_comparisons),
            ),
            "tolerant_accuracy": _safe_div(
                sum(
                    comparison.tolerant_match is True
                    for comparison in field_comparisons
                ),
                len(field_comparisons),
            ),
            "support": len(field_comparisons),
        }
    return {
        "support": len(rows),
        "per_field": per_field,
        "item_completeness_mean": statistics.mean(completeness_scores)
        if completeness_scores
        else 0.0,
        "item_completeness_median": statistics.median(completeness_scores)
        if completeness_scores
        else 0.0,
        "full_row_exact_match_rate": _safe_div(
            sum(row.full_row_exact_match is True for row in rows), len(rows)
        ),
    }


def _slice_metrics(scored_rows: list[ScoredRow]) -> dict[str, Any]:
    dimensions = {
        "coverage_group": lambda row: row.row.coverage_group,
        "coverage_case": lambda row: row.row.coverage_case,
        "difficulty": lambda row: row.row.difficulty,
        "platform": lambda row: row.row.platform,
    }
    output: dict[str, Any] = {}
    for dimension, getter in dimensions.items():
        groups: dict[str, list[ScoredRow]] = defaultdict(list)
        for row in scored_rows:
            value = getter(row)
            if value:
                groups[str(value)].append(row)
        output[dimension] = {
            key: {
                "total_rows": len(rows),
                "success_rows": sum(row.status == "success" for row in rows),
                "eval_mismatch_rows": sum(
                    row.status == "eval_mismatch" for row in rows
                ),
                "system_error_rows": sum(row.status == "system_error" for row in rows),
                "classification_accuracy": _safe_div(
                    sum(row.classification_correct is True for row in rows),
                    sum(row.classification_correct is not None for row in rows),
                ),
            }
            for key, rows in sorted(groups.items())
        }
    return output


def _error_metrics(scored_rows: list[ScoredRow]) -> dict[str, Any]:
    error_rows = [row for row in scored_rows if row.status == "system_error"]
    counts = Counter(row.pipeline_result.error_type or "unknown" for row in error_rows)
    messages = Counter(row.pipeline_result.error_message or "" for row in error_rows)
    return {
        "total_system_errors": len(error_rows),
        "error_rate": _safe_div(len(error_rows), len(scored_rows)),
        "counts_by_type": dict(sorted(counts.items())),
        "top_repeated_error_messages": [
            {"message": message, "count": count}
            for message, count in messages.most_common(10)
            if message
        ],
    }


def _performance_metrics(
    scored_rows: list[ScoredRow],
    *,
    cost_config: TokenCostConfig | None,
) -> dict[str, Any]:
    row_latencies = [
        row.pipeline_result.processing_time_ms
        for row in scored_rows
        if row.pipeline_result.processing_time_ms is not None
    ]
    classification_latencies = [
        row.pipeline_result.classification_time_ms
        for row in scored_rows
        if row.pipeline_result.classification_time_ms is not None
    ]
    extraction_latencies = [
        row.pipeline_result.extract_time_ms
        for row in scored_rows
        if row.pipeline_result.extract_time_ms is not None
    ]
    classification_usage = _sum_usage_dicts(
        [
            row.pipeline_result.classification_usage
            for row in scored_rows
            if row.pipeline_result.classification_usage
        ]
    )
    extraction_usage = _sum_usage_dicts(
        [
            row.pipeline_result.extraction_usage
            for row in scored_rows
            if row.pipeline_result.extraction_usage
        ]
    )
    total_usage = _sum_usage_dicts([classification_usage, extraction_usage])

    return {
        "row_latency_ms": _latency_stats(row_latencies),
        "classification_latency_ms": _latency_stats(classification_latencies),
        "extraction_latency_ms": _latency_stats(extraction_latencies),
        "token_usage": {
            "classification": classification_usage,
            "extraction": extraction_usage,
            "total": total_usage,
        },
        "cost": _cost_metrics(total_usage, cost_config),
    }


def _confusion_matrix(rows: list[ScoredRow]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        gold = row.row.gold_entity_type
        predicted = row.predicted_entity_type or "<none>"
        matrix[gold][predicted] += 1
    return {
        gold: dict(sorted(predicted.items()))
        for gold, predicted in sorted(matrix.items())
    }


def _latency_stats(values: list[int]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "min": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }

    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": statistics.mean(ordered),
        "min": ordered[0],
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1],
    }


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    index = max(0, math.ceil(q * len(values)) - 1)
    return values[index]


def _sum_usage_dicts(items: list[dict[str, Any] | None]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        if not item:
            continue
        result = _merge_usage_dicts(result, item)
    return result


def _merge_usage_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if isinstance(value, dict):
            existing = merged.get(key)
            if isinstance(existing, dict):
                merged[key] = _merge_usage_dicts(existing, value)
            else:
                merged[key] = _merge_usage_dicts({}, value)
        elif isinstance(value, (int, float)):
            merged[key] = merged.get(key, 0) + value
        elif key not in merged:
            merged[key] = value
    return merged


def _cost_metrics(
    total_usage: dict[str, Any], cost_config: TokenCostConfig | None
) -> dict[str, Any]:
    prompt_tokens = _usage_number(total_usage, "prompt_tokens")
    completion_tokens = _usage_number(total_usage, "completion_tokens")
    estimated_total = None
    pricing = None
    if cost_config is not None:
        estimated_total = (
            prompt_tokens * cost_config.input_price_usd_per_1m_tokens
            + completion_tokens * cost_config.output_price_usd_per_1m_tokens
        ) / 1_000_000
        pricing = {
            "input_price_usd_per_1m_tokens": cost_config.input_price_usd_per_1m_tokens,
            "output_price_usd_per_1m_tokens": cost_config.output_price_usd_per_1m_tokens,
        }
    return {
        "estimated_total_usd": estimated_total,
        "pricing": pricing,
    }


def _usage_number(usage: dict[str, Any], key: str) -> float:
    value = usage.get(key, 0)
    if isinstance(value, (int, float)):
        return value
    return 0.0


def _weighted_average(per_class: dict[str, Any], key: str, support_total: int) -> float:
    if support_total == 0:
        return 0.0
    return (
        sum(entry[key] * entry["support"] for entry in per_class.values())
        / support_total
    )


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0
