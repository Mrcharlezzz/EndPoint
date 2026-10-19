from __future__ import annotations

import csv
from pathlib import Path

import pytest

from evaluation.eval.loader import (
    DatasetValidationError,
    apply_row_selection,
    load_eval_rows,
)


MESSAGE_FIELDS = [
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
]
EVAL_DATA_DIR = Path(__file__).parents[1] / "telegram_llm"


def _require_eval_files(*filenames: str) -> None:
    if any(not (EVAL_DATA_DIR / filename).is_file() for filename in filenames):
        pytest.skip("Private evaluation dataset is not available")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_message_index(
    data_dir: Path, *, extra_fields: list[str] | None = None
) -> None:
    fields = MESSAGE_FIELDS + (extra_fields or [])
    _write_csv(
        data_dir / "message_index.csv",
        fields,
        [
            {
                "id": "row-assignment",
                "external_id": "ext-assignment",
                "platform": "telegram",
                "course_name": "Algorithms",
                "published_at": "2026-03-01T09:00:00Z",
                "body": "Homework due tomorrow at 23:59",
                "entity_type_gold": "assignment",
                "difficulty": "easy",
                "coverage_group": "assignment_deadline",
                "coverage_case": "relative_deadline",
                "slice_tags_json": '["relative_date"]',
                "notes": "clear deadline",
                **({"author": "Private Person"} if extra_fields else {}),
            }
        ],
    )


def test_load_eval_rows_joins_extraction_rows_to_message_index(tmp_path: Path) -> None:
    _write_message_index(tmp_path)
    _write_csv(
        tmp_path / "assignment_extraction.csv",
        ["id", "course_name", "published_at", "body", "expected_deadline", "notes"],
        [
            {
                "id": "row-assignment",
                "course_name": "Algorithms",
                "published_at": "2026-03-01T09:00:00Z",
                "body": "Homework due tomorrow at 23:59",
                "expected_deadline": "2026-03-02T23:59:00+01:00",
                "notes": "clear deadline",
            }
        ],
    )

    rows = load_eval_rows(tmp_path, dataset="assignment")

    assert len(rows) == 1
    row = rows[0]
    assert row.row_id == "row-assignment"
    assert row.dataset_name == "assignment"
    assert row.source_file == "assignment_extraction.csv"
    assert row.platform == "telegram"
    assert row.external_id == "ext-assignment"
    assert row.gold_entity_type == "assignment"
    assert row.gold_fields == {"expected_deadline": "2026-03-02T23:59:00+01:00"}
    assert row.slice_tags == ["relative_date"]


def test_telegram_event_dataset_includes_temporal_coverage_cases() -> None:
    _require_eval_files("message_index.csv", "event_extraction.csv")
    rows = load_eval_rows(EVAL_DATA_DIR, dataset="event")
    temporal_rows = [
        row
        for row in rows
        if row.coverage_group in {"relative_time_extraction", "time_control"}
    ]

    assert [row.coverage_group for row in temporal_rows].count(
        "relative_time_extraction"
    ) == 0
    assert [row.coverage_group for row in temporal_rows].count("time_control") == 2
    assert all(row.coverage_case == row.coverage_group for row in temporal_rows)
    assert {row.gold_fields["expected_start_time"] for row in temporal_rows} == {
        "2026-04-17T14:00:00+02:00",
        "2026-03-19T09:45:00+01:00",
    }


def test_classification_dataset_labels_immediate_temporal_reminders_as_raw() -> None:
    _require_eval_files("message_index.csv")
    rows = load_eval_rows(EVAL_DATA_DIR, dataset="classification")
    rows_by_id = {row.row_id: row for row in rows}

    expected_raw_ids = {
        "manual_temporal_relative_in_5_minutes",
        "manual_temporal_relative_in_2_hours",
        "manual_temporal_relative_10_minutes_from_now",
        "manual_temporal_explicit_today_at_0945",
    }

    assert expected_raw_ids.issubset(rows_by_id)
    assert {rows_by_id[row_id].gold_entity_type for row_id in expected_raw_ids} == {
        "raw_message"
    }
    assert {rows_by_id[row_id].coverage_group for row_id in expected_raw_ids} == {
        "raw_ephemeral_reminder"
    }


def test_classification_dataset_includes_integration_raw_message_regressions() -> None:
    _require_eval_files("message_index.csv")
    rows = load_eval_rows(EVAL_DATA_DIR, dataset="classification")
    rows_by_id = {row.row_id: row for row in rows}

    expected_raw_ids = {
        "tg_3701371409_86",
        "tg_3766025133_341",
        "tg_3736855178_313",
        "tg_3736855178_331",
        "tg_3766025133_340",
        "tg_3747483133_91",
        "tg_3766025133_347",
        "tg_3709134149_147",
        "tg_3709134149_151",
        "tg_3709134149_154",
        "tg_3709134149_156",
        "tg_3709134149_157",
    }

    assert expected_raw_ids.issubset(rows_by_id)
    assert {rows_by_id[row_id].gold_entity_type for row_id in expected_raw_ids} == {
        "raw_message"
    }
    assert rows_by_id["tg_3709134149_106"].gold_entity_type == "assignment"


def test_loader_rejects_author_column_in_eval_csv(tmp_path: Path) -> None:
    _write_message_index(tmp_path, extra_fields=["author"])

    with pytest.raises(DatasetValidationError, match="author"):
        load_eval_rows(tmp_path, dataset="classification")


def test_apply_row_selection_filters_shuffles_and_limits_stably(tmp_path: Path) -> None:
    _write_csv(
        tmp_path / "message_index.csv",
        MESSAGE_FIELDS,
        [
            {
                "id": f"row-{index}",
                "external_id": f"ext-{index}",
                "platform": "telegram",
                "course_name": "Algorithms",
                "published_at": "2026-03-01T09:00:00Z",
                "body": f"Message {index}",
                "entity_type_gold": "announcement",
                "difficulty": "easy",
                "coverage_group": "announcement",
                "coverage_case": "plain",
                "slice_tags_json": "[]",
                "notes": "",
            }
            for index in range(6)
        ],
    )
    rows = load_eval_rows(tmp_path, dataset="classification")

    first = apply_row_selection(rows, ids=None, offset=1, limit=3, shuffle=True, seed=7)
    second = apply_row_selection(
        rows, ids=None, offset=1, limit=3, shuffle=True, seed=7
    )

    assert [row.row_id for row in first] == [row.row_id for row in second]
    assert len(first) == 3
    assert [
        row.row_id
        for row in apply_row_selection(rows, ids=["row-4"], offset=0, limit=None)
    ] == ["row-4"]
