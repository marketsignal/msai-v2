# Symbol Onboarding — E2E Use Cases

> Draft — graduates to permanent regression set after Phase 5.4 PASS.
> Branch: `feat/symbol-onboarding`. Plan: `docs/plans/2026-04-24-symbol-onboarding.md`.
> Council verdict: Option A (single arq entrypoint, in-process ingest helper).

## UC-SYM-001 — Onboard 4-symbol manifest (happy path)

**Interface:** API + CLI

**Intent:** An operator with a fresh system wants to onboard SPY, AAPL, QQQ, IWM (equities) for 2024 full-year via a single CLI invocation.

**Setup (ARRANGE):**

- Write manifest `watchlists/demo.yaml`:
  ```yaml
  watchlist_name: demo
  symbols:
    - { symbol: SPY, asset_class: equity, start: 2024-01-01, end: 2024-12-31 }
    - { symbol: AAPL, asset_class: equity, start: 2024-01-01, end: 2024-12-31 }
    - { symbol: QQQ, asset_class: equity, start: 2024-01-01, end: 2024-12-31 }
    - { symbol: IWM, asset_class: equity, start: 2024-01-01, end: 2024-12-31 }
  ```
- Bring the dev stack up: `docker compose -f docker-compose.dev.yml up -d`.
- Restart workers to pick up new code: `./scripts/restart-workers.sh`.
- No prior registry rows for these symbols (clean state).

**Steps:**

1. `msai symbols onboard --manifest watchlists/demo.yaml --dry-run` — capture estimated USD + confidence.
2. If confidence == `high` and estimate is acceptable, proceed: `msai symbols onboard --manifest watchlists/demo.yaml`.
3. `msai symbols status <run_id> --watch` until terminal.
4. `curl -H "X-API-Key: ${MSAI_API_KEY}" http://localhost:8800/api/v1/symbols/readiness?symbol=SPY&asset_class=equity&start=2024-01-01&end=2024-12-31` → confirm `backtest_data_available=true`.

**Verification:**

- Dry-run prints a dollar amount > 0 and `confidence=high`.
- Status terminates at `completed`; all 4 symbols show `status=succeeded`, `step=ib_skipped`.
- Readiness for each symbol returns `registered=true`, `backtest_data_available=true`, `coverage_status=full`, `live_qualified=false`.

**Persistence:** Re-running `msai symbols onboard --manifest watchlists/demo.yaml` returns 200 with the SAME `run_id` (idempotency contract — see UC-SYM-007). Status stays at the prior terminal state. Registry rows persist across `docker compose down/up`.

---

## UC-SYM-002 — Preflight cost ceiling rejects oversized window

**Interface:** API + CLI

**Intent:** Operator accidentally requests a 20-year window; preflight cost shows the surprise before any data is downloaded.

**Setup:** Manifest with 1 symbol, `start: 2005-01-01`, `end: 2025-12-31` (20 years of minute-bar data).

**Steps:**

1. `msai symbols onboard --manifest <file> --cost-ceiling-usd 5.00 --dry-run` — prints estimate.
2. Inspect output. If estimate > $5, do NOT proceed.

**Verification:** Dry-run output shows the actual estimated cost (likely $40+ for 20y of minute-bar equity data) and `confidence=high`. Operator can compare against their `--cost-ceiling-usd` ceiling and abort. The CLI does not enforce the ceiling client-side in v1 (ceiling enforcement on the server is part of US-004 follow-up); v1 surfaces the cost up-front so a human operator makes the call.

**Persistence:** No run row created (dry-run is read-only). `GET /readiness` shows no registration changes.

---

## UC-SYM-003 — Partial-batch failure + repair

**Interface:** API

**Intent:** Databento rejects one symbol as ambiguous; the rest succeed; operator repairs the one failed symbol with a disambiguated alias.

**Setup:** Seed run with 3 symbols, one of which is known-ambiguous in Databento (e.g., `BRK` without the `.B` suffix in a window where Databento reports ambiguity). If real ambiguity is not reproducible at run time (Databento universe changes), test via a patched bootstrap service that returns `outcome="ambiguous"` for that symbol — same code path, deterministic result.

**Steps:**

1. POST /api/v1/symbols/onboard with 3 symbols including the ambiguous one.
2. Poll `GET /onboard/{run_id}/status` until terminal; expect `status=completed_with_failures`, one `symbol.status=failed` with `error.code=BOOTSTRAP_AMBIGUOUS`.
3. POST `/api/v1/symbols/onboard/{run_id}/repair` with `{"symbols":["BRK.B"]}` (disambiguated symbol).
4. Poll the new child `run_id` /status; expect `status=completed`.

**Verification:** Parent run stays `completed_with_failures` (immutable per pinned status semantics — repair is a NEW run, not a parent mutation); child run reaches `completed`. Registry now contains all 3 instruments. Per-symbol error envelopes are machine-readable: `error.code` matches the canonical vocabulary defined in T6.

**Persistence:** Same invariant as UC-SYM-001 — registry rows survive container restarts.

---

## UC-SYM-004 — Readiness window scoping is truthful (pin #3 correction)

**Interface:** API

**Intent:** Validate that `backtest_data_available` is never `true` without a window scope. This is the Contrarian's binding objection from the iter-1 council.

