#!/usr/bin/env bash
# Restore the latest post-process snapshot into the shared DB and start the
# dev processor entrypoint against it.
#
# Usage:
#   scripts/run_dev_processor_from_latest_post_process.sh
#   scripts/run_dev_processor_from_latest_post_process.sh --snapshot /path/to/snapshot.sql.gz
#   scripts/run_dev_processor_from_latest_post_process.sh --snapshot-dir /path/to/post-process
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    cat >&2 <<'EOF'
usage: scripts/run_dev_processor_from_latest_post_process.sh [--snapshot PATH] [--snapshot-dir DIR]
EOF
}

read_env_var() {
    local key="$1"
    local env_file="$REPO_ROOT/.env"
    if [[ ! -f "$env_file" ]]; then
        return 0
    fi
    awk -F= -v key="$key" '
        /^[[:space:]]*($|#)/ { next }
        {
            env_key = $1
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", env_key)
            if (env_key == key) {
                sub(/^[^=]*=/, "")
                gsub(/\r$/, "")
                print
                exit
            }
        }
    ' "$env_file"
}

latest_snapshot_in_dir() {
    local snapshot_dir="$1"
    find "$snapshot_dir" -maxdepth 1 -type f -name '*.sql.gz' | sort | tail -n 1
}

wait_for_db() {
    local postgres_user="$1"
    local postgres_db="$2"
    local attempts=30
    local sleep_seconds=1

    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if docker compose "${COMPOSE_ARGS[@]}" exec -T db \
            pg_isready --username="$postgres_user" --dbname="$postgres_db" >/dev/null 2>&1; then
            return 0
        fi
        sleep "$sleep_seconds"
    done

    echo "Database did not become ready in time." >&2
    return 1
}

SNAPSHOT_PATH=""
SNAPSHOT_DIR="$REPO_ROOT/data/snapshots/post-process"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --snapshot)
            SNAPSHOT_PATH="${2:-}"
            shift 2
            ;;
        --snapshot-dir)
            SNAPSHOT_DIR="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "$SNAPSHOT_PATH" ]]; then
    SNAPSHOT_PATH="$(latest_snapshot_in_dir "$SNAPSHOT_DIR")"
fi

if [[ -z "$SNAPSHOT_PATH" ]]; then
    echo "No post-process snapshots found in: $SNAPSHOT_DIR" >&2
    exit 2
fi

if [[ ! -f "$SNAPSHOT_PATH" ]]; then
    echo "Snapshot not found: $SNAPSHOT_PATH" >&2
    exit 2
fi

POSTGRES_DB="${POSTGRES_DB:-$(read_env_var POSTGRES_DB)}"
POSTGRES_USER="${POSTGRES_USER:-$(read_env_var POSTGRES_USER)}"

if [[ -z "$POSTGRES_DB" || -z "$POSTGRES_USER" ]]; then
    echo "POSTGRES_DB and POSTGRES_USER must be set in the environment or .env" >&2
    exit 2
fi

COMPOSE_ARGS=()

echo "Selected latest post-process snapshot: $SNAPSHOT_PATH"
echo "Stopping dev processor if it is already running..."
docker compose "${COMPOSE_ARGS[@]}" stop processor >/dev/null 2>&1 || true

echo "Starting db service..."
docker compose "${COMPOSE_ARGS[@]}" up db -d

echo "Waiting for db to accept connections..."
wait_for_db "$POSTGRES_USER" "$POSTGRES_DB"

echo "Restoring post-process snapshot..."
"$REPO_ROOT/scripts/restore.sh" "$SNAPSHOT_PATH"

echo "Starting dev processor..."
docker compose "${COMPOSE_ARGS[@]}" up processor -d --build --force-recreate

echo "Ready: dev processor is up against the restored post-process snapshot."
echo "Inspect status with:"
echo "  docker compose ps db processor"
echo "Tail logs with:"
echo "  docker compose logs -f processor"
