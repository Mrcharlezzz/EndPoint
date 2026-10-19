#!/usr/bin/env bash
# Quick checks that the Presentation API is reachable (for Next.js / frontend dev).
# Usage (from endpoint-nextjs/):
#   ./scripts/test-presentation-api-smoke.sh
#   BASE_URL=http://127.0.0.1:8000 ./scripts/test-presentation-api-smoke.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f ".env" ]; then
  set +u
  # shellcheck disable=SC1091
  set -a && source ".env" && set +a
  set -u
fi

BASE_URL="${BASE_URL:-${NEXT_PUBLIC_PRESENTATION_API_URL:-http://localhost:8000}}"
BASE_URL="${BASE_URL%/}"

echo "== Presentation API smoke test =="
echo "Base: $BASE_URL"
echo

code_health="$(curl -sS -o /tmp/endpoint-health.body -w "%{http_code}" "$BASE_URL/health/" || true)"
body_health="$(cat /tmp/endpoint-health.body 2>/dev/null || true)"
echo "GET /health/  → HTTP $code_health"
if [ -n "$body_health" ]; then
  echo "$body_health" | head -c 500
  echo
fi

if [ "$code_health" != "200" ] && [ "$code_health" != "204" ]; then
  echo
  echo "Tip: start the Django API (and DB) from your team’s presentation-api repo, then set"
  echo "NEXT_PUBLIC_PRESENTATION_API_URL in .env to this same base URL."
  exit 1
fi

TOKEN="${PRESENTATION_API_BEARER_TOKEN:-}"
if [ -z "$TOKEN" ] || [ "$TOKEN" = "PASTE_TOKEN_HERE" ]; then
  echo
  echo "Optional: set PRESENTATION_API_BEARER_TOKEN in .env to test authenticated routes."
  exit 0
fi

code_courses="$(curl -sS -o /tmp/endpoint-courses.body -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json" \
  "$BASE_URL/api/v1/courses/" || true)"
echo "GET /api/v1/courses/ (with token) → HTTP $code_courses"
if command -v python3 >/dev/null 2>&1; then
  python3 -m json.tool < /tmp/endpoint-courses.body 2>/dev/null | head -40 || cat /tmp/endpoint-courses.body | head -c 600
  echo
fi
