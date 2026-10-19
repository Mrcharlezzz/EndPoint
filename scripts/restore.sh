#!/usr/bin/env bash
# Restore a compressed plain-SQL Postgres dump into the running `db` service.
#
# Usage:
#   scripts/restore.sh <snapshot.sql.gz>
#
# This is destructive: it drops and recreates the public schema before
# streaming the snapshot into psql. It expects snapshots created by
# scripts/snapshot.sh, which writes gzip-compressed plain SQL.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    echo "usage: scripts/restore.sh <snapshot.sql.gz>" >&2
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

SNAPSHOT_PATH="${1:-}"

if [[ -z "$SNAPSHOT_PATH" ]]; then
    usage
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

echo "Restoring snapshot '$SNAPSHOT_PATH' into database '$POSTGRES_DB'..."
docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db \
    psql \
        --username="$POSTGRES_USER" \
        --dbname="$POSTGRES_DB" \
        --set=ON_ERROR_STOP=on \
        --command="DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

gzip -dc "$SNAPSHOT_PATH" | docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db \
    psql \
        --username="$POSTGRES_USER" \
        --dbname="$POSTGRES_DB" \
        --set=ON_ERROR_STOP=on

echo "Restore complete: $SNAPSHOT_PATH"
