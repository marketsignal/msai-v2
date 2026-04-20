# PRD Discussion: Live-path wiring onto instrument registry

**Status:** In Progress
**Started:** 2026-04-19
**Participants:** User (Pablo), Claude
**Related:** `docs/decisions/live-path-registry-wiring.md` (5-advisor council verdict, ratified 2026-04-19)

## Discovery research (Phase 0 — minimal)

This is an internal architecture change inside MSAI's trading stack, not a product feature with competitor benchmarks or industry-standard UX patterns. Brief discovery:

- **Registry-as-source-of-truth for tradable instruments** is the standard pattern across production trading platforms (QuantConnect, Tradestation, Alpaca, NautilusTrader itself via `Cache.instrument(instrument_id)`) — the pattern isn't novel; the gap in MSAI is that we populated a registry but didn't wire the live-start consumer.
- **Hardcoded closed-universe instrument maps** are a known anti-pattern once a system exits single-asset-class MVP. The "extend the if-chain by hand" workflow is the exact gap `canonical_instrument_id()` creates today.
- **Effective-date windowing on aliases** (what `instrument_aliases.effective_from/effective_to` models) is standard for futures roll handling — Bloomberg BPIPE, Nautilus's own aliasing, and every security master I've seen uses this pattern.

**Implication:** the decision isn't what pattern to adopt — the registry schema already adopts the right one. The PRD's job is to capture which pieces of the live-start path consume the registry, and what operator-facing promises it makes.

---

## Original user stories (extracted from 2026-04-19 scope brief + council verdict)

The user hasn't given explicit Gherkin-style stories; I'm extracting from the scope brief and council doc so we can refine into proper form.

### Implicit stories in the scope brief

1. **Operator-onboards-a-new-symbol (end-to-end)** — "As an operator, I want to add a new tradable symbol (QQQ, GOOGL, NQ, GBP/USD, etc.) and have it become live-tradable without editing Python code or redeploying."

2. **Registry-as-single-source-of-truth-for-live** — "As an operator, I want the registry populated by `msai instruments refresh` to be the thing `/live/start-portfolio` actually consumes — no more write-only registry."

3. **Fail-fast-when-registry-cold** — "As an operator, if I forget to pre-warm a symbol and try to deploy it, I want a clear error telling me to run `msai instruments refresh --symbols X`, not a silent bypass to the old hardcoded chain, and not an IB Gateway round-trip that hangs the live-start request."

4. **Futures-roll-safety** — "As an operator, on a futures-roll-day, the symbol-resolution step and the subprocess must agree on the same front-month — no more midnight-CT races."

5. **Options-later-without-rearchitect** — "As an operator, when we add options trading in a future PR, the resolver contract shouldn't have to change — just the payload shape."

## Discussion log

### Round 1 — targeted questions

The goal of this round is to surface what's still ambiguous before we write the PRD.

---

**Q1 — scope of "any IB-qualifiable symbol":** You said "any equity, index ETF, forex pair, future." Is there a specific _universe boundary_ we should commit to in the PRD?

Examples:

- US equities only, or include ADRs / LSE / TSE listings?
- Futures: CME/CBOT/NYMEX/ICE — all of them? Or CME-only for Phase 1 and expand later?
- FX: all majors, or also crosses (EUR/GBP, EUR/JPY) and exotics?
- Include crypto? (IB has some via Paxos integration.)

The registry schema itself is universe-agnostic, but ingest/IB-qualifier coverage isn't. Stating the universe upfront keeps the follow-up "Symbol Onboarding UI" honest about what it can onboard.

---

**Q2 — error UX on registry miss:** The council said "fail fast with operator hint to run `msai instruments refresh --symbols X`." Where does that error show up?

- Just in the API response body (HTTP 4xx with structured error)?
- Also surfaced in the UI's portfolio-deploy dialog?
- Alert via the `/api/v1/alerts/` system?
- All three?

And: should the error include the _command to run_ (copy-paste ready), or just the fact?

---

**Q3 — acceptance criteria for "works":** The council mandated a U4705114-style real-money drill. What counts as "drill passes" concretely?

