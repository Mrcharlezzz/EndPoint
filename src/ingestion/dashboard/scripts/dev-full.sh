#!/usr/bin/env bash
# Boot the real ingestion daemon on :8080 alongside Vite on :5173 so
# dashboard dev doesn't require a second terminal. Ctrl-C kills both.
#
# One-time setup:
#   1. `docker compose up -d db` (Postgres).
#   2. Create `dashboard/.env.local` with `VITE_DAEMON_URL=http://localhost:8080`.
#
# Then from `dashboard/`: `pnpm dev:full`.
set -euo pipefail

# Kill the whole process group on exit so both children go down together.
cleanup() {
  trap - EXIT INT TERM
  kill 0 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Resolve to <repo>/src/ingestion/ (this file is at dashboard/scripts/).
INGESTION_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)/.."

(
  cd "$INGESTION_ROOT"
  exec uv run python scripts/e2e_dashboard_bootstrap.py --keep-alive --port 8080
) &

# Give the daemon a head-start so Vite's initial proxy probe doesn't race.
# Vite itself retries the daemon, so this is a nicety, not a correctness gate.
sleep 1

exec pnpm vite
