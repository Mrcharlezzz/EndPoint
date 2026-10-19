from __future__ import annotations

import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "prod"


def _write_required_env(path: Path, *, telegram_token: str | None = None) -> None:
    lines = [
        "POSTGRES_DB=shared_db",
        "POSTGRES_USER=postgres",
        "POSTGRES_PASSWORD=postgres",
        "ENDPOINT_FERNET_KEY=fernet-key",
        "ENDPOINT_ADMIN_PASSWORD=admin-password-long-enough",
        "ENDPOINT_SESSION_SECRET=session-secret",
        "ENDPOINT_HEADLESS__INTERNAL_SECRET=headless-secret",
        "PRESENTATION_API_SECRET_KEY=presentation-secret-long-enough-for-local-prod-only-12345",
    ]
    if telegram_token is not None:
        lines.append(f"ENDPOINT_TELEGRAM__BOT_TOKEN={telegram_token}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_telegram_bootstrap_env(path: Path, token: str) -> None:
    path.write_text(f"ENDPOINT_TELEGRAM__BOT_TOKEN={token}\n", encoding="utf-8")


def _write_fake_runtime_bin(
    tmp_path: Path,
    *,
    fail_seed_as_non_empty: bool = False,
) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"

    docker = bin_dir / "docker"
    docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{docker_log}"
printf 'RECENCY=%s\\n' "${{ENDPOINT_DAEMON__INGESTION_CONTROLS__RECENCY_DAYS:-}}" >> "{docker_log}"
if [[ "$1" == "inspect" ]]; then
    cid="${{@: -1}}"
    if [[ "$3" == *Health.Status* ]]; then
        echo healthy
    elif [[ "$3" == *State.Status* ]]; then
        if [[ "$cid" == migrate* ]]; then
            echo exited
        else
            echo running
        fi
    elif [[ "$3" == *State.ExitCode* ]]; then
        echo 0
    fi
    exit 0
fi
if [[ "$1" != "compose" ]]; then
    exit 0
fi
if [[ " $* " == *" ps -q "* || " $* " == *" ps -aq "* ]]; then
    echo "${{@: -1}}-cid"
    exit 0
fi
if [[ " $* " == *" config --quiet "* ]]; then
    exit 0
fi
if [[ "{1 if fail_seed_as_non_empty else 0}" == "1" && " $* " == *" import-production-seed "* ]]; then
    echo "fresh-only import requires empty table" >&2
    exit 1
fi
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)

    return bin_dir, docker_log


