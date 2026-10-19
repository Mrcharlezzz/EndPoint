from __future__ import annotations

import gzip
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run_dev_processor_from_latest_post_process.sh"


def test_script_rejects_snapshot_dir_without_snapshots(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "post-process"
    snapshot_dir.mkdir()

    result = subprocess.run(
        ["bash", str(SCRIPT), "--snapshot-dir", str(snapshot_dir)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 2
    assert f"No post-process snapshots found in: {snapshot_dir}" in result.stderr


def test_script_uses_latest_snapshot_and_starts_dev_processor(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "post-process"
    snapshot_dir.mkdir()
    older_snapshot = snapshot_dir / "20260420T101913Z-mixed-small-rebuilt.sql.gz"
    latest_snapshot = snapshot_dir / "20260423T114016Z-20260423-latest.sql.gz"
    with gzip.open(older_snapshot, "wt", encoding="utf-8") as handle:
        handle.write("SELECT 'older';\n")
    with gzip.open(latest_snapshot, "wt", encoding="utf-8") as handle:
        handle.write("SELECT 'latest';\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    stdin_log = tmp_path / "stdin.sql"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{docker_log}"
if ! [ -t 0 ]; then
    cat >> "{stdin_log}"
fi
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "POSTGRES_DB": "shared_db_test",
        "POSTGRES_USER": "postgres_test",
    }

    result = subprocess.run(
        ["bash", str(SCRIPT), "--snapshot-dir", str(snapshot_dir)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert f"Selected latest post-process snapshot: {latest_snapshot}" in result.stdout
    assert "Ready: dev processor is up against the restored post-process snapshot." in result.stdout

    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "compose up db -d" in docker_calls
    assert "up db -d" in docker_calls
    assert "exec -T db pg_isready --username=postgres_test --dbname=shared_db_test" in docker_calls
    assert "stop processor" in docker_calls
    assert "up processor -d --build --force-recreate" in docker_calls
    assert stdin_log.read_text(encoding="utf-8") == "SELECT 'latest';\n"
