from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "dev.sh"
DASHBOARD_VITE = REPO_ROOT / "src/ingestion/dashboard/node_modules/.bin/vite"
FRONTEND_NEXT = REPO_ROOT / "src/EndpointNextJS/endpoint-nextjs/node_modules/.bin/next"


@contextmanager
def _temporary_executable(path: Path) -> Iterator[None]:
    created = not path.exists()
    created_dirs: list[Path] = []
    original_mode = path.stat().st_mode if path.exists() else None

    if created:
        parent = path.parent
        while not parent.exists():
            created_dirs.append(parent)
            parent = parent.parent
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    elif not os.access(path, os.X_OK):
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    try:
        yield
    finally:
        if created:
            path.unlink(missing_ok=True)
            for directory in created_dirs:
                directory.rmdir()
        elif original_mode is not None:
            path.chmod(original_mode)


@contextmanager
def _ensure_frontend_executables() -> Iterator[None]:
    managers = []
    try:
        if not DASHBOARD_VITE.exists():
            managers.append(_temporary_executable(DASHBOARD_VITE))
        if not FRONTEND_NEXT.exists():
            managers.append(_temporary_executable(FRONTEND_NEXT))
        for manager in managers:
            manager.__enter__()
        yield
    finally:
        while managers:
            managers.pop().__exit__(None, None, None)


def _write_required_env(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "POSTGRES_DB=shared_db",
                "POSTGRES_USER=postgres",
                "POSTGRES_PASSWORD=postgres",
                "ENDPOINT_FERNET_KEY=fernet-key",
                "ENDPOINT_ADMIN_PASSWORD=admin-password-long-enough",
                "ENDPOINT_SESSION_SECRET=session-secret",
                "ENDPOINT_HEADLESS__INTERNAL_SECRET=headless-secret",
                "PRESENTATION_API_SECRET_KEY=presentation-secret-long-enough-for-local-dev-only-12345",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_cli_credentials(home: Path) -> Path:
    credentials = home / ".config" / "ingest" / "credentials.json"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text('{"teams": {"graph_token": "token", "team_ids": ["team"]}}\n', encoding="utf-8")
    return credentials


def _make_dev_runtime_bin_dir(
    tmp_path: Path,
    *,
    docker_row_count: str = "0",
    fail_legacy_seed_probe: bool = False,
) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    docker_log = tmp_path / "docker.log"
    uv_log = tmp_path / "uv.log"

    _write_executable(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$PWD|$*" >> "{docker_log}"
if [[ "$1" == "inspect" ]]; then
    if [[ "$3" == *Health.Status* ]]; then
        echo healthy
    else
        echo exited
    fi
    exit 0
fi
if [[ "$1" != "compose" ]]; then
    exit 0
fi
if [[ " $* " == *" ps -q db "* ]]; then
    echo db-cid
    exit 0
fi
if [[ " $* " == *" ps --status running db "* ]]; then
    exit 0
fi
if [[ " $* " == *" exec -T db "* && " $* " == *" psql "* ]]; then
    if [[ "{1 if fail_legacy_seed_probe else 0}" == "1" && " $* " == *"COUNT(*)::text FROM public.ingested_items"* ]]; then
        echo 'ERROR: relation "public.ingested_items" does not exist' >&2
        exit 1
    fi
    echo "{docker_row_count}"
    exit 0
fi
exit 0
""",
    )

    _write_executable(
        bin_dir / "uv",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$PWD|$*" >> "{uv_log}"
exit 0
""",
    )

    _write_executable(
        bin_dir / "npm",
        "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"__endpoint_dev_probe__"* ]]; then
    printf '404'
    exit 0
fi
exit 0
""",
    )
    _write_executable(
        bin_dir / "setsid",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'recency=%s\\ncmd=%s\\n' "${ENDPOINT_DAEMON__INGESTION_CONTROLS__RECENCY_DAYS:-}" "$*"
exec /usr/bin/setsid /bin/sleep 4
""",
    )
    _write_executable(
        bin_dir / "sleep",
        "#!/usr/bin/env bash\nexec /bin/sleep 0.1\n",
    )

    return bin_dir, docker_log, uv_log


def test_dev_help_describes_full_hybrid_stack() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0
    assert "full Endpoint hybrid dev instance" in result.stderr
    assert "frontend" in result.stderr
    assert "presentation-api" in result.stderr
    assert "headless auth" in result.stderr


def test_dev_rejects_missing_non_generated_required_env_vars(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_USER=postgres",
                "POSTGRES_PASSWORD=postgres",
                "ENDPOINT_FERNET_KEY=fernet-key",
                "ENDPOINT_ADMIN_PASSWORD=admin-password-long-enough",
                "ENDPOINT_SESSION_SECRET=session-secret",
                "ENDPOINT_HEADLESS__INTERNAL_SECRET=headless-secret",
                "PRESENTATION_API_SECRET_KEY=presentation-secret-long-enough-for-local-dev-only-12345",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "DEV_ENV_FILE": str(env_file),
        },
    )

    assert result.returncode == 2
    assert "POSTGRES_DB" in result.stderr
    assert "ENDPOINT_HEADLESS__INTERNAL_SECRET" not in result.stderr
    assert "PRESENTATION_API_SECRET_KEY" not in result.stderr


