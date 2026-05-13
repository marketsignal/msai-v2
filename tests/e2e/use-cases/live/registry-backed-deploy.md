# E2E Use Cases — Registry-backed live-start

**Feature:** live-path wiring onto instrument registry (PR feat/live-path-wiring-registry)
**Interface types:** API (primary), CLI (for ARRANGE), no UI
**Drafted:** 2026-04-20 (Phase 3.2b of `/new-feature` workflow)
**Graduates to** `tests/e2e/use-cases/live/` after Phase 5.4 verify-e2e passes.

Per `.claude/rules/testing.md`: each case = Intent → Steps → Verification → Persistence. Setup through any user-accessible interface (API/CLI); VERIFY through the same interface the use case targets.

---

## UC-L-REG-001 — Deploy a non-Phase-1 ETF (QQQ) after CLI refresh

**Intent:** Operator wants to trade QQQ live on a paper account without editing any Python code or redeploying the stack.

**Pre-conditions:**

- Stack up: `curl -sf http://localhost:8800/health` returns 200
- IB Gateway reachable on paper (`IB_GATEWAY_PORT_PAPER=4004` via socat → gateway loopback 4002, account `DUP733211`)
- QQQ is NOT in the registry (verify: `psql $DATABASE_URL -c "SELECT * FROM instrument_aliases WHERE alias_string LIKE 'QQQ%'"` returns zero rows)

**Steps:**

1. CLI: `uv run msai instruments refresh --symbols QQQ --provider interactive_brokers` — warms the registry via IB qualification.
2. API: `POST /api/v1/live-portfolios/` — create a portfolio. Body: `{"name": "qqq-smoke", "description": "UC-L-REG-001"}`.
3. API: `POST /api/v1/live-portfolios/{id}/strategies` — add a strategy member referencing `strategies/example/buy_hold.py` with `instruments=["QQQ"]`, `weight=1.0`.
4. API: `POST /api/v1/live-portfolios/{id}/snapshot` — snapshot + freeze (renamed from `/revisions` in a prior PR).
5. API: `POST /api/v1/live/start-portfolio` with `{"portfolio_revision_id": "<id>", "account_id": "DUP733211", "paper_trading": true}`.
6. API: poll `GET /api/v1/live/status` until `status=running` or `failed` (max 60s).

**Verification (through the API only):**

- Response from step 5 is HTTP 200 with `status=running` (or `ready`), `paper_trading=true`, valid `deployment_slug`.
- `GET /api/v1/live/status` shows the deployment in `running` state.
- Within 60 seconds of step 5, `GET /api/v1/live/trades?deployment_id={id}` eventually shows bar data flowing (or at minimum the deployment remains `running` for 60+ seconds — not flipped to `failed`).
- Supervisor stdout (`docker compose logs live-supervisor 2>&1 | grep live_instrument_resolved`) contains a line with `source=registry`, `symbol=QQQ`, `canonical_id=QQQ.NASDAQ|QQQ.ARCA`, `asset_class=equity`.

**Persistence:**

- `GET /api/v1/live/status` after 30s still shows the deployment.
- Re-run `GET /api/v1/live/status` from a fresh shell (simulating a frontend reload) — same result.

**Teardown:** `POST /api/v1/live/kill-all` to flatten + stop before the next test.

**Priority:** Must Have (PRD US-001 acceptance)

---

## UC-L-REG-002 — Un-warmed symbol returns HTTP 422 with operator hint

**Intent:** Operator tries to deploy GBP/USD WITHOUT first running `msai instruments refresh`; the system fails fast with a copy-pastable fix command, not a silent subscription failure.

**Pre-conditions:**

- Stack up + IB Gateway paper reachable
- GBP/USD is NOT in the registry

**Steps:**

1. API: `POST /api/v1/live-portfolios/` — create portfolio `gbp-unwarmed-smoke`.
2. API: add a strategy member with `instruments=["GBP/USD"]`.
3. API: snapshot + freeze the revision.
4. API: `POST /api/v1/live/start-portfolio` with the revision id.

**Verification:**

- Response from step 4 is HTTP 422.
- Response body matches:
  ```json
  {
    "error": {
      "code": "REGISTRY_MISS",
      "message": "Symbol(s) not in registry: ['GBP/USD'] as of <YYYY-MM-DD>. Run: msai instruments refresh --symbols GBP/USD --provider interactive_brokers",
      "details": {
        "missing_symbols": ["GBP/USD"],
        "as_of_date": "<ISO date>"
      }
    },
    "failure_kind": "registry_miss"
  }
  ```
- `details.missing_symbols` is a list with exactly `["GBP/USD"]`.
- Response body contains the EXACT `msai instruments refresh --symbols GBP/USD --provider interactive_brokers` command (copy-pastable).

**Persistence:**

- `GET /api/v1/live/status` does NOT show a running/starting deployment for this revision (the spawn short-circuited).
- `GET /api/v1/alerts/` (paginated) contains a WARN-level entry with title "Live instrument registry miss" and GBP/USD in the message — fired by `_fire_alert_bounded` before the raise.
- **Retry-after-fix is NOT cacheable:** run the `msai instruments refresh --symbols GBP/USD --provider interactive_brokers` command. Re-POST the same `/start-portfolio` request with the SAME `Idempotency-Key`. Expected: the new request proceeds (not a cached 422 replay). This verifies `registry_permanent_failure(cacheable=False)`.

**Priority:** Must Have (PRD US-002 acceptance + iter-4 P1 for cacheability)

---

## UC-L-REG-003 — Futures-roll boundary returns the correct contract month