**Setup:** UC-SYM-001 has landed; SPY has full 2024 coverage.

**Steps:**

1. `GET /readiness?symbol=SPY&asset_class=equity` (no start/end) → `backtest_data_available=null`, `coverage_status=null`, `coverage_summary` is non-null human-friendly hint.
2. `GET /readiness?symbol=SPY&asset_class=equity&start=2024-01-01&end=2024-12-31` → `backtest_data_available=true`, `coverage_status=full`.
3. `GET /readiness?symbol=SPY&asset_class=equity&start=2023-01-01&end=2024-12-31` → `backtest_data_available=false`, `coverage_status=gapped`, `missing_ranges=[{start:"2023-01-01",end:"2023-12-31"}]`.

**Verification:** Response shapes match the `ReadinessResponse` Pydantic contract. `coverage_status` transitions `null → full → gapped` across the three calls. The `null` case is **structural**, not a workaround — the API explicitly refuses to claim data is "available" without specifying a window.

**Persistence:** Read-only endpoint; no persistence required.

---

## UC-SYM-005 — Live qualification opt-in (paper IB account)

**Interface:** API + IB Gateway (opt-in via `RUN_PAPER_E2E=1` — costs ~$0 to run)

**Intent:** `request_live_qualification=true` triggers IB qualification after ingest; `live_qualified=true` flips for the symbol after the run terminates.

**Setup:**

- `COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml up -d` so IB Gateway is reachable on socat port 4004 (paper).
- Paper IB account configured (`DU…` account prefix, client port 4004 — gateway binds 4002 internally).
- Manifest with SPY only.

**Steps:**

1. POST /api/v1/symbols/onboard with `{"watchlist_name":"sym005", "symbols":[{"symbol":"SPY","asset_class":"equity","start":"2024-01-01","end":"2024-12-31"}], "request_live_qualification": true}`.
2. Poll /status until terminal (expect `completed`). The symbol's terminal step should be `completed` (the canonical IB-qualified terminal step per `SymbolStepStatus`).
3. `GET /readiness?symbol=SPY&asset_class=equity` → `live_qualified=true`.

**Verification:** Last symbol step in /status is `completed` (terminal step for the IB-qualified path per the canonical `SymbolStepStatus` vocabulary). Registry has an `interactive_brokers` provider alias row for SPY.

**Persistence:** IB alias row persists; future `/live/start-portfolio` deploys will resolve SPY via the registry (confirms PR #37 live-path-wiring still intact end-to-end).

---

## UC-SYM-006 — IB Gateway unavailable → `IB_TIMEOUT` per-symbol failure

**Interface:** API (with IB Gateway deliberately stopped)

**Intent:** `ib_timeout_s` (120s default) enforcement is real; the run terminates gracefully with a failed symbol, a clear error code, and the Prometheus timeout counter increments.

**Setup:**

- IB Gateway container stopped (`docker compose stop ib-gateway`).
- Manifest with SPY only.
- `request_live_qualification=true`.

**Steps:**

1. POST /onboard.
2. Poll /status — expect terminal **run** status `completed_with_failures` (per-symbol failures NEVER bubble to run-level `failed`; see status-contract table in plan), with the SPY symbol at `status=failed`, `step=ib_qualify`, `error.code=IB_TIMEOUT`.
3. Check Prometheus on `/metrics`: `msai_onboarding_ib_timeout_total` increased by exactly 1 since the test started.

**Verification:** /status surfaces the specific timeout code in the per-symbol error envelope plus `next_action="Retry with request_live_qualification=false then rerun IB later."` Metric counter reflects the event. Run-level status is `completed_with_failures`, NOT `failed` (the latter is reserved for systemic short-circuits — outer try/except crashes only).

**Persistence:** Registry is untouched — the symbol remains `registered` (from the bootstrap phase, which runs BEFORE IB qualification) but `live_qualified=false`. This is correct: a failure at the IB-qualification step must NOT roll back the bootstrap-level registration. The operator can retry IB qualification later without re-running bootstrap or ingest.

---

## UC-SYM-007 — Idempotency: duplicate POST returns the existing run (council-pinned)

**Interface:** API

**Intent:** Validate the iter-2 P1-B fix — exact-duplicate POSTs collapse onto the existing run with HTTP 200 (not 202), zero new rows, zero new enqueues.

**Setup:** Fresh state; one prior `POST /onboard` for `watchlist_name=core` + `[{SPY, equity, 2024-01-01, 2024-12-31}]` has been issued and returned `run_id=<X>` with status 202.

**Steps:**

1. Re-POST /onboard with the IDENTICAL body (same `watchlist_name`, same symbols, same windows, same `request_live_qualification`).

**Verification:** Response is **HTTP 200 OK** (not 202). Body's `run_id` matches the prior run's `run_id` exactly. `GET /onboard/{run_id}/status` shows the run is in whatever state it had reached on the original POST (no fresh start). `SELECT count(*) FROM symbol_onboarding_runs` increases by 0 across the duplicate POST. Prometheus `msai_onboarding_jobs_total` does NOT increment a second time for this digest.

**Persistence:** The `job_id_digest` unique index on `symbol_onboarding_runs` is the structural enforcement; the application logic short-circuits on the SELECT-FOR-UPDATE before the index ever rejects.
