# Council Decision Context — Live-path wiring onto instrument registry

## The decision

**How should `/api/v1/live/start-portfolio` + the live supervisor start reading from the `instrument_definitions` + `instrument_aliases` tables, instead of recomputing instrument IDs via the closed-universe helper `canonical_instrument_id()`?**

This is the PR #32 scope-out that's been deferred since 2026-04-17. CONTINUITY lists it as the highest strategic value of the remaining deferred items.

## What's already done (don't re-litigate)

- Registry schema exists: `instrument_definitions` + `instrument_aliases` (Alembic revision `v0q1r2s3t4u5`)
- `SecurityMaster.resolve_for_backtest()` honors `start` kwarg for alias windowing (PR #33)
- `msai instruments refresh --provider interactive_brokers` CLI populates the registry via IB qualification — shipped in PR #35
- `SecurityMaster.resolve_for_live()` exists at `backend/src/msai/services/nautilus/security_master/service.py:214` — only caller today is the CLI at `backend/src/msai/cli.py:1027`

## Current live-start flow (what's wrong)

1. HTTP: `POST /api/v1/live/start-portfolio` (`backend/src/msai/api/live.py:243`) — loads the frozen `LivePortfolioRevision` + `LivePortfolioRevisionStrategy` members from the DB, then publishes a command to `LiveCommandBus`.
2. Supervisor (`backend/src/msai/live_supervisor/__main__.py`) consumes the command. For each member, it calls:
   ```python
   member_canonical = [
       canonical_instrument_id(inst, today=spawn_today)
       for inst in member.instruments
   ]
   ```
3. `canonical_instrument_id()` lives at `backend/src/msai/services/nautilus/live_instrument_bootstrap.py:142` — it's a **hardcoded if/elif chain** (AAPL → `AAPL.NASDAQ`, MSFT → `MSFT.NASDAQ`, SPY → `SPY.ARCA`, ES → `ESM6.CME` with a futures-roll calculation, EUR/USD → `EUR/USD.IDEALPRO`).
4. `live_instrument_bootstrap.py` has **zero** references to `instrument_definitions`, `instrument_aliases`, `AsyncSession`, or `select(...)` — it never reads the DB.
5. Canonical IDs get passed to the trading subprocess which hands them to Nautilus for subscription.

**Operational impact:** registry rows written by `msai instruments refresh` are **write-only** from the live path's perspective. New symbols can be added to the registry but `/live/start-portfolio` won't recognize them until `canonical_instrument_id()`'s if-chain is extended by hand.

## Strawmen

### A. Rip out `canonical_instrument_id()` entirely

- Supervisor (or subprocess) calls `SecurityMaster.resolve_for_live(member.instruments)` directly
- Registry is the only source of truth
- Cold-miss path: `resolve_for_live` qualifies the symbol via IB on-the-fly and writes new rows
- Pros: single path, no drift
- Cons: adds IB network dependency to live-start critical path; couples supervisor to SecurityMaster + its IB qualifier chain; if IB is slow/down the whole live-start blocks

### B. Registry-first, `canonical_instrument_id()` fallback on miss

- Try `SecurityMaster.resolve_for_live()` first (read-only, no IB)
- On registry miss, fall back to `canonical_instrument_id(inst, today=spawn_today)`
- Pros: incremental migration; no IB dependency change
- Cons: two code paths to reason about forever; the fallback silently bypasses registry for misconfigured or newly-added symbols (defeats the point)

### C. Preflight in the `/live/start-portfolio` HTTP handler

- The FastAPI handler (which already has a DB session) calls `SecurityMaster.resolve_for_live()` BEFORE publishing the command
- Resolved canonical IDs travel in the command payload to the supervisor
- Supervisor stays sync/DB-free — it just forwards the payload to the subprocess
- Pros: DB reads happen where they already happen; supervisor complexity unchanged; concurrent /start-portfolio calls serialize at the DB transaction level (FastAPI dependency injection already provides `AsyncSession` with SAVEPOINT semantics)
- Cons: canonical IDs in the payload can't be re-canonicalized if a futures-roll boundary crosses between API-side preflight and supervisor spawn (narrow window; operator-recoverable); IB qualifier cold-path needs to happen somewhere — either in the handler (blocks HTTP) or delegated back to CLI refresh

### D. Read registry inside the supervisor spawn with a short-lived DB session

- Supervisor opens its own `AsyncSession`, reads rows matching `member.instruments` from `instrument_definitions` + `instrument_aliases`
- NO IB qualifier at spawn time — if registry miss, fail fast with an operator hint: "run `msai instruments refresh --symbols X` first"
- Pros: explicit DB read; no IB coupling; strict registry discipline
- Cons: adds DB session to supervisor code path; requires operator to pre-warm registry before deploying new symbols (intentional — matches the PRD's "registry is an operator-managed control plane")

## Key tensions the advisors should weigh in on

1. **Where does the read boundary live?** API handler (option C) vs supervisor (options A / D) vs subprocess (option A variant)?
2. **Does live-start need IB coupling?** Option A says yes (convenience), options C/D say no (registry must be pre-warmed by CLI)
3. **Fallback discipline.** Option B keeps `canonical_instrument_id()` alive as a safety net — does that help (incremental migration) or hurt (silent registry bypass for misconfigured instruments)?
4. **Futures-roll `today` parameter.** Current code uses `spawn_today` (computed in supervisor at spawn time, passed through `TradingNodePayload.spawn_today_iso` — see `nautilus.md` gotcha for midnight-CT-on-roll-day race). Registry alias windows also have dates. How do we reconcile? Does the preflight approach (C) introduce a new gap between "resolve time" and "spawn time" `today`?
5. **Concurrent `/start-portfolio` serialization.** Two simultaneous starts on different portfolio revisions — do they race on the registry? The CLI has semaphore protection for the refresh path; the live-start consumer path is currently race-free because `canonical_instrument_id()` is pure. Any registry-reading approach must either stay pure (read-only) or add serialization.
6. **The `msai instruments refresh` CLI is pull-based.** The registry is populated manually today. Is that the intended durable model (C or D's "operator pre-warm" discipline), or is the plan to move to automatic registry warming (which pushes us toward A)?
7. **Cold-start scenarios.** First deploy of a brand-new symbol after `msai instruments refresh --symbols NEW_SYM`. Does option C allow this? Option D?

## Your job

Pick the approach (A/B/C/D or a hybrid/alternative you propose) and state why. Use the output schema below. Spot-check the code referenced above at least once before forming your verdict — don't rubber-stamp.

## Output schema

```
## [Your Advisor Name]

### Position
[One sentence]

### Analysis
[2-5 bullets with file:line evidence from your spot-check]

### Blocking Objections
[or "None"]

### Risks Accepted
[trade-offs]

### Verdict
APPROVE | OBJECT | CONDITIONAL
```
