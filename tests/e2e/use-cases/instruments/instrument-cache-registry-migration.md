# E2E Use Cases — Instrument Cache → Registry Migration

**Branch:** `feat/instrument-cache-registry-migration`
**Plan:** `docs/plans/2026-04-27-instrument-cache-registry-migration.md`
**PRD:** `docs/prds/instrument-cache-registry-migration.md`

This file graduates the 6 use cases from the plan's Phase 3.2b for permanent regression coverage. UC-ICR-006 is the operator-driven US-005 restart drill (council Q8 binding evidence) — Pablo runs it himself; the other 5 are agent-executable when this branch's stack is up.

## Pre-flight

1. `curl -sf http://localhost:8800/health` — if it fails, start the stack:
   ```
   docker compose -f docker-compose.dev.yml up -d
   ./scripts/restart-workers.sh
   ```
2. Confirm registry tables present + populated post-migration:
   ```
   docker exec msai-claude-postgres psql -U msai -d msai -c "SELECT count(*) FROM instrument_definitions; SELECT count(*) FROM instrument_aliases"
   ```
3. UC-ICR-005 only: confirm IB Gateway is up + `RUN_PAPER_E2E=1` set (the use case is opt-in cost-bearing).

## UC-ICR-001 — Migration applies cleanly on a populated dev stack

**Interface:** CLI + API
**Operator-driven:** parts (requires checkout of pre-migration commit + IB-seeded `instrument_cache`).

### Setup (ARRANGE)

1. Check out the parent commit of this branch's HEAD: `git checkout HEAD~1` (pre-migration state).
2. Bring up the stack: `docker compose -f docker-compose.dev.yml up -d && ./scripts/restart-workers.sh`.
3. Seed `instrument_cache` with at least one row via `msai instruments refresh --provider interactive_brokers --symbols AAPL --asset-class stk` (requires IB Gateway paper).

### Steps

1. `cd backend && uv run python scripts/preflight_cache_migration.py` → expect exit 0.
2. `pg_dump -t instrument_cache -t instrument_definitions -t instrument_aliases msai > /tmp/checkpoint.sql`.
3. `docker compose -f docker-compose.dev.yml down`.
4. `docker compose -f docker-compose.dev.yml up -d postgres redis`.
5. Check out this branch's HEAD: `git checkout feat/instrument-cache-registry-migration`.
6. `cd backend && uv run alembic upgrade head`.
7. `docker compose -f docker-compose.dev.yml up -d && ./scripts/restart-workers.sh`.

### Verification (VERIFY)

- `curl -sf http://localhost:8800/health` returns 200.
- `GET /api/v1/instruments/registry?symbol=AAPL` returns the migrated definition (active alias `AAPL.NASDAQ`, asset_class `equity`).
- `docker exec msai-claude-postgres psql -U msai -d msai -c "\\d instrument_cache"` returns "Did not find any relation" (table dropped).
- `docker compose -f docker-compose.dev.yml logs backend worker | grep -i "instrument_cache"` returns empty (no log line references the dropped table).

### Persistence

Restart the backend container; re-run the API call. Same row.

---

## UC-ICR-002 — Preflight fails loud on missing alias for active deployment

**Interface:** CLI

### Setup

- Pre-migration stack up.
- Active `LiveDeployment` whose portfolio revision has a `LivePortfolioRevisionStrategy.instruments=['BOGUS']` member, in `running` state (use `msai live start-portfolio` against a portfolio referencing a strategy whose `instruments` list contains a symbol intentionally NOT in the registry).

### Steps

1. Run `cd backend && uv run python scripts/preflight_cache_migration.py`.

### Verification

- Exit code is non-zero.
- stdout contains `deployment <slug>: 'BOGUS.XNYS'` and `Run: msai instruments refresh --symbols BOGUS --provider interactive_brokers`.
- The migration is NOT applied (alembic head is unchanged).

### Persistence

The active deployment row is unchanged.

