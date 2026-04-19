# Decision: Wire the live-start path onto the instrument registry

**Date:** 2026-04-19
**Status:** **FINAL — council-ratified (modified Option D)**
**Predecessor:** PR #32 (db-backed instrument registry, schema + SecurityMaster.resolve_for_backtest), PR #35 (`msai instruments refresh --provider interactive_brokers`)
**Context:** `docs/decisions/scratch/council-live-path-registry-wiring.md`

---

## TL;DR

Don't just swap `canonical_instrument_id()` for a registry read — that's half the problem. The subprocess's IB preload (`build_ib_instrument_provider_config()`, `live_node_config.py:478`) ALSO closed-universe-gates on `PHASE_1_PAPER_SYMBOLS`, and `SecurityMaster.resolve_for_live()` is the wrong runtime API (it mixes reads, IB cold-miss qualification, and upserts). Build a new pure-read `lookup_for_live(symbols, as_of_date)` API first, then wire the supervisor + IB preload builder onto it, with `spawn_today` explicitly threaded in `America/Chicago`. Registry miss fails fast — no IB fallback at live-start.

---

## The problem

### What's already done

- Schema: `instrument_definitions` + `instrument_aliases` (Alembic revision `v0q1r2s3t4u5`).
- `SecurityMaster.resolve_for_backtest()` honors alias windowing by `start` date (PR #33).
- `msai instruments refresh --provider interactive_brokers` CLI populates the registry via IB qualification (PR #35).

### What's broken

The **live-start path** never reads the registry. Every `/api/v1/live/start-portfolio` runs through:

1. HTTP handler at `backend/src/msai/api/live.py:243` — loads portfolio revision + members from DB, publishes `LiveCommandBus` command.
2. Supervisor at `backend/src/msai/live_supervisor/__main__.py:280-285` — calls `canonical_instrument_id(inst, today=spawn_today)` per member instrument.
3. Subprocess via `build_ib_instrument_provider_config()` at `backend/src/msai/services/nautilus/live_instrument_bootstrap.py:270-308` — rejects any symbol not in `PHASE_1_PAPER_SYMBOLS`.
4. `live_node_config.py:478-481` — feeds raw symbols into the same Phase-1-only builder.

Registry rows written by the CLI are **write-only** from the live path's perspective. Adding symbol #6 requires hand-editing the `canonical_instrument_id()` if-chain AND extending `PHASE_1_PAPER_SYMBOLS`.

---

## Council verdict (2026-04-19, xhigh chairman)

### Recommendation

Choose a **modified Option D**: first add a pure-read `lookup_for_live(symbols, as_of_date)` resolver, then make the supervisor spawn path call it with `spawn_today` in `America/Chicago`. That resolver must be:

- Registry-only (no IB qualifier call, no upserts)
- Returns canonical instrument IDs AND the preload contract/spec data the subprocess needs
- Fails fast on registry miss with an operator hint: "run `msai instruments refresh --symbols X` first"

`canonical_instrument_id()` may survive temporarily as bootstrap/CLI code, but it leaves the runtime path.

### Advisor tally

| Advisor          | Engine | Verdict                                | Preferred option                                        |
| ---------------- | ------ | -------------------------------------- | ------------------------------------------------------- |
| Simplifier       | Claude | OBJECT (defer) / CONDITIONAL minimal-D | Minimal D slice                                         |
| Scalability Hawk | Claude | CONDITIONAL C (with rigor) or defer    | C with timeout/telemetry/drill                          |
| Pragmatist       | Claude | APPROVE C                              | C with env-flag rollout                                 |
| Contrarian       | Codex  | **OBJECT** (wrong seam)                | New `lookup_for_live` API first                         |
| Maintainer       | Codex  | CONDITIONAL D                          | D with registry-only resolver, canonical out of runtime |

### Consensus points

- Option A is dead — no advisor supported IB qualification on the live-start critical path.
- Option B is rejected outright — silent fallback creates permanent dual truth and the worst observability.
- Live-start runtime should be registry-prewarmed and read-only; misses fail fast, not qualify via IB.
- Futures resolution must use an explicit spawn-scoped Chicago-local date, not the registry helper's implicit UTC default.
- Observability + operational proof are mandatory: structured resolution telemetry AND a live-money drill before merge.

### Blocking constraints (must be satisfied by the implementation PR)

1. **`canonical_instrument_id()` replacement is incomplete without IB preload coverage.** `build_ib_instrument_provider_config()` + `live_node_config.py` also gate on `PHASE_1_PAPER_SYMBOLS`. Both must be wired to the new resolver in the same PR (or the registry stays half-plugged).
2. **Current `SecurityMaster.resolve_for_live()` is not the runtime entrypoint.** It mixes registry reads + IB cold-miss qualification + registry upserts. The new `lookup_for_live()` must be a clean pure-read subset.
3. **Explicit `spawn_today` threaded in Chicago-local time.** Registry's `find_by_alias()` defaults to UTC (`backend/src/msai/services/nautilus/security_master/registry.py:47-60`). The resolver API must take `as_of_date: date` explicitly with Chicago-local semantics. Naive rewiring that inherits UTC default would regress roll-day behavior.
4. **No runtime canonical fallback. No `ib_cold` path.** Registry miss = explicit failure with operator hint. Keeping `canonical_instrument_id()` as a silent fallback (Option B) was unanimously rejected.
5. **Real-money drill before merge.** Equivalent to the 2026-04-16 U4705114 AAPL BUY/SELL drill — but exercising the new registry-backed path, not the canonical helper.
6. **Structured telemetry.** Log `live_instrument_resolved` with `{source: registry|registry_miss, symbol, canonical_id, as_of_date}`. Counter should be scrape-able for dashboards.

### Minority report

- **Contrarian (Codex) objected** that A/B/C/D attacked the wrong seam because the subprocess still preloads from `PHASE_1_PAPER_SYMBOLS`, and because a pure-read `lookup_for_live` API doesn't exist. **Sustained and adopted directly into the recommendation.**
- **Simplifier (Claude) objected** that there is no present outage and the work could be deferred entirely, preferring at most a minimal-D slice if forced. **Overruled on timing** (highest-value deferred registry integration; current half-hardcoded runtime blocks honest expansion). **Minimization principle adopted**: no IB coupling, no silent fallback, smallest registry-only slice.

### Missing evidence (to resolve during implementation)

- No end-to-end proof yet that registry rows alone can reconstruct all Phase-1 preload contracts, especially ES at a roll boundary. Needs spike.
- No evidence yet that API-side preflight materially improves operator outcomes once the preload path is also registry-backed. HTTP preflight deferred as optional advisory.
- No fresh live-money validation of the registry-backed path; the 2026-04-16 drill exercised the canonical helper, not the proposed resolver.

---

## What this means for the implementation

- **Implementation plan:** `docs/plans/2026-04-19-live-path-registry-wiring.md` (sibling doc).
- **Branch strategy:** `/new-feature live-path-wiring-registry` when ready to start.
- **Scope expansion note:** the council reframed "wire live-start onto registry" from ~50-LOC canonical-swap to a 3-surface change (resolver API + supervisor + IB preload builder + timezone + telemetry + drill). Estimate shifted from ~2 days to ~1-2 weeks.
- **Non-goals for this PR** (deferred to subsequent work):
  - HTTP preflight layer (Option C) — revisit after D is live and producing telemetry
  - Deleting `canonical_instrument_id()` — stays for bootstrap/CLI code paths initially; can be removed after one clean paper week
  - Registry management UI — deferred; operators still run `msai instruments refresh` manually
  - Automatic registry warming — deferred; registry remains operator-managed control plane per original PRD stance

---

## Follow-ups (not blocking this PR)

1. **Registry schema enhancement** — once real usage surfaces gaps: should effective-date windows support sub-daily granularity for sub-daily roll strategies? Open question.
2. **HTTP preflight (Option C)** as an advisory gate — "refuse `/start-portfolio` if ANY requested symbol is registry-missing, BEFORE publishing command." Nice UX, not required for correctness once the runtime path is registry-backed.
3. **Architecture-governance review (2026-10-19)** — per the PR #36 postscript; this registry wiring PR is part of the evidence base for that review.
