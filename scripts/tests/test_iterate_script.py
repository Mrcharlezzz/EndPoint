from __future__ import annotations

import gzip
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ITERATE_SCRIPT = REPO_ROOT / "scripts" / "iterate.sh"


def test_iterate_requires_snapshot_path() -> None:
    result = subprocess.run(
        ["bash", str(ITERATE_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 2
    assert "usage: scripts/iterate.sh <pre-process-snapshot>" in result.stderr


def test_iterate_rejects_missing_snapshot(tmp_path: Path) -> None:
    missing_snapshot = tmp_path / "missing.sql.gz"

    result = subprocess.run(
        ["bash", str(ITERATE_SCRIPT), str(missing_snapshot)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 2
    assert f"Snapshot not found: {missing_snapshot}" in result.stderr


def test_iterate_restores_runs_polls_stops_and_reports(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.sql.gz"
    with gzip.open(snapshot, "wt", encoding="utf-8") as handle:
        handle.write("SELECT 1;\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    uv_log = tmp_path / "uv.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{docker_log}"
if [[ "$*" == *"logs -f"* ]]; then
    printf '%s\\n' "processor log line"
fi
if ! [ -t 0 ]; then
    cat >/dev/null
fi
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{uv_log}"
exit 0
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    run_root = tmp_path / "runs"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ENDPOINT_ITERATE_TIMESTAMP": "20260420T120000Z",
        "POSTGRES_DB": "shared_db_test",
        "POSTGRES_USER": "postgres_test",
    }

    result = subprocess.run(
        [
            "bash",
            str(ITERATE_SCRIPT),
            str(snapshot),
            "--timeout",
            "30",
            "--poll-interval",
            "0",
            "--label",
            "baseline",
            "--run-root",
            str(run_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    run_dir = run_root / "20260420T120000Z-baseline"
    assert (run_dir / "kind").read_text(encoding="utf-8") == "processor\n"
    state = (run_dir / "state.env").read_text(encoding="utf-8")
    assert "TIMEOUT_SECONDS=30" in state
    assert "TERMINATION_CAUSE=idle" in state
    assert f"SNAPSHOT_PATH={snapshot}" in state
    assert "processor log line" in (run_dir / "processor.log").read_text(
        encoding="utf-8"
    )
    assert not (run_dir / "docker-compose.override.yml").exists()

    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "stop processor" in docker_calls
    assert "up processor -d" in docker_calls
    assert "logs -f --since=" in docker_calls
    uv_calls = uv_log.read_text(encoding="utf-8")
    assert "wait_for_processor_idle.py --timeout 30" in uv_calls
    assert "report.py" in uv_calls
    assert f"{run_dir} --termination-cause idle" in uv_calls
    assert "static_run_inspector.py" in uv_calls
    assert str(run_dir) in uv_calls