def test_dev_generates_missing_dev_secrets(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_USER=postgres",
                "POSTGRES_PASSWORD=postgres",
                "ENDPOINT_FERNET_KEY=fernet-key",
                "ENDPOINT_ADMIN_PASSWORD=admin-password-long-enough",
                "ENDPOINT_SESSION_SECRET=session-secret",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "DEV_ENV_FILE": str(env_file),
        },
    )

    assert result.returncode == 2
    assert "POSTGRES_DB" in result.stderr
    assert "ENDPOINT_HEADLESS__INTERNAL_SECRET" not in result.stderr
    assert "PRESENTATION_API_SECRET_KEY" not in result.stderr
    assert "generated ENDPOINT_HEADLESS__INTERNAL_SECRET" in result.stdout
    assert "generated PRESENTATION_API_SECRET_KEY" in result.stdout

    generated = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        generated[name] = value

    headless_secret = generated["ENDPOINT_HEADLESS__INTERNAL_SECRET"]
    presentation_secret = generated["PRESENTATION_API_SECRET_KEY"]
    assert len(headless_secret) >= 48
    assert len(presentation_secret) >= 50
    assert headless_secret not in result.stdout
    assert headless_secret not in result.stderr
    assert presentation_secret not in result.stdout
    assert presentation_secret not in result.stderr


def test_dev_uses_secret_key_fallback_before_generating_presentation_secret(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POSTGRES_USER=postgres",
                "POSTGRES_PASSWORD=postgres",
                "SECRET_KEY=legacy-secret",
                "ENDPOINT_FERNET_KEY=fernet-key",
                "ENDPOINT_ADMIN_PASSWORD=admin-password-long-enough",
                "ENDPOINT_SESSION_SECRET=session-secret",
                "ENDPOINT_HEADLESS__INTERNAL_SECRET=headless-secret",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "DEV_ENV_FILE": str(env_file),
        },
    )

    assert result.returncode == 2
    assert "POSTGRES_DB" in result.stderr
    assert "using SECRET_KEY for this run" in result.stdout
    assert "generated PRESENTATION_API_SECRET_KEY" not in result.stdout
    assert "PRESENTATION_API_SECRET_KEY=" not in env_file.read_text(encoding="utf-8")


def test_dev_check_selects_alternate_ports_when_defaults_conflict(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    fake_docker = bin_dir / "docker"
    fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_docker.chmod(0o755)

    fake_lsof = bin_dir / "lsof"
    fake_lsof.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
for arg in "$@"; do
    case "$arg" in
        -iTCP:3000|-iTCP:8000|-iTCP:8001|-iTCP:8765|-iTCP:5173)
            exit 0
            ;;
    esac
