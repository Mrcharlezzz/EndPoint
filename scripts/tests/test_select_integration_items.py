from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.select_integration_items import (
    INACTIVE_STATUS,
    IntegrationSelectionError,
    build_apply_sql,
    build_select_ids_sql,
    build_summary_sql,
    load_scenarios,
)


def test_load_scenarios_reads_include_and_exclude_blocks(tmp_path: Path) -> None:
    config = tmp_path / "selection.toml"
    config.write_text(
        """
[scenarios.mixed_small]
description = "CTMS-22 and Telegram smoke run."

[[scenarios.mixed_small.include]]
platform = ["teams"]
course_name = ["F25_CTMS-22_Matrix Algebra and Advanced Calculus I"]
content_type = ["assignment"]

[[scenarios.mixed_small.exclude]]
body_contains = ["cancelled"]
""",
        encoding="utf-8",
    )

    scenarios = load_scenarios(config)

    scenario = scenarios["mixed_small"]
    assert scenario.description == "CTMS-22 and Telegram smoke run."
    assert scenario.include[0]["platform"] == ["teams"]
    assert scenario.exclude[0]["body_contains"] == ["cancelled"]


def test_build_select_ids_sql_combines_include_blocks_with_limits() -> None:
    scenarios = load_scenarios(Path("scripts/integration_selection.toml"))

    sql = build_select_ids_sql(scenarios["mixed_small"])

    assert "F25_CTMS-22_Matrix Algebra and Advanced Calculus I" in sql
    assert "platform IN ('moodle', 'teams')" in sql
    assert "platform IN ('telegram')" in sql
    assert "make_interval(days => 60)" in sql
    assert "LIMIT 100" in sql
    assert "UNION" in sql


def test_default_config_includes_unbounded_linear_algebra_and_telegram() -> None:
    scenarios = load_scenarios(Path("scripts/integration_selection.toml"))

    linear_algebra_sql = build_select_ids_sql(scenarios["linear_algebra_messages"])
    assert "S26_CH-151-A_Linear Algebra" in linear_algebra_sql
    assert "payload->>'content_type' IN ('raw_message')" in linear_algebra_sql
    assert "make_interval" not in linear_algebra_sql
    assert "LIMIT" not in linear_algebra_sql

    telegram_sql = build_select_ids_sql(scenarios["telegram_full"])
    assert "platform IN ('telegram')" in telegram_sql
    assert "payload->>'content_type' IN ('raw_message')" in telegram_sql
    assert "make_interval" not in telegram_sql
    assert "LIMIT" not in telegram_sql


def test_build_apply_sql_marks_unselected_rows_manually_skipped() -> None:
    scenarios = load_scenarios(Path("scripts/integration_selection.toml"))

    sql = build_apply_sql(scenarios["ctms22_dedup"])

    assert f"SET processing_status = '{INACTIVE_STATUS}'" in sql
    assert "WHERE processing_status IN ('pending', 'manually_skipped')" in sql
    assert "SET processing_status = 'pending'" in sql
    assert "WITH selected AS" in sql


def test_selection_only_considers_pending_or_manually_skipped_rows() -> None:
    scenarios = load_scenarios(Path("scripts/integration_selection.toml"))

    sql = build_select_ids_sql(scenarios["ctms22_dedup"])

    assert "processing_status IN ('pending', 'manually_skipped')" in sql


def test_unknown_filter_key_fails_fast(tmp_path: Path) -> None:
    config = tmp_path / "selection.toml"
    config.write_text(
        """
[scenarios.invalid]

[[scenarios.invalid.include]]
unknown_key = "value"
""",
        encoding="utf-8",
    )

    scenarios = load_scenarios(config)
    with pytest.raises(IntegrationSelectionError, match="Unknown filter key"):
        build_select_ids_sql(scenarios["invalid"])


def test_summary_sql_reports_pending_breakdown() -> None:
    sql = build_summary_sql()

    assert "processing_status = 'pending'" in sql
    assert "by_platform" in sql
    assert "by_content_type" in sql
    assert "by_course" in sql
    assert "status_counts" in sql


def test_cli_dry_run_writes_report_and_metrics_with_fake_docker(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{docker_log}"
cat >/dev/null
printf '%s\\n' '{{"selected": {{"total": 2, "by_platform": {{"teams": 1, "telegram": 1}}, "by_content_type": {{"assignment": 1, "raw_message": 1}}, "by_course": {{"F25_CTMS-22_Matrix Algebra and Advanced Calculus I": 1, "<none>": 1}}}}, "status_counts": {{"pending": 2, "manually_skipped": 3}}}}'
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    output_root = tmp_path / "reports"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "POSTGRES_DB": "shared_db_test",
        "POSTGRES_USER": "postgres_test",
        "ENDPOINT_SELECTION_TIMESTAMP": "20260420T120000Z",
    }

    result = subprocess.run(
        [
            "python",
            "scripts/select_integration_items.py",
            "mixed_small",
            "--dry-run",
            "--output-root",
            str(output_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "Selection dry run: mixed_small" in result.stdout
    assert "Selected pending: 2" in result.stdout
    report_dir = output_root / "20260420T120000Z-mixed_small"
    assert (report_dir / "report.md").exists()
    metrics = json.loads((report_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["scenario"] == "mixed_small"
    assert metrics["dry_run"] is True
    assert metrics["summary"]["selected"]["total"] == 2
    assert "--command" in docker_log.read_text(encoding="utf-8")
