from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Final


class DatasetValidationError(ValueError):
    """Raised when an evaluation CSV violates the expected contract."""


@dataclass(frozen=True, slots=True)
class EvalRow:
    row_id: str
    dataset_name: str
    source_file: str
    external_id: str | None
    platform: str
    course_name: str
    published_at: str
    body: str
    gold_entity_type: str
    gold_fields: dict[str, str]
    difficulty: str | None
    coverage_group: str | None
    coverage_case: str | None
    slice_tags: list[str]
    notes: str | None
    order_index: int


CLASSIFICATION_DATASET = "classification"
DATASET_CHOICES: Final[tuple[str, ...]] = (
    "classification",
    "assignment",
    "event",
    "announcement",
    "material",
    "all",
)

_MESSAGE_INDEX_FILE: Final = "message_index.csv"
_CLASSIFICATION_REQUIRED: Final[frozenset[str]] = frozenset(
    {
        "id",
        "external_id",
        "platform",
        "course_name",
        "published_at",
        "body",
        "entity_type_gold",
        "difficulty",
        "coverage_group",
        "coverage_case",
        "slice_tags_json",
        "notes",
    }
)
_EXTRACTION_SCHEMAS: Final[dict[str, tuple[str, frozenset[str], tuple[str, ...]]]] = {
    "assignment": (
        "assignment_extraction.csv",
        frozenset(
            {"id", "course_name", "published_at", "body", "expected_deadline", "notes"}
        ),
        ("expected_deadline",),
    ),
    "event": (
        "event_extraction.csv",
        frozenset(
            {
                "id",
                "course_name",
                "published_at",
                "body",
                "expected_start_time",
                "expected_end_time",
                "expected_location",
                "notes",
            }
        ),
        ("expected_start_time", "expected_end_time", "expected_location"),
    ),
    "announcement": (
        "announcement_extraction.csv",
        frozenset(
            {
                "id",
                "course_name",
                "published_at",
                "body",
                "expected_title",
                "expected_dates_json",
                "notes",
            }
        ),
        ("expected_title", "expected_dates_json"),
    ),
    "material": (
        "material_extraction.csv",
        frozenset(
            {
                "id",
                "course_name",
                "published_at",
                "body",
                "expected_title",
                "expected_dates_json",
                "notes",
            }
        ),
        ("expected_title", "expected_dates_json"),
    ),
}


def load_eval_rows(data_dir: str | Path, *, dataset: str = "all") -> list[EvalRow]:
    """Load evaluation rows and validate the dataset contract."""

    if dataset not in DATASET_CHOICES:
        raise DatasetValidationError(
            f"Unknown dataset {dataset!r}; expected one of {', '.join(DATASET_CHOICES)}"
        )

    root = Path(data_dir)
    message_rows = _read_csv(root / _MESSAGE_INDEX_FILE, _CLASSIFICATION_REQUIRED)
    _reject_duplicate_ids(message_rows, _MESSAGE_INDEX_FILE)
    message_by_id = {row["id"]: row for row in message_rows}

    loaded: list[EvalRow] = []
    if dataset in ("classification", "all"):
        loaded.extend(_classification_rows(message_rows, start_index=len(loaded)))

    extraction_datasets = tuple(_EXTRACTION_SCHEMAS) if dataset == "all" else (dataset,)
    for extraction_dataset in extraction_datasets:
        if extraction_dataset == "classification":
            continue
        loaded.extend(
            _extraction_rows(
                root,
                extraction_dataset,
                message_by_id,
                start_index=len(loaded),
                required=dataset != "all",
            )
        )
    return loaded


def apply_row_selection(
    rows: list[EvalRow],
    *,
    ids: list[str] | None,
    offset: int,
    limit: int | None,
    shuffle: bool = False,
    seed: int | None = None,
) -> list[EvalRow]:
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")

    selected = rows
    if ids:
        wanted = set(ids)
        selected = [row for row in selected if row.row_id in wanted]

    selected = list(selected)
    if shuffle:
        random.Random(seed).shuffle(selected)

    if offset:
        selected = selected[offset:]
    if limit is not None:
        selected = selected[:limit]
    return selected