done
exit 1
""",
        encoding="utf-8",
    )
    fake_lsof.chmod(0o755)

    with _ensure_frontend_executables():
        result = subprocess.run(
            ["bash", str(SCRIPT), "--check"],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "DEV_ENV_FILE": str(env_file),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            },
        )

    assert result.returncode == 0, result.stderr
    assert "[dev] frontend port 3000 is in use; using 3001" in result.stdout
    assert "[dev] headless auth port 8000 is in use; using 8002" in result.stdout
    assert "[dev] presentation-api port 8001 is in use; using 8003" in result.stdout
    assert "[dev] daemon API port 8765 is in use; using 8766" in result.stdout
    assert "[dev] daemon dashboard port 5173 is in use; using 5174" in result.stdout
    assert "[dev] check OK" in result.stdout


def test_dev_port_check_falls_back_to_ss_when_lsof_misses_listener(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    fake_docker = bin_dir / "docker"
    fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_docker.chmod(0o755)

    fake_lsof = bin_dir / "lsof"
    fake_lsof.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    fake_lsof.chmod(0o755)

    fake_ss = bin_dir / "ss"
    fake_ss.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'
if [[ "$*" == *'sport = :8000'* ]]; then
    echo 'LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*'
fi
""",
        encoding="utf-8",
    )
    fake_ss.chmod(0o755)

    with _ensure_frontend_executables():
        result = subprocess.run(
            ["bash", str(SCRIPT), "--check"],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "DEV_ENV_FILE": str(env_file),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            },
        )

    assert result.returncode == 0, result.stderr
    assert "[dev] headless auth port 8000 is in use; using 8002" in result.stdout


def test_dev_check_does_not_stop_compose_services(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{docker_log}"
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    with _ensure_frontend_executables():
        result = subprocess.run(
            ["bash", str(SCRIPT), "--check"],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "DEV_ENV_FILE": str(env_file),
                "ENDPOINT_DEV_SKIP_PORT_CHECK": "1",
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            },
        )

    assert result.returncode == 0, result.stderr
    assert "[dev] check OK" in result.stdout
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert "compose --env-file" in docker_calls
    assert " stop " not in f" {docker_calls} "
    assert " up " not in f" {docker_calls} "


def test_dev_script_has_headless_auth_runtime_gates() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "checking Playwright Chromium" in script
    assert "playwright.chromium.launch(headless=True)" in script
    assert "/internal/auth/teams/__endpoint_dev_probe__/payload" in script
    assert "X-Internal-Secret: $ENDPOINT_HEADLESS__INTERNAL_SECRET" in script
    assert "uvicorn ingestion.headless.api:app" in script
    assert "--port '$HEADLESS_AUTH_PORT'" in script


def test_dev_script_wires_selected_ports_into_dependent_services() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'HEADLESS_INTERNAL_URL="$HEADLESS_AUTH_URL"' in script
    assert 'HEADLESS_PUBLIC_URL="$HEADLESS_AUTH_URL"' in script
    assert 'NEXT_PUBLIC_PRESENTATION_API_URL="$PRESENTATION_API_URL"' in script
    assert 'ENDPOINT_DAEMON__API__PORT="$DAEMON_API_PORT"' in script
    assert 'VITE_DAEMON_URL="$DAEMON_API_URL"' in script
    assert 'VITE_DEV_SERVER_PORT="$DASHBOARD_PORT"' in script
    assert 'GLITCHTIP_DOMAIN="$GLITCHTIP_URL"' in script
    assert 'ENDPOINT_DAEMON__SENTRY__PUBLIC_URL="$GLITCHTIP_URL"' in script
    assert "runserver 0.0.0.0:'$PRESENTATION_API_PORT'" in script
    assert "npm run dev -- --port '$FRONTEND_PORT'" in script


def test_dev_script_starts_glitchtip_and_processor_for_stack_parity() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "--profile observability up -d" in script
    assert "glitchtip-web glitchtip-worker" in script
    assert 'wait_for_url "GlitchTip" "$GLITCHTIP_URL"' in script
    assert "cd '$PROCESSOR_DIR' && exec uv run python main.py" in script
    assert "$LOG_DIR/processor.log" in script
    assert "Postgres and GlitchTip are left running" in script


