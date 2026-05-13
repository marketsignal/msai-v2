# E2E Use Cases — databento-registry-bootstrap

Graduated 2026-04-23 from Phase 3.2b draft in `docs/plans/2026-04-23-databento-registry-bootstrap.md`.
Run mode: `verify-e2e` agent for API + `python -m msai.cli` for CLI. UI: N/A (this PR is backend-only).

**Prerequisites:**

1. Dev stack up: `docker compose -f docker-compose.dev.yml up -d && ./scripts/restart-workers.sh`
2. Migrations at head: `docker exec msai-claude-backend uv run alembic upgrade head`
3. API key header: `X-API-Key: $MSAI_API_KEY` (defaults to `msai-dev-key` in dev)
4. `DATABENTO_API_KEY` set on backend container.

---

## UC-DRB-001 — Bootstrap AAPL via API (happy path)

**Intent:** Register an equity symbol end-to-end through the public API so later backtests can reference `AAPL.NASDAQ`.

**Interface:** API

**Setup:** none — idempotent; re-runs return `noop` or `alias_rotated` depending on DB state.

**Steps:**

```bash
curl -sS -X POST http://localhost:8800/api/v1/instruments/bootstrap \
  -H "Content-Type: application/json" \
  -H "X-API-Key: msai-dev-key" \
  -d '{"provider":"databento","symbols":["AAPL"]}'
```

**Verification:**

- HTTP **200**
- `results[0].registered == true`
- `results[0].canonical_id == "AAPL.NASDAQ"` (Databento MIC `XNAS` → registry exchange-name `NASDAQ`)
- `results[0].dataset == "XNAS.ITCH"`
- `results[0].outcome ∈ {created, noop, alias_rotated}` — all three are success states
- `results[0].live_qualified == false` (UC-006 covers the true case)

**Persistence:** Re-run within seconds → `outcome == "noop"`. See UC-004.

---

## UC-DRB-002 — Bootstrap SPY via CLI

**Intent:** Verify the CLI parity surface (ARRANGE via `msai instruments bootstrap`, VERIFY via GET).

**Interface:** CLI (setup) + API (verify)

**Steps:**

```bash
docker exec msai-claude-backend sh -c "cd /app && \
  uv run python -m msai.cli instruments bootstrap --provider databento --symbols SPY"
```

_Note: invocation form is `python -m msai.cli` (not `msai`) — the pyproject entry-point isn't on PATH in the Docker image._

**Verification:**

- Exit code **0**
- stdout is valid JSON; `summary.total == 1`; `summary.failed == 0`
- `results[0].registered == true`
- `results[0].canonical_id` is one of `SPY.NASDAQ` (XNAS.ITCH) or `SPY.ARCA` (ARCX.PILLAR) — depends on which equity dataset returned SPY's definition first
- `results[0].outcome ∈ {created, noop, alias_rotated}`

**Persistence:** Immediate second CLI run → `outcome=noop`.

---

## UC-DRB-003 — Ambiguous symbol path (unit-covered)

**Intent:** Verify the 422-with-candidates envelope fires when Databento returns multiple distinct instruments for a symbol.

**Interface:** API

**Note:** As of 2026-04-23, no equity symbol is reliably ambiguous against real Databento data (the historical candidate BRK.B resolves cleanly now). This UC is **covered by unit tests with a mock ambiguity side-effect**:

- `backend/tests/unit/test_databento_client_ambiguity.py::test_ambiguous_raises_on_multiple_canonical_ids`
- `backend/tests/unit/services/nautilus/security_master/test_databento_bootstrap_equities.py::test_bootstrap_ambiguous_per_symbol`

**E2E verdict:** SKIPPED — ambiguity is not reproducible at E2E without injection.

---

## UC-DRB-004 — Idempotent re-run

**Intent:** Second call for the same symbol must not create duplicate registry rows; operator script re-runs are safe.

**Interface:** API

**Steps:**

```bash
# First call
curl -sS -X POST .../bootstrap -d '{"provider":"databento","symbols":["AAPL"]}' \
  -H "X-API-Key: msai-dev-key" -H "Content-Type: application/json"

# Second call — identical body
curl -sS -X POST .../bootstrap -d '{"provider":"databento","symbols":["AAPL"]}' \
  -H "X-API-Key: msai-dev-key" -H "Content-Type: application/json"
```

**Verification:**

- First response: `outcome ∈ {created, noop, alias_rotated}`, `registered=true`
- Second response: `outcome == "noop"`, `registered=true`, `canonical_id` identical to first

**Persistence:** Active-alias row stable across both calls (canonical_id equal).

---

## UC-DRB-005 — ES continuous-futures bootstrap

**Intent:** Register a continuous-futures symbol so operators can backtest across futures rolls without manual canonical_id construction.

**Interface:** CLI + API

**Opt-in:** Set `RUN_PAPER_E2E=1` — Databento GLBX.MDP3 queries have cost.

**Steps:**

```bash
docker exec -e RUN_PAPER_E2E=1 msai-claude-backend sh -c "cd /app && \
  uv run python -m msai.cli instruments bootstrap --provider databento --symbols ES.n.0"
```

**Verification:**

- `results[0].asset_class == "futures"`
- `results[0].canonical_id == "ES.n.0.CME"` (raw_symbol preserved verbatim + `.CME` suffix; NOT the month-rewritten `ESH6.CME`)
- `results[0].dataset == "GLBX.MDP3"`
- `results[0].outcome ∈ {created, noop, alias_rotated}`

---

## UC-DRB-006 — Two-step graduation (live_qualified flag)

**Intent:** Verify the contract that `live_qualified=true` only after a separate `instruments refresh --provider interactive_brokers` step.

**Interface:** API

**Prerequisite:** IB Gateway container running (`COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml up -d`) with paper account reachable on socat port 4004 (gateway loopback bind: 4002).

**Steps:**

1. Bootstrap AAPL via Databento only → verify `live_qualified == false`.
2. `docker exec msai-claude-backend sh -c "uv run python -m msai.cli instruments refresh --provider interactive_brokers --symbols AAPL"`
3. Re-bootstrap AAPL via Databento → verify `live_qualified == true`.

**Verification:** `live_qualified` flips from `false` → `true` only after the IB refresh step. The flag is the gate the `/api/v1/live/start-portfolio` endpoint reads to decide whether a symbol is live-tradable.

---

## Observed failure mode (fixed 2026-04-23 during Phase 5.4)

The initial bootstrap implementation probed Databento with `start = today_utc_midnight` which fails with 4xx `data_start_after_available_end` during the nightly window before Databento publishes today's definition rows (observed 00:38 UTC). Fixed at `databento_bootstrap.py` by probing a 7-day historical window ending yesterday (`start = today - 7d`, `end = today - 1d`). Any future regression where all equity symbols return `outcome=upstream_error` with this specific diagnostic should revert-check that change.
