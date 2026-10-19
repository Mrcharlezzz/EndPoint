#!/usr/bin/env bash
set -euo pipefail

seed_dir="${PRODUCTION_SEED_DIR:-/seeds}"
sqlite_path="${ENDPOINT_DAEMON__SQLITE_PATH:-/var/lib/ingestor/state.db}"
artifact="${SEED_ARTIFACT:-}"

if [[ -z "$artifact" ]]; then
    artifact="$(find "$seed_dir" -maxdepth 1 -type f -name '*.json.gz' | sort | tail -n 1)"
fi

if [[ -z "$artifact" ]]; then
    echo "No production seed artifacts found under $seed_dir" >&2
    exit 1
fi

exec uv run python scripts/production_seed_import.py "$artifact" --sqlite-path "$sqlite_path"