---

## UC-ICR-003 — `MarketHoursService` answers RTH question correctly post-migration

**Interface:** API

### Setup

- Post-migration stack up.
- Registry has AAPL.NASDAQ with NYSE-style trading hours (migrated in step 5 of UC-001).

### Steps

1. Submit a backtest via `POST /api/v1/backtests/run` against AAPL.NASDAQ during a backtest window that includes both pre-market and RTH bars.
2. Wait for the backtest to complete.

### Verification

- `GET /api/v1/backtests/{id}/results` returns `series_status: ready`.
- `GET /api/v1/backtests/{id}/trades` shows trades only during RTH (no pre-market trades unless `allow_eth=True`).

### Persistence

Re-fetch the trades API. Same answer.

---

## UC-ICR-004 — `lookup_for_live` fail-loud cold-miss on `/live/start-portfolio`

**Interface:** API

### Setup

- Post-migration stack up.
- Registry has AAPL.NASDAQ; does NOT have GOOG.NASDAQ.

### Steps

1. Submit `POST /api/v1/live/start-portfolio` with a portfolio whose strategy `instruments` list references GOOG.

### Verification

- API returns 422 with body containing `RegistryMissError` (the existing `live_resolver.RegistryMissError` raised by `lookup_for_live` at supervisor spawn time, surfaced through the API's failure-kind dispatch) and an operator-action hint pointing at `msai instruments refresh --symbols GOOG --provider interactive_brokers --asset-class stk`.

### Persistence

No `LiveDeployment` row in `running` state was created (deployment may transition to `starting` then immediately `failed` per supervisor's permanent-catch path; verify via `GET /api/v1/live/status` shows GOOG deployment as failed with `FailureKind.REGISTRY_MISS`).

---

## UC-ICR-005 — `msai instruments refresh` per-asset-class factories work end-to-end (paper IB)

**Interface:** CLI
**Opt-in:** Requires `RUN_PAPER_E2E=1` + IB Gateway paper reachable (broker compose profile up).

### Setup

- Post-migration stack up + IB Gateway paper reachable.

### Steps

1. `msai instruments refresh --provider interactive_brokers --asset-class fut --symbols ES`.
2. `msai instruments refresh --provider interactive_brokers --asset-class stk --symbols AAPL`.
3. `msai instruments refresh --provider interactive_brokers --asset-class cash --symbols EUR/USD`.

### Verification

- Each command exits 0.
- Output JSON contains `"resolved": [{"symbol": "ES", "canonical": "ESM6.CME"}, ...]` (or the actual front-month for ES today).
- Subsequent `GET /api/v1/instruments/registry?symbol=ES` returns the row.

### Persistence

Re-run command 1 — second invocation is a no-op (idempotent).

**Note:** Default test runs SKIP this UC; only triggered with `RUN_PAPER_E2E=1` because IB Gateway round-trips cost broker rate-limit budget.

---

## UC-ICR-006 — Branch-local restart drill (operator step, US-005 evidence)

**Interface:** CLI + API + manual operator workflow.
**Operator-driven:** This UC produces the Phase 5.4 evidence required by US-005 (council Q8 binding decision).

### Setup

- Pre-migration stack up.
- Spawn a paper deploy via `msai live start-portfolio` against a symbol the registry has covered (e.g. AAPL).
- Wait for at least one trade to fire (or use a long-running test strategy that doesn't trade yet — open positions optional).

### Steps

As documented in `docs/runbooks/instrument-cache-migration.md` Step 7.

### Verification

Per runbook Step 7 + structured log `position_reader_rehydrated` after restart with the deployment's open positions intact.

### Persistence

Stop + restart backend container; positions still hydrate.

**This UC produces the Phase 5.4 evidence required by US-005.** Pablo runs it manually on the dev DB; the F6 fix (close-prior `effective_to=now.date()` instead of sentinel) is specifically validated against real alias-rotation history during this drill.
