#!/usr/bin/env bash
# CI test for ./Caddyfile.
#
# Asserts:
#   1. The Caddyfile uses `handle /api/*` NOT `handle_path /api/*` (prefix preservation;
#      research §3 + Caddyfile comment block — strikes a backend route 404 if regressed)
#   2. `caddy validate` exits 0 with MSAI_HOSTNAME=test.example.com
#   3. `caddy validate` exits non-zero on a deliberate syntax error (sanity check)

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

CADDYFILE="./Caddyfile"
[[ -f "$CADDYFILE" ]] || { echo "FAIL: $CADDYFILE missing" >&2; exit 1; }

echo "=== Caddyfile grep assertions ==="

if grep -vE '^\s*#' "$CADDYFILE" | grep -q "handle_path /api"; then
    echo "FAIL: Caddyfile uses handle_path /api/* — must use handle /api/* to preserve the /api prefix for backend routes (research §3)" >&2
    exit 1
fi

grep -q "handle /api/\*" "$CADDYFILE" \
    || { echo "FAIL: Caddyfile must have 'handle /api/*' block reverse-proxying to backend:8000" >&2; exit 1; }

grep -q "reverse_proxy backend:8000" "$CADDYFILE" \
    || { echo "FAIL: Caddyfile must reverse_proxy /api/* to backend:8000" >&2; exit 1; }

grep -q "reverse_proxy frontend:3000" "$CADDYFILE" \
    || { echo "FAIL: Caddyfile must reverse_proxy catch-all to frontend:3000" >&2; exit 1; }

grep -q '{\$MSAI_HOSTNAME}' "$CADDYFILE" \
    || { echo "FAIL: Caddyfile must use {\$MSAI_HOSTNAME} env interpolation" >&2; exit 1; }

# ─── caddy validate (requires Docker) ─────────────────────────────────────────

if ! command -v docker &>/dev/null; then
    echo "WARN: docker not installed; skipping caddy validate"
    exit 0
fi

echo "=== caddy validate (positive case) ==="
docker run --rm \
    -v "$(pwd)/$CADDYFILE:/etc/caddy/Caddyfile:ro" \
    -e MSAI_HOSTNAME=test.example.com \
    caddy:2-alpine \
    caddy validate --config /etc/caddy/Caddyfile

echo "=== caddy validate (negative case — deliberately broken) ==="
TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT
cat >"$TMPFILE" <<'EOF'
{$MSAI_HOSTNAME} {
    handlee /api/* {  # deliberate typo: handlee
        reverse_proxy backend:8000
    }
}
EOF

if docker run --rm \
    -v "$TMPFILE:/etc/caddy/Caddyfile:ro" \
    -e MSAI_HOSTNAME=test.example.com \
    caddy:2-alpine \
    caddy validate --config /etc/caddy/Caddyfile 2>/dev/null; then
    echo "FAIL: caddy validate accepted a Caddyfile with 'handlee' typo (negative test broken)" >&2
    exit 1
fi

echo "Caddyfile validation tests passed."
