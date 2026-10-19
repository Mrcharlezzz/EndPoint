from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


class ScriptError(Exception):
    """The idle poll could not query the database."""


def pending_count_sql() -> str:
    return "SELECT count(*) FROM ingested_items WHERE processing_status = 'pending';"


def query_pending_count(repo_root: Path) -> int:
    postgres_db = _read_config_value(repo_root, "POSTGRES_DB")
    postgres_user = _read_config_value(repo_root, "POSTGRES_USER")
    if not postgres_db or not postgres_user:
        raise ScriptError(
            "POSTGRES_DB and POSTGRES_USER must be set in the environment or .env"
        )

    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(repo_root / "docker-compose.yml"),
            "exec",
            "-T",
            "db",
            "psql",
            f"--username={postgres_user}",
            f"--dbname={postgres_db}",
            "--no-align",
            "--tuples-only",
            "--quiet",
            "--command",
            pending_count_sql(),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        message = (
            result.stderr.strip() or result.stdout.strip() or "unknown psql failure"
        )
        raise ScriptError(f"Database query failed: {message}")

    raw_count = result.stdout.strip()
    try:
        return int(raw_count)
    except ValueError as exc:
        raise ScriptError(
            f"Database query returned invalid count: {raw_count!r}"
        ) from exc


def wait_for_idle(
    *,
    repo_root: Path,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while True:
        pending_count = query_pending_count(repo_root)
        if pending_count == 0:
            print("idle")
            return 0
        if time.monotonic() >= deadline:
            print(f"timeout pending={pending_count}")
            return 1
        sleep_seconds = min(
            poll_interval_seconds, max(0.0, deadline - time.monotonic())
        )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Poll ingested_items until processor work is idle."
    )
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    args = parser.parse_args(argv)

    if args.timeout < 0:
        print("--timeout must be non-negative", file=sys.stderr)
        return 2
    if args.poll_interval < 0:
        print("--poll-interval must be non-negative", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    try:
        return wait_for_idle(
            repo_root=repo_root,
            timeout_seconds=args.timeout,
            poll_interval_seconds=args.poll_interval,
        )
    except (OSError, ScriptError) as exc:
        print(f"Script error: {exc}", file=sys.stderr)
        return 2


def _read_config_value(repo_root: Path, key: str) -> str | None:
    env_value = os.environ.get(key)
    if env_value:
        return env_value

    env_path = repo_root / ".env"
    if not env_path.exists():
        return None

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        env_key, value = stripped.split("=", 1)
        if env_key.strip() == key:
            return _strip_env_value(value)
    return None


def _strip_env_value(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


if __name__ == "__main__":
    raise SystemExit(main())