Strawman: "1-share BUY of a non-Phase-1 symbol (e.g., QQQ), held for <5 min, flattened via `/kill-all`, trade persisted to `trades` table, pnl calculated correctly." Is that enough, or do we need more?

---

**Q4 — rollout:** The council said no silent `canonical_instrument_id` fallback. Two sub-options:

- **Hard cutover:** on merge, `canonical_instrument_id` is removed from the live-start path entirely. Any deploy of a symbol not yet in the registry fails.
- **Soft cutover with explicit opt-in:** `canonical_instrument_id` stays for one release as an explicitly-enabled fallback behind env var `MSAI_LIVE_LEGACY_CANONICAL=1`. Flip to off after one clean paper week.

The council verdict implies hard cutover ("registry miss = explicit failure") but the Pragmatist advisor suggested a safety-net rollout. Which do you want?

---

**Q5 — observability:** The council specified `live_instrument_resolved` log event with `{source, symbol, canonical_id, as_of_date}`. Does the PRD also need:

- Prometheus/metrics counter (`msai_live_instrument_resolved_total{source="registry|miss"}`)?
- Dashboard panel in Grafana? (if we even have Grafana wired — I don't think we do yet)
- Alert on `registry_miss` rate > N/hour?

Or are we good with just the structured log for now?

---

**Q6 — ingestion-coverage audit:** You want onboarding for any asset class. Current `msai ingest` supports:

- `stocks` → Polygon or Databento
- `equities` → same
- `futures` → Databento

Likely gaps:

- **FX pairs** — is Polygon/Databento wired for FX? I don't think so.
- **Options** — out of scope for this PR per your direction.

Should the PRD include "audit `msai ingest` coverage for all 4 asset classes and note gaps" as an IN-scope deliverable? Or defer that to the follow-up Symbol Onboarding feature (so this PR stays laser-focused on live-start)?

---

**Q7 — paper vs live rollout order:** Post-merge, do you want:

- **Paper-first** — ship, let it soak on paper for a week, then run the real-money drill
- **Drill-first** — council-mandated real-money drill BEFORE merge; paper soak is table stakes during TDD

The council said "before merge" which implies drill-first. Confirm?

---

**Q8 — degraded-case behavior:** If the registry row exists but has corrupt/incomplete data (e.g., missing `listing_venue` field), what should `lookup_for_live` do?

- Hard fail the deploy
- Log warning + fall through to IB qualifier (violates "no IB at live-start" rule)
- Log warning + use partial data optimistically
- Something else

---

**Q9 — concurrent-deploy semantics:** Two `/start-portfolio` calls arrive simultaneously for different portfolio revisions, both referencing QQQ. The registry has QQQ. What's the expected behavior?

Strawman: both succeed concurrently, since the lookup is pure-read — no race, no serialization needed. Confirm?

---

**Q10 — "done" definition:** What makes you say "this PR is done, merge it"?

Candidates (pick which apply):

- All unit + integration tests pass
- Paper drill on ≥3 non-Phase-1 symbols across ≥2 asset classes (e.g., QQQ + GBP/USD + NQ) succeeds
- Real-money drill on U4705114 with a non-Phase-1 symbol succeeds
- Post-merge, `canonical_instrument_id` can be grep'd from the live-path-runtime code and returns zero hits
- Telemetry shows `source=registry` ≥99% of resolutions in the first 7 days
- Symbol Onboarding PR (#3b follow-up) has its design doc started

---

When you've answered (or said "don't care, proceed with your defaults"), I'll update this discussion file and we move to `/prd:create`.

### Round 1 answers (user chose option 2 — "use my defaults, proceed"; 2026-04-19)

User directive: proceed with defaults. Below are the defaults I applied. Any can be contested during plan-review.

**A1 — universe boundary:** Punt to "whatever IB + the registry already represent." Concretely: US + international equities + ETFs IB supports, CME/CBOT/NYMEX/ICE futures (whatever IB routes), FX majors + non-exotic crosses. OUT: crypto (IB's Paxos path is separate integration work), options (deferred per scope brief). Rationale: the registry schema is universe-agnostic; the real boundary is IB-qualifier coverage, not something we gate in MSAI.

**A2 — error UX on registry miss:** API body + alerting service. Defer UI changes to the Symbol Onboarding follow-up PR.

- API: HTTP 422 (Unprocessable Entity), body shape `{error: {code: "REGISTRY_MISS", message: "Symbol(s) not in registry: ['QQQ']. Run: msai instruments refresh --symbols QQQ --provider interactive_brokers", details: {missing_symbols: [...]}, request_id: ...}}`
- Alert: WARN-level via existing `alerting_service` — "Registry miss on deploy" with symbol list. Not ERROR (operator can self-correct).
- UI: no changes in this PR (the portfolio-deploy dialog will surface the API error text via its existing error rendering).

**A3 — drill acceptance criteria (concrete):**

1. Symbol pre-warmed via `msai instruments refresh --symbols X --provider interactive_brokers` (X = non-Phase-1 symbol, default QQQ for ETF test)
2. Deploy via `/api/v1/live/start-portfolio` on IB paper DUP733211 for initial verification, then U4705114 for real-money drill
3. Order fills route via IB Gateway to the target account
4. Bar events fire within 60s of deploy (visible in supervisor logs + `/api/v1/live/stream/{id}`)
5. `/kill-all` flattens the position within 5s
6. `trades` table gets rows with correct `side=BUY|SELL` strings, `pnl` populated from PositionClosed, `broker_trade_id` populated
7. Telemetry `live_instrument_resolved{source="registry"}` emitted for the deploy
8. Total drill cost <$5 (slippage + commissions on a 1-share trade)

**A4 — cutover strategy:** HARD cutover per council verdict. `canonical_instrument_id()` stays in CLI/bootstrap code (still used by `msai instruments refresh` for initial seeding) but is REMOVED from `live_supervisor/__main__.py` and `live_instrument_bootstrap.build_ib_instrument_provider_config()` runtime paths. Post-merge grep of those two files for `canonical_instrument_id(` must return zero hits. Reversal if drill fails = one revert commit.

**A5 — observability:** Structured log + Prometheus counter. No Grafana/alert work.

- `log.info("live_instrument_resolved", extra={"source": "registry", "symbol": "QQQ", "canonical_id": "QQQ.NASDAQ", "as_of_date": "2026-04-19"})` at each resolution
- Counter `msai_live_instrument_resolved_total{source, asset_class}` via existing `msai.services.observability.trading_metrics` module (assumes it exists; verify during plan phase)
- Dashboard + alerts: deferred to CI-hardening or ops follow-up PR

**A6 — ingestion-coverage audit:** DEFER to Symbol Onboarding follow-up PR (#3b). This PR stays focused on live-start. PRD must document the assumption: "This PR assumes operators run `msai ingest` to populate historical data before deploying. Coverage gaps per asset class are Symbol Onboarding's problem, not this one."

**A7 — drill before merge:** Confirmed per council — drill BEFORE merge, not after.

**A8 — corrupt/partial registry row:** Hard fail. `lookup_for_live` raises `InstrumentDefinitionIncompleteError` with a structured message (symbol + missing field). Supervisor handler surfaces it as the same 422 "REGISTRY_MISS"-family error, with a distinct code `REGISTRY_INCOMPLETE`. No log-and-continue; no IB fallback.

**A9 — concurrent deploys:** Pure-read, no serialization. Both succeed independently. No DB locks, no semaphores. Standard async SQLAlchemy session-per-operation is enough.

**A10 — done definition (5 items, all must pass):**

1. Unit + integration tests green (including new test fixtures for registry miss + corrupt row cases)
2. Paper drill on ≥2 non-Phase-1 symbols across ≥2 asset classes succeeds (default: QQQ + one FX pair, but FX may be gated on ingestion availability — fall back to 2 equities if FX-ingest is gap-blocked)
3. Real-money drill on U4705114 with ≥1 non-Phase-1 symbol (default: QQQ) passes per A3's 8-point checklist
4. Post-merge `grep "canonical_instrument_id(" backend/src/msai/live_supervisor backend/src/msai/services/nautilus/live_instrument_bootstrap.py` returns zero hits outside the function definition itself and CLI seeding code paths
5. Paper drill logs show `live_instrument_resolved{source="registry"}` for every deploy

## Refined Understanding

### Personas

- **Operator (sole persona for this PR)** — runs MSAI as manager of their personal hedge fund. Comfortable with CLI (uses `msai` sub-apps daily). Interacts with the backend via HTTP API, UI, and CLI. No other personas for this PR (no analysts, no devs-at-rest, no external API consumers).

### User Stories (Refined)

- **US-001: Deploy any IB-qualifiable symbol without code edits.** As an operator, when I've pre-warmed the registry via `msai instruments refresh --symbols X --provider interactive_brokers`, I can deploy X via `/api/v1/live/start-portfolio` with no code changes. The live supervisor resolves X through the registry, the subprocess preloads X's contract spec from the registry, and IB Gateway subscribes/trades X successfully.
- **US-002: Fail fast on registry miss with a copy-pastable fix.** As an operator, if I try to deploy Y and Y is not in the registry, I get an HTTP 422 with code `REGISTRY_MISS` and a message containing the exact `msai instruments refresh` command to run. No silent fallback, no IB Gateway round-trip.
- **US-003: Futures-roll safety at spawn.** As an operator, on a futures-roll-day, the registry alias window evaluation and the subprocess's contract preload agree on the same front-month (e.g., ESM6 vs ESU6). `spawn_today` is passed explicitly in `America/Chicago`, not inferred from UTC.
- **US-004: Options-ready resolver contract.** The `lookup_for_live` API accepts a generic instrument descriptor — when options trading ships later, the contract doesn't change; only a new variant of the payload emerges for option specs (expiry + strike + call/put). No runtime contract break expected.
- **US-005: Observability for resolution source.** Every live-start resolution emits `live_instrument_resolved{source=registry|miss, symbol, canonical_id, as_of_date}` to structured logs + Prometheus counter. Operators can confirm the path is registry-backed by checking logs post-deploy.

### Non-Goals (explicit)

- Options trading (deferred — separate PRD + council required)
- HTTP preflight layer (Option C from council; revisit after D is live)
- UI for registry management (operators still use `msai instruments refresh` CLI)
- Automatic registry warming (registry remains operator-managed control plane)
- Ingestion-coverage audit across all 4 asset classes (deferred to Symbol Onboarding follow-up #3b)
- Dashboards / Grafana / alert wiring (deferred to CI-hardening or ops follow-up)
- Deleting `canonical_instrument_id()` from CLI/bootstrap seeding paths (only removed from the live-start RUNTIME path)

### Key Decisions

- **Hard cutover** per council, not soft opt-in — no env-flag fallback
- **`lookup_for_live(symbols, as_of_date: date) -> list[ResolvedInstrument]`** as the new API signature (pure-read, DB-only, no IB)
- **`spawn_today` in America/Chicago** passed explicitly from supervisor → `lookup_for_live` → NOT inherited from UTC defaults
- **3 call sites converge on the same resolver**: `live_supervisor/__main__.py`, `build_ib_instrument_provider_config()`, `live_node_config.py:478`
- **Registry miss = 422 HTTP with structured error + WARN alert**, not 500, not 503 (the operator can fix it)
- **Real-money drill with QQQ before merge** — cost cap $5, U4705114 account

### Open Questions (remaining, to resolve during plan-review)

- [ ] Does `msai.services.observability.trading_metrics` already exist, or do we need to create it? (Plan-review spot-check.)
- [ ] What `asset_class` labels do we use for the Prometheus counter? Match `instrument_definitions.asset_class` column values — verify during plan-review.
- [ ] Does the existing `alerting_service` accept WARN level + structured details, or do we need extension? (Plan-review spot-check.)
- [ ] Is there an FX pair currently supported by `msai ingest`? If no, drill falls back to 2 equities (QQQ + GOOGL).

**Status:** Complete — ready for `/prd:create live-path-wiring-registry`.
