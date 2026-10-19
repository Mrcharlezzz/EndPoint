from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "demo"


def test_demo_rejects_missing_required_env_vars(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_DB=shared_db",
                "POSTGRES_USER=postgres",
                "POSTGRES_PASSWORD=postgres",
                "SECRET_KEY=legacy-secret",
                "ENDPOINT_FERNET_KEY=fernet-key",
                "ENDPOINT_ADMIN_PASSWORD=admin-password-long-enough",
                "ENDPOINT_SESSION_SECRET=session-secret",
                "",
            ]
        ),
        encoding="utf-8",
    )

    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    (snapshot_dir / "snapshot.sql.gz").write_bytes(b"placeholder")

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "DEMO_ENV_FILE": str(env_file),
            "DEMO_SNAPSHOT_DIR": str(snapshot_dir),
        },
    )

    assert result.returncode == 2
    assert "ENDPOINT_HEADLESS__INTERNAL_SECRET" in result.stderr


def test_demo_uses_secret_key_fallback_and_starts_stack(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_DB=shared_db",
                "POSTGRES_USER=postgres",
                "POSTGRES_PASSWORD=postgres",
                "SECRET_KEY=legacy-secret",
                "ENDPOINT_FERNET_KEY=fernet-key",
                "ENDPOINT_ADMIN_PASSWORD=admin-password-long-enough",
                "ENDPOINT_SESSION_SECRET=session-secret",
                "ENDPOINT_HEADLESS__INTERNAL_SECRET=dummy",
                "",
            ]
        ),
        encoding="utf-8",
    )

    credentials_file = tmp_path / ".config" / "ingest" / "credentials.json"
    credentials_file.parent.mkdir(parents=True)
    credentials_file.write_text("{}", encoding="utf-8")

    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_dir / "20260426T224057Z-20260427-clean-migrated.sql.gz"
    snapshot.write_bytes(b"placeholder")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{docker_log}"
if [[ "$1" == "inspect" ]]; then
    format="$3"
    case "$format" in
        *Health.Status*)
            echo healthy
            ;;
        *State.Status*)
            if [[ "$4" == *migrate-cid || "$4" == *seed-post-process-snapshot-cid || "$4" == *import-cli-tokens-cid ]]; then
                echo exited
            else
                echo running
            fi
            ;;
        *State.ExitCode*)
            echo 0
            ;;
    esac
    exit 0
fi
if [[ "$1" == "compose" ]]; then
    for arg in "$@"; do
        if [[ "$arg" == "ps" ]]; then
            service="${{@: -1}}"
            echo "$service-cid"
            exit 0
        fi
    done
fi
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = {
        **os.environ,
        "DEMO_ENV_FILE": str(env_file),
        "DEMO_SNAPSHOT_DIR": str(snapshot_dir),
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "using SECRET_KEY for this run" in result.stdout
    assert "[demo] stack ready" in result.stdout

    docker_calls = docker_log.read_text(encoding="utf-8")
    assert f"compose --env-file {env_file} down --remove-orphans" in docker_calls
    assert f"compose --env-file {env_file} up -d" in docker_calls
    assert (
        f"compose --env-file {env_file} ps -q seed-post-process-snapshot"
        in docker_calls
    )
    assert f"compose --env-file {env_file} ps -q import-cli-tokens" in docker_calls
