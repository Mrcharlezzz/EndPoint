#!/usr/bin/env bash
# Full hybrid dev loop: postgres in docker, every app service on the host.
# Start: scripts/dev.sh  (or from repo root: bash scripts/dev.sh)
# Stop:  Ctrl-C. Host services stop; the postgres container is left running.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${DEV_ENV_FILE:-$REPO_ROOT/.env}"

INGESTION_DIR="$REPO_ROOT/src/ingestion"
DASHBOARD_DIR="$INGESTION_DIR/dashboard"
DB_CORE_DIR="$REPO_ROOT/src/common/db_core"
PRESENTATION_API_DIR="$REPO_ROOT/src/backend/presentation-api"
FRONTEND_DIR="$REPO_ROOT/src/EndpointNextJS/endpoint-nextjs"
PROCESSOR_DIR="$REPO_ROOT/src/processor"

LOG_DIR="${ENDPOINT_DEV_LOG_DIR:-/tmp/endpoint-dev}"
RUN_DIR="${ENDPOINT_DEV_RUN_DIR:-/tmp/endpoint-dev/run}"
RUN_STATE_FILE="$RUN_DIR/services.tsv"

service_names=()
service_pgids=()
service_logs=()
service_ports=()
selected_ports=()
current_run_state_written=0

usage() {
    cat >&2 <<'EOF'
usage: scripts/dev.sh [--check] [--no-seed] [--no-process] [--reset] [--recency DAYS] [--seed-snapshot FILE]

Start the full Endpoint hybrid dev instance:

  Docker:
    db                  localhost:5432
    GlitchTip           http://localhost:8011

  Host:
    frontend            http://localhost:3000
    headless auth       http://localhost:8000
    presentation-api    http://localhost:8001
    processor           worker loop
    daemon API          http://localhost:8765
    daemon dashboard    http://localhost:5173

Options:
  --check       Validate env, dependencies, and resolvable ports, then exit.
  --no-seed     Skip loading the post-process snapshot after migrations.
  --no-process  Do not start the processor worker loop.
  --reset       Remove local dev state and compose volumes before startup.
  --recency     Override daemon ingestion recency filtering in days.
  --seed-snapshot
                Load this post-process snapshot instead of the latest one.
  -h, --help    Show this help.

Environment:
  DEV_ENV_FILE=/path/to/.env              Use a different env file.
  ENDPOINT_DEV_LOG_DIR=/path/to/logs      Override /tmp/endpoint-dev.
  ENDPOINT_DEV_RUN_DIR=/path/to/run-state Override /tmp/endpoint-dev/run.
  ENDPOINT_DEV_SKIP_SYNC=1                Skip uv sync before startup.
  ENDPOINT_DEV_SKIP_PORT_CHECK=1          Use default ports without conflict checks.
  ENDPOINT_DEV_FRONTEND_PORT=3000         Preferred frontend port.
  ENDPOINT_DEV_HEADLESS_PORT=8000         Preferred headless auth port.
  ENDPOINT_DEV_PRESENTATION_API_PORT=8001 Preferred presentation-api port.
  ENDPOINT_DEV_DAEMON_API_PORT=8765       Preferred daemon API port.
  ENDPOINT_DEV_DASHBOARD_PORT=5173        Preferred daemon dashboard port.
  ENDPOINT_DEV_GLITCHTIP_PORT=8011        Preferred GlitchTip UI port.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

check_only=0
no_seed=0
no_process=0
reset_requested=0
recency_days=""
seed_snapshot=""
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --check)
            check_only=1
            ;;
        --no-seed)
            no_seed=1
            ;;
        --no-process)
            no_process=1
            ;;
        --reset)
            reset_requested=1
            ;;
        --recency)
            shift
            if [[ "$#" -eq 0 ]] || ! [[ "$1" =~ ^[1-9][0-9]*$ ]]; then
                echo "error: --recency requires a positive integer day count" >&2
                usage
                exit 2
            fi
            recency_days="$1"
            ;;
        --seed-snapshot)
            shift
            if [[ "$#" -eq 0 ]] || [[ -z "$1" ]]; then
                echo "error: --seed-snapshot requires a snapshot filename or path" >&2
                usage
                exit 2
            fi
            seed_snapshot="$1"
            ;;
        *)
            usage
            exit 2
            ;;
    esac
    shift
done

error() {
    echo "error: $*" >&2
}

require_command() {
    local command_name="$1"
    local install_hint="$2"

    if ! command -v "$command_name" >/dev/null 2>&1; then
        error "$command_name is required"
        echo "       $install_hint" >&2
        exit 2
    fi
}

