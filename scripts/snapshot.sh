#!/usr/bin/env bash
# Write a compressed Postgres dump of the running `db` service.
#
# Usage:
#   scripts/snapshot.sh <kind> [label]
#
#   <kind>   pre-process | post-process
#   [label]  optional, appended to the filename after the timestamp
#
# Output:
#   data/snapshots/<kind>/<UTC-timestamp>[-<label>].sql.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

read_env_var() {
    local key="$1"
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
    ' "$REPO_ROOT/.env"
}

POSTGRES_DB="${POSTGRES_DB:-$(read_env_var POSTGRES_DB)}"
POSTGRES_USER="${POSTGRES_USER:-$(read_env_var POSTGRES_USER)}"

if [[ -z "$POSTGRES_DB" || -z "$POSTGRES_USER" ]]; then
    echo "POSTGRES_DB and POSTGRES_USER must be set in the environment or .env" >&2
    exit 2
fi

KIND="${1:-}"
LABEL="${2:-}"

if [[ "$KIND" != "pre-process" && "$KIND" != "post-process" ]]; then
    echo "usage: scripts/snapshot.sh <pre-process|post-process> [label]" >&2
    exit 2
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
if [[ -n "$LABEL" ]]; then
    FILENAME="${TIMESTAMP}-${LABEL}.sql.gz"
else
    FILENAME="${TIMESTAMP}.sql.gz"
fi

OUT_DIR="$REPO_ROOT/data/snapshots/$KIND"
mkdir -p "$OUT_DIR"
OUT_PATH="$OUT_DIR/$FILENAME"

echo "Dumping database '$POSTGRES_DB' to $OUT_PATH..."
docker compose -f "$REPO_ROOT/docker-compose.yml" exec -T db \
    pg_dump \
        --username="$POSTGRES_USER" \
        --dbname="$POSTGRES_DB" \
        --format=plain \
        --no-owner \
        --no-privileges \
    | gzip --fast > "$OUT_PATH"

echo "Snapshot written: $OUT_PATH"
