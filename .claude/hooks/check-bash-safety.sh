#!/usr/bin/env bash
set -u
FORGE_PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
exec "$FORGE_PROJECT_ROOT/.forge/hooks/check-bash-safety.sh" "$@"
