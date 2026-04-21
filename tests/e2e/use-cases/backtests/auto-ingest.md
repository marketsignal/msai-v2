# E2E Use Cases — Backtest Auto-Ingest on Missing Data

**Feature:** PR #40 (branch `feat/backtest-auto-ingest-on-missing-data`). When a backtest fails with `FailureCode.MISSING_DATA`, the platform auto-downloads the missing data (bounded lazy) and re-runs the backtest. Failure envelope only surfaces when auto-heal itself fails (guardrail rejection, 30-min cap, provider error).

**Interface:** fullstack — API-first, UI for observation.

**Surface under test:**

- `POST /api/v1/backtests/run` (auth: `X-API-Key: msai-dev-key` in dev)
- `GET /api/v1/backtests/{id}/status` (polling; new fields `phase` + `progress_message`)
- `GET /api/v1/backtests/history` (new fields `phase` + `progress_message` per row)
- `GET /api/v1/backtests/{id}/results` (aggregate metrics — timeseries fields deferred to follow-up PR; see CONTINUITY §"Next — remaining deferred items" #7)
- UI detail page `/backtests/{id}` — phase indicator testids `backtest-phase-indicator` + `backtest-phase-message`
- UI list page `/backtests` — badge testid `backtest-list-fetching-badge`

**Environment requirements:**

- Dev stack up (`docker compose -f docker-compose.dev.yml up -d`).
- Migration head = `y3s4t5u6v7w8` (auto-heal columns present on `backtests` table).
- `DATABENTO_API_KEY` set in backend env (equities + futures via Databento).
- At least one symbol pre-registered in `instrument_aliases` for the asset class under test. For SPY: `INSERT INTO instrument_definitions (raw_symbol, listing_venue, routing_venue, asset_class, provider, lifecycle_state) VALUES ('SPY', 'XNAS', 'SMART', 'equity', 'databento', 'active');` + matching alias row. (Registry bootstrap from Databento is a deferred follow-up — see CONTINUITY #8.)

---

## UC-BAI-001 — Transparent auto-heal happy path (API)

**Intent:** Platform agent submits a backtest for a symbol with no pre-existing data; the platform heals transparently; agent sees `status=completed` with real metrics, no error envelope.

**Setup:**

1. `docker compose -f docker-compose.dev.yml exec backend find /app/data/parquet/stocks/SPY -delete` (clean raw).
2. `docker compose -f docker-compose.dev.yml exec backend find /app/data/nautilus -type d -name "SPY*" -exec rm -rf {} +` (clean Nautilus catalog).
3. `curl http://localhost:8800/api/v1/strategies/ -H "X-API-Key: msai-dev-key" | jq '.items[0].id'` to grab a strategy id.

**Steps:**

1. `POST /api/v1/backtests/run` with body:
   ```json
   {
     "strategy_id": "<id>",
     "config": {
       "fast_ema_period": 10,
       "slow_ema_period": 20,
       "trade_size": 1000
     },
     "instruments": ["SPY"],
     "start_date": "2024-01-01",
     "end_date": "2024-01-31"
   }
   ```
   Expect HTTP 201 with `{id, status="pending"}`. No `phase` field (exclude_none strips it).
2. Poll `GET /api/v1/backtests/{id}/status` every 3s.
3. Within the first 1–2 polls (subsecond-to-3s window), expect:
   ```
   status=running, phase=awaiting_data, progress_message="Downloading stocks data for SPY.XNAS"
   ```
4. Within ~15s total, expect `status=completed` with `phase` absent.
5. `GET /api/v1/backtests/{id}/results` — expect `metrics.num_trades > 0` (SPY Jan 2024 with default EMA-cross should produce 400+ trades), `metrics.sharpe_ratio` non-null, `metrics.total_return` non-null.
6. Confirm `status` response has NO `error` key (response_model_exclude_none strips it).

**Verification:**

- API status transitions `pending → running+awaiting_data → completed` observed.
- `metrics` populated with all 6 aggregate fields (`sharpe_ratio`, `sortino_ratio`, `max_drawdown`, `total_return`, `win_rate`, `num_trades`).
- Persistence: repeat `GET /status` after 30s; state is still `completed` with same metrics.
- `GET /api/v1/backtests/history` lists the row with `status="completed"` and no `phase`/`progress_message` fields (terminal — cleared).

**Expected failure modes:**

- Provider up + symbol entitled → PASS.
- Provider rate-limit → FAIL_INFRA (retry once).
- Cold symbol not in registry → 422 at `/backtests/run` (registry must be pre-seeded). Tag as `SKIPPED_FAIL_INFRA` for that symbol, open a registry-bootstrap follow-up.

---

## UC-BAI-002 — Guardrail rejection (>10y range)

**Intent:** Requests outside the 10-year cap fail immediately without consuming provider API calls.

**Setup:** authenticated client, any valid strategy id.

**Steps:**

1. `POST /api/v1/backtests/run` with `instruments=["ES.n.0"]`, `start_date=2013-01-01`, `end_date=2024-12-31` (~12 years).
2. Poll `/status`.

**Verification:**

- Within 5 seconds: `status=failed`.
- `error.code == "missing_data"`.
- `error.message` contains "year" and "10".
- `error.remediation.auto_available == false`.
- `error.suggested_action` starts with `Run: msai ingest`.
- `error.remediation.asset_class == "futures"` (correctly derived server-side from `ES.n.0` — NOT `"stocks"`, which was the PR #39 bug this PR closes).
- Structured log `backtest_auto_heal_guardrail_rejected` emitted with `reason=range_exceeds_max_years` and `details.range_years >= 12`.
- No `backtest_auto_heal_ingest_enqueued` event fires (guardrail short-circuits before enqueue).

**Expected failure modes:** PASS unless stack is down.

---

## UC-BAI-003 — Server-side asset_class derivation (futures)

**Intent:** A futures symbol submitted WITHOUT `asset_class` in config is correctly routed through the futures ingest path (Databento GLBX.MDP3), closing the PR #39 stocks-mis-routing bug.

**Setup:** authenticated; `ES.n.0` pre-registered in `instrument_aliases` (provider=databento).

**Steps:**

1. `POST /run` with `instruments=["ES.n.0"]`, `start_date=2024-01-01`, `end_date=2024-03-31`, `config={"fast_ema_period": 10, "slow_ema_period": 20, "trade_size": 1}` (NO `asset_class` field).
2. Poll `/status`.

**Verification (two paths depending on Databento entitlement):**

- **Entitled-to-ES path (GREEN end-to-end):** `status=completed` with real ES futures metrics.
- **Not-entitled-to-ES path (still validates the fix):** `status=failed` with `error.code="engine_crash"` (INGEST_FAILED → RuntimeError → classifier tags as ENGINE_CRASH); Inspect structured logs via `docker compose logs backtest-worker | grep $BT_ID`:
  - `backtest_auto_heal_started asset_class=futures` (orchestrator derived futures correctly).
  - `backtest_auto_heal_ingest_enqueued` present.
  - Ingest worker log `run_ingest(asset_class='futures', ..., provider='auto')` fires — NOT `asset_class='stocks'`.

Either path validates the core PR #39 bug closure: `asset_class` is derived from the canonical instrument id shape (`ES.n.0` → futures), not from caller-supplied config or hardcoded `"stocks"` default.

---

## UC-BAI-004 — Concurrent dedupe

**Intent:** Two concurrent submits for the same missing symbol/range share a single ingest job (no duplicate provider spend).

**Setup:** authenticated; cold futures symbol `ES.n.0` (or cleaned SPY for stocks).

**Steps:**

1. Within 300ms, submit two `POST /run` requests with IDENTICAL `instruments` + `start_date` + `end_date`.
2. Capture both response IDs (A and B).
3. Poll both.
4. Grep the backtest-worker log for `backtest_auto_heal_ingest_enqueued`.

**Verification:**

- Both backtests eventually reach the same terminal state (both `completed` if data available, OR both `failed` with identical error if not).
- TWO `backtest_auto_heal_ingest_enqueued` events fire, but with DISTINCT `dedupe_result`:
  - First event: `dedupe_result=acquired`, with some `ingest_job_id=X`.
  - Second event: `dedupe_result=wait_for_existing:X` (same job_id).
- Both events share the SAME `lock_key=auto_heal:<sha>`.
- Only ONE arq ingest job was actually dispatched (same `ingest_job_id` in both events).

**Expected failure modes:** PASS if structured logs are surfacing. If log routing is broken (structured events not in container stdout), mark `SKIPPED_FAIL_INFRA` — the dedupe logic is covered by unit test `test_auto_heal.py::test_dedupe_lock_already_held_waits_for_existing_holder`.

---

## UC-BAI-005 — UI phase indicator + persistence (fullstack)

**Intent:** Operator opens the backtest detail page during an in-flight auto-heal; sees the phase indicator + progress message; reload persists; transition to terminal state clears the indicator; list page badge visible for running-with-awaiting_data rows.

**Setup (tip to hold the awaiting_data window long enough for browser observation):**

1. `docker compose -f docker-compose.dev.yml pause ingest-worker` — the heal will sit in awaiting_data until unpaused.
2. Clean target symbol data (as in UC-BAI-001).
3. Submit a backtest via API (or UI Run form) for the cold symbol.

**Steps:**

1. Navigate browser to `/backtests/{id}` for the in-flight backtest.
2. Assert DOM:
   - `document.querySelector('[data-testid="backtest-phase-indicator"]')` is present + visible (`offsetParent !== null`).
   - `[data-testid="backtest-phase-message"]` text matches the progress_message (e.g., `"Downloading stocks data for SPY.XNAS"`).
3. `page.reload()` — re-assert indicator + message still visible (persistence).
4. Navigate to `/backtests` list page.
   - Assert `document.querySelector('[data-testid="backtest-list-fetching-badge"]')` visible on the running row.
   - Assert the row has a clickable `a[href="/backtests/{id}"]` ExternalLink (iter-1 P1-e fix — running rows were not clickable before this PR).
5. Click through to detail page — indicator still visible.
6. `docker compose unpause ingest-worker` — heal resumes.
7. Within ~10-30s (Databento ingest + coverage re-check + backtest re-run), detail page transitions:
   - Indicator disappears (`phase` cleared).
   - Status badge shows `completed` (or `failed` if heal failed).
   - If completed: Sharpe/Sortino/Max DD/Total Return/Win Rate/Trade-count metric cards populate with real numbers.

**Verification:**

- All 4 testid gates pass when `phase == "awaiting_data"`.
- Reload preserves indicator.
- Terminal transition clears indicator AND refreshes UI without manual reload (the polling `useEffect` handles it).

**Known out-of-scope UI limitations (not blocking this UC):**

- Equity Curve / Drawdown chart / Monthly Returns Heatmap all render empty — pre-existing, see CONTINUITY §"Known issues surfaced this session" #UI-RESULTS-01.
- Trade Log renders empty even though `/results` returns 418 trades — pre-existing, same tracking item.

**Expected failure modes:** PASS. Auth-required UI — if MSAL redirects to a login page (production) rather than accepting `NEXT_PUBLIC_MSAI_API_KEY` (dev), switch to dev-mode API-key auth or authenticate first.

---

## Regression-suite notes

Run these 5 UCs via the `verify-e2e` agent (or directly via Playwright MCP + curl) on every PR that touches:

- `backend/src/msai/services/backtests/auto_heal.py` or adjacent helpers
- `backend/src/msai/workers/backtest_job.py` (retry-once loop integrity)
- `backend/src/msai/services/backtests/classifier.py`
- `backend/src/msai/services/backtests/derive_asset_class.py`
- `backend/src/msai/services/nautilus/security_master/service.py::asset_class_for_alias` (taxonomy map)
- `backend/src/msai/services/nautilus/catalog_builder.py::verify_catalog_coverage` (gap tolerance)
- `frontend/src/app/backtests/[id]/page.tsx` (polling loop)
- `frontend/src/app/backtests/page.tsx` (list page badge)

UC-BAI-001 is the "golden path" — if it regresses, no backtest can auto-heal.