require_executable() {
    local path="$1"
    local service="$2"
    local install_hint="$3"

    if [[ ! -x "$path" ]]; then
        error "$service dependency is missing: $path"
        echo "       $install_hint" >&2
        exit 2
    fi
}

resolve_seed_snapshot() {
    local requested="$1"
    local candidate=""

    if [[ -z "$requested" ]]; then
        return 0
    fi

    if [[ "$requested" = /* ]]; then
        candidate="$requested"
    elif [[ "$requested" == */* ]]; then
        candidate="$REPO_ROOT/$requested"
    else
        candidate="$REPO_ROOT/data/snapshots/post-process/$requested"
    fi

    if [[ ! -f "$candidate" ]]; then
        error "seed snapshot not found: $requested"
        echo "       Expected a .sql.gz file under data/snapshots/post-process, or an existing path." >&2
        exit 2
    fi

    if [[ "$(cd "$(dirname "$candidate")" && pwd -P)" != "$(cd "$REPO_ROOT/data/snapshots/post-process" && pwd -P)" ]]; then
        error "seed snapshot must live under data/snapshots/post-process: $requested"
        exit 2
    fi

    seed_snapshot="$(basename "$candidate")"
}

generate_dev_secret() {
    local token_bytes="$1"
    local python_bin=""

    for python_bin in python3 python; do
        if command -v "$python_bin" >/dev/null 2>&1; then
            "$python_bin" - "$token_bytes" <<'PY'
import secrets
import sys

print(secrets.token_urlsafe(int(sys.argv[1])))
PY
            return 0
        fi
    done

    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 "$token_bytes" | tr '+/' '-_' | tr -d '=\n'
        printf '\n'
        return 0
    fi

    error "cannot generate local dev secret; install Python or OpenSSL, or set it in $ENV_FILE"
    exit 2
}

append_generated_env_var() {
    local var_name="$1"
    local var_value="$2"

    if [[ ! -w "$ENV_FILE" ]]; then
        error "cannot write generated $var_name to env file: $ENV_FILE"
        exit 2
    fi

    {
        if [[ -s "$ENV_FILE" ]] && [[ "$(tail -c 1 "$ENV_FILE")" != "" ]]; then
            printf '\n'
        fi
        printf '%s=%s\n' "$var_name" "$var_value"
    } >> "$ENV_FILE"
}

ensure_dev_secret() {
    local var_name="$1"
    local token_bytes="$2"
    local generated_secret=""

    if [[ -n "${!var_name:-}" ]]; then
        return 0
    fi

    generated_secret="$(generate_dev_secret "$token_bytes")"
    append_generated_env_var "$var_name" "$generated_secret"
    export "$var_name=$generated_secret"
    echo "[dev] generated $var_name in $ENV_FILE"
}

port_is_listening() {
    local port="$1"

    if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$port" -sTCP:LISTEN -nP >/dev/null 2>&1; then
        return 0
    fi

    if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$port" 2>/dev/null | awk 'NR > 1 { found = 1 } END { exit found ? 0 : 1 }'; then
        return 0
    fi

    return 1
}

port_already_selected() {
    local candidate="$1"
    local selected_port=""

    for selected_port in "${selected_ports[@]}"; do
        if [[ "$selected_port" == "$candidate" ]]; then
            return 0
        fi
    done

    return 1
}

select_port() {
    local result_var="$1"
    local service="$2"
    local preferred="$3"
    local fallback_start="$4"
    local fallback_end="$5"
    local port=""

    if ! [[ "$preferred" =~ ^[0-9]+$ && "$fallback_start" =~ ^[0-9]+$ && "$fallback_end" =~ ^[0-9]+$ ]]; then
        error "invalid port configuration for $service"
        exit 2
    fi

    if [[ "${ENDPOINT_DEV_SKIP_PORT_CHECK:-}" == "1" ]]; then
        selected_ports+=("$preferred")
        printf -v "$result_var" '%s' "$preferred"
        return 0
    fi

    if ! port_already_selected "$preferred" && ! port_is_listening "$preferred"; then
        selected_ports+=("$preferred")
        printf -v "$result_var" '%s' "$preferred"
        return 0
    fi

    port="$fallback_start"
    while (( port <= fallback_end )); do
        if ! port_already_selected "$port" && ! port_is_listening "$port"; then
            selected_ports+=("$port")
            printf -v "$result_var" '%s' "$port"
            echo "[dev] $service port $preferred is in use; using $port"
            return 0
        fi
        port=$((port + 1))
    done

    error "no free port found for $service in range $preferred,$fallback_start-$fallback_end"
    exit 2
}