def test_dev_script_keeps_running_after_ready_service_exits() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "leaving remaining services running" in script
    assert "monitor_services" in script
    assert "wait -n" not in script
    assert "a host service exited; shutting down the rest" not in script


def test_dev_script_manages_a_single_previous_instance() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "RUN_STATE_FILE" in script
    assert "stop_previous_dev_instance" in script
    assert "write_run_state" in script
    assert "clear_run_state" in script
    assert "stopping previous hybrid dev instance" in script
    assert "stop_legacy_daemon_lock_holder" in script


def test_dev_check_accepts_reset_and_recency_flags(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", "#!/usr/bin/env bash\nexit 0\n")

    with _ensure_frontend_executables():
        result = subprocess.run(
            ["bash", str(SCRIPT), "--check", "--reset", "--recency", "14"],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "DEV_ENV_FILE": str(env_file),
                "ENDPOINT_DEV_SKIP_PORT_CHECK": "1",
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            },
        )

    assert result.returncode == 0, result.stderr
    assert "[dev] check OK" in result.stdout


def test_dev_runs_bootstrap_migrations_and_cli_import(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)
    _write_cli_credentials(tmp_path)
    bin_dir, docker_log, uv_log = _make_dev_runtime_bin_dir(tmp_path)
    log_dir = tmp_path / "logs"
    run_dir = tmp_path / "run"

    with _ensure_frontend_executables():
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "DEV_ENV_FILE": str(env_file),
                "ENDPOINT_DEV_SKIP_SYNC": "1",
                "ENDPOINT_DEV_SKIP_PORT_CHECK": "1",
                "ENDPOINT_DEV_LOG_DIR": str(log_dir),
                "ENDPOINT_DEV_RUN_DIR": str(run_dir),
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            },
        )

    assert result.returncode == 0, result.stderr

    docker_calls = docker_log.read_text(encoding="utf-8").splitlines()
    uv_calls = uv_log.read_text(encoding="utf-8").splitlines()
    stdout = result.stdout

    db_core_migrate = (
        f"{REPO_ROOT / 'src/common/db_core'}|run python manage.py migrate --no-input"
    )
    presentation_api_migrate = (
        f"{REPO_ROOT / 'src/backend/presentation-api'}|run python manage.py migrate --no-input"
    )
    assert db_core_migrate in uv_calls
    assert presentation_api_migrate in uv_calls

    docker_text = "\n".join(docker_calls)
    assert f"compose --env-file {env_file} run --rm seed-post-process-snapshot" in docker_text
    assert f"compose --env-file {env_file} run --rm import-cli-tokens" in docker_text
    seed_index = docker_text.index("run --rm seed-post-process-snapshot")
    import_index = docker_text.index("run --rm import-cli-tokens")

    assert seed_index < import_index
    assert stdout.index("[dev] fresh database detected; loading post-process snapshot...") < stdout.index(
        "[dev] applying db-core migrations..."
    )
    assert stdout.index("[dev] applying db-core migrations...") < stdout.index(
        "[dev] applying presentation-api migrations..."
    )
    assert stdout.index("[dev] applying presentation-api migrations...") < stdout.index(
        "[dev] importing CLI credentials..."
    )


def test_dev_skips_cli_import_when_credentials_file_is_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)
    bin_dir, docker_log, _uv_log = _make_dev_runtime_bin_dir(tmp_path)
    log_dir = tmp_path / "logs"
    run_dir = tmp_path / "run"

    with _ensure_frontend_executables():
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "DEV_ENV_FILE": str(env_file),
                "ENDPOINT_DEV_SKIP_SYNC": "1",
                "ENDPOINT_DEV_SKIP_PORT_CHECK": "1",
                "ENDPOINT_DEV_LOG_DIR": str(log_dir),
                "ENDPOINT_DEV_RUN_DIR": str(run_dir),
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            },
        )

    assert result.returncode == 0, result.stderr
    assert "[dev] no CLI credentials found; skipping platform credential import" in result.stdout
    assert "Real Teams/Moodle syncs can be enabled by logging in through the app" in result.stdout
    assert "run --rm import-cli-tokens" not in docker_log.read_text(encoding="utf-8")


