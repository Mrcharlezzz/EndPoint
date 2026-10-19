#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    cat <<'EOF'
usage: scripts/run_component_checks.sh <target>

Run the Definition of Done checks for one safe component target.

Targets:
  ingestion         Ruff + format + pyright + pytest tests/ -m "not e2e"
  ingestion-e2e     Run ingestion end-to-end tests only
  presentation-api  Ruff + format + pyright + pytest tests/
  db-core           Ruff + format + pyright + pytest tests/
  processor         Ruff + format + pyright + pytest tests/
  ui                npm run lint + npm run test + npm run build
  all               Run the safe non-e2e checks for all components above

Examples:
  bash scripts/run_component_checks.sh ingestion
  bash scripts/run_component_checks.sh presentation-api
  bash scripts/run_component_checks.sh ui
  bash scripts/run_component_checks.sh ingestion-e2e
  bash scripts/run_component_checks.sh all
EOF
}

error() {
    echo "error: $*" >&2
}

run_cmd() {
    local component_dir="$1"
    shift

    (
        cd "$component_dir"
        uv "$@"
    )
}

run_standard_checks() {
    local component_dir="$1"
    shift

    run_cmd "$component_dir" run --with ruff ruff check .
    run_cmd "$component_dir" run --with ruff ruff format --check .
    run_cmd "$component_dir" run --with pyright pyright
    local pytest_cmd=(run pytest tests/)
    pytest_cmd+=("$@")
    pytest_cmd+=(-v)
    run_cmd "$component_dir" "${pytest_cmd[@]}"
}

run_npm_checks() {
    local component_dir="$1"

    (
        cd "$component_dir"
        npm run lint
        npm run test
        npm run build
    )
}

run_target() {
    local target="$1"

    case "$target" in
        ingestion)
            run_standard_checks "$REPO_ROOT/src/ingestion" -m "not e2e"
            ;;
        ingestion-e2e)
            run_cmd "$REPO_ROOT/src/ingestion" run pytest tests/e2e/ -v -m e2e
            ;;
        presentation-api)
            run_standard_checks "$REPO_ROOT/src/backend/presentation-api"
            ;;
        db-core)
            run_standard_checks "$REPO_ROOT/src/common/db_core"
            ;;
        processor)
            run_standard_checks "$REPO_ROOT/src/processor"
            ;;
        ui)
            run_npm_checks "$REPO_ROOT/src/EndpointNextJS/endpoint-nextjs"
            ;;
        all)
            run_target ingestion
            run_target presentation-api
            run_target db-core
            run_target processor
            run_target ui
            ;;
        *)
            error "unknown target: $target"
            usage >&2
            exit 2
            ;;
    esac
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

if [[ "$#" -ne 1 ]]; then
    usage >&2
    exit 2
fi

run_target "$1"
