#!/usr/bin/env bash
#
# Phase 1 E2E helper — seeds the smoke strategy and exports the env
# vars the pytest harness needs, then runs the test.
#
# Prerequisites (manual):
#   - docker compose -f docker-compose.dev.yml up -d (already running)
#   - IB Gateway paper container reachable from the backend container
#   - IB_ACCOUNT_ID exported in your shell (or .env)
#
# Usage:
#   export IB_ACCOUNT_ID=DU1234567
#   ./scripts/e2e_phase1.sh
#
# The test itself lives in
# backend/tests/e2e/test_live_trading_phase1.py and is gated by
# MSAI_E2E_IB_ENABLED=1.

set -euo pipefail

if [[ -z "${IB_ACCOUNT_ID:-}" ]]; then
  echo "IB_ACCOUNT_ID is required (set to a real paper account id starting with DU)." >&2
  exit 1
fi

cd "$(dirname "$0")/.."

BACKEND_URL="${BACKEND_URL:-http://localhost:8800}"

# ---------------------------------------------------------------------------
# Seed the smoke strategy row
# ---------------------------------------------------------------------------
# We seed via a short Python snippet so the env + DB URL parsing matches
# the production path exactly. Running raw SQL against the container
# would force us to replicate connection-string parsing here.
cd backend
STRATEGY_ID=$(uv run python - <<'PY'
import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from msai.core.config import settings
from msai.models.strategy import Strategy
from msai.models.user import User


async def main() -> None:
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        # Reuse an existing user if one exists; otherwise create a stable
        # e2e-operator row so the started_by foreign key points somewhere.
        user = (
            await session.execute(
                select(User).where(User.entra_id == "e2e-operator")
            )
        ).scalar_one_or_none()
        if user is None:
            user = User(
                id=uuid.uuid4(),
                entra_id="e2e-operator",
                email="e2e@example.com",
                role="operator",
            )
            session.add(user)
            await session.flush()

        # Idempotent: if the smoke strategy is already seeded, reuse it.
        existing = (
            await session.execute(
                select(Strategy).where(Strategy.name == "smoke_market_order")
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(str(existing.id))
            return

        strat = Strategy(
            id=uuid.uuid4(),
            name="smoke_market_order",
            file_path="/app/strategies/example/smoke_market_order.py",
            strategy_class="SmokeMarketOrderStrategy",
            default_config={},
            created_by=user.id,
        )
        session.add(strat)
        await session.commit()
        print(str(strat.id))

    await engine.dispose()


asyncio.run(main())
PY
)

echo "Smoke strategy seeded: ${STRATEGY_ID}" >&2

export MSAI_E2E_IB_ENABLED=1
export MSAI_E2E_STRATEGY_ID="${STRATEGY_ID}"
export MSAI_E2E_BACKEND_URL="${BACKEND_URL}"
export MSAI_E2E_BACKEND_CONTAINER="${MSAI_E2E_BACKEND_CONTAINER:-msai-claude-backend}"
export MSAI_E2E_COMPOSE_FILE="${MSAI_E2E_COMPOSE_FILE:-docker-compose.dev.yml}"

exec uv run pytest tests/e2e/test_live_trading_phase1.py -vv "$@"