**Intent:** On a futures-roll day, the resolver and the subprocess agree on the same front-month contract. A deploy at `spawn_today=pre-roll` subscribes to `ESM6`; at `spawn_today=post-roll` subscribes to `ESU6`.

**Pre-conditions:**

- Registry seeded with TWO ES aliases via direct SQL (allowed as test ARRANGE since it's mimicking what `msai instruments refresh` would produce at real roll boundary):
  - `ESM6.CME` with `effective_from=2026-03-20, effective_to=2026-06-20`
  - `ESU6.CME` with `effective_from=2026-06-20, effective_to=NULL`
- IB Gateway paper reachable

**Steps (pre-roll case):**

1. Set supervisor spawn date override via env var (dev-only): `SPAWN_TODAY_OVERRIDE=2026-06-19` in the live-supervisor container (requires a small dev-only knob — confirm during Phase 4 TDD whether this exists or must be added; if not, defer this case to a unit integration test that stubs `exchange_local_today()`).
2. API: create portfolio with strategy member `instruments=["ES"]`, snapshot + freeze.
3. API: `POST /api/v1/live/start-portfolio` for DUP733213 (the futures-enabled paper account).

**Verification (pre-roll):**

- Response HTTP 200, `status=running`.
- Supervisor log `live_instrument_resolved` shows `symbol=ES, canonical_id=ESM6.CME`.
- `GET /api/v1/live/trades` (if market hours) shows bar data on ESM6.

**Steps + Verification (post-roll):**

Repeat with `SPAWN_TODAY_OVERRIDE=2026-06-20`; expect `canonical_id=ESU6.CME` in the log.

**Persistence:** Each case's deployment remains `running` until `/kill-all`.

**Priority:** Must Have (PRD US-003 acceptance). If env-var override is not yet implemented, fall back to an integration-level test at `test_lookup_for_live.py` that covers the alias-window semantics — that's already in Task 6 of the plan.

---

## UC-L-REG-004 — Option asset class rejected with HTTP 422

**Intent:** Trying to deploy an option symbol that's in the registry (from some prior refresh) must be rejected with `UNSUPPORTED_ASSET_CLASS` — options trading is deferred to a separate PRD.

**Pre-conditions:**

- Registry contains an option row (seed via SQL: `asset_class='option'` on `instrument_definitions`, alias like `SPY_CALL_500_20260619.CBOE`).

**Steps:**

1. Create portfolio + revision with `instruments=["SPY_CALL_500_20260619.CBOE"]`.
2. `POST /api/v1/live/start-portfolio`.

**Verification:**

- HTTP 422.
- Body: `{"error": {"code": "UNSUPPORTED_ASSET_CLASS", "message": "...", "details": {"symbol": "...", "asset_class": "option"}}, "failure_kind": "unsupported_asset_class"}`.

**Persistence:** No running deployment created.

**Priority:** Must Have (PRD US-004 + design constraint verification)

---

## UC-L-REG-005 — Telemetry signals reach `/metrics` endpoint

**Intent:** Every `lookup_for_live` call increments the `msai_live_instrument_resolved_total` counter with the correct `source` + `asset_class` labels; every miss/incomplete emits a structured log entry.

**Pre-conditions:**

- Stack up; QQQ pre-warmed in registry (from UC-L-REG-001).
- Snapshot `GET /metrics` baseline for `msai_live_instrument_resolved_total`.

**Steps:**

1. Repeat UC-L-REG-001's deploy (QQQ with registry hit).
2. Repeat UC-L-REG-002's deploy (un-warmed symbol miss).
3. Repeat UC-L-REG-004's deploy (option → unsupported).
4. `GET /metrics` and diff against baseline.

**Verification:**

- `msai_live_instrument_resolved_total{source="registry",asset_class="equity"}` increased by ≥ 1.
- `msai_live_instrument_resolved_total{source="registry_miss",asset_class="unknown"}` increased by ≥ 1.
- Supervisor stdout contains structured log lines matching the 3 flows:
  - `{event: "live_instrument_resolved", source: "registry", symbol: "QQQ", ...}`
  - `{event: "live_instrument_resolved", source: "registry_miss", symbol: "GBP/USD", ...}`

**Persistence:** Metrics values survive across scrape intervals.

**Priority:** Must Have (PRD US-005 + council constraint #6)

---

## Notes for Phase 5.4 verify-e2e agent

- **ARRANGE:** use the API + CLI only. `msai instruments refresh` is the only way to seed registry for the miss-then-fix case (UC-L-REG-002). Registry rows for UC-L-REG-003 (pre-seeded aliases) and UC-L-REG-004 (option row) are test-setup fixtures acceptable for E2E arrange per `.claude/rules/testing.md`.
- **VERIFY:** ONLY through `/api/v1/live/start-portfolio`, `/api/v1/live/status`, `/api/v1/live/trades`, `/api/v1/alerts/`, `/metrics`. Never touch Postgres/Redis directly in VERIFY.
- **Live-trading safety:** all use cases default to paper account (`DUP733211`/`DUP733213`). No live-account use cases in this regression suite.
- **Stop-the-world rule:** if any API call during a flow returns 5xx, halt the test — don't proceed to UI (there's no UI in scope here anyway; this PR is API-only).

## Graduation criteria (Phase 6.2b)

Each use case promotes to `tests/e2e/use-cases/live/registry-backed-deploy.md` after:

1. Verify-e2e agent executes it end-to-end against `http://localhost:8800`
2. All 5 cases report PASS classification (not FAIL_BUG / FAIL_STALE / FAIL_INFRA)
3. Real-money drill on U4705114 (Task 15 of the plan) separately validates at least one case against live trading