def test_dev_bootstraps_telegram_from_env_token(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)
    bin_dir, _docker_log, uv_log = _make_dev_runtime_bin_dir(tmp_path)
    log_dir = tmp_path / "logs"
    run_dir = tmp_path / "run"

    with _ensure_frontend_executables():
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "DEV_ENV_FILE": str(env_file),
                "ENDPOINT_DEV_SKIP_SYNC": "1",
                "ENDPOINT_DEV_SKIP_PORT_CHECK": "1",
                "ENDPOINT_DEV_LOG_DIR": str(log_dir),
                "ENDPOINT_DEV_RUN_DIR": str(run_dir),
                "ENDPOINT_TELEGRAM__BOT_TOKEN": "123456:dev-bot-token",
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            },
        )

    assert result.returncode == 0, result.stderr
    assert (
        "[dev] bootstrapping Telegram credential from ENDPOINT_TELEGRAM__BOT_TOKEN..."
        in result.stdout
    )
    assert (
        f"{REPO_ROOT / 'src/ingestion'}|run python scripts/bootstrap_telegram_credential.py"
        in uv_log.read_text(encoding="utf-8")
    )


def test_dev_no_process_skips_processor_while_rest_of_stack_runs(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)
    bin_dir, _docker_log, _uv_log = _make_dev_runtime_bin_dir(tmp_path)
    log_dir = tmp_path / "logs"
    run_dir = tmp_path / "run"

    with _ensure_frontend_executables():
        result = subprocess.run(
            ["bash", str(SCRIPT), "--no-process"],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "DEV_ENV_FILE": str(env_file),
                "ENDPOINT_DEV_SKIP_SYNC": "1",
                "ENDPOINT_DEV_SKIP_PORT_CHECK": "1",
                "ENDPOINT_DEV_LOG_DIR": str(log_dir),
                "ENDPOINT_DEV_RUN_DIR": str(run_dir),
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            },
        )

    assert result.returncode == 0, result.stderr
    assert "[dev] starting processor" not in result.stdout
    assert "Processor           worker loop                 disabled" in result.stdout
    assert not (log_dir / "processor.log").exists()

    assert (log_dir / "frontend.log").exists()
    assert (log_dir / "daemon.log").exists()


def test_import_cli_tokens_does_not_depend_on_snapshot_service() -> None:
    override = (REPO_ROOT / "docker-compose.override.yml").read_text(encoding="utf-8")

    import_section = override.split("  import-cli-tokens:\n", maxsplit=1)[1]
    import_section = import_section.split("\n  ", maxsplit=1)[0]

    assert "depends_on:" not in import_section
    assert "seed-post-process-snapshot:" not in import_section


def test_seed_post_process_snapshot_always_selects_latest_snapshot() -> None:
    override = (REPO_ROOT / "docker-compose.override.yml").read_text(encoding="utf-8")

    seed_section = override.split("  seed-post-process-snapshot:\n", maxsplit=1)[1]
    seed_section = seed_section.split("\n  import-cli-tokens:\n", maxsplit=1)[0]

    assert "20260426T224057Z-20260427-clean-migrated.sql.gz" not in seed_section
    assert "find /snapshots -maxdepth 1 -type f -name '*.sql.gz' | sort | tail -n 1" in seed_section


def test_seed_post_process_snapshot_accepts_explicit_snapshot() -> None:
    override = (REPO_ROOT / "docker-compose.override.yml").read_text(encoding="utf-8")

    seed_section = override.split("  seed-post-process-snapshot:\n", maxsplit=1)[1]
    seed_section = seed_section.split("\n  import-cli-tokens:\n", maxsplit=1)[0]

    assert "ENDPOINT_DEV_SEED_SNAPSHOT" in seed_section
    assert "/snapshots/$$ENDPOINT_DEV_SEED_SNAPSHOT" in seed_section


