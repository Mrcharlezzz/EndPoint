#!/usr/bin/env bash
# End-to-end smoke against the running presentation-api. Assumes
# scripts/seed_demo.py has been run so the demo course + assignment exist.
#
# Usage:
#   scripts/smoke.sh                      # localhost:8001
#   API=http://host:port scripts/smoke.sh

set -euo pipefail

API="${API:-http://localhost:8001/api/v1}"
EMAIL="${EMAIL:-smoke@example.com}"
DEMO_COURSE_ID="11111111-1111-1111-1111-111111111111"

echo "--- login ---"
LOGIN=$(curl -sf -X POST "$API/auth/login/" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\"}")
TOKEN=$(echo "$LOGIN" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
echo "token=${TOKEN:0:20}..."

echo
echo "--- courses (should include DEMO-101) ---"
curl -sf "$API/courses/" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo
echo "--- select demo course ---"
curl -sf -X PUT "$API/users/me/courses/" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"course_ids\":[\"$DEMO_COURSE_ID\"]}" | python3 -m json.tool

echo
echo "--- assignments (should include Demo Assignment 1) ---"
curl -sf "$API/assignments/" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
