#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../.env"
export POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD PG_HOST PG_PORT

docker compose -f "$SCRIPT_DIR/../docker-compose.yml" down -v
docker compose -f "$SCRIPT_DIR/../docker-compose.yml" up db -d

echo "Waiting for Postgres to be ready..."
until docker compose -f "$SCRIPT_DIR/../docker-compose.yml" exec -T db pg_isready -U "$POSTGRES_USER" > /dev/null 2>&1; do
    sleep 1
done

"$SCRIPT_DIR/migrate.sh"
"$SCRIPT_DIR/seed.sh"
