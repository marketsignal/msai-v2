# PRD Discussion: Symbol Onboarding

**Status:** Complete (2026-04-24)
**Started:** 2026-04-24
**Participants:** Pablo, Claude, 5 council advisors (Simplifier / Scalability Hawk / Pragmatist via Claude + Contrarian / Maintainer via Codex) + Codex xhigh chairman

## Original User Stories

**User vision (verbatim, 2026-04-24):**

> "How the user via CLI, API and UI tells msai that it wants a series of symbols at different bars (1s, 1m, 5m, etc), for example (SPY, E-mini, IWM, AAPL, all in 5 mins and 1 min)."

## Architecturally pinned decisions (ratified before PRD)

1. **Storage is 1m canonical.** 5m / 10m / 30m / 1h / 1d all derive at backtest time via Nautilus `BarAggregator`. Onboarding has NO UI/API surface for "pick 5m AND 1m" — that's implicit. User's watchlist-level timeframe picker is a pure display affordance informing the strategy template, NOT an ingest multiplier.
2. **1-second bars are out of scope for v1.** Different Databento schema, different storage partition, ~60× cost. Deferred to a separate PRD once demand is real.
3. **Three readiness states** (the contract from PR #44) are the primary data model: `registered` / `backtest_data_available` / `live_qualified`. API/CLI/UI all surface these.
4. **`live_qualify: false` default** — onboarding stops at "backtest-ready" unless explicitly opted-in.
5. **Manifest-driven CLI** — trader's watchlist is a `.yaml` in git alongside strategies. Batch onboarding from manifest is the common case.
6. **API is primary** — orchestrates existing primitives (bootstrap from PR #44 + ingest pipeline + IB refresh).
7. **Asynchronous job** — `POST /api/v1/symbols/onboard` returns 202 + job_id; status polled via `GET /api/v1/symbols/onboard/{id}/status`.

## Competitor / industry notes

- **QuantConnect** is code-declarative: `ManualUniverseSelectionModel(symbols=[...])` lives inside the strategy. No UI concept of "add symbol to my data library" — the platform owns the cloud data catalog and symbols are always available. Not our model; we pay per-query to Databento.
- **Tradestation / IBKR** focus UX on trading side, not backfill side. Historical data is a passive side-effect of having a market-data subscription.
- **Alpaca** is developer-API-first, no UI concept here.
- **Insight:** MSAI's "onboarding" surface is genuinely novel — we OWN the data catalog + pay per-query, and Pablo is both operator and trader. The closest analog is dev-ops "seed my dev environment" patterns (`terraform apply`, `make seed-dev`) applied to market-data.

## Discussion Log

### 2026-04-24 — Initial questions for Pablo

I have the 7 pinned decisions above. These are the gaps I see before we can write a solid PRD. I'll ask them in groups of related concerns.

#### Watchlist structure + personas

**Q1 — One universe or many?**
Is `/universe` a SINGLE list ("everything Pablo cares about") or multiple named watchlists ("Equities", "Futures", "Vol regime experiments")? CRUD on named watchlists, or just one?

**Q2 — Who uses this besides Pablo?**
CLAUDE.md says single-user, but is there a read-only "view my universe" case (e.g., a dashboard widget)? Or is everything edit-equals-view?

#### Data windows + coverage semantics

**Q3 — Ingest window defaults.**
When I onboard AAPL, do I always specify `start / end`? Or is there a default like "everything Databento has for this symbol" OR "last 5 years"? When I onboard AAPL today for 2024-01-01 → today, and tomorrow want today+1, does the system auto-extend or require a new call?

**Q4 — Coverage-gap remediation — read-only or actionable?**
If onboarding AAPL 2024-01-01 → 2025-04-01 ingests OK except for 2024-07-04 → 2024-07-08 (holiday week feed glitch), does the UI:

- (a) Show "partial: 99% coverage, gap 2024-07-04 to 2024-07-08" read-only — user has to call a separate remediation job, OR
- (b) Show a "Fill gaps" button on the row that triggers a re-ingest of just the missing range, OR
- (c) Auto-retry silently on discovery?

#### Mid-job semantics

**Q5 — Partial-batch failure.**
I onboard 20 symbols. Symbol #7 times out on Databento. Do the other 19 continue? The PR #44 bootstrap precedent is "continue others + HTTP 207 mixed." Same here? Or should onboarding be more conservative (stop on first failure, surface to user, let them restart)?

**Q6 — Can I cancel mid-run?**
20-symbol × 5-year batch takes minutes. Is cancel a requirement? What does cancel leave behind — nothing, partial ingest (symbols 1-5 backfilled, 6-20 aborted), or "clean up everything"?

#### Integration with existing surfaces

**Q7 — Relationship to existing `POST /api/v1/market-data/ingest`.**
That endpoint already exists (queues an arq ingest job). Is new `/symbols/onboard` a SUPERSET that orchestrates bootstrap+ingest+IB — replacing `/market-data/ingest` as the primary user-facing surface? Or does `/market-data/ingest` stay as a lower-level primitive and `/symbols/onboard` sits above it?

**Q8 — Nautilus catalog rebuild.**
After ingest writes Parquet, Nautilus needs a catalog rebuild to pick up new data. PR #16 auto-rebuilds on backtest run. Does the onboard job ALSO rebuild eagerly (so `backtest_data_available=true` really means ready-to-backtest-immediately), or leave it lazy (the flag means "data exists in Parquet, catalog builds on first backtest")?

#### Operational / UX details

**Q9 — Manifest file location.**
Where does `watchlist.yaml` live? Options: repo root `watchlists/*.yaml`, under `strategies/<strat>/watchlist.yaml` (per-strategy), or a single `watchlist.yaml`? Affects git workflow + which strategies reference which universes.

**Q10 — Cost preview.**
Databento charges per-query. Should the API/UI show an estimate before onboarding ("this batch will cost ~$2.40")? If yes, affects whether we pre-call `metadata.list_datasets` to size the estimate. If no, Pablo learns cost from Databento's own dashboard after-the-fact.

---

## Answers delegated to Engineering Council (2026-04-24)

Pablo routed the 10 open questions to `/council`. Five advisors responded; Codex xhigh synthesized. Verdict below is **binding** for the PRD.

---

## Refined Understanding (council-ratified, 2026-04-24)

### Personas

- **Pablo (operator + trader)**: single writer/admin, runs CLI + hits API + browses UI. All write semantics assume one operator in v1.
- **Read-only API/UI consumers**: strategies reading which symbols/windows are live-qualified, dashboards showing coverage state. Allowed in v1; **no** separate auth role or collaboration model required.

### Pinned architectural decisions (do not reopen)

1. Storage is 1m canonical; 5m/10m/30m/1h/1d derive at backtest time via Nautilus `BarAggregator`.
2. 1-second bars OUT of v1 (different Databento schema, 60× cost, separate PRD).
3. Three-state readiness model **with the Contrarian's amendment**: `registered` / `backtest_data_available(provider, window)` / `live_qualified`. `backtest_data_available` is **NOT symbol-global** — it's scoped by historical provider + requested window. List views without window-in-scope return `null` or a coverage summary, NOT an unqualified `true`.
4. `live_qualify: false` is the default on onboarding requests.
5. Manifest-driven CLI (file-based YAML).
6. API primary, CLI secondary, UI tertiary.
7. Async job: `202 + job_id`, polled via `GET /api/v1/symbols/onboard/{id}/status`.

### Binding answers to Q1–Q10 (from Council verdict)

1. **Many named watchlists**, repo-root `watchlists/*.yaml`; backend compiles them into one canonical desired-symbol set for dedupe.
2. **Single-writer v1**, read-only consumers allowed (no role model).
3. **No hidden default window.** Every request must resolve to explicit `start`/`end`; CLI/manifest sugar like `trailing_5y` acceptable only if the server echoes resolved dates in job status. Repeats idempotent — only enqueue missing uncovered ranges.
4. **Explicit gap reporting + explicit repair.** `coverage_status=gapped` with named missing ranges. Separate repair action. No silent retry.
5. **Continue per symbol, fail fast only on systemic faults.** Mixed batch ends `completed_with_failures` with per-symbol `step` / `error` / `next_action`.
6. **No cancel in v1.** If added later: best-effort stop-future-work, partial ingest persists, no rollback.
7. **Compose, don't replace.** `/symbols/onboard` orchestrates `/instruments/bootstrap` + `/market-data/ingest` + optional IB refresh. `/market-data/ingest` stays as lower-level primitive. **`/api/v1/universe` is DEPRECATED as a user-facing write surface** — legacy/internal only, ideally derived from watchlists for nightly-ingest compatibility.
8. **Lazy Nautilus catalog rebuild.** Keep PR #16 path. `backtest_data_available` means "historical source data for requested window exists in canonical storage," NOT "catalog already rebuilt."
9. **`watchlists/*.yaml` at repo root.** Strategies reference watchlists by name; don't embed symbol lists.
10. **Preflight estimate + spend ceiling.** `dry_run` returns `estimated_cost_usd`, `estimate_basis`, `estimate_confidence`; execution accepts `max_estimated_cost_usd` and fails closed if exceeded.

### Non-goals (v1)

- ❌ UI surface (`/universe` Next.js page). Deferred to post-v1 once API+CLI are proven.
- ❌ Cancel mid-run.
- ❌ Rollback semantics.
- ❌ Silent auto-retry on coverage gaps.
- ❌ Per-strategy manifests.
- ❌ Single top-level `watchlist.yaml` (doesn't scale to named lists).
- ❌ DB-backed watchlist CRUD.
- ❌ 1-second / tick bars.
- ❌ Multi-user / RBAC / sharing.
- ❌ Cron-based onboarding scheduler.
- ❌ New data providers (Databento + IB only in v1).

### Binding contract corrections (non-negotiable PRD requirements)

1. **`backtest_data_available` must be window + provider scoped.**
2. **`/api/v1/universe` deprecation.** `asset_universe.resolution` must be removed from user intent (pin #1 makes storage 1m canonical).

### Minority Report preserved

- **Simplifier** wanted ONE watchlist + no cost preview. Overruled — multi-file solves real organization problems; cost preview is operational self-defense.
- **Scalability Hawk** demanded circuit-breaker + cancel-now + 3 metrics. Circuit-breaker + cancel-now overruled (tenacity + arq handle common cases; single-user load). Metrics narrowed to 2 low-cardinality.
- **Contrarian** raised steel-manned "kill the feature" argument. Overruled — but his pin-#3 fatal-flaw flag ACCEPTED.
- **Maintainer** caught `/api/v1/universe` + `asset_universe` model fracture. ACCEPTED in full — deprecation mandatory.

### Open Questions (deferred to Phase 2 research or Phase 4 TDD)

- [ ] Exact Databento `Historical.metadata.get_cost(...)` semantics (accuracy, asset-class coverage).
- [ ] Actual runtime usage of `/api/v1/universe` beyond nightly ingest — confirm deprecation is safe.
- [ ] Safest migration from `asset_universe` → derived watchlist projection.
- [ ] Exact status payload for window-scoped coverage (`true` / `gapped` / `null`).
- [ ] Whether `asset_universe.resolution` is renamed (`storage_resolution`) or dropped.
- [ ] Minimum Prometheus metrics set for v1.

### Next step

Proceed to `/prd:create symbol-onboarding`.