resolve_dev_ports() {
    selected_ports=()

    select_port FRONTEND_PORT "frontend" "${ENDPOINT_DEV_FRONTEND_PORT:-3000}" 3001 3099
    select_port HEADLESS_AUTH_PORT "headless auth" "${ENDPOINT_DEV_HEADLESS_PORT:-8000}" 8002 8099
    select_port PRESENTATION_API_PORT "presentation-api" "${ENDPOINT_DEV_PRESENTATION_API_PORT:-8001}" 8002 8099
    select_port DAEMON_API_PORT "daemon API" "${ENDPOINT_DEV_DAEMON_API_PORT:-8765}" 8766 8799
    select_port DASHBOARD_PORT "daemon dashboard" "${ENDPOINT_DEV_DASHBOARD_PORT:-5173}" 5174 5199

    FRONTEND_URL="http://localhost:$FRONTEND_PORT"
    HEADLESS_AUTH_URL="http://localhost:$HEADLESS_AUTH_PORT"
    PRESENTATION_API_URL="http://localhost:$PRESENTATION_API_PORT"
    DAEMON_API_URL="http://localhost:$DAEMON_API_PORT"
    DASHBOARD_URL="http://localhost:$DASHBOARD_PORT"
}

resolve_glitchtip_port() {
    GLITCHTIP_PORT="${ENDPOINT_DEV_GLITCHTIP_PORT:-${GLITCHTIP_PORT:-8011}}"
    if ! [[ "$GLITCHTIP_PORT" =~ ^[0-9]+$ ]]; then
        error "invalid port configuration for GlitchTip"
        exit 2
    fi
    GLITCHTIP_URL="http://localhost:$GLITCHTIP_PORT"
}

configure_glitchtip_env() {
    export GLITCHTIP_PORT
    export GLITCHTIP_DOMAIN="$GLITCHTIP_URL"
    export ENDPOINT_DAEMON__SENTRY__DSN="http://00000000000040008000000000000001@localhost:$GLITCHTIP_PORT/1"
    export ENDPOINT_DAEMON__SENTRY__PUBLIC_URL="$GLITCHTIP_URL"
    export NEXT_PUBLIC_SENTRY_DSN="http://00000000000040008000000000000002@localhost:$GLITCHTIP_PORT/2"
    export SENTRY_URL="$GLITCHTIP_URL/"
    export PRESENTATION_API_SENTRY_DSN="http://00000000000040008000000000000003@localhost:$GLITCHTIP_PORT/3"
}

wait_for_url() {
    local name="$1"
    local url="$2"
    local timeout_seconds="$3"
    local elapsed=0

    echo -n "[dev] waiting for $name"
    while (( elapsed < timeout_seconds )); do
        if curl -fsS "$url" >/dev/null 2>&1; then
            echo " OK"
            return 0
        fi
        echo -n "."
        sleep 2
        elapsed=$((elapsed + 2))
    done

    echo
    error "$name did not become ready within ${timeout_seconds}s: $url"
    exit 1
}

wait_for_db_health() {
    local cid=""
    local status=""

    echo -n "[dev] waiting for postgres to be healthy"
    cid="$(docker compose --env-file "$ENV_FILE" ps -q db)"
    if [[ -z "$cid" ]]; then
        echo
        error "could not resolve db container id"
        exit 1
    fi

    for _ in $(seq 1 60); do
        status="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo starting)"
        if [[ "$status" == "healthy" ]]; then
            echo " OK"
            return 0
        fi
        echo -n "."
        sleep 1
    done

    echo
    error "postgres did not become healthy in 60s"
    exit 1
}

