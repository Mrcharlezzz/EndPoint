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
SAMPLE_SIZE = 5


@dataclass(frozen=True)
class ProcessorReport:
    metrics: dict[str, Any]
    markdown: str


class ScriptError(Exception):
    """A configuration, database, or filesystem problem prevented reporting."""


def build_report(
    raw_data: dict[str, list[dict[str, Any]]],
    *,
    run_dir: Path,
    now: datetime,
    termination_cause: str,
    time_window_days: int,
) -> ProcessorReport:
    ingested_items = raw_data.get("ingested_items", [])
    classified_items = raw_data.get("classified_items", [])
    classification_logs = raw_data.get("classification_logs", [])
    dedup_logs = raw_data.get("dedup_logs", [])

    metrics: dict[str, Any] = {
        "generated_at": _format_datetime(now),
        "run_dir": str(run_dir),
        "termination_cause": termination_cause,
        "time_window_days": time_window_days,
        "window": {
            "platforms": _window_metrics(
                ingested_items,
                now=now,
                time_window_days=time_window_days,
            )
        },
        "processing": {
            "by_status": _count_by_key(ingested_items, "processing_status"),
            "by_platform_status": _nested_count(
                ingested_items,
                outer_key="platform",
                inner_key="processing_status",
            ),
            "classified_by_platform_entity": _nested_count(
                classified_items,
                outer_key="platform",
                inner_key="entity_type",
            ),
            "raw_message_unclassified": _raw_message_unclassified_counts(
                classification_logs
            ),
        },
        "failures": {
            "top_reasons": _failure_reasons(ingested_items),
        },
        "dedup": {
            "by_status": _dedup_counts(dedup_logs, classified_items),
        },
        "samples": _samples_by_entity_type(classified_items),
    }
    return ProcessorReport(metrics=metrics, markdown=render_markdown(metrics))


