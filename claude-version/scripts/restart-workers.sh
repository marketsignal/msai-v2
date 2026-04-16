#!/usr/bin/env bash
# Restart every long-running worker container so the next subprocess
# spawn picks up source changes from the host volume mount.
#
# Why this exists
# ---------------
# Worker containers (backend, backtest-worker, ingest-worker,
# job-watchdog, live-supervisor, etc.) cache imported Python modules
# in memory at startup. Volume-mounted source updates are visible to
# the FILESYSTEM but the running interpreter keeps the OLD module
# until the process restarts.
#
# This bites in two specific scenarios:
#
# 1. A merge changes a function's signature. Long-running workers
#    that imported the OLD signature keep calling the new code with
#    the old args, raising ``TypeError: takes N positional arguments
#    but M were given``. Drill 2026-04-15 P0-B blocked every backtest
#    after PR #5 changed ``_run_in_subprocess`` from 2 args to 1.
#
# 2. A merge adds a new module-level import (e.g. a new symbol in a
#    bootstrap dict). The running worker's module dict is frozen at
#    startup and won't see the new entry until restart. Drill
#    2026-04-15 saw SPY rejected on first attempt; restarting the
#    supervisor unblocked it.
#
# Run this after ANY merge to main that touches a file under
# ``src/msai/services/`` or ``src/msai/workers/`` or
# ``src/msai/live_supervisor/``. Cheap (~10 s total restart),
# idempotent.

set -euo pipefail

cd "$(dirname "$0")/.."

# Default profile is non-broker so workers running without IB
# credentials still get restarted. Add ``--with-broker`` to also
# include ``live-supervisor`` and ``ib-gateway``.
PROFILE_FLAGS=""
if [[ "${1:-}" == "--with-broker" ]]; then
    PROFILE_FLAGS="--profile broker"
    echo "→ restarting workers + broker stack (live-supervisor, ib-gateway)"
else
    echo "→ restarting workers (use --with-broker to include live-supervisor + ib-gateway)"
fi

SERVICES=(
    backend
    backtest-worker
    ingest-worker
    job-watchdog
    portfolio-worker
    research-worker
)

if [[ -n "$PROFILE_FLAGS" ]]; then
    SERVICES+=(live-supervisor)
fi

# shellcheck disable=SC2086  # we want word-splitting on $PROFILE_FLAGS
docker compose -f docker-compose.dev.yml $PROFILE_FLAGS restart "${SERVICES[@]}"

echo
echo "✓ Restarted: ${SERVICES[*]}"
echo
echo "If a backtest or live deployment was failing with a stale-import"
echo "TypeError or 'symbol not registered' error, retry it now."
