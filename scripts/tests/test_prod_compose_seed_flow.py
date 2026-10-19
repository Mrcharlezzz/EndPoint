from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_prod_compose_has_runtime_telegram_credential_bootstrap() -> None:
    compose = (REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "  bootstrap-telegram-credential:\n" in compose
    lines = compose.splitlines()
    start = lines.index("  bootstrap-telegram-credential:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            end = index
            break
    section = "\n".join(lines[start:end])

    assert "image: endpoint/ingestor" in section
    assert "working_dir: /app/src/ingestion" in section
    assert (
        'command: ["uv", "run", "python", "scripts/bootstrap_telegram_credential.py"]'
        in section
    )
    assert "PG_HOST: db" in section
    assert 'PG_PORT: "5432"' in section
    assert "db:\n        condition: service_healthy" in section
    assert "migrate:\n        condition: service_completed_successfully" in section
    assert 'profiles: ["telegram-bootstrap"]' in section
    assert (
        "env_file:\n      - .env\n      - ${TELEGRAM_BOOTSTRAP_ENV_FILE:-.env.telegram-bootstrap}"
        in section
    )
    assert "ENDPOINT_TELEGRAM__BOT_TOKEN" not in section
    assert "import-production-seed:" not in section
    assert 'restart: "no"' in section


def test_prod_daemon_keeps_shared_env_file_and_does_not_allowlist_runtime_env() -> None:
    compose = (REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "  daemon:\n" in compose
    lines = compose.splitlines()
    start = lines.index("  daemon:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            end = index
            break
    section = "\n".join(lines[start:end])

    assert "env_file: !reset []" not in section
    assert "ENDPOINT_TELEGRAM__BOT_TOKEN" not in section
    assert "POSTGRES_PASSWORD" not in section
    assert "ENDPOINT_FERNET_KEY" not in section


def test_telegram_bootstrap_env_file_is_documented_but_not_committed() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".env.*" in gitignore
    assert "!.env.telegram-bootstrap.example" in gitignore
    assert (REPO_ROOT / ".env.telegram-bootstrap.example").is_file()


def test_prod_compose_runs_presentation_migrations_before_seed_import() -> None:
    compose = (REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "  migrate-presentation-api:\n" in compose
    lines = compose.splitlines()
    start = lines.index("  migrate-presentation-api:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            end = index
            break
    section = "\n".join(lines[start:end])

    assert "image: endpoint/presentation-api" in section
    assert "PG_HOST: db" in section
    assert 'PG_PORT: "5432"' in section
    assert 'command: ["python", "manage.py", "migrate", "--noinput"]' in section
    assert "db:\n        condition: service_healthy" in section
    assert 'restart: "no"' in section

    import_start = lines.index("  import-production-seed:")
    import_end = len(lines)
    for index in range(import_start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            import_end = index
            break
    import_section = "\n".join(lines[import_start:import_end])

    assert (
        "migrate:\n        condition: service_completed_successfully" in import_section
    )
    assert (
        "migrate-presentation-api:\n        condition: service_completed_successfully"
        in import_section
    )
    assert 'profiles: ["seed-import"]' in import_section


def test_prod_script_runs_presentation_migrations_before_seed_import() -> None:
    script = (REPO_ROOT / "scripts" / "prod").read_text(encoding="utf-8")
    main_body = script[script.index("main() {") :]

    presentation_migration = 'echo "[prod] starting presentation-api migrations..."'
    seed_import = "run_seed_import"

    assert presentation_migration in main_body
    assert main_body.index(presentation_migration) < main_body.index(seed_import)
    assert "wait_for_successful_exit migrate-presentation-api 120" in main_body


def test_prod_path_has_no_local_cli_credential_dependency() -> None:
    prod_files = [
        REPO_ROOT / "scripts" / "prod",
        REPO_ROOT / "docker-compose.prod.yml",
        REPO_ROOT / "docs" / "DEPLOYMENT.md",
    ]

    for path in prod_files:
        content = path.read_text(encoding="utf-8")
        assert "import-cli-tokens" not in content
        assert ".config/ingest" not in content
        assert "credentials.json" not in content
        assert "${HOME}" not in content
        assert "~/" not in content


def test_task3_spec_documents_prod_independence_from_local_cli_credentials() -> None:
    split_tasks = (
        REPO_ROOT
        / "docs"
        / "integration"
        / "sprint4"
        / "final-speedrun"
        / "split-tasks.md"
    ).read_text(encoding="utf-8")

    assert (
        "Prod-shaped startup must not depend on local CLI credentials or `~/.config`."
        in split_tasks
    )
    assert (
        "Telegram bootstrap should run automatically when `ENDPOINT_TELEGRAM__BOT_TOKEN` is set in `.env.telegram-bootstrap`."
        in split_tasks
    )
    assert (
        "Missing Telegram bootstrap token must warn loudly without blocking normal prod startup."
        in split_tasks
    )
