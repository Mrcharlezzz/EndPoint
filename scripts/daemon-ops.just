# Daemon ops recipes — wrappers over the ingestion-daemon control-panel API.
# Imported into the repo-root Justfile via:
#
#     import 'scripts/daemon-ops.just'
#
# Each recipe echoes its curl command first so operators learn the API shape.
#
# Auth precedence:
#   DAEMON_SESSION_COOKIE    raw value of the endpoint_admin_session cookie,
#                            OR
#   ENDPOINT_ADMIN_PASSWORD  triggers a one-shot POST /api/auth/login.
#
# Other env:
#   DAEMON_BASE_URL          default http://localhost:8765
#
# Mutating endpoints carry X-Requested-With: XMLHttpRequest (CSRF gate).
#
# Tooling assumptions: every recipe shells out to `curl` (and `awk` /
# `mktemp` for one-shot helpers). On a minimal container without curl
# (e.g. distroless), prefer `endpoint-admin` (the Python CLI) instead —
# it talks the same API over httpx and ships with the daemon's
# requirements. Recipe-to-CLI mapping:
#   daemon-pause         -> endpoint-admin pause USER --platform PLATFORM
#   daemon-unpause       -> endpoint-admin unpause USER --platform PLATFORM
#   daemon-pause-user    -> endpoint-admin pause USER
#   daemon-force-sync    -> endpoint-admin force-sync USER --platform PLATFORM
#   daemon-dlq-clear     -> endpoint-admin dlq clear USER --platform PLATFORM
#   daemon-status        -> endpoint-admin status   (exit 1 on /ready 503)
#   daemon-metrics       -> endpoint-admin metrics
#   daemon-syncs-running -> endpoint-admin syncs running
#   daemon-platform-toggle -> endpoint-admin config set platforms.PLATFORM_enabled VALUE

# Internal helper: resolve the session cookie from env or login. Echoes the
# cookie value (no newline) on stdout. Exits 2 when neither source is set
# or when the login fails.
_daemon-cookie:
    #!/usr/bin/env bash
    set -euo pipefail
    base="${DAEMON_BASE_URL:-http://localhost:8765}"
    if [ -n "${DAEMON_SESSION_COOKIE:-}" ]; then
        printf '%s' "$DAEMON_SESSION_COOKIE"
    elif [ -n "${ENDPOINT_ADMIN_PASSWORD:-}" ]; then
        cookie=$(curl -sS -c - -X POST "$base/api/auth/login" \
            -H "Content-Type: application/json" \
            -d "{\"password\":\"$ENDPOINT_ADMIN_PASSWORD\"}" \
            | awk '$6=="endpoint_admin_session"{print $7}')
        [ -n "$cookie" ] || { echo "[daemon] login failed" >&2; exit 2; }
        printf '%s' "$cookie"
    else
        echo "[daemon] DAEMON_SESSION_COOKIE or ENDPOINT_ADMIN_PASSWORD required" >&2
        exit 2
    fi

# Pause one (user, platform) pair (idempotent — already-paused returns ok).
daemon-pause USER PLATFORM:
    #!/usr/bin/env bash
    set -euo pipefail
    base="${DAEMON_BASE_URL:-http://localhost:8765}"
    cookie=$(just _daemon-cookie)
    url="$base/api/actions/users/{{USER}}/{{PLATFORM}}/pause"
    echo "+ curl -sS -X POST '$url' -H 'X-Requested-With: XMLHttpRequest' --cookie ..."
    curl -sS -X POST "$url" -H "X-Requested-With: XMLHttpRequest" \
        -H "Content-Type: application/json" \
        -b "endpoint_admin_session=$cookie" -d '{}'

# Unpause one (user, platform) pair (idempotent).
daemon-unpause USER PLATFORM:
    #!/usr/bin/env bash
    set -euo pipefail
    base="${DAEMON_BASE_URL:-http://localhost:8765}"
    cookie=$(just _daemon-cookie)
    url="$base/api/actions/users/{{USER}}/{{PLATFORM}}/unpause"
    echo "+ curl -sS -X POST '$url' -H 'X-Requested-With: XMLHttpRequest' --cookie ..."
    curl -sS -X POST "$url" -H "X-Requested-With: XMLHttpRequest" \
        -H "Content-Type: application/json" \
        -b "endpoint_admin_session=$cookie" -d '{}'

