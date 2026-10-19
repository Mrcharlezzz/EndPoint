#!/usr/bin/env bash
# Shell entry point for the processor post-run reporter.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

uv run python "$REPO_ROOT/scripts/report.py" "$@"
