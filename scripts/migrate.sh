#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../.env"
export POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD PG_HOST PG_PORT

cd "$SCRIPT_DIR/../src/common/db_core"
python manage.py migrate
