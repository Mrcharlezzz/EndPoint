#!/usr/bin/env python3
"""Select the ingested_items subset the processor should handle next.

The processor polls only rows with ``processing_status = 'pending'``. This
script keeps integration selection outside the production processor path by
marking unselected fixture rows as ``manually_skipped`` and selected rows as
``pending``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "scripts" / "integration_selection.toml"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "data" / "reports" / "integration-selection"
ACTIVE_STATUS = "pending"
INACTIVE_STATUS = "manually_skipped"
RESET_STATUSES = (ACTIVE_STATUS, INACTIVE_STATUS)
NO_COURSE_LABEL = "<none>"

CRITERIA_KEYS = {
    "adapter_version",
    "body_contains",
    "content_type",
    "course_external_id",
    "course_name",
    "external_id",
    "external_id_prefix",
    "has_attachments",
    "has_body",
    "has_links",
    "has_published_at",
    "has_title",
    "ingestion_mode",
    "platform",
    "published_after",
    "published_before",
    "title_contains",
    "window_days",
}
CONTROL_KEYS = {"limit", "order_by"}
ALLOWED_BLOCK_KEYS = CRITERIA_KEYS | CONTROL_KEYS
ORDER_BY_SQL = {
    "created_at_asc": "created_at ASC, external_id ASC",
    "created_at_desc": "created_at DESC, external_id ASC",
    "external_id_asc": "external_id ASC",
    "published_at_asc": (
        "NULLIF(payload->>'published_at', '')::timestamptz ASC NULLS LAST, "
        "created_at ASC, external_id ASC"
    ),
    "published_at_desc": (
        "NULLIF(payload->>'published_at', '')::timestamptz DESC NULLS LAST, "
        "created_at ASC, external_id ASC"
    ),
}


class IntegrationSelectionError(Exception):
    """The selection config or database interaction is invalid."""


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    include: list[dict[str, Any]]
    exclude: list[dict[str, Any]]


def load_scenarios(config_path: Path) -> dict[str, Scenario]:
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise IntegrationSelectionError(f"Could not read {config_path}: {exc}") from exc

    raw_scenarios = raw.get("scenarios")
    if not isinstance(raw_scenarios, dict) or not raw_scenarios:
        raise IntegrationSelectionError("Config must define at least one scenario")

    scenarios: dict[str, Scenario] = {}
    for name, raw_scenario in raw_scenarios.items():
        if not isinstance(name, str) or not _valid_name(name):
            raise IntegrationSelectionError(f"Invalid scenario name: {name!r}")
        if not isinstance(raw_scenario, dict):
            raise IntegrationSelectionError(f"Scenario {name} must be a table")

        description = raw_scenario.get("description", "")
        if not isinstance(description, str):
            raise IntegrationSelectionError(
                f"Scenario {name} description must be a string"
            )

        include = _read_blocks(
            raw_scenario.get("include"), scenario=name, required=True
        )
        exclude = _read_blocks(
            raw_scenario.get("exclude", []), scenario=name, required=False
        )
        scenarios[name] = Scenario(
            name=name,
            description=description,
            include=include,
            exclude=exclude,
        )
    return scenarios


def build_apply_sql(scenario: Scenario) -> str:
    selected_sql = build_select_ids_sql(scenario)
    return (
        "BEGIN;\n"
        "UPDATE ingested_items\n"
        f"SET processing_status = '{INACTIVE_STATUS}',\n"
        "    error_message = NULL,\n"
        "    updated_at = NOW()\n"
        f"WHERE processing_status IN ({_sql_list(RESET_STATUSES)});\n"
        "WITH selected AS (\n"
        f"{_indent(selected_sql)}\n"
        ")\n"
        "UPDATE ingested_items\n"
        f"SET processing_status = '{ACTIVE_STATUS}',\n"
        "    error_message = NULL,\n"
        "    updated_at = NOW()\n"
        "WHERE id IN (SELECT id FROM selected);\n"
        "COMMIT;\n"
    )


def build_select_ids_sql(scenario: Scenario) -> str:
    if not scenario.include:
        raise IntegrationSelectionError(
            f"Scenario {scenario.name} has no include blocks"
        )

    exclude_predicate = _exclude_predicate(scenario.exclude)
    selections = []
    for index, block in enumerate(scenario.include):
        predicate = _block_predicate(block)
        if exclude_predicate is not None:
            predicate = f"({predicate}) AND NOT ({exclude_predicate})"
        predicate = (
            f"processing_status IN ({_sql_list(RESET_STATUSES)}) AND ({predicate})"
        )
        order_by = _order_by(block)
        limit = _limit_clause(block)
        selections.append(
            "SELECT id FROM (\n"
            "    SELECT id\n"
            "    FROM ingested_items\n"
            f"    WHERE {predicate}\n"
            f"    ORDER BY {order_by}\n"
            f"{limit}"
            f") AS include_{index}"
        )
    return "\nUNION\n".join(selections)


def build_summary_sql(selected_ids_sql: str | None = None) -> str:
    selected_cte = (
        selected_ids_sql
        if selected_ids_sql is not None
        else "SELECT id FROM ingested_items WHERE processing_status = 'pending'"
    )
    return (
        "WITH selected AS (\n"
        f"{_indent(selected_cte)}\n"
        "),\n"
        "selected_items AS (\n"
        "    SELECT * FROM ingested_items WHERE id IN (SELECT id FROM selected)\n"
        "),\n"
        "status_counts AS (\n"
        "    SELECT processing_status, count(*) AS total\n"
        "    FROM ingested_items\n"
        "    GROUP BY processing_status\n"
        "),\n"
        "platform_counts AS (\n"
        "    SELECT platform, count(*) AS total\n"
        "    FROM selected_items\n"
        "    GROUP BY platform\n"
        "),\n"
        "content_type_counts AS (\n"
        "    SELECT COALESCE(NULLIF(payload->>'content_type', ''), '<missing>') AS content_type,\n"
        "           count(*) AS total\n"
        "    FROM selected_items\n"
        "    GROUP BY content_type\n"
        "),\n"
        "course_counts AS (\n"
        f"    SELECT COALESCE(NULLIF(payload->>'course_name', ''), '{NO_COURSE_LABEL}') AS course_name,\n"
        "           count(*) AS total\n"
        "    FROM selected_items\n"
        "    GROUP BY course_name\n"
        "),\n"
        "platform_content_type_counts AS (\n"
        "    SELECT platform,\n"
        "           COALESCE(NULLIF(payload->>'content_type', ''), '<missing>') AS content_type,\n"
        "           count(*) AS total\n"
        "    FROM selected_items\n"
        "    GROUP BY platform, content_type\n"
        ")\n"
        "SELECT jsonb_build_object(\n"
        "    'selected', jsonb_build_object(\n"
        "        'total', (SELECT count(*) FROM selected_items),\n"
        "        'by_platform', COALESCE((\n"
        "            SELECT jsonb_object_agg(platform, total)\n"
        "            FROM (SELECT * FROM platform_counts ORDER BY platform) ordered\n"
        "        ), '{}'::jsonb),\n"
        "        'by_content_type', COALESCE((\n"
        "            SELECT jsonb_object_agg(content_type, total)\n"
        "            FROM (SELECT * FROM content_type_counts ORDER BY content_type) ordered\n"
        "        ), '{}'::jsonb),\n"
        "        'by_course', COALESCE((\n"
        "            SELECT jsonb_object_agg(course_name, total)\n"
        "            FROM (SELECT * FROM course_counts ORDER BY course_name) ordered\n"
        "        ), '{}'::jsonb),\n"
        "        'by_platform_content_type', COALESCE((\n"
        "            SELECT jsonb_agg(jsonb_build_object(\n"
        "                'platform', platform,\n"
        "                'content_type', content_type,\n"
        "                'count', total\n"
        "            ))\n"
        "            FROM (\n"
        "                SELECT * FROM platform_content_type_counts\n"
        "                ORDER BY platform, content_type\n"
        "            ) ordered\n"
        "        ), '[]'::jsonb)\n"
        "    ),\n"
        "    'status_counts', COALESCE((\n"
        "        SELECT jsonb_object_agg(processing_status, total)\n"
        "        FROM (SELECT * FROM status_counts ORDER BY processing_status) ordered\n"
        "    ), '{}'::jsonb)\n"
        ")::text;"
    )


def terminal_summary(
    *,
    scenario: Scenario,
    dry_run: bool,
    summary: dict[str, Any],
    report_path: Path,
    metrics_path: Path,
) -> str:
    selected = _dict_value(summary.get("selected"))
    status_counts = _dict_value(summary.get("status_counts"))
    lines = [
        f"Selection {'dry run' if dry_run else 'applied'}: {scenario.name}",
        f"Report: {report_path}",
        f"Metrics: {metrics_path}",
        "",
        f"Selected pending: {_int_value(selected.get('total'))}",
    ]
    manually_skipped = status_counts.get(INACTIVE_STATUS)
    if manually_skipped is not None:
        lines.append(f"Manually skipped: {_int_value(manually_skipped)}")

    for label, key in (
        ("By platform", "by_platform"),
        ("By content type", "by_content_type"),
        ("By course", "by_course"),
    ):
        values = _dict_value(selected.get(key))
        if not values:
            continue
        lines.extend(["", f"{label}:"])
        for item_key, count in sorted(values.items()):
            lines.append(f"- {item_key}: {_int_value(count)}")

    return "\n".join(lines)


def write_artifacts(
    *,
    scenario: Scenario,
    dry_run: bool,
    summary: dict[str, Any],
    output_root: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    timestamp = os.environ.get("ENDPOINT_SELECTION_TIMESTAMP") or datetime.now(
        UTC
    ).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / f"{timestamp}-{scenario.name}"
    run_dir.mkdir(parents=True, exist_ok=True)

    metrics = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scenario": scenario.name,
        "description": scenario.description,
        "dry_run": dry_run,
        "active_status": ACTIVE_STATUS,
        "inactive_status": INACTIVE_STATUS,
        "criteria": {
            "include": scenario.include,
            "exclude": scenario.exclude,
        },
        "summary": summary,
    }

    report_path = run_dir / "report.md"
    metrics_path = run_dir / "metrics.json"
    report_path.write_text(_markdown_report(metrics), encoding="utf-8")
    metrics_path.write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report_path, metrics_path, metrics


def run_psql_script(sql: str) -> None:
    command = _psql_command()
    subprocess.run(command, input=sql, text=True, check=True, cwd=REPO_ROOT)


def run_psql_command(sql: str) -> str:
    command = [*_psql_command(), "--command", sql]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown failure"
        raise IntegrationSelectionError(f"Database query failed: {message}")
    return result.stdout


def parse_summary(raw_output: str) -> dict[str, Any]:
    for raw_line in reversed(raw_output.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise IntegrationSelectionError("Database query returned no JSON summary")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select which ingested_items remain pending for integration."
    )
    parser.add_argument("scenario", nargs="?", help="Scenario name from the config")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Selection config path. Defaults to {DEFAULT_CONFIG}.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the selected subset without updating processing_status.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Report output directory. Defaults to {DEFAULT_OUTPUT_ROOT}.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available scenarios and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        scenarios = load_scenarios(args.config)
        if args.list:
            for scenario in scenarios.values():
                suffix = f" - {scenario.description}" if scenario.description else ""
                print(f"{scenario.name}{suffix}")
            return 0
        if not args.scenario:
            print("Scenario is required unless --list is used", file=sys.stderr)
            return 2
        scenario = scenarios.get(args.scenario)
        if scenario is None:
            names = ", ".join(sorted(scenarios))
            print(
                f"Unknown scenario: {args.scenario}. Available: {names}",
                file=sys.stderr,
            )
            return 2

        selected_ids_sql = build_select_ids_sql(scenario)
        if args.dry_run:
            summary = parse_summary(
                run_psql_command(build_summary_sql(selected_ids_sql))
            )
        else:
            run_psql_script(build_apply_sql(scenario))
            summary = parse_summary(run_psql_command(build_summary_sql()))

        report_path, metrics_path, _metrics = write_artifacts(
            scenario=scenario,
            dry_run=args.dry_run,
            summary=summary,
            output_root=args.output_root,
        )
    except (IntegrationSelectionError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Selection failed: {exc}", file=sys.stderr)
        return 2

    print(
        terminal_summary(
            scenario=scenario,
            dry_run=args.dry_run,
            summary=summary,
            report_path=report_path,
            metrics_path=metrics_path,
        )
    )
    return 0


def _read_blocks(value: Any, *, scenario: str, required: bool) -> list[dict[str, Any]]:
    if value is None:
        if required:
            raise IntegrationSelectionError(
                f"Scenario {scenario} must define at least one include block"
            )
        return []
    if not isinstance(value, list):
        raise IntegrationSelectionError(f"Scenario {scenario} blocks must be lists")
    if required and not value:
        raise IntegrationSelectionError(
            f"Scenario {scenario} must define at least one include block"
        )

    blocks: list[dict[str, Any]] = []
    for index, block in enumerate(value):
        if not isinstance(block, dict):
            raise IntegrationSelectionError(
                f"Scenario {scenario} block {index} must be a table"
            )
        blocks.append(dict(block))
    return blocks


def _block_predicate(block: dict[str, Any]) -> str:
    _validate_keys(block)
    predicates: list[str] = []
    for key, value in block.items():
        if key in CONTROL_KEYS:
            continue
        predicate = _criterion_predicate(key, value)
        if predicate != "TRUE":
            predicates.append(predicate)
    return " AND ".join(predicates) if predicates else "TRUE"


def _exclude_predicate(blocks: list[dict[str, Any]]) -> str | None:
    if not blocks:
        return None
    predicates = [f"({_block_predicate(block)})" for block in blocks]
    return " OR ".join(predicates)


def _criterion_predicate(key: str, value: Any) -> str:
    match key:
        case "platform":
            return f"platform IN ({_sql_list(_strings(value, key))})"
        case "content_type":
            return f"payload->>'content_type' IN ({_sql_list(_strings(value, key))})"
        case "course_name":
            return f"payload->>'course_name' IN ({_sql_list(_strings(value, key))})"
        case "course_external_id":
            values = _strings(value, key)
            return (
                f"(course_external_id IN ({_sql_list(values)}) "
                f"OR payload->>'course_external_id' IN ({_sql_list(values)}))"
            )
        case "external_id":
            return f"external_id IN ({_sql_list(_strings(value, key))})"
        case "external_id_prefix":
            return _any_like("external_id", _strings(value, key), suffix_only=True)
        case "title_contains":
            return _any_like("payload->>'title'", _strings(value, key))
        case "body_contains":
            return _any_like("payload->>'body'", _strings(value, key))
        case "has_title":
            return _presence_predicate("payload->>'title'", _bool(value, key))
        case "has_body":
            return _presence_predicate("payload->>'body'", _bool(value, key))
        case "has_published_at":
            return _presence_predicate("payload->>'published_at'", _bool(value, key))
        case "has_attachments":
            return _array_presence_predicate("attachments", _bool(value, key))
        case "has_links":
            return _array_presence_predicate("links", _bool(value, key))
        case "ingestion_mode":
            return f"ingestion_mode IN ({_sql_list(_strings(value, key))})"
        case "adapter_version":
            return f"adapter_version IN ({_sql_list(_strings(value, key))})"
        case "window_days":
            days = _positive_int(value, key)
            return (
                "NULLIF(payload->>'published_at', '')::timestamptz "
                f">= now() - make_interval(days => {days})"
            )
        case "published_after":
            return (
                "NULLIF(payload->>'published_at', '')::timestamptz "
                f">= {_sql_literal(_string(value, key))}::timestamptz"
            )
        case "published_before":
            return (
                "NULLIF(payload->>'published_at', '')::timestamptz "
                f"< {_sql_literal(_string(value, key))}::timestamptz"
            )
        case _:
            raise IntegrationSelectionError(f"Unknown filter key: {key}")


def _order_by(block: dict[str, Any]) -> str:
    raw_order = block.get("order_by", "created_at_asc")
    order = _string(raw_order, "order_by")
    try:
        return ORDER_BY_SQL[order]
    except KeyError as exc:
        allowed = ", ".join(sorted(ORDER_BY_SQL))
        raise IntegrationSelectionError(
            f"Unknown order_by {order!r}. Allowed: {allowed}"
        ) from exc


def _limit_clause(block: dict[str, Any]) -> str:
    if "limit" not in block:
        return ""
    limit = _positive_int(block["limit"], "limit")
    return f"    LIMIT {limit}\n"


def _validate_keys(block: dict[str, Any]) -> None:
    for key in block:
        if key not in ALLOWED_BLOCK_KEYS:
            raise IntegrationSelectionError(f"Unknown filter key: {key}")


def _strings(value: Any, key: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) for item in value)
    ):
        return list(value)
    raise IntegrationSelectionError(f"{key} must be a string or non-empty string list")


def _string(value: Any, key: str) -> str:
    if isinstance(value, str) and value:
        return value
    raise IntegrationSelectionError(f"{key} must be a non-empty string")


def _bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise IntegrationSelectionError(f"{key} must be a boolean")


def _positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise IntegrationSelectionError(f"{key} must be a positive integer")
    return value


def _presence_predicate(expression: str, expected: bool) -> str:
    operator = "IS NOT NULL" if expected else "IS NULL"
    return f"NULLIF({expression}, '') {operator}"


def _array_presence_predicate(field: str, expected: bool) -> str:
    comparator = "> 0" if expected else "= 0"
    return (
        "jsonb_array_length(CASE "
        f"WHEN jsonb_typeof(payload->'{field}') = 'array' THEN payload->'{field}' "
        f"ELSE '[]'::jsonb END) {comparator}"
    )


def _any_like(expression: str, values: list[str], *, suffix_only: bool = False) -> str:
    predicates = []
    for value in values:
        pattern = (
            f"{_escape_like(value)}%" if suffix_only else f"%{_escape_like(value)}%"
        )
        predicates.append(f"{expression} ILIKE '{pattern}' ESCAPE '\\\\'")
    return "(" + " OR ".join(predicates) + ")"


def _sql_list(values: tuple[str, ...] | list[str]) -> str:
    return ", ".join(_sql_literal(value) for value in values)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _escape_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
        .replace("'", "''")
    )


def _indent(sql: str) -> str:
    return "\n".join(f"    {line}" if line else "" for line in sql.splitlines())


def _psql_command() -> list[str]:
    postgres_db = os.environ.get("POSTGRES_DB") or _read_env_var("POSTGRES_DB")
    postgres_user = os.environ.get("POSTGRES_USER") or _read_env_var("POSTGRES_USER")
    if not postgres_db or not postgres_user:
        raise IntegrationSelectionError(
            "POSTGRES_DB and POSTGRES_USER must be set in env or .env"
        )
    return [
        "docker",
        "compose",
        "-f",
        str(REPO_ROOT / "docker-compose.yml"),
        "exec",
        "-T",
        "db",
        "psql",
        f"--username={postgres_user}",
        f"--dbname={postgres_db}",
        "--set=ON_ERROR_STOP=on",
        "--no-align",
        "--tuples-only",
        "--quiet",
    ]


def _read_env_var(key: str) -> str:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return ""
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        env_key, value = line.split("=", 1)
        if env_key.strip() == key:
            return _strip_env_value(value)
    return ""


def _strip_env_value(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def _markdown_report(metrics: dict[str, Any]) -> str:
    selected = _dict_value(_dict_value(metrics.get("summary")).get("selected"))
    status_counts = _dict_value(
        _dict_value(metrics.get("summary")).get("status_counts")
    )
    lines = [
        "# Integration Selection Report",
        "",
        f"- Scenario: `{metrics['scenario']}`",
        f"- Generated: `{metrics['generated_at']}`",
        f"- Dry run: `{metrics['dry_run']}`",
        f"- Active status: `{metrics['active_status']}`",
        f"- Inactive status: `{metrics['inactive_status']}`",
    ]
    if metrics.get("description"):
        lines.append(f"- Description: {metrics['description']}")

    lines.extend(
        [
            "",
            "## Selected Pending",
            "",
            f"- Total: `{_int_value(selected.get('total'))}`",
        ]
    )
    _append_count_table(lines, "By Platform", _dict_value(selected.get("by_platform")))
    _append_count_table(
        lines, "By Content Type", _dict_value(selected.get("by_content_type"))
    )
    _append_count_table(lines, "By Course", _dict_value(selected.get("by_course")))
    _append_count_table(lines, "Database Status Counts", status_counts)
    lines.append("")
    return "\n".join(lines)


def _append_count_table(lines: list[str], title: str, values: dict[str, Any]) -> None:
    if not values:
        return
    lines.extend(["", f"## {title}", "", "| Value | Count |", "| --- | ---: |"])
    for key, count in sorted(values.items()):
        lines.append(f"| {key} | {_int_value(count)} |")


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_value(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _valid_name(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_.-]+", value) is not None


if __name__ == "__main__":
    raise SystemExit(main())