run_seed_if_needed() {
    local row_count=""
    local table_exists=""

    if [[ "${ENDPOINT_DEV_NO_SEED:-}" == "1" || "$no_seed" == "1" ]]; then
        echo "[dev] skipping seed (--no-seed or ENDPOINT_DEV_NO_SEED=1)"
        return 0
    fi

    table_exists="$(
        docker compose --env-file "$ENV_FILE" exec -T db \
            psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
            "SELECT to_regclass('public.ingested_items') IS NOT NULL" \
            2>/dev/null | tr -d '[:space:]'
    )"

    if [[ -z "$table_exists" ]]; then
        error "failed to determine whether the ingested_items table exists in the dev database"
        exit 1
    fi

    if [[ "$table_exists" != "t" ]]; then
        row_count="0"
    else
        row_count="$(
            docker compose --env-file "$ENV_FILE" exec -T db \
                psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
                "SELECT COUNT(*)::text FROM public.ingested_items" \
                2>/dev/null | tr -d '[:space:]'
        )"
    fi

    if [[ -z "$row_count" ]]; then
        error "failed to determine whether the dev database is already seeded"
        exit 1
    fi

    if [[ "$row_count" != "0" ]]; then
        echo "[dev] database already has content ($row_count ingested_items); skipping seed"
        return 0
    fi

    if [[ -n "$seed_snapshot" ]]; then
        echo "[dev] fresh database detected; loading post-process snapshot: $seed_snapshot"
        docker compose --env-file "$ENV_FILE" run --rm \
            -e "ENDPOINT_DEV_SEED_SNAPSHOT=$seed_snapshot" \
            seed-post-process-snapshot
    else
        echo "[dev] fresh database detected; loading post-process snapshot..."
        docker compose --env-file "$ENV_FILE" run --rm seed-post-process-snapshot
    fi
    echo "[dev] snapshot loaded"
}

import_cli_credentials_if_available() {
    local credentials_file="${HOME}/.config/ingest/credentials.json"

    if [[ ! -f "$credentials_file" ]]; then
        echo "[dev] no CLI credentials found; skipping platform credential import"
        echo "[dev] Real Teams/Moodle syncs can be enabled by logging in through the app"
        return 0
    fi

    echo "[dev] importing CLI credentials..."
    docker compose --env-file "$ENV_FILE" run --rm import-cli-tokens
}

bootstrap_telegram_credential_if_available() {
    if [[ -z "${ENDPOINT_TELEGRAM__BOT_TOKEN:-}" ]]; then
        echo "[dev] ENDPOINT_TELEGRAM__BOT_TOKEN not set; live Telegram polling disabled"
        return 0
    fi

    echo "[dev] bootstrapping Telegram credential from ENDPOINT_TELEGRAM__BOT_TOKEN..."
    (cd "$INGESTION_DIR" && uv run python scripts/bootstrap_telegram_credential.py)
    unset ENDPOINT_TELEGRAM__BOT_TOKEN
}

service_alive() {
    local name="$1"
    local i=""

    for i in "${!service_names[@]}"; do
        if [[ "${service_names[$i]}" == "$name" ]]; then
            kill -0 -- "-${service_pgids[$i]}" 2>/dev/null
            return
        fi
    done

    return 1
}

service_log_for() {
    local name="$1"
    local i=""

    for i in "${!service_names[@]}"; do
        if [[ "${service_names[$i]}" == "$name" ]]; then
            printf '%s\n' "${service_logs[$i]}"
            return 0
        fi
    done

    return 1
}

tail_service_log() {
    local name="$1"
    local log_file=""

    log_file="$(service_log_for "$name" 2>/dev/null || true)"
    if [[ -n "$log_file" && -f "$log_file" ]]; then
        echo "[dev] last 40 log lines for $name ($log_file):" >&2
        tail -n 40 "$log_file" >&2 || true
    fi
}

check_service_still_running() {
    local name="$1"
    local seconds="$2"
    local elapsed=0

    echo -n "[dev] checking $name"
    while (( elapsed < seconds )); do
        if ! service_alive "$name"; then
            echo
            error "$name exited during startup"
            tail_service_log "$name"
            exit 1
        fi
        echo -n "."
        sleep 1
        elapsed=$((elapsed + 1))
    done
    echo " OK"
}

wait_for_http() {
    local name="$1"
    local url="$2"
    local timeout_seconds="$3"
    local elapsed=0

    echo -n "[dev] waiting for $name"
    while (( elapsed < timeout_seconds )); do
        if ! service_alive "$name"; then
            echo
            error "$name exited before it became ready"
            tail_service_log "$name"
            exit 1
        fi

        if curl -fsS "$url" >/dev/null 2>&1 && service_alive "$name"; then
            echo " OK"
            return 0
        fi

        echo -n "."
        sleep 1
        elapsed=$((elapsed + 1))
    done

    echo
    error "$name did not become ready within ${timeout_seconds}s: $url"
    tail_service_log "$name"
    exit 1
}

