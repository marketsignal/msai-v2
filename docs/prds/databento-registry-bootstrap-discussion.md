# PRD Discussion: Databento registry bootstrap

**Status:** Complete
**Started:** 2026-04-23
**Closed:** 2026-04-23
**Participants:** User (Pablo), Claude, Codex (Pablo-delegated advisor)

## Original User Stories (verbatim north-star, 2026-04-23)

> "I want to be able to run backtests and add symbols and run backtests and then graduate them and go into production. I won't be able to use any instruments and create strategies for any instrument, any time frame, any bar (1-minute, 5-minute, 1-hour bar) of any asset type, and iterate quickly and see where I can find alpha before I can graduate it to a portfolio and then to live trading."

**Scope clarification from Pablo (Round 1):**

> "Ultimately what I want is any asset class instrument: Forex, indexes, equities, futures, and options later. In any bar format: 1m, 5m, 10m, 30m, 1h, 1d. Download that data whenever I want so I can run strategies on top. A strategy might work on a 1m bar but not on 1h — they use different strategies. Remember this is API-first, CLI-second, UI-third, so the three options are available to do what I want."

**Concrete pain point (2026-04-21, PR #40 SPY live demo):** registry empty for equities; raw SQL INSERTs required to seed SPY because the only Databento CLI branch was futures-only.

---

## Council Pre-Work (2026-04-23) — DONE

Full scope council already ratified. Decision doc at `docs/decisions/databento-registry-bootstrap.md`.

- **Verdict:** `1b + 2b + 3a + 4a` (arbitrary on-demand + equities/ETFs/futures + Databento-as-peer + metered-mindful)
- **7 blocking constraints:** rate-limit/retry/counter, advisory lock, divergence observability, backtest-discoverable-only semantics, no catalog-sync cron, AAPL/SPY/QQQ end-to-end acceptance, new verb (`bootstrap`)

## Round 1 — 10 design questions (2026-04-23)

Claude posted 10 questions. Pablo delegated to Codex ("I don't have enough information to answer this, ask Codex"). Codex ran `sonbox=read-only` + WebSearch on current Databento docs, returned concrete recommendations, flagged 3 scope mismatches against Pablo's north-star + 1 meta-observation.

### Codex's answers — Pablo accepted wholesale

- **Q1 Command surface:** API + CLI in v1. `POST /api/v1/instruments/bootstrap` is primary; `msai instruments bootstrap` is a thin wrapper. UI deferred.
- **Q2 Batch:** batches with `max_concurrent=3` hard cap; HTTP 207 Multi-Status on partial success.
- **Q3 Asset class:** auto-detect with `--asset-class`/`asset_class_override` as optional, required for ambiguous cases.
- **Q4 Datasets:** `XNAS.ITCH` / `XNYS.PILLAR` / `ARCX.PILLAR` / `GLBX.MDP3` as primary venue datasets. `EQUS.MINI` as bars-fallback for research (not registry-authoring). `EQUS.SUMMARY` as daily-only fallback.
- **Q5 Ambiguity:** fail-fast, 422 `AMBIGUOUS_BOOTSTRAP_SYMBOL` + structured `candidates[]`.
- **Q6 Error envelope:** reuse existing `_emit_json` pattern + `{error:{code,message,details}}` on failure.
- **Q7 Idempotency:** always-success with outcomes `created` / `noop` / `alias_rotated`.
- **Q8 Metrics:** 3 counters + 1 histogram (`msai_registry_bootstrap_duration_seconds{provider,outcome}`).
- **Q9 Futures in v1:** YES, don't split. Codex claims `BE-01` appears fixed in current branch parser (Phase 2 research verifies).
- **Q10 Acceptance:** `AAPL + SPY + QQQ` at 1m full E2E + `SPY` at 30m aggregated smoke + `ES` at 1m if futures claimed. Not the full 1m/5m/10m/30m/1h/1d matrix (mid-range bars ride aggregation).

### Scope reality checks Codex flagged (Pablo accepted all)

1. **Forex NOT in v1 via Databento** — Databento's Spot FX page says "Coming soon" as of 2026-04-23. FX continues through the existing IB-only path (PR #37 live drill ran EUR/USD via IB successfully). PR scope is equities + ETFs + futures only.
2. **Cash indexes NOT tradeable as Databento bars** — SPX/NDX/RUT not publishable. Use ETF proxies (`SPY`/`QQQ`/`IWM`) or index futures (`ES`/`NQ`/`RTY`) — which are what you'd actually trade anyway.
3. **Bar-timeframe matrix smaller than it looks** — Databento natively publishes `1s/1m/1h/1d`. `5m/10m/30m` come from aggregating 1m through existing pipeline. Acceptance demo: prove 1m + one aggregation, not 6 separate demos.

### Codex meta-observation (accepted)

**Three readiness states** must be explicit in the PRD and API response model, not implicit:

- `registered` — row exists in the registry (what `bootstrap` produces)
- `backtest-data-available` — auto-heal has downloaded bars for the requested window
- `live-qualified` — `msai instruments refresh --provider interactive_brokers` has run and IB can stream

Prevents the "bootstrap SPY = live-ready" expectation trap. Enables future UI to show a readiness checklist per symbol.

---

## Refined Understanding

### Personas

- **Pablo (single power-user)** — operator + trader. Uses API (primary), CLI (via scripts / terminal), and future UI. Needs iteration-speed on any asset class + any timeframe.

### User Stories (Refined)

- **US-001 — Bootstrap a batch of equity/ETF/futures symbols via API:**
  As Pablo, I call `POST /api/v1/instruments/bootstrap` with `{provider: "databento", symbols: ["AAPL", "SPY", "ES.n.0"]}` and receive per-symbol outcomes showing which symbols are now `registered` (backtest-discoverable) so I can proceed to backtest without raw SQL.

- **US-002 — Bootstrap same batch via CLI:**
  As Pablo, I run `msai instruments bootstrap --provider databento --symbols AAPL,SPY,ES.n.0` and see per-symbol success/failure lines on stderr plus a structured JSON result on stdout.

- **US-003 — Immediate backtest after bootstrap:**
  As Pablo, after bootstrapping `AAPL`, I submit `POST /api/v1/backtests/run` with `{strategy: "ema_cross", symbols: ["AAPL"], bar_spec: "1-MINUTE"}` and the request does NOT 422 on registry miss; auto-heal downloads the missing bars and the backtest completes with metrics.

- **US-004 — Ambiguous symbol handling:**
  As Pablo, when I bootstrap `BRK.B` and Databento returns multiple candidates, I receive a 422 with an `AMBIGUOUS_BOOTSTRAP_SYMBOL` code and a `candidates[]` list showing each candidate's listing venue, dataset, and canonical alias, so I can re-run with `--exact-id <id>` (CLI) or `exact_id` (API).

- **US-005 — Idempotent re-run:**
  As Pablo, re-running `bootstrap SPY` succeeds. If nothing changed, the result shows `outcome: "noop"`. If Databento's canonical ID changed (rare), the old alias closes, a new alias opens at `effective_from=today`, and the result shows `outcome: "alias_rotated"`.

- **US-006 — Explicit readiness states in response:**
  As Pablo, the API response makes readiness explicit: each symbol comes back with `{registered: true, backtest_data_available: false, live_qualified: false}` — so when I build a UI later, it renders a 3-state checklist and nobody confuses "bootstrapped" with "live-ready."

- **US-007 — Bootstrap ES (futures) end-to-end:**
  As Pablo, I run `bootstrap ES.n.0`, the continuous-futures resolver finds the current front-month contract, registers it, and a subsequent backtest on `ES.n.0` 1m bars works against `GLBX.MDP3` data. This proves Codex's "BE-01 appears fixed" claim OR surfaces a fresh defect in the same PR.

- **US-008 — Metered-mindful rate limiting:**
  As Pablo, when I bootstrap 20 symbols at once, the request caps concurrent Databento API calls at 3, retries with exponential backoff on 429, and surfaces final per-symbol outcomes. No partial-write poisoning.

- **US-009 — Divergence observability:**
  As Pablo, when I later run `msai instruments refresh --provider interactive_brokers` for a symbol Databento already registered, and IB qualifies a different venue, the registry writes the new alias AND emits a `registry_bootstrap_divergence` structured log + increments `msai_registry_venue_divergence_total{databento_venue, ib_venue}` — so I can detect venue renames (e.g., ETF moves ARCA→BATS) before they hit live.

- **US-010 — Graduation (explicit two-step workflow):**
  As Pablo, after bootstrap + backtest succeeds, to deploy live I MUST run `msai instruments refresh --provider interactive_brokers --symbols AAPL` as an explicit step. The bootstrap response and CLI `--help` text document this requirement; the API never implies live-readiness.

### Non-Goals (explicit out-of-scope for this PR)

- **Options** — defer to separate PRD (chain loading, strike-band policy, OPRA entitlement).
- **Forex** — Databento doesn't sell Spot FX yet (2026-04-23). FX stays on the existing IB path.
- **Cash indexes (SPX/NDX/RUT)** — not Databento-tradeable. Use ETF/futures proxies.
- **Recurring catalog-sync cron** — undoes PR #37 operator-managed decision.
- **Databento replaces IB as canonical** — IB stays authoritative for live qualification.
- **UI for symbol onboarding** — separate `Symbol Onboarding UI` PRD (item #2 in backlog).
- **Per-symbol cost estimation in Prometheus** — Codex: requires extra pricing calls, creates false precision. Put estimated-request-count in API response JSON instead.
- **Bulk seed from file (`--from-file sp500.txt`)** — v2 if ever useful.
- **Multiple bar timeframes in acceptance matrix** — prove 1m + one aggregation smoke.

### Key Decisions (from discussion)

1. **Two surfaces in v1:** API (`POST /api/v1/instruments/bootstrap`) + CLI (`msai instruments bootstrap`). CLI is a thin wrapper over the API. UI deferred to separate PRD.
2. **Command name:** `bootstrap`, not `refresh`. `refresh` remains "re-qualify/warm existing provider path" (IB qualification, Databento continuous-futures synthesis).
3. **Batch with concurrency cap:** `max_concurrent=3` hard cap in v1; HTTP 207 Multi-Status on partial success; CLI exits non-zero if any symbol failed.
4. **Auto-detect asset class** with `--asset-class` / `asset_class_override` as optional override. Required for ambiguous symbols (ADRs, cash-index requests).
5. **Dataset tiering:** venue datasets first (`XNAS.ITCH`, `XNYS.PILLAR`, `ARCX.PILLAR`, `GLBX.MDP3`). `EQUS.MINI` as fallback for research only (not registry-authoring).
6. **Fail-fast on ambiguity:** 422 `AMBIGUOUS_BOOTSTRAP_SYMBOL` + structured `candidates[]`.
7. **Idempotent re-run:** outcomes `created` / `noop` / `alias_rotated`.
8. **Three readiness states** in API response model: `registered` / `backtest_data_available` / `live_qualified`.
9. **Metrics v1:** 3 counters (`msai_databento_api_calls_total`, `msai_registry_bootstrap_total`, `msai_registry_venue_divergence_total`) + 1 histogram (`msai_registry_bootstrap_duration_seconds`). Estimated-request-count in JSON response, not Prometheus.
10. **Acceptance demo:** AAPL + SPY + QQQ 1m full E2E + SPY 30m aggregated smoke + ES 1m if futures claimed. Not the full 6-timeframe matrix.
11. **Error envelope shape:** stderr human text + stdout `_emit_json` pattern (no new `--json` flag). HTTP `{error:{code,message,details}}` on failures.
12. **Two-step graduation:** bootstrap = backtest-discoverable only. Live deployment requires explicit `instruments refresh --provider interactive_brokers` second step. Documented in `--help` and API response.

### Open Questions (for Phase 2 research-first agent to resolve)

- [ ] **OQ-1:** Does Pablo's current Databento plan cover `XNAS.ITCH`, `XNYS.PILLAR`, `ARCX.PILLAR`, `GLBX.MDP3`? 30-second check: `curl -H "Authorization: Bearer $DATABENTO_API_KEY" https://hist.databento.com/v0/metadata.list_datasets | jq`.
- [ ] **OQ-2:** Real Databento rate limits on `metadata.list_symbols` / `timeseries.get_range?schema=definition`. Drives the `max_concurrent=3` default.
- [ ] **OQ-3:** Codex claims `BE-01` (`FuturesContract.to_dict()` signature drift in `parser.py:188`) is fixed on this branch. Needs reproduction attempt to confirm before claiming futures support in v1.
- [ ] **OQ-4:** Advisory-lock necessity — do existing UniqueConstraint + CHECK already serialize the alias-upsert path, or is `pg_advisory_xact_lock` required?
- [ ] **OQ-5:** `fetch_definition_instruments` behavior for ambiguous equity inputs (`BRK.B`, `BF.B`, dual-listed ADRs) — what does the SDK actually return? Drives the `candidates[]` payload shape.
- [ ] **OQ-6:** Bar aggregation in existing pipeline — confirm 5m/10m/30m derive from 1m via the existing resampler and don't require separate Databento schema requests (would 2x billing).

## Ready for `/prd:create`

Pablo's approval line (verbatim 2026-04-23): "Yeah, it looks okay. All four of them."

Proceed to `/prd:create databento-registry-bootstrap`.