def render_markdown(metrics: dict[str, Any]) -> str:
    lines = [
        "# Processor Run Report",
        "",
        f"- Generated: `{metrics['generated_at']}`",
        f"- Run directory: `{metrics['run_dir']}`",
        f"- Termination cause: `{metrics['termination_cause']}`",
        f"- Time window days: `{metrics['time_window_days']}`",
        "",
        "## Window Coverage",
        "",
        "| Platform | Total | In Window | Out Of Window | Missing Published At |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]

    for platform in EXPECTED_PLATFORMS:
        platform_metrics = metrics["window"]["platforms"][platform]
        lines.append(
            "| "
            f"{platform} | "
            f"{platform_metrics['total']} | "
            f"{platform_metrics['in_window']} | "
            f"{platform_metrics['out_of_window']} | "
            f"{platform_metrics['missing_published_at']} |"
        )

    lines.extend(["", "## Processing Status", ""])
    lines.extend(_count_lines(metrics["processing"]["by_status"]))

    lines.extend(["", "## Classified Entities", ""])
    classified_counts = metrics["processing"]["classified_by_platform_entity"]
    if classified_counts:
        for platform in sorted(classified_counts):
            for entity_type, count in sorted(classified_counts[platform].items()):
                lines.append(f"- `{platform}` / `{entity_type}`: `{count}`")
    else:
        lines.append("- None")

    lines.extend(["", "## Raw Messages", ""])
    raw_counts = metrics["processing"]["raw_message_unclassified"]
    if raw_counts:
        for platform, count in sorted(raw_counts.items()):
            lines.append(f"- `{platform}` unclassified raw messages: `{count}`")
    else:
        lines.append("- None")

    lines.extend(["", "## Failure Reasons", ""])
    failure_reasons = metrics["failures"]["top_reasons"]
    if failure_reasons:
        for item in failure_reasons:
            lines.append(f"- `{item['reason']}`: `{item['count']}`")
    else:
        lines.append("- None")

    lines.extend(["", "## Dedup Decisions", ""])
    lines.extend(_count_lines(metrics["dedup"]["by_status"]))

    lines.extend(["", "## Samples", ""])
    samples = metrics["samples"]
    if samples:
        for entity_type in sorted(samples):
            lines.extend(
                [
                    f"### {entity_type}",
                    "",
                    "| Platform | Title | Published At |",
                    "| --- | --- | --- |",
                ]
            )
            for sample in samples[entity_type]:
                lines.append(
                    "| "
                    f"{_markdown_cell(sample['platform'])} | "
                    f"{_markdown_cell(sample['title'])} | "
                    f"{_markdown_cell(sample['published_at'])} |"
                )
            lines.append("")
    else:
        lines.append("- None")

    return "\n".join(lines).rstrip() + "\n"


def write_artifacts(report: ProcessorReport, run_dir: Path) -> tuple[Path, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.md"
    metrics_path = run_dir / "metrics.json"
    report_path.write_text(report.markdown, encoding="utf-8")
    metrics_path.write_text(
        json.dumps(report.metrics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report_path, metrics_path


def fetch_report_data(repo_root: Path) -> dict[str, list[dict[str, Any]]]:
    postgres_db = _read_config_value(repo_root, "POSTGRES_DB")
    postgres_user = _read_config_value(repo_root, "POSTGRES_USER")
    if not postgres_db or not postgres_user:
        raise ScriptError(
            "POSTGRES_DB and POSTGRES_USER must be set in the environment or .env"
        )

    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(repo_root / "docker-compose.yml"),
            "exec",
            "-T",
            "db",
            "psql",
            f"--username={postgres_user}",
            f"--dbname={postgres_db}",
            "--no-align",
            "--tuples-only",
            "--quiet",
            "--command",
            _report_sql(),
        ],
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
        raw_data = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise ScriptError(f"Database query returned invalid JSON: {exc}") from exc
    if not isinstance(raw_data, dict):
        raise ScriptError("Database query returned JSON that was not an object")
    return _normalize_report_data(raw_data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write processor integration report artifacts for a run directory."
    )
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--termination-cause", default=None)
    parser.add_argument("--time-window-days", type=int, default=None)
    args = parser.parse_args(argv)

    if args.time_window_days is not None and args.time_window_days < 0:
        print("--time-window-days must be non-negative", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    state = _read_state(args.run_dir)
    termination_cause = (
        args.termination_cause or state.get("TERMINATION_CAUSE") or "unknown"
    )
    time_window_days = (
        args.time_window_days
        if args.time_window_days is not None
        else _int_from_state(state, "TIME_WINDOW_DAYS", default=0)
    )

    try:
        raw_data = fetch_report_data(repo_root)
        report = build_report(
            raw_data,
            run_dir=args.run_dir,
            now=datetime.now(UTC),
            termination_cause=termination_cause,
            time_window_days=time_window_days,
        )
        report_path, metrics_path = write_artifacts(report, args.run_dir)
    except (OSError, ScriptError) as exc:
        print(f"Script error: {exc}", file=sys.stderr)
        return 2

    print(f"Report written: {report_path}")
    print(f"Metrics written: {metrics_path}")
    return 0


def _window_metrics(
    ingested_items: list[dict[str, Any]],
    *,
    now: datetime,
    time_window_days: int,
) -> dict[str, dict[str, int]]:
    cutoff = now - timedelta(days=time_window_days) if time_window_days > 0 else None
    result = {
        platform: {
            "total": 0,
            "in_window": 0,
            "out_of_window": 0,
            "missing_published_at": 0,
        }
        for platform in EXPECTED_PLATFORMS
    }

    for item in ingested_items:
        platform = _string_value(item.get("platform"), default="unknown")
        result.setdefault(
            platform,
            {
                "total": 0,
                "in_window": 0,
                "out_of_window": 0,
                "missing_published_at": 0,
            },
        )
        result[platform]["total"] += 1
        payload = item.get("payload")
        published_at = _parse_datetime(
            payload.get("published_at") if isinstance(payload, dict) else None
        )
        if published_at is None:
            result[platform]["missing_published_at"] += 1
        elif cutoff is None or published_at >= cutoff:
            result[platform]["in_window"] += 1
        else:
            result[platform]["out_of_window"] += 1
    return result


def _raw_message_unclassified_counts(
    classification_logs: list[dict[str, Any]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for log in classification_logs:
        if (
            log.get("entity_type") != "raw_message"
            or log.get("classified_item_id") is not None
        ):
            continue
        platform = _string_value(log.get("platform"), default="unknown")
        counts[platform] = counts.get(platform, 0) + 1
    return counts


def _failure_reasons(ingested_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in ingested_items:
        if item.get("processing_status") != "failed":
            continue
        reason = _string_value(item.get("error_message"), default="unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )
    ][:10]


def _dedup_counts(
    dedup_logs: list[dict[str, Any]],
    classified_items: list[dict[str, Any]],
) -> dict[str, int]:
    source = dedup_logs if dedup_logs else classified_items
    key = "status" if dedup_logs else "dedup_status"
    return _count_by_key(source, key)


def _samples_by_entity_type(
    classified_items: list[dict[str, Any]],
) -> dict[str, list[dict[str, str | None]]]:
    samples: dict[str, list[dict[str, str | None]]] = {}
    for item in sorted(
        classified_items,
        key=lambda value: (
            _string_value(value.get("entity_type"), default=""),
            _string_value(value.get("title"), default=""),
        ),
    ):
        entity_type = _string_value(item.get("entity_type"), default="unknown")
        entity_samples = samples.setdefault(entity_type, [])
        if len(entity_samples) >= SAMPLE_SIZE:
            continue
        entity_samples.append(
            {
                "platform": _string_value(item.get("platform"), default="unknown"),
                "title": _optional_string(item.get("title")),
                "published_at": _optional_string(item.get("published_at")),
            }
        )
    return samples


def _count_by_key(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = _string_value(row.get(key), default="unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _nested_count(
    rows: list[dict[str, Any]],
    *,
    outer_key: str,
    inner_key: str,
) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        outer = _string_value(row.get(outer_key), default="unknown")
        inner = _string_value(row.get(inner_key), default="unknown")
        counts.setdefault(outer, {})
        counts[outer][inner] = counts[outer].get(inner, 0) + 1
    return counts


def _count_lines(counts: dict[str, int]) -> list[str]:
    if not counts:
        return ["- None"]
    return [f"- `{key}`: `{value}`" for key, value in sorted(counts.items())]


def _normalize_report_data(raw_data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    normalized: dict[str, list[dict[str, Any]]] = {}
    for key in (
        "ingested_items",
        "classified_items",
        "classification_logs",
        "dedup_logs",
    ):
        value = raw_data.get(key, [])
        if not isinstance(value, list):
            raise ScriptError(f"Database query returned non-list {key}")
        rows: list[dict[str, Any]] = []
        for row in value:
            if not isinstance(row, dict):
                raise ScriptError(f"Database query returned non-object row in {key}")
            rows.append(row)
        normalized[key] = rows
    return normalized


def _report_sql() -> str:
    return """
SELECT jsonb_build_object(
    'ingested_items', (
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'id', id::text,
            'platform', platform,
            'processing_status', processing_status,
            'error_message', error_message,
            'payload', payload,
            'created_at', created_at
        ) ORDER BY created_at, id), '[]'::jsonb)
        FROM ingested_items
    ),
    'classified_items', (
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'id', b.id::text,
            'ingested_item_id', b.ingested_item_id::text,
            'platform', i.platform,
            'entity_type', b.entity_type,
            'title', b.title,
            'dedup_status', b.dedup_status,
            'published_at', b.published_at,
            'created_at', b.created_at
        ) ORDER BY b.entity_type, b.created_at, b.id), '[]'::jsonb)
        FROM base_classified_items b
        JOIN ingested_items i ON i.id = b.ingested_item_id
    ),
    'classification_logs', (
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'ingested_item_id', c.ingested_item_id::text,
            'platform', i.platform,
            'entity_type', c.entity_type,
            'classified_item_id', c.classified_item_id::text,
            'created_at', c.created_at
        ) ORDER BY c.created_at, c.id), '[]'::jsonb)
        FROM classification_logs c
        JOIN ingested_items i ON i.id = c.ingested_item_id
    ),
    'dedup_logs', (
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'status', d.status,
            'platform', i.platform,
            'entity_type', b.entity_type,
            'error_message', d.error_message,
            'created_at', d.created_at
        ) ORDER BY d.created_at, d.id), '[]'::jsonb)
        FROM dedup_logs d
        JOIN base_classified_items b ON b.id = d.classified_item_id
        JOIN ingested_items i ON i.id = b.ingested_item_id
    )
)::text
"""


def _read_state(run_dir: Path) -> dict[str, str]:
    state_path = run_dir / "state.env"
    if not state_path.exists():
        return {}
    state: dict[str, str] = {}
    for line in state_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        state[key] = value
    return state


def _int_from_state(state: dict[str, str], key: str, *, default: int) -> int:
    value = state.get(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ScriptError(f"{key} in state.env must be an integer") from exc


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
        if env_key.strip() == key:
            return _strip_env_value(value)
    return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or value.strip() == "":
        return None
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


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _string_value(value: object, *, default: str) -> str:
    result = _optional_string(value)
    return default if result is None else result


def _markdown_cell(value: str | None) -> str:
    if value is None:
        return "-"
    return value.replace("|", "\\|").replace("\n", " ").strip() or "-"


def _strip_env_value(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


if __name__ == "__main__":
    raise SystemExit(main())