wait_for_headless_auth() {
    local code=""
    local elapsed=0
    local probe_url="$HEADLESS_AUTH_URL/internal/auth/teams/__endpoint_dev_probe__/payload"

    wait_for_http "headless auth" "$HEADLESS_AUTH_URL/" 90

    echo -n "[dev] checking headless internal auth"
    while (( elapsed < 30 )); do
        if ! service_alive "headless auth"; then
            echo
            error "headless auth exited before internal auth became ready"
            tail_service_log "headless auth"
            exit 1
        fi

        code="$(
            curl -sS -o /dev/null -w '%{http_code}' \
                -H "X-Internal-Secret: $ENDPOINT_HEADLESS__INTERNAL_SECRET" \
                "$probe_url" 2>/dev/null || true
        )"

        if [[ "$code" == "404" ]]; then
            echo " OK"
            return 0
        fi

        if [[ "$code" == "401" || "$code" == "503" ]]; then
            echo
            error "headless internal auth probe returned HTTP $code"
            tail_service_log "headless auth"
            exit 1
        fi

        echo -n "."
        sleep 1
        elapsed=$((elapsed + 1))
    done

    echo
    error "headless internal auth probe did not return the expected 404 in 30s"
    tail_service_log "headless auth"
    exit 1
}

verify_playwright_browser() {
    echo -n "[dev] checking Playwright Chromium"
    if (
        cd "$INGESTION_DIR"
        uv run --extra daemon --extra cli python - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    browser.close()
PY
    ) >/dev/null 2>&1; then
        echo " OK"
        return 0
    fi

    echo
    error "Playwright Chromium could not launch"
    echo "       Run: cd $INGESTION_DIR && uv run --extra cli playwright install chromium" >&2
    exit 1
}

write_run_state() {
    local i=""

    mkdir -p "$RUN_DIR"
    : >"$RUN_STATE_FILE"
    for i in "${!service_names[@]}"; do
        printf '%s\t%s\t%s\t%s\n' \
            "${service_names[$i]}" \
            "${service_pgids[$i]}" \
            "${service_ports[$i]}" \
            "${service_logs[$i]}" >>"$RUN_STATE_FILE"
    done
    current_run_state_written=1
}

clear_run_state() {
    if [[ "$current_run_state_written" == "1" ]]; then
        rm -f "$RUN_STATE_FILE"
        current_run_state_written=0
    fi
}

pid_is_alive() {
    local pid="$1"

    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

stop_pid() {
    local pid="$1"
    local label="$2"

    if ! pid_is_alive "$pid"; then
        return 0
    fi

    echo "[dev] stopping $label (pid $pid)"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 8); do
        pid_is_alive "$pid" || return 0
        sleep 0.5
    done
    kill -KILL "$pid" 2>/dev/null || true
}

stop_process_group() {
    local pgid="$1"
    local label="$2"

    if ! [[ "$pgid" =~ ^[0-9]+$ ]] || ! kill -0 -- "-$pgid" 2>/dev/null; then
        return 0
    fi

    echo "[dev] stopping $label (pgid $pgid)"
    kill -TERM -- "-$pgid" 2>/dev/null || true
}

wait_for_process_groups_to_exit() {
    local -a pgids=("$@")
    local pgid=""
    local alive=0

    for _ in $(seq 1 8); do
        alive=0
        for pgid in "${pgids[@]}"; do
            kill -0 -- "-$pgid" 2>/dev/null && alive=1
        done
        [[ "$alive" -eq 0 ]] && return 0
        sleep 0.5
    done

    for pgid in "${pgids[@]}"; do
        kill -KILL -- "-$pgid" 2>/dev/null || true
    done
}

stop_previous_dev_instance() {
    local -a pgids=()
    local name=""
    local pgid=""
    local port=""
    local log_file=""
    local line=""
    local found_alive=0

    if [[ ! -f "$RUN_STATE_FILE" ]]; then
        return 0
    fi

    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" ]] && continue
        IFS=$'\t' read -r name pgid port log_file <<<"$line"
        if [[ "$pgid" =~ ^[0-9]+$ ]] && kill -0 -- "-$pgid" 2>/dev/null; then
            if ((found_alive == 0)); then
                echo "[dev] stopping previous hybrid dev instance..."
                found_alive=1
            fi
            stop_process_group "$pgid" "$name${port:+ on port $port}"
            pgids+=("$pgid")
        fi
    done <"$RUN_STATE_FILE"

    if ((found_alive == 1)); then
        wait_for_process_groups_to_exit "${pgids[@]}"
    fi

    rm -f "$RUN_STATE_FILE"
}

reset_dev_state() {
    echo "[dev] resetting local dev state..."
    stop_previous_dev_instance
    stop_legacy_daemon_lock_holder
    docker compose --env-file "$ENV_FILE" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$LOG_DIR" "$RUN_DIR"
    rm -f "$ENDPOINT_DAEMON__SQLITE_PATH" "$ENDPOINT_DAEMON__SQLITE_PATH.lock"
}

