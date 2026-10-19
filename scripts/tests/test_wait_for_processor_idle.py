from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "wait_for_processor_idle.py"


def _write_fake_docker(
    tmp_path: Path,
    *,
    count: int,
) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{docker_log}"
printf '%s\\n' "{count}"
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    return bin_dir, docker_log


def test_wait_for_processor_idle_exits_zero_when_pending_count_is_zero(
    tmp_path: Path,
) -> None:
    bin_dir, docker_log = _write_fake_docker(tmp_path, count=0)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "POSTGRES_DB": "shared_db_test",
        "POSTGRES_USER": "postgres_test",
    }

    result = subprocess.run(
        [
            "python",
            str(HELPER),
            "--timeout",
            "0",
            "--poll-interval",
            "0",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    assert result.returncode == 0
    assert "idle" in result.stdout
    assert (
        "psql --username=postgres_test --dbname=shared_db_test"
        in docker_log.read_text(encoding="utf-8")
    )


def test_wait_for_processor_idle_exits_one_on_timeout(tmp_path: Path) -> None:
    bin_dir, _docker_log = _write_fake_docker(tmp_path, count=3)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "POSTGRES_DB": "shared_db_test",
        "POSTGRES_USER": "postgres_test",
    }

    result = subprocess.run(
        [
            "python",
            str(HELPER),
            "--timeout",
            "0",
            "--poll-interval",
            "0",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    assert result.returncode == 1
    assert "timeout pending=3" in result.stdout


def test_wait_for_processor_idle_counts_all_pending_items(tmp_path: Path) -> None:
    bin_dir, docker_log = _write_fake_docker(tmp_path, count=0)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "POSTGRES_DB": "shared_db_test",
        "POSTGRES_USER": "postgres_test",
    }

    result = subprocess.run(
        [
            "python",
            str(HELPER),
            "--timeout",
            "0",
            "--poll-interval",
            "0",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    assert result.returncode == 0
    docker_call = docker_log.read_text(encoding="utf-8")
    assert "WHERE processing_status = 'pending'" in docker_call
    assert "payload->>'published_at'" not in docker_call