def _run_prod(
    tmp_path: Path,
    args: list[str],
    *,
    env_file: Path,
    telegram_bootstrap_env_file: Path | None = None,
    seed_dir: Path | None = None,
    fail_seed_as_non_empty: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir, docker_log = _write_fake_runtime_bin(
        tmp_path,
        fail_seed_as_non_empty=fail_seed_as_non_empty,
    )
    env = {
        **os.environ,
        "PROD_ENV_FILE": str(env_file),
        "PROD_TELEGRAM_BOOTSTRAP_ENV_FILE": str(
            telegram_bootstrap_env_file
            if telegram_bootstrap_env_file is not None
            else tmp_path / ".env.telegram-bootstrap"
        ),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    if seed_dir is not None:
        env["PROD_SEED_DIR"] = str(seed_dir)

    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    return result, docker_log


def test_prod_check_validates_compose_config_without_starting_services(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)

    result, docker_log = _run_prod(tmp_path, ["--check"], env_file=env_file)

    assert result.returncode == 0, result.stderr
    assert "[prod] check OK" in result.stdout
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "config --quiet" in docker_calls
    assert " up " not in f" {docker_calls} "
    assert " run " not in f" {docker_calls} "
    assert " down " not in f" {docker_calls} "


def test_prod_without_seed_does_not_run_seed_import(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)

    result, docker_log = _run_prod(tmp_path, [], env_file=env_file)

    assert result.returncode == 0, result.stderr
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "import-production-seed" not in docker_calls
    assert "down -v" not in docker_calls
    assert "RECENCY=90" in docker_calls


def test_prod_recency_overrides_default_daemon_recency(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)

    result, docker_log = _run_prod(tmp_path, ["--recency", "14"], env_file=env_file)

    assert result.returncode == 0, result.stderr
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "RECENCY=14" in docker_calls
    assert "RECENCY=90" not in docker_calls


def test_prod_recency_requires_positive_integer(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)

    result, docker_log = _run_prod(tmp_path, ["--recency", "0"], env_file=env_file)

    assert result.returncode == 2
    assert "error: --recency requires a positive integer day count" in result.stderr
    assert not docker_log.exists()


def test_prod_recency_requires_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)

    result, docker_log = _run_prod(tmp_path, ["--recency"], env_file=env_file)

    assert result.returncode == 2
    assert "error: --recency requires a positive integer day count" in result.stderr
    assert not docker_log.exists()


def test_prod_warns_urgently_when_telegram_token_is_missing_for_runtime(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)

    result, docker_log = _run_prod(tmp_path, [], env_file=env_file)

    assert result.returncode == 0, result.stderr
    assert "[prod] URGENT:" in result.stderr
    assert str(tmp_path / ".env.telegram-bootstrap") in result.stderr
    assert "live Telegram polling will not be bootstrapped" in result.stderr
    assert "bootstrap-telegram-credential" not in docker_log.read_text(encoding="utf-8")


def test_prod_bootstraps_telegram_credential_before_runtime_when_token_is_present(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)
    telegram_bootstrap_env_file = tmp_path / ".env.telegram-bootstrap"
    _write_telegram_bootstrap_env(
        telegram_bootstrap_env_file,
        "123456:prod-bot-token",
    )

    result, docker_log = _run_prod(
        tmp_path,
        [],
        env_file=env_file,
        telegram_bootstrap_env_file=telegram_bootstrap_env_file,
    )

    assert result.returncode == 0, result.stderr
    assert (
        "[prod] bootstrapping Telegram credential from ENDPOINT_TELEGRAM__BOT_TOKEN..."
        in result.stdout
    )
    docker_calls = docker_log.read_text(encoding="utf-8")
    bootstrap_index = docker_calls.index("run --rm --no-deps bootstrap-telegram-credential")
    runtime_index = docker_calls.index("up -d daemon processor headless presentation-api frontend dashboard")
    assert bootstrap_index < runtime_index


def test_prod_does_not_bootstrap_telegram_from_shared_env_file(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file, telegram_token="123456:legacy-shared-token")

    result, docker_log = _run_prod(tmp_path, [], env_file=env_file)

    assert result.returncode == 2
    assert str(tmp_path / ".env.telegram-bootstrap") in result.stderr
    assert "shared .env" in result.stderr
    assert not docker_log.exists()


def test_prod_check_rejects_telegram_token_in_shared_env_file(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file, telegram_token="123456:legacy-shared-token")

    result, docker_log = _run_prod(tmp_path, ["--check"], env_file=env_file)

    assert result.returncode == 2
    assert str(tmp_path / ".env.telegram-bootstrap") in result.stderr
    assert "shared .env" in result.stderr
    assert not docker_log.exists()


def test_prod_seed_selects_latest_seed_artifact_and_uses_seed_profile(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    (seed_dir / "20260505T134340Z-production-seed.json.gz").write_bytes(b"old")
    (seed_dir / "20260510T000527Z-production-seed.json.gz").write_bytes(b"new")

    result, docker_log = _run_prod(
        tmp_path,
        ["--seed"],
        env_file=env_file,
        seed_dir=seed_dir,
    )

    assert result.returncode == 0, result.stderr
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "--profile seed-import" in docker_calls
    assert (
        "-e SEED_ARTIFACT=/seeds/20260510T000527Z-production-seed.json.gz "
        "import-production-seed"
    ) in docker_calls


def test_prod_reset_resets_volumes_and_seeds_from_latest_artifact(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    (seed_dir / "20260510T000527Z-production-seed.json.gz").write_bytes(b"seed")

    result, docker_log = _run_prod(
        tmp_path,
        ["--reset"],
        env_file=env_file,
        seed_dir=seed_dir,
    )

    assert result.returncode == 0, result.stderr
    docker_calls = docker_log.read_text(encoding="utf-8")
    reset_index = docker_calls.index("down -v --remove-orphans")
    db_index = docker_calls.index("up -d db")
    seed_index = docker_calls.index("import-production-seed")
    runtime_index = docker_calls.index("up -d daemon processor headless presentation-api frontend dashboard")
    assert reset_index < db_index < seed_index < runtime_index


def test_prod_seed_import_refusal_stops_before_runtime(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    (seed_dir / "20260510T000527Z-production-seed.json.gz").write_bytes(b"seed")

    result, docker_log = _run_prod(
        tmp_path,
        ["--seed"],
        env_file=env_file,
        seed_dir=seed_dir,
        fail_seed_as_non_empty=True,
    )

    assert result.returncode == 1
    assert (
        "error: production seed import refused because the target Postgres or daemon state is not empty"
        in result.stderr
    )
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "import-production-seed" in docker_calls
    assert "up -d daemon processor headless presentation-api frontend dashboard" not in docker_calls


def test_prod_seed_artifact_requires_seed_or_reset(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)

    result, _docker_log = _run_prod(
        tmp_path,
        ["--seed-artifact", "seed.json.gz"],
        env_file=env_file,
    )

    assert result.returncode == 2
    assert "error: --seed-artifact requires --seed or --reset" in result.stderr