legacy_endpoint_daemon_pid() {
    local pid="$1"
    local command=""
    local cwd=""

    command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"

    [[ "$cwd" == "$INGESTION_DIR" && "$command" == *"ingestion.daemon"* ]]
}

stop_legacy_daemon_lock_holder() {
    local lock_file="$ENDPOINT_DAEMON__SQLITE_PATH.lock"
    local pid=""
    local command=""

    if [[ ! -f "$lock_file" ]]; then
        return 0
    fi

    pid="$(head -n 1 "$lock_file" 2>/dev/null || true)"
    if ! pid_is_alive "$pid"; then
        return 0
    fi

    if legacy_endpoint_daemon_pid "$pid"; then
        stop_pid "$pid" "previous Endpoint daemon lock holder"
        return 0
    fi

    command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    error "daemon state is locked by live PID $pid"
    echo "       lock: $lock_file" >&2
    if [[ -n "$command" ]]; then
        echo "       command: $command" >&2
    fi
    echo "       Stop that daemon, then rerun scripts/dev.sh." >&2
    exit 2
}

remember_service() {
    local name="$1"
    local pgid="$2"
    local log_file="$3"
    local port="${4:-}"

    service_names+=("$name")
    service_pgids+=("$pgid")
    service_logs+=("$log_file")
    service_ports+=("$port")
    write_run_state
}

start_service() {
    local name="$1"
    local log_file="$2"
    local command="$3"
    local port="${4:-}"

    echo "[dev] starting $name (log: $log_file)"
    : > "$log_file"
    setsid bash -c "$command" > "$log_file" 2>&1 &
    remember_service "$name" "$!" "$log_file" "$port"
}

monitor_services() {
    local -a reported=()
    local active_count=0
    local exit_code=0
    local final_exit_code=0
    local i=""
    local name=""
    local pgid=""

    for i in "${!service_names[@]}"; do
        reported[$i]=0
    done

    while true; do
        active_count=0

        for i in "${!service_names[@]}"; do
            if [[ "${reported[$i]:-0}" == "1" ]]; then
                continue
            fi

            name="${service_names[$i]}"
            pgid="${service_pgids[$i]}"
            if kill -0 -- "-$pgid" 2>/dev/null; then
                active_count=$((active_count + 1))
                continue
            fi

            set +e
            wait "$pgid" 2>/dev/null
            exit_code=$?
            set -e

            reported[$i]=1
            if (( exit_code != 0 && final_exit_code == 0 )); then
                final_exit_code="$exit_code"
            fi

            echo
            echo "[dev] $name exited with status $exit_code; leaving remaining services running"
            tail_service_log "$name"
        done

        if (( active_count == 0 )); then
            echo
            echo "[dev] all host services have exited"
            service_pgids=()
            clear_run_state
            return "$final_exit_code"
        fi

        sleep 2
    done
}