# Pause every PlatformCredential row for USER (all platforms).
daemon-pause-user USER:
    #!/usr/bin/env bash
    set -euo pipefail
    base="${DAEMON_BASE_URL:-http://localhost:8765}"
    cookie=$(just _daemon-cookie)
    url="$base/api/actions/users/{{USER}}/pause"
    echo "+ curl -sS -X POST '$url' -H 'X-Requested-With: XMLHttpRequest' --cookie ..."
    curl -sS -X POST "$url" -H "X-Requested-With: XMLHttpRequest" \
        -H "Content-Type: application/json" \
        -b "endpoint_admin_session=$cookie" -d '{}'

# Force an immediate sync for (user, platform).
daemon-force-sync USER PLATFORM:
    #!/usr/bin/env bash
    set -euo pipefail
    base="${DAEMON_BASE_URL:-http://localhost:8765}"
    cookie=$(just _daemon-cookie)
    url="$base/api/actions/users/{{USER}}/{{PLATFORM}}/force_sync"
    echo "+ curl -sS -X POST '$url' -H 'X-Requested-With: XMLHttpRequest' --cookie ..."
    curl -sS -X POST "$url" -H "X-Requested-With: XMLHttpRequest" \
        -H "Content-Type: application/json" \
        -b "endpoint_admin_session=$cookie" -d '{}'

# Clear dead-letter suppression for (user, platform).
daemon-dlq-clear USER PLATFORM:
    #!/usr/bin/env bash
    set -euo pipefail
    base="${DAEMON_BASE_URL:-http://localhost:8765}"
    cookie=$(just _daemon-cookie)
    url="$base/api/actions/dead_letter/{{USER}}/{{PLATFORM}}/clear"
    echo "+ curl -sS -X POST '$url' -H 'X-Requested-With: XMLHttpRequest' --cookie ..."
    curl -sS -X POST "$url" -H "X-Requested-With: XMLHttpRequest" \
        -H "Content-Type: application/json" \
        -b "endpoint_admin_session=$cookie" -d '{}'

# Combined health + readiness probe (exit non-zero when /ready != 200).
daemon-status:
    #!/usr/bin/env bash
    set -euo pipefail
    base="${DAEMON_BASE_URL:-http://localhost:8765}"
    echo "+ curl -sS $base/api/health"
    curl -sS "$base/api/health"; echo
    echo "+ curl -sS -o /dev/null -w '%{http_code}\n' $base/api/ready"
    tmp=$(mktemp)
    code=$(curl -sS -o "$tmp" -w "%{http_code}" "$base/api/ready")
    cat "$tmp"; echo
    rm -f "$tmp"
    if [ "$code" != "200" ]; then
        echo "[daemon] not ready ($code)" >&2
        exit 1
    fi

# Prometheus exposition (text format, public — no cookie needed).
daemon-metrics:
    #!/usr/bin/env bash
    set -euo pipefail
    base="${DAEMON_BASE_URL:-http://localhost:8765}"
    echo "+ curl -sS $base/api/metrics"
    curl -sS "$base/api/metrics"

# In-flight syncs (state=running).
daemon-syncs-running:
    #!/usr/bin/env bash
    set -euo pipefail
    base="${DAEMON_BASE_URL:-http://localhost:8765}"
    cookie=$(just _daemon-cookie)
    url="$base/api/syncs?state=running&limit=200"
    echo "+ curl -sS '$url' --cookie ..."
    curl -sS "$url" -b "endpoint_admin_session=$cookie"

# Toggle a platform's enabled flag via the config overlay.
# PLATFORM is teams|moodle|telegram. ENABLED is true|false.
daemon-platform-toggle PLATFORM ENABLED:
    #!/usr/bin/env bash
    set -euo pipefail
    base="${DAEMON_BASE_URL:-http://localhost:8765}"
    cookie=$(just _daemon-cookie)
    url="$base/api/config/overlay/platforms.{{PLATFORM}}_enabled"
    echo "+ curl -sS -X PUT '$url' -H 'X-Requested-With: XMLHttpRequest' --cookie ..."
    curl -sS -X PUT "$url" -H "X-Requested-With: XMLHttpRequest" \
        -H "Content-Type: application/json" \
        -b "endpoint_admin_session=$cookie" \
        -d "{\"value\": {{ENABLED}}}"
