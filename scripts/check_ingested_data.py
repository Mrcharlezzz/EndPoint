from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

EXPECTED_PLATFORMS = ("teams", "moodle", "telegram")
REQUIRED_COURSE_PLATFORMS = {"teams", "moodle"}
PUBLISHED_AT_MIN_COVERAGE = 0.90
COURSE_MIN_COVERAGE = 0.90
OUT_OF_WINDOW_CRITICAL_RATE = 0.25
SAMPLE_SIZE = 5
KNOWN_NOISE_TERMS = ("quiz is out", "east hall", "see you", "open the door")
BLOB_TERMS = ("scopeId", "storageId", "callId", "Meeting in GeneralPlay")
LOW_VALUE_RAW_MESSAGES = {
    ".",
    "^",
    "edit",
    "huh",
    "idk",
    "none",
    "ok",
    "ok!",
    "same",
    "wdym",
    "yeah",
    "yep",
    "yes",
    "yup",
}


@dataclass(frozen=True)
class IngestedRow:
    external_id: str
    platform: str
    course_external_id: str | None
    payload: dict[str, Any]
    created_at: str
    updated_at: str
    processing_status: str


@dataclass(frozen=True)
class IngestionReport:
    passed: bool
    critical_failures: list[str]
    warnings: list[str]
    metrics: dict[str, Any]
    markdown: str


def build_report(
    rows: list[IngestedRow],
    *,
    now: datetime,
    window_days: int,
    allowed_empty_platforms: set[str],
    all_rows: list[IngestedRow] | None = None,
) -> IngestionReport:
    all_rows = rows if all_rows is None else all_rows
    generated_at = _format_datetime(now)
    cutoff = now - timedelta(days=window_days)
    critical_failures: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {
        "generated_at": generated_at,
        "window_days": window_days,
        "window_start": _format_datetime(cutoff),
        "allowed_empty_platforms": sorted(allowed_empty_platforms),
        "total_items": len(rows),
        "processing_status": _count_by_processing_status(rows),
        "unexpected_platforms": sorted(
            {row.platform for row in rows} - set(EXPECTED_PLATFORMS)
        ),
        "platforms": {},
        "seed_refresh": _seed_refresh_metrics(rows, all_rows),
    }

    if not rows:
        critical_failures.append("No ingested_items rows found.")
    if metrics["unexpected_platforms"]:
        critical_failures.append(
            "Unexpected platform values found: "
            + ", ".join(metrics["unexpected_platforms"])
        )

    for platform in EXPECTED_PLATFORMS:
        platform_rows = [row for row in rows if row.platform == platform]
        platform_metrics = _platform_metrics(platform, platform_rows, cutoff)
        metrics["platforms"][platform] = platform_metrics

        if not platform_rows:
            if platform not in allowed_empty_platforms:
                critical_failures.append(f"Platform {platform} has 0 items.")
            continue

        coverage = platform_metrics["published_at"]["coverage"]
        if coverage < PUBLISHED_AT_MIN_COVERAGE:
            critical_failures.append(
                f"Platform {platform} published_at coverage {_format_percent(coverage)} "
                f"is below required {_format_percent(PUBLISHED_AT_MIN_COVERAGE)}."
            )

        out_of_window_rate = platform_metrics["window"]["out_of_window_rate"]
        if out_of_window_rate > OUT_OF_WINDOW_CRITICAL_RATE:
            critical_failures.append(
                f"Platform {platform} has {_format_percent(out_of_window_rate)} "
                f"of timestamped items outside the requested {window_days}-day window."
            )

        empty_payloads = platform_metrics["sample_sanity"]["empty_payloads"]
        if empty_payloads > 0:
            critical_failures.append(
                f"Platform {platform} has {empty_payloads} rows with empty payload."
            )

        course_resolution = platform_metrics["course_resolution"]
        if (
            course_resolution["required"]
            and course_resolution["rate"] < COURSE_MIN_COVERAGE
        ):
            critical_failures.append(
                f"Platform {platform} course resolution "
                f"{_format_percent(course_resolution['rate'])} is below required "
                f"{_format_percent(COURSE_MIN_COVERAGE)}."
            )

        invalid_dates = platform_metrics["published_at"]["invalid"]
        if invalid_dates > 0:
            warnings.append(
                f"Platform {platform} has {invalid_dates} rows with invalid published_at values."
            )

    metrics["passed"] = not critical_failures
    metrics["critical_failures"] = critical_failures
    metrics["warnings"] = warnings
    markdown = render_markdown(metrics)
    return IngestionReport(
        passed=not critical_failures,
        critical_failures=critical_failures,
        warnings=warnings,
        metrics=metrics,
        markdown=markdown,
    )