def test_dev_can_select_seed_snapshot(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)
    bin_dir, docker_log, _ = _make_dev_runtime_bin_dir(tmp_path)
    log_dir = tmp_path / "logs"
    run_dir = tmp_path / "run"
    snapshot_name = "20260510T000636Z-seed-refresh-20260510.sql.gz"

    with _ensure_frontend_executables():
        result = subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--seed-snapshot",
                snapshot_name,
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "DEV_ENV_FILE": str(env_file),
                "ENDPOINT_DEV_SKIP_SYNC": "1",
                "ENDPOINT_DEV_SKIP_PORT_CHECK": "1",
                "ENDPOINT_DEV_LOG_DIR": str(log_dir),
                "ENDPOINT_DEV_RUN_DIR": str(run_dir),
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            },
        )

    assert result.returncode == 0, result.stderr

    docker_text = docker_log.read_text(encoding="utf-8")
    assert (
        f"compose --env-file {env_file} run --rm "
        f"-e ENDPOINT_DEV_SEED_SNAPSHOT={snapshot_name} seed-post-process-snapshot"
    ) in docker_text


def test_dev_bootstrap_handles_missing_ingested_items_table(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)
    bin_dir, docker_log, uv_log = _make_dev_runtime_bin_dir(
        tmp_path,
        fail_legacy_seed_probe=True,
    )
    log_dir = tmp_path / "logs"
    run_dir = tmp_path / "run"

    with _ensure_frontend_executables():
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "DEV_ENV_FILE": str(env_file),
                "ENDPOINT_DEV_SKIP_SYNC": "1",
                "ENDPOINT_DEV_SKIP_PORT_CHECK": "1",
                "ENDPOINT_DEV_LOG_DIR": str(log_dir),
                "ENDPOINT_DEV_RUN_DIR": str(run_dir),
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            },
        )

    assert result.returncode == 0, result.stderr
    assert "[dev] fresh database detected; loading post-process snapshot..." in result.stdout

    docker_calls = docker_log.read_text(encoding="utf-8")
    uv_calls = uv_log.read_text(encoding="utf-8")
    assert "COUNT(*)::text FROM public.ingested_items" not in docker_calls
    assert "SELECT to_regclass('public.ingested_items') IS NOT NULL" in docker_calls
    assert (
        f"{REPO_ROOT / 'src/common/db_core'}|run python manage.py migrate --no-input"
        in uv_calls
    )


def test_dev_reset_clears_previous_state_and_applies_recency_override(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    _write_required_env(env_file)
    bin_dir, docker_log, _uv_log = _make_dev_runtime_bin_dir(tmp_path)
    log_dir = tmp_path / "logs"
    run_dir = tmp_path / "run"
    sqlite_path = tmp_path / ".local/share/endpoint/state.db"

    log_dir.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    (log_dir / "stale.log").write_text("old", encoding="utf-8")
    (run_dir / "stale.tsv").write_text("old", encoding="utf-8")
    sqlite_path.write_text("old-state", encoding="utf-8")
    sqlite_lock = sqlite_path.with_suffix(f"{sqlite_path.suffix}.lock")
    sqlite_lock.write_text("999999\n", encoding="utf-8")

    with _ensure_frontend_executables():
        result = subprocess.run(
            ["bash", str(SCRIPT), "--reset", "--recency", "14"],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "DEV_ENV_FILE": str(env_file),
                "ENDPOINT_DEV_SKIP_SYNC": "1",
                "ENDPOINT_DEV_SKIP_PORT_CHECK": "1",
                "ENDPOINT_DEV_LOG_DIR": str(log_dir),
                "ENDPOINT_DEV_RUN_DIR": str(run_dir),
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
            },
        )

    assert result.returncode == 0, result.stderr

    docker_text = docker_log.read_text(encoding="utf-8")
    assert f"compose --env-file {env_file} down -v --remove-orphans" in docker_text
    assert "run --rm seed-post-process-snapshot" in docker_text

    assert not (log_dir / "stale.log").exists()
    assert not (run_dir / "stale.tsv").exists()
    assert not sqlite_path.exists()
    assert not sqlite_lock.exists()

    daemon_log = log_dir / "daemon.log"
    assert daemon_log.exists()
    daemon_output = daemon_log.read_text(encoding="utf-8")
    assert "recency=14" in daemon_output
