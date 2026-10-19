from __future__ import annotations

import gzip
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SCRIPT = REPO_ROOT / "scripts" / "restore.sh"


def test_restore_requires_snapshot_path() -> None:
    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 2
    assert "usage: scripts/restore.sh <snapshot.sql.gz>" in result.stderr


def test_restore_rejects_missing_snapshot(tmp_path: Path) -> None:
    missing_snapshot = tmp_path / "missing.sql.gz"

    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT), str(missing_snapshot)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 2
    assert f"Snapshot not found: {missing_snapshot}" in result.stderr


def test_restore_resets_schema_and_streams_snapshot_to_psql(tmp_path: Path) -> None:
    sql = "CREATE TABLE example (id integer);\nINSERT INTO example VALUES (1);\n"
    snapshot = tmp_path / "snapshot.sql.gz"
    with gzip.open(snapshot, "wt", encoding="utf-8") as handle:
        handle.write(sql)

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
        ["bash", str(RESTORE_SCRIPT), str(snapshot)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    docker_calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert len(docker_calls) == 2
    assert "compose -f" in docker_calls[0]
    assert "psql --username=postgres_test --dbname=shared_db_test" in docker_calls[0]
    assert "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" in docker_calls[0]
    assert "psql --username=postgres_test --dbname=shared_db_test" in docker_calls[1]
    assert "--set=ON_ERROR_STOP=on" in docker_calls[1]
    assert stdin_log.read_text(encoding="utf-8") == sql