def render_markdown(metrics: dict[str, Any]) -> str:
    result = "PASS" if metrics["passed"] else "FAIL"
    lines = [
        "# Ingestion Data Quality Report",
        "",
        f"- Generated: `{metrics['generated_at']}`",
        f"- Window: last `{metrics['window_days']}` days since `{metrics['window_start']}`",
        f"- Result: **{result}**",
        f"- Total rows: `{metrics['total_items']}`",
        "",
        "## Critical Failures",
        "",
    ]

    if metrics["critical_failures"]:
        lines.extend(f"- {failure}" for failure in metrics["critical_failures"])
    else:
        lines.append("- None")

    lines.extend(["", "## Warnings", ""])
    if metrics["warnings"]:
        lines.extend(f"- {warning}" for warning in metrics["warnings"])
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## Platform Summary",
            "",
            "| Platform | Rows | Published At | In Window | Out Of Window | Missing/Invalid | Course Resolution |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for platform in EXPECTED_PLATFORMS:
        platform_metrics = metrics["platforms"][platform]
        published = platform_metrics["published_at"]
        window = platform_metrics["window"]
        course = platform_metrics["course_resolution"]
        lines.append(
            "| "
            f"{platform} | "
            f"{platform_metrics['total']} | "
            f"{_format_percent(published['coverage'])} | "
            f"{window['in_window']} | "
            f"{window['out_of_window']} | "
            f"{window['missing_published_at'] + window['invalid_published_at']} | "
            f"{_format_percent(course['rate'])}"
            " |"
        )

    lines.extend(
        [
            "",
            "## Date Ranges",
            "",
            "| Platform | Min Published At | Max Published At | Out Of Window Rate |",
            "| --- | --- | --- | ---: |",
        ]
    )
    for platform in EXPECTED_PLATFORMS:
        platform_metrics = metrics["platforms"][platform]
        date_range = platform_metrics["date_range"]
        window = platform_metrics["window"]
        lines.append(
            "| "
            f"{platform} | "
            f"{date_range['min_published_at'] or '-'} | "
            f"{date_range['max_published_at'] or '-'} | "
            f"{_format_percent(window['out_of_window_rate'])}"
            " |"
        )

    lines.extend(["", "## Processing Status", ""])
    if metrics["processing_status"]:
        lines.extend(
            f"- `{status}`: `{count}`"
            for status, count in sorted(metrics["processing_status"].items())
        )
    else:
        lines.append("- None")

    seed = metrics["seed_refresh"]
    lines.extend(
        [
            "",
            "## Seed Refresh Checks",
            "",
            "### Selected Rows",
            "",
            f"- Rows in this report scope: `{seed['selected']['total']}`",
            f"- Pending rows: `{seed['selected']['pending']}`",
            f"- Processed rows: `{seed['selected']['processed']}`",
            f"- Manually skipped rows: `{seed['selected']['manually_skipped']}`",
            f"- Fresh v5 candidates, approximated as pending rows: `{seed['selected']['pending']}`",
            f"- Excluded rows, approximated as manually skipped rows: `{seed['all_statuses'].get('manually_skipped', 0)}`",
            "",
            "### Counts By Content Type",
            "",
        ]
    )
    lines.extend(_markdown_counts(seed["by_content_type"]))
    lines.extend(["", "### Counts By Course", ""])
    lines.extend(_markdown_counts(seed["by_course"], limit=40))
    lines.extend(["", "### Duplicate External IDs", ""])
    lines.append(f"- In report scope: `{seed['duplicates']['selected_total']}` rows")
    lines.append(f"- Across all statuses: `{seed['duplicates']['all_total']}` rows")
    if seed["duplicates"]["examples"]:
        lines.extend(
            f"- `{item['external_id']}`: `{item['count']}` rows"
            for item in seed["duplicates"]["examples"]
        )
    lines.extend(["", "### Scope Flags", ""])
    lines.extend(_markdown_issue_group(seed["scope_flags"]))
    lines.extend(["", "### Known Noise Searches", ""])
    lines.extend(_markdown_issue_group(seed["known_noise"]))
    lines.extend(["", "### Raw Message Quality Flags", ""])
    lines.extend(_markdown_issue_group(seed["raw_message_quality"]))

    lines.extend(["", "## Samples", ""])
    for platform in EXPECTED_PLATFORMS:
        lines.extend(
            [
                f"### {platform}",
                "",
                "| External ID | Type | Title | Course | Published At |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        samples = metrics["platforms"][platform]["samples"]
        if samples:
            for sample in samples:
                lines.append(
                    "| "
                    f"{_markdown_cell(sample['external_id'])} | "
                    f"{_markdown_cell(sample['content_type'])} | "
                    f"{_markdown_cell(sample['title'])} | "
                    f"{_markdown_cell(sample['course'])} | "
                    f"{_markdown_cell(sample['published_at'])}"
                    " |"
                )
        else:
            lines.append("| - | - | - | - | - |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def terminal_summary(
    report: IngestionReport, report_path: Path, metrics_path: Path
) -> str:
    result = "PASS" if report.passed else "FAIL"
    lines = [
        f"Ingestion data check: {result}",
        f"Report: {report_path}",
        f"Metrics: {metrics_path}",
        "",
    ]
    for platform in EXPECTED_PLATFORMS:
        platform_metrics = report.metrics["platforms"][platform]
        published = platform_metrics["published_at"]
        window = platform_metrics["window"]
        course = platform_metrics["course_resolution"]
        lines.append(
            f"{platform}: {platform_metrics['total']} rows; "
            f"published_at {_format_percent(published['coverage'])}; "
            f"in-window {window['in_window']}; "
            f"out-of-window {window['out_of_window']}; "
            f"course {_format_percent(course['rate'])}"
        )

    seed = report.metrics["seed_refresh"]
    lines.extend(
        [
            "",
            "Seed refresh checks:",
            f"- scope rows: {seed['selected']['total']}; "
            f"pending {seed['selected']['pending']}; "
            f"processed {seed['selected']['processed']}; "
            f"manually_skipped {seed['selected']['manually_skipped']}",
            f"- duplicate external_id rows in scope: {seed['duplicates']['selected_total']}; "
            f"across all statuses: {seed['duplicates']['all_total']}",
            f"- scope-flag hits: {_issue_total(seed['scope_flags'])}",
            f"- known-noise hits: {_issue_total(seed['known_noise'])}",
            f"- raw-message quality hits: {_issue_total(seed['raw_message_quality'])}",
        ]
    )

    if report.critical_failures:
        lines.extend(["", "Critical failures:"])
        lines.extend(f"- {failure}" for failure in report.critical_failures)

    if report.warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in report.warnings)

    return "\n".join(lines)


def write_artifacts(
    report: IngestionReport,
    *,
    output_root: Path,
    timestamp: str,
) -> tuple[Path, Path]:
    output_dir = output_root / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.md"
    metrics_path = output_dir / "metrics.json"
    report_path.write_text(report.markdown, encoding="utf-8")
    metrics_path.write_text(
        json.dumps(report.metrics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report_path, metrics_path


def fetch_rows_from_db(repo_root: Path) -> list[IngestedRow]:
    postgres_db = _read_config_value(repo_root, "POSTGRES_DB")
    postgres_user = _read_config_value(repo_root, "POSTGRES_USER")
    if not postgres_db or not postgres_user:
        raise ScriptError(
            "POSTGRES_DB and POSTGRES_USER must be set in the environment or .env"
        )

    sql = """
SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'external_id', external_id,
                'platform', platform,
                'course_external_id', course_external_id,
                'payload', payload,
                'created_at', created_at,
                'updated_at', updated_at,
                'processing_status', processing_status
            )
            ORDER BY platform, created_at, external_id
        ),
        '[]'::jsonb
    )::text
FROM ingested_items
"""
    command = [
        "docker",
        "compose",
        "-f",
        str(repo_root / "docker-compose.yml"),
        "exec",
        "-T",
        "db",
        "psql",
        "--username",
        postgres_user,
        "--dbname",
        postgres_db,
        "--no-align",
        "--tuples-only",
        "--quiet",
        "--command",
        sql,
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        message = (
            result.stderr.strip() or result.stdout.strip() or "unknown psql failure"
        )
        raise ScriptError(f"Database query failed: {message}")

    try:
        raw_rows = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError as exc:
        raise ScriptError(f"Database query returned invalid JSON: {exc}") from exc

    if not isinstance(raw_rows, list):
        raise ScriptError("Database query returned JSON that was not a list")

    return [_row_from_mapping(raw_row) for raw_row in raw_rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check ingested_items data quality before taking a pre-process snapshot."
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=60,
        help="Number of recent days expected to be covered by ingestion.",
    )
    parser.add_argument(
        "--allow-empty-platform",
        action="append",
        choices=EXPECTED_PLATFORMS,
        default=[],
        help="Platform that may be empty without failing; repeatable.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Directory for report artifacts. Defaults to data/reports/ingestion.",
    )
    parser.add_argument(
        "--status",
        action="append",
        default=[],
        help=(
            "Restrict the main report scope to rows with this processing_status. "
            "Repeatable. Seed-refresh cross-status checks still inspect all rows."
        ),
    )
    args = parser.parse_args(argv)

    if args.window_days < 1:
        print("--window-days must be at least 1", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    output_root = args.output_root or repo_root / "data" / "reports" / "ingestion"
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    try:
        all_rows = fetch_rows_from_db(repo_root)
        rows = (
            [row for row in all_rows if row.processing_status in set(args.status)]
            if args.status
            else all_rows
        )
        now = datetime.now(UTC)
        report = build_report(
            rows,
            now=now,
            window_days=args.window_days,
            allowed_empty_platforms=set(args.allow_empty_platform),
            all_rows=all_rows,
        )
        report_path, metrics_path = write_artifacts(
            report,
            output_root=output_root,
            timestamp=timestamp,
        )
    except (OSError, ScriptError) as exc:
        print(f"Script error: {exc}", file=sys.stderr)
        return 2

    print(terminal_summary(report, report_path, metrics_path))
    return 0 if report.passed else 1


class ScriptError(Exception):
    """A configuration or database access problem prevented the check from running."""


def _seed_refresh_metrics(
    selected_rows: list[IngestedRow],
    all_rows: list[IngestedRow],
) -> dict[str, Any]:
    return {
        "selected": {
            "total": len(selected_rows),
            "pending": _count_status(selected_rows, "pending"),
            "processed": _count_status(selected_rows, "processed"),
            "manually_skipped": _count_status(selected_rows, "manually_skipped"),
        },
        "all_statuses": _count_by_processing_status(all_rows),
        "by_content_type": _count_by_payload_key(selected_rows, "content_type"),
        "by_course": _count_by_course(selected_rows),
        "duplicates": {
            "selected_total": _duplicate_total(selected_rows),
            "all_total": _duplicate_total(all_rows),
            "examples": _duplicate_examples(all_rows),
        },
        "known_noise": {
            term: _issue_metrics(
                selected_rows,
                lambda row, term=term: term in _search_text(row).lower(),
            )
            for term in KNOWN_NOISE_TERMS
        },
        "scope_flags": {
            "fall_2025_or_f25": _issue_metrics(selected_rows, _is_fall_2025_row),
            "s26_ca_999": _issue_metrics(selected_rows, _is_ca999_row),
            "housing_team": _issue_metrics(
                selected_rows,
                lambda row: "housing team" in _course_text(row).lower(),
            ),
            "bsc_cs_students": _issue_metrics(
                selected_rows,
                lambda row: "bsc cs students" in _course_text(row).lower(),
            ),
            "club_or_society": _issue_metrics(
                selected_rows,
                lambda row: "club" in _course_text(row).lower()
                or "society" in _course_text(row).lower(),
            ),
            "missing_course": _issue_metrics(
                selected_rows,
                lambda row: not _has_course(row),
            ),
        },
        "raw_message_quality": {
            "empty_raw_message_body": _issue_metrics(
                selected_rows,
                lambda row: _is_raw_message(row) and not _has_text(row.payload.get("body")),
            ),
            "low_value_short_raw_message": _issue_metrics(
                selected_rows,
                lambda row: _is_low_value_raw_message(row),
            ),
            "teams_json_blob": _issue_metrics(
                selected_rows,
                lambda row: row.platform == "teams" and _looks_like_json_blob(row),
            ),
            "teams_meeting_placeholder": _issue_metrics(
                selected_rows,
                lambda row: row.platform == "teams"
                and "meeting in generalplay" in _search_text(row).lower(),
            ),
            "teams_blob_terms": {
                term: _issue_metrics(
                    selected_rows,
                    lambda row, term=term: row.platform == "teams"
                    and term.lower() in _search_text(row).lower(),
                )
                for term in BLOB_TERMS
            },
        },
    }


def _issue_metrics(
    rows: list[IngestedRow],
    predicate: Any,
    *,
    sample_size: int = SAMPLE_SIZE,
) -> dict[str, Any]:
    matched = [row for row in rows if predicate(row)]
    return {
        "count": len(matched),
        "samples": [_issue_sample(row) for row in matched[:sample_size]],
    }


def _issue_sample(row: IngestedRow) -> dict[str, str | None]:
    return {
        "external_id": row.external_id,
        "platform": row.platform,
        "content_type": _string_or_none(row.payload.get("content_type")),
        "course": _display_course(row),
        "published_at": _string_or_none(row.payload.get("published_at")),
        "title": _truncate(_string_or_none(row.payload.get("title")), 80),
        "body": _truncate(_string_or_none(row.payload.get("body")), 140),
    }


def _count_status(rows: list[IngestedRow], status: str) -> int:
    return sum(1 for row in rows if row.processing_status == status)


def _count_by_payload_key(rows: list[IngestedRow], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = _string_or_none(row.payload.get(key)) or "<missing>"
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _count_by_course(rows: list[IngestedRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = _display_course(row) or "<missing>"
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _duplicate_total(rows: list[IngestedRow]) -> int:
    counts = _external_id_counts(rows)
    return sum(count for count in counts.values() if count > 1)


def _duplicate_examples(rows: list[IngestedRow]) -> list[dict[str, Any]]:
    counts = _external_id_counts(rows)
    return [
        {"external_id": external_id, "count": count}
        for external_id, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
        if count > 1
    ][:SAMPLE_SIZE]


def _external_id_counts(rows: list[IngestedRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.external_id] = counts.get(row.external_id, 0) + 1
    return counts


def _is_fall_2025_row(row: IngestedRow) -> bool:
    text = f"{row.course_external_id or ''} {_course_text(row)}".lower()
    return "fall 2025" in text or "f25_" in text


def _is_ca999_row(row: IngestedRow) -> bool:
    text = f"{row.course_external_id or ''} {_course_text(row)}".lower()
    return "s26_ca-999" in text


def _is_raw_message(row: IngestedRow) -> bool:
    return row.payload.get("content_type") == "raw_message"


def _is_low_value_raw_message(row: IngestedRow) -> bool:
    if not _is_raw_message(row):
        return False
    body = _string_or_none(row.payload.get("body"))
    if body is None:
        return False
    normalized = " ".join(body.strip().lower().split())
    return normalized in LOW_VALUE_RAW_MESSAGES or (
        len(normalized) <= 25 and normalized in LOW_VALUE_RAW_MESSAGES
    )


def _looks_like_json_blob(row: IngestedRow) -> bool:
    body = _string_or_none(row.payload.get("body"))
    if body is None:
        return False
    stripped = body.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _search_text(row: IngestedRow) -> str:
    return " ".join(
        part
        for part in (
            _string_or_none(row.payload.get("title")),
            _string_or_none(row.payload.get("body")),
            _string_or_none(row.payload.get("course_name")),
        )
        if part
    )


def _course_text(row: IngestedRow) -> str:
    return _string_or_none(row.payload.get("course_name")) or ""


def _display_course(row: IngestedRow) -> str | None:
    return _string_or_none(row.payload.get("course_name")) or row.course_external_id


def _issue_total(group: dict[str, Any]) -> int:
    total = 0
    for value in group.values():
        if isinstance(value, dict) and "count" in value:
            total += int(value["count"])
        elif isinstance(value, dict):
            total += _issue_total(value)
    return total


def _markdown_counts(counts: dict[str, int], *, limit: int | None = None) -> list[str]:
    if not counts:
        return ["- None"]
    items = list(counts.items())
    visible = items[:limit] if limit is not None else items
    lines = [f"- `{key}`: `{count}`" for key, count in visible]
    if limit is not None and len(items) > limit:
        lines.append(f"- ... `{len(items) - limit}` more")
    return lines


def _markdown_issue_group(group: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in group.items():
        if isinstance(value, dict) and "count" in value:
            lines.append(f"- `{key}`: `{value['count']}`")
            for sample in value["samples"]:
                lines.append(
                    "  - "
                    f"`{sample['external_id']}` "
                    f"({sample['platform']}, {sample['content_type'] or '-'}) "
                    f"{sample['course'] or '-'} | "
                    f"{sample['published_at'] or '-'} | "
                    f"{_markdown_cell(sample['title'] or sample['body'])}"
                )
        elif isinstance(value, dict):
            lines.append(f"- `{key}`")
            nested = _markdown_issue_group(value)
            lines.extend(f"  {line}" for line in nested)
    return lines or ["- None"]


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _platform_metrics(
    platform: str,
    rows: list[IngestedRow],
    cutoff: datetime,
) -> dict[str, Any]:
    parsed_rows = [
        (row, _parse_payload_datetime(row.payload.get("published_at"))) for row in rows
    ]
    valid_datetimes = [parsed for _, parsed in parsed_rows if parsed is not None]
    missing_published_at = sum(
        1
        for row, parsed in parsed_rows
        if parsed is None and not _has_text(row.payload.get("published_at"))
    )
    invalid_published_at = sum(
        1
        for row, parsed in parsed_rows
        if parsed is None and _has_text(row.payload.get("published_at"))
    )
    in_window = sum(1 for published_at in valid_datetimes if published_at >= cutoff)
    out_of_window = len(valid_datetimes) - in_window
    out_of_window_rate = (
        out_of_window / len(valid_datetimes) if valid_datetimes else 0.0
    )

    resolved_courses = sum(1 for row in rows if _has_course(row))
    course_rate = resolved_courses / len(rows) if rows else 1.0
    required = platform in REQUIRED_COURSE_PLATFORMS

    return {
        "total": len(rows),
        "published_at": {
            "populated": len(valid_datetimes),
            "missing": missing_published_at,
            "invalid": invalid_published_at,
            "coverage": len(valid_datetimes) / len(rows) if rows else 1.0,
        },
        "window": {
            "in_window": in_window,
            "out_of_window": out_of_window,
            "out_of_window_rate": out_of_window_rate,
            "missing_published_at": missing_published_at,
            "invalid_published_at": invalid_published_at,
        },
        "date_range": {
            "min_published_at": _format_datetime(min(valid_datetimes))
            if valid_datetimes
            else None,
            "max_published_at": _format_datetime(max(valid_datetimes))
            if valid_datetimes
            else None,
        },
        "course_resolution": {
            "resolved": resolved_courses,
            "missing": len(rows) - resolved_courses,
            "rate": course_rate,
            "required": required,
        },
        "sample_sanity": {
            "empty_payloads": sum(1 for row in rows if not row.payload),
        },
        "samples": _samples(parsed_rows),
    }


def _samples(
    parsed_rows: list[tuple[IngestedRow, datetime | None]],
) -> list[dict[str, str | None]]:
    sorted_rows = sorted(
        parsed_rows,
        key=lambda item: (
            item[1] is None,
            -item[1].timestamp() if item[1] is not None else 0,
            item[0].external_id,
        ),
    )
    samples = []
    for row, parsed_published_at in sorted_rows[:SAMPLE_SIZE]:
        course_name = row.payload.get("course_name")
        samples.append(
            {
                "external_id": row.external_id,
                "content_type": _string_or_none(row.payload.get("content_type")),
                "title": _string_or_none(row.payload.get("title")),
                "course": row.course_external_id or _string_or_none(course_name),
                "published_at": _format_datetime(parsed_published_at)
                if parsed_published_at is not None
                else _string_or_none(row.payload.get("published_at")),
            }
        )
    return samples


def _row_from_mapping(raw_row: object) -> IngestedRow:
    if not isinstance(raw_row, dict):
        raise ScriptError("Database query returned a non-object row")
    payload = raw_row.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ScriptError("Database query returned a row with non-object payload")
    return IngestedRow(
        external_id=_required_string(raw_row, "external_id"),
        platform=_required_string(raw_row, "platform"),
        course_external_id=_optional_string(raw_row.get("course_external_id")),
        payload=payload,
        created_at=_required_string(raw_row, "created_at"),
        updated_at=_required_string(raw_row, "updated_at"),
        processing_status=_required_string(raw_row, "processing_status"),
    )


def _required_string(raw_row: dict[str, object], key: str) -> str:
    value = raw_row.get(key)
    if not isinstance(value, str):
        raise ScriptError(f"Database query returned a row with invalid {key}")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ScriptError("Database query returned a row with invalid optional string")


def _count_by_processing_status(rows: list[IngestedRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.processing_status] = counts.get(row.processing_status, 0) + 1
    return counts


def _has_course(row: IngestedRow) -> bool:
    return _has_text(row.course_external_id) or _has_text(
        row.payload.get("course_name")
    )


def _parse_payload_datetime(value: object) -> datetime | None:
    if not _has_text(value):
        return None
    assert isinstance(value, str)
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _has_text(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _markdown_cell(value: str | None) -> str:
    if value is None:
        return "-"
    return value.replace("|", "\\|").replace("\n", " ").strip() or "-"


def _read_config_value(repo_root: Path, key: str) -> str | None:
    env_value = os.environ.get(key)
    if env_value:
        return env_value

    env_path = repo_root / ".env"
    if not env_path.exists():
        return None

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        env_key, value = stripped.split("=", 1)
        if env_key.strip() != key:
            continue
        return _strip_env_value(value)
    return None


def _strip_env_value(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


if __name__ == "__main__":
    raise SystemExit(main())