cleanup() {
    trap - EXIT INT TERM

    if (( ${#service_pgids[@]} == 0 )); then
        clear_run_state
        return 0
    fi

    echo
    echo "[dev] shutting down host services..."

    local pgid=""
    for pgid in "${service_pgids[@]}"; do
        kill -TERM -- "-$pgid" 2>/dev/null || true
    done

    wait_for_process_groups_to_exit "${service_pgids[@]}"
    clear_run_state
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

if [[ ! -f "$ENV_FILE" ]]; then
    error "env file not found: $ENV_FILE"
    echo "       Copy .env.example to .env and fill the required values." >&2
    exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${PRESENTATION_API_SECRET_KEY:-}" && -n "${SECRET_KEY:-}" ]]; then
    export PRESENTATION_API_SECRET_KEY="$SECRET_KEY"
    echo "[dev] PRESENTATION_API_SECRET_KEY not set in $ENV_FILE; using SECRET_KEY for this run"
fi

ensure_dev_secret "ENDPOINT_HEADLESS__INTERNAL_SECRET" 48
ensure_dev_secret "PRESENTATION_API_SECRET_KEY" 50

required_vars=(
    POSTGRES_DB
    POSTGRES_USER
    POSTGRES_PASSWORD
    ENDPOINT_FERNET_KEY
    ENDPOINT_ADMIN_PASSWORD
    ENDPOINT_SESSION_SECRET
    ENDPOINT_HEADLESS__INTERNAL_SECRET
    PRESENTATION_API_SECRET_KEY
)

missing_vars=()
for var_name in "${required_vars[@]}"; do
    if [[ -z "${!var_name:-}" ]]; then
        missing_vars+=("$var_name")
    fi
done

if (( ${#missing_vars[@]} > 0 )); then
    printf 'error: missing required env vars in %s: %s\n' \
        "$ENV_FILE" "${missing_vars[*]}" >&2
    exit 2
fi

require_command docker "Install Docker and make sure the daemon is running."
require_command uv "Install uv: https://docs.astral.sh/uv/"
require_command npm "Install Node.js/npm for the frontend."
require_command curl "Install curl for readiness checks."

require_executable \
    "$DASHBOARD_DIR/node_modules/.bin/vite" \
    "daemon dashboard" \
    "Run: cd $DASHBOARD_DIR && pnpm install"
require_executable \
    "$FRONTEND_DIR/node_modules/.bin/next" \
    "frontend" \
    "Run: cd $FRONTEND_DIR && npm install"

if ! docker compose --env-file "$ENV_FILE" version >/dev/null 2>&1; then
    error "docker compose is unavailable or Docker is not running"
    exit 2
fi

if (( check_only == 1 )); then
    resolve_glitchtip_port
    resolve_dev_ports
    echo "[dev] check OK"
    exit 0
fi

mkdir -p "$LOG_DIR" "$RUN_DIR"
resolve_seed_snapshot "$seed_snapshot"

# Host-side components must talk to postgres through the host port, even if
# .env contains compose-oriented service names.
export PG_HOST="${ENDPOINT_DEV_PG_HOST:-127.0.0.1}"
export PG_PORT="${PG_PORT:-5432}"

# The daemon's default SQLite path (/var/lib/ingestor/state.db) is only
# writable inside the container. Point it somewhere user-writable.
: "${ENDPOINT_DAEMON__SQLITE_PATH:=$HOME/.local/share/endpoint/state.db}"
export ENDPOINT_DAEMON__SQLITE_PATH
mkdir -p "$(dirname "$ENDPOINT_DAEMON__SQLITE_PATH")"

cd "$REPO_ROOT"

if (( reset_requested == 1 )); then
    reset_dev_state
else
    stop_previous_dev_instance
    stop_legacy_daemon_lock_holder
fi

mkdir -p "$LOG_DIR" "$RUN_DIR" "$(dirname "$ENDPOINT_DAEMON__SQLITE_PATH")"

echo "[dev] stopping compose app containers that conflict with host hybrid..."
docker compose --env-file "$ENV_FILE" stop \
    daemon dashboard processor headless presentation-api frontend >/dev/null 2>&1 || true

resolve_glitchtip_port
configure_glitchtip_env

echo "[dev] starting GlitchTip support containers..."
docker compose --env-file "$ENV_FILE" --profile observability up -d \
    glitchtip-web glitchtip-worker
wait_for_url "GlitchTip" "$GLITCHTIP_URL" 180

resolve_dev_ports

if [[ "${ENDPOINT_DEV_SKIP_SYNC:-}" != "1" ]]; then
    echo "[dev] syncing Python dependencies..."
    (cd "$DB_CORE_DIR" && uv sync --extra dev)
    (cd "$INGESTION_DIR" && uv sync --extra daemon --extra cli --extra dev)
    (cd "$PRESENTATION_API_DIR" && uv sync --extra dev)
    if (( no_process == 0 )); then
        (cd "$PROCESSOR_DIR" && uv sync --extra dev)
    fi
fi

verify_playwright_browser

if ! docker compose --env-file "$ENV_FILE" ps --status running db --format '{{.Name}}' | grep -q .; then
    echo "[dev] starting postgres..."
    docker compose --env-file "$ENV_FILE" up -d db
fi

wait_for_db_health

run_seed_if_needed

echo "[dev] applying db-core migrations..."
(cd "$DB_CORE_DIR" && uv run python manage.py migrate --no-input)

echo "[dev] applying presentation-api migrations..."
(cd "$PRESENTATION_API_DIR" && uv run python manage.py migrate --no-input)

import_cli_credentials_if_available
bootstrap_telegram_credential_if_available

export ENDPOINT_HEADLESS__INTERNAL_SECRET

export SECRET_KEY="$PRESENTATION_API_SECRET_KEY"
export ALLOW_INSECURE_SECRET_KEY="${ALLOW_INSECURE_SECRET_KEY:-1}"
export DEBUG="${PRESENTATION_API_DEBUG:-True}"
export ALLOWED_HOSTS="${PRESENTATION_API_ALLOWED_HOSTS:-localhost,127.0.0.1,0.0.0.0}"
if [[ -n "${PRESENTATION_API_CORS_ALLOWED_ORIGINS:-}" && "$PRESENTATION_API_CORS_ALLOWED_ORIGINS" != "$FRONTEND_URL" ]]; then
    export CORS_ALLOWED_ORIGINS="$FRONTEND_URL,$PRESENTATION_API_CORS_ALLOWED_ORIGINS"
else
    export CORS_ALLOWED_ORIGINS="$FRONTEND_URL"
fi
export SESSION_TTL_HOURS="${PRESENTATION_API_SESSION_TTL_HOURS:-24}"
export MS_GRAPH_BASE_URL="${PRESENTATION_API_MS_GRAPH_BASE_URL:-https://graph.microsoft.com/v1.0}"
export HEADLESS_INTERNAL_URL="$HEADLESS_AUTH_URL"

export ENDPOINT_DAEMON__API__PORT="$DAEMON_API_PORT"
export VITE_DAEMON_URL="$DAEMON_API_URL"
export VITE_DEV_SERVER_PORT="$DASHBOARD_PORT"
if [[ -n "$recency_days" ]]; then
    export ENDPOINT_DAEMON__INGESTION_CONTROLS__RECENCY_DAYS="$recency_days"
fi

export HEADLESS_PUBLIC_URL="$HEADLESS_AUTH_URL"
export NEXT_PUBLIC_PRESENTATION_API_URL="$PRESENTATION_API_URL"

start_service \
    "headless auth" \
    "$LOG_DIR/headless.log" \
    "cd '$INGESTION_DIR' && exec uv run --extra daemon --extra cli python -m uvicorn ingestion.headless.api:app --host 0.0.0.0 --port '$HEADLESS_AUTH_PORT'" \
    "$HEADLESS_AUTH_PORT"

start_service \
    "presentation-api" \
    "$LOG_DIR/presentation-api.log" \
    "cd '$PRESENTATION_API_DIR' && exec uv run python manage.py runserver 0.0.0.0:'$PRESENTATION_API_PORT'" \
    "$PRESENTATION_API_PORT"

start_service \
    "daemon API" \
    "$LOG_DIR/daemon.log" \
    "cd '$INGESTION_DIR' && exec uv run --extra daemon python -m ingestion.daemon" \
    "$DAEMON_API_PORT"

if (( no_process == 0 )); then
    start_service \
        "processor" \
        "$LOG_DIR/processor.log" \
        "cd '$PROCESSOR_DIR' && exec uv run python main.py"
else
    echo "[dev] skipping processor (--no-process)"
fi

start_service \
    "daemon dashboard" \
    "$LOG_DIR/dashboard.log" \
    "cd '$DASHBOARD_DIR' && exec ./node_modules/.bin/vite --host localhost" \
    "$DASHBOARD_PORT"

start_service \
    "frontend" \
    "$LOG_DIR/frontend.log" \
    "cd '$FRONTEND_DIR' && exec npm run dev -- --port '$FRONTEND_PORT'" \
    "$FRONTEND_PORT"

wait_for_headless_auth
wait_for_http "presentation-api" "$PRESENTATION_API_URL/health/" 90
wait_for_http "daemon API" "$DAEMON_API_URL/v1/health" 120
if (( no_process == 0 )); then
    check_service_still_running "processor" 3
fi
wait_for_http "daemon dashboard" "$DASHBOARD_URL/" 60
wait_for_http "frontend" "$FRONTEND_URL/" 120

cat <<EOF

Endpoint hybrid dev is ready

Service             URL                         Status
--------------------------------------------------------
Frontend            $FRONTEND_URL       OK
Presentation API    $PRESENTATION_API_URL       OK
Headless Auth       $HEADLESS_AUTH_URL       OK
Daemon API          $DAEMON_API_URL       OK
Daemon Dashboard    $DASHBOARD_URL       OK
Processor           worker loop                 $([[ "$no_process" == "1" ]] && printf 'disabled' || printf 'OK')
GlitchTip           $GLITCHTIP_URL       OK
Postgres            localhost:5432              OK

Logs:
  $LOG_DIR/frontend.log
  $LOG_DIR/presentation-api.log
  $LOG_DIR/headless.log
  $LOG_DIR/daemon.log
  $([[ "$no_process" == "0" ]] && printf '%s\n' "$LOG_DIR/processor.log")
  $LOG_DIR/dashboard.log

Ctrl-C stops host services. Postgres and GlitchTip are left running.
EOF

monitor_services
