#!/usr/bin/env bash
# Run one processor integration iteration against a pre-process snapshot.
#
# Usage:
#   scripts/iterate.sh <pre-process-snapshot> [--timeout SECONDS]
#       [--poll-interval SECONDS] [--label NAME]
#
# The script drives the production `processor` service: stop, restore,
# start, capture logs, poll the database for idle/timeout, stop, then invoke
# scripts/report.py for the run directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    cat >&2 <<'EOF'
usage: scripts/iterate.sh <pre-process-snapshot> [--timeout SECONDS] [--poll-interval SECONDS] [--label NAME]
EOF
}

is_non_negative_integer() {
    [[ "$1" =~ ^[0-9]+$ ]]
}

SNAPSHOT_PATH="${1:-}"
if [[ -z "$SNAPSHOT_PATH" ]]; then
    usage
    exit 2
fi
shift

TIMEOUT_SECONDS="900"
POLL_INTERVAL_SECONDS="5"
LABEL=""
RUN_ROOT="$REPO_ROOT/data/runs"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --timeout)
            TIMEOUT_SECONDS="${2:-}"
            shift 2
            ;;
        --poll-interval)
            POLL_INTERVAL_SECONDS="${2:-}"
            shift 2
            ;;
        --label)
            LABEL="${2:-}"
            shift 2
            ;;
        --run-root)
            RUN_ROOT="${2:-}"
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

if [[ ! -f "$SNAPSHOT_PATH" ]]; then
    echo "Snapshot not found: $SNAPSHOT_PATH" >&2
    exit 2
fi

if ! is_non_negative_integer "$TIMEOUT_SECONDS"; then
    echo "--timeout must be a non-negative integer" >&2
    exit 2
fi
if ! is_non_negative_integer "$POLL_INTERVAL_SECONDS"; then
    echo "--poll-interval must be a non-negative integer" >&2
    exit 2
fi
if [[ -n "$LABEL" && ! "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "--label may contain only letters, numbers, '.', '_', and '-'" >&2
    exit 2
fi

TIMESTAMP="${ENDPOINT_ITERATE_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
if [[ -n "$LABEL" ]]; then
    RUN_NAME="$TIMESTAMP-$LABEL"
else
    RUN_NAME="$TIMESTAMP"
fi
RUN_DIR="$RUN_ROOT/$RUN_NAME"
PROCESSOR_LOG="$RUN_DIR/processor.log"
STATE_FILE="$RUN_DIR/state.env"
RUN_START_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TERMINATION_CAUSE=""

mkdir -p "$RUN_DIR"
printf 'processor\n' > "$RUN_DIR/kind"

write_state() {
    cat > "$STATE_FILE" <<EOF
KIND=processor
SNAPSHOT_PATH=$SNAPSHOT_PATH
RUN_DIR=$RUN_DIR
RUN_STARTED_AT=$RUN_START_ISO
TIMEOUT_SECONDS=$TIMEOUT_SECONDS
POLL_INTERVAL_SECONDS=$POLL_INTERVAL_SECONDS
TERMINATION_CAUSE=$TERMINATION_CAUSE
EOF
}

write_state

COMPOSE_ARGS=(-f "$REPO_ROOT/docker-compose.yml")

PROCESSOR_STARTED=0
LOG_PID=""

cleanup() {
    if [[ -n "${LOG_PID:-}" ]] && kill -0 "$LOG_PID" 2>/dev/null; then
        kill "$LOG_PID" 2>/dev/null || true
        wait "$LOG_PID" 2>/dev/null || true
    fi
    if [[ "$PROCESSOR_STARTED" -eq 1 ]]; then
        docker compose "${COMPOSE_ARGS[@]}" stop processor >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

echo "Stopping processor..."
docker compose "${COMPOSE_ARGS[@]}" stop processor

echo "Restoring snapshot..."
"$REPO_ROOT/scripts/restore.sh" "$SNAPSHOT_PATH"

echo "Starting processor..."
docker compose "${COMPOSE_ARGS[@]}" up processor -d
PROCESSOR_STARTED=1

docker compose "${COMPOSE_ARGS[@]}" logs -f --since="$RUN_START_ISO" processor \
    > "$PROCESSOR_LOG" 2>&1 &
LOG_PID="$!"

set +e
uv run python "$REPO_ROOT/scripts/wait_for_processor_idle.py" \
    --timeout "$TIMEOUT_SECONDS" \
    --poll-interval "$POLL_INTERVAL_SECONDS"
POLL_STATUS="$?"
set -e

case "$POLL_STATUS" in
    0)
        TERMINATION_CAUSE="idle"
        ;;
    1)
        TERMINATION_CAUSE="timeout"
        ;;
    *)
        TERMINATION_CAUSE="poll_error"
        write_state
        exit 2
        ;;
esac
write_state

echo "Stopping processor after $TERMINATION_CAUSE..."
docker compose "${COMPOSE_ARGS[@]}" stop processor
PROCESSOR_STARTED=0

if [[ -n "$LOG_PID" ]] && kill -0 "$LOG_PID" 2>/dev/null; then
    kill "$LOG_PID" 2>/dev/null || true
    wait "$LOG_PID" 2>/dev/null || true
fi
LOG_PID=""

echo "Writing report..."
uv run python "$REPO_ROOT/scripts/report.py" "$RUN_DIR" \
    --termination-cause "$TERMINATION_CAUSE"

echo "Writing static inspector..."
uv run python "$REPO_ROOT/scripts/static_run_inspector.py" "$RUN_DIR"

echo "Iteration complete: $RUN_DIR"
