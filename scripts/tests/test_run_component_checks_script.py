from __future__ import annotations

import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run_component_checks.sh"


def _make_fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_log = tmp_path / "uv.log"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s|%s\\n' "$PWD" "$*" >> "{uv_log}"
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    return bin_dir, uv_log


def test_help_lists_safe_component_targets() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0
    assert "ingestion" in result.stdout
    assert "ingestion-e2e" in result.stdout
    assert "presentation-api" in result.stdout
    assert "db-core" in result.stdout
    assert "processor" in result.stdout


def test_ingestion_runs_safe_checks_without_e2e(tmp_path: Path) -> None:
    bin_dir, uv_log = _make_fake_uv(tmp_path)

    result = subprocess.run(
        ["bash", str(SCRIPT), "ingestion"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    calls = uv_log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        f"{REPO_ROOT / 'src/ingestion'}|run --with ruff ruff check .",
        f"{REPO_ROOT / 'src/ingestion'}|run --with ruff ruff format --check .",
        f"{REPO_ROOT / 'src/ingestion'}|run --with pyright pyright",
        f"{REPO_ROOT / 'src/ingestion'}|run pytest tests/ -m not e2e -v",
    ]


def test_ingestion_e2e_runs_in_isolation(tmp_path: Path) -> None:
    bin_dir, uv_log = _make_fake_uv(tmp_path)

    result = subprocess.run(
        ["bash", str(SCRIPT), "ingestion-e2e"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    calls = uv_log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        f"{REPO_ROOT / 'src/ingestion'}|run pytest tests/e2e/ -v -m e2e",
    ]


def test_presentation_api_runs_component_checks(tmp_path: Path) -> None:
    bin_dir, uv_log = _make_fake_uv(tmp_path)

    result = subprocess.run(
        ["bash", str(SCRIPT), "presentation-api"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    calls = uv_log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        f"{REPO_ROOT / 'src/backend/presentation-api'}|run --with ruff ruff check .",
        f"{REPO_ROOT / 'src/backend/presentation-api'}|run --with ruff ruff format --check .",
        f"{REPO_ROOT / 'src/backend/presentation-api'}|run --with pyright pyright",
        f"{REPO_ROOT / 'src/backend/presentation-api'}|run pytest tests/ -v",
    ]


def test_rejects_unknown_component() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "nope"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 2
    assert "unknown target: nope" in result.stderr