def _read_csv(path: Path, required_columns: frozenset[str]) -> list[dict[str, str]]:
    if not path.exists():
        raise DatasetValidationError(f"Missing evaluation CSV: {path}")

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        if "author" in fieldnames:
            raise DatasetValidationError(
                f"{path.name} contains forbidden author column; remove private author metadata"
            )
        missing = required_columns.difference(fieldnames)
        if missing:
            missing_text = ", ".join(sorted(missing))
            raise DatasetValidationError(
                f"{path.name} missing required columns: {missing_text}"
            )
        rows = [dict(row) for row in reader]

    return rows


def _reject_duplicate_ids(rows: list[dict[str, str]], source_file: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in rows:
        row_id = _required_value(row, "id", source_file)
        if row_id in seen:
            duplicates.add(row_id)
        seen.add(row_id)
    if duplicates:
        raise DatasetValidationError(
            f"{source_file} contains duplicate ids: {', '.join(sorted(duplicates))}"
        )


def _classification_rows(
    rows: list[dict[str, str]], *, start_index: int
) -> list[EvalRow]:
    return [
        _row_from_message_index(
            row,
            dataset_name=CLASSIFICATION_DATASET,
            source_file=_MESSAGE_INDEX_FILE,
            gold_fields={},
            order_index=start_index + index,
        )
        for index, row in enumerate(rows)
    ]


def _extraction_rows(
    root: Path,
    dataset_name: str,
    message_by_id: dict[str, dict[str, str]],
    *,
    start_index: int,
    required: bool,
) -> list[EvalRow]:
    schema = _EXTRACTION_SCHEMAS.get(dataset_name)
    if schema is None:
        raise DatasetValidationError(f"Unsupported extraction dataset: {dataset_name}")

    source_file, required_columns, expected_fields = schema
    path = root / source_file
    if not path.exists() and not required:
        return []
    extraction_rows = _read_csv(path, required_columns)
    _reject_duplicate_ids(extraction_rows, source_file)

    loaded: list[EvalRow] = []
    for index, extraction_row in enumerate(extraction_rows):
        row_id = _required_value(extraction_row, "id", source_file)
        message_row = message_by_id.get(row_id)
        if message_row is None:
            raise DatasetValidationError(
                f"{source_file} row {row_id!r} has no matching message_index.csv row"
            )
        gold_fields = {
            field: value.strip()
            for field in expected_fields
            if (value := extraction_row.get(field, "").strip())
        }
        loaded.append(
            _row_from_message_index(
                message_row,
                dataset_name=dataset_name,
                source_file=source_file,
                gold_fields=gold_fields,
                notes=extraction_row.get("notes") or message_row.get("notes"),
                order_index=start_index + index,
            )
        )
    return loaded


def _row_from_message_index(
    row: dict[str, str],
    *,
    dataset_name: str,
    source_file: str,
    gold_fields: dict[str, str],
    order_index: int,
    notes: str | None = None,
) -> EvalRow:
    source_label = source_file
    row_id = _required_value(row, "id", source_label)
    return EvalRow(
        row_id=row_id,
        dataset_name=dataset_name,
        source_file=source_file,
        external_id=row.get("external_id") or row_id,
        platform=_required_value(row, "platform", source_label),
        course_name=_required_value(row, "course_name", source_label),
        published_at=_required_value(row, "published_at", source_label),
        body=_required_value(row, "body", source_label),
        gold_entity_type=_required_value(row, "entity_type_gold", source_label),
        gold_fields=gold_fields,
        difficulty=row.get("difficulty") or None,
        coverage_group=row.get("coverage_group") or None,
        coverage_case=row.get("coverage_case") or None,
        slice_tags=_parse_slice_tags(row.get("slice_tags_json", "[]"), row_id),
        notes=notes if notes is not None else row.get("notes") or None,
        order_index=order_index,
    )


def _required_value(row: dict[str, str], field: str, source_file: str) -> str:
    value = row.get(field, "")
    if value == "":
        raise DatasetValidationError(
            f"{source_file} has empty required value for {field}"
        )
    return value


def _parse_slice_tags(value: str, row_id: str) -> list[str]:
    if not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DatasetValidationError(
            f"message_index.csv row {row_id!r} has invalid slice_tags_json"
        ) from exc
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise DatasetValidationError(
            f"message_index.csv row {row_id!r} slice_tags_json must be a list of strings"
        )
    return parsed
