# Decision: Which MSAI v2 version to keep

**Date:** 2026-04-19
**Status:** **FINAL — council-ratified keep-claude / kill-codex**
**Current heads:** claude-version @ `c6b42bb` · codex-version (baseline, older)
**PRD:** [`docs/plans/2026-02-25-msai-v2-design.md`](../plans/2026-02-25-msai-v2-design.md)

---

## TL;DR

Keep `claude-version`. Kill `codex-version` — **no residuals ported** (see [Postscript](#postscript-2026-04-19--option-c-adopted-no-port)). Deferred architecture-governance review: revisit the carrying cost of claude's multi-login gateway + supervisor complexity in 6 months with runtime evidence.

**Original plan:** port 2 residuals (Playwright e2e + CLI sub-apps) before deletion. **Superseded 2026-04-19** after plan review surfaced material UI drift between the two versions. See postscript for details.

---

## Rubric (locked before exploration)

Six axes. Operational maturity weighted 2x because real money is involved (validated 2026-04-16 on IB account U4705114: AAPL BUY @ $261.33 → SELL flatten @ $262.46 via `/kill-all`).

| Axis                    | Weight |
| ----------------------- | ------ |
| 1. PRD coverage         | 1x     |
| 2. Operational maturity | **2x** |
| 3. Backend code quality | 1x     |
| 4. Frontend UI quality  | 1x     |
| 5. Efficiency           | 1x     |
| 6. Migration risk       | 1x     |

---

## Executive scorecard (council-corrected)

| Axis                    | Claude                                            | Codex                | Evidence                                                                                                                                                                                                                                                                 |
| ----------------------- | ------------------------------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1. PRD coverage         | **Parity** (31/39 core) + claude-integration wins | 31/39 core           | Both implement §2/§6/§7/§8 baseline and both have `PortfolioService` / `RevisionService` / deployment-identity tables. Claude's implementations are more integrated with the supervisor/command-bus and have shipped E2E validation via the 2026-04-16 real-money drill. |
| 2. Operational maturity | **Winner**                                        | behind               | live_supervisor subprocess pattern; 4-layer kill-all; explicit reconciliation; OrderFilled→trades pipeline; `restart-workers.sh`; heartbeat_monitor. Codex has none of these.                                                                                            |
| 3. Backend code quality | **Winner (with caveat)**                          | behind               | 32,520 vs 17,055 LOC; 536 vs 175 tests; mypy strict (yes vs no); 23 vs 9 migrations. Caveat: claude's `process_manager.py` carries comment-heavy decision-archaeology that a future reader must triage.                                                                  |
| 4. Frontend UI quality  | **Winner**                                        | behind (but has e2e) | 15 vs 0 shadcn primitives; 26 typed Response types vs raw fetch; 34 vs 7 CSS design tokens; 37 vs 20 components. Codex's ONE UI axis lead is 9 Playwright specs (2,021 LOC) — fully portable.                                                                            |
| 5. Efficiency           | **Winner (marginal)**                             | behind               | Dev: 10.5 CPU / 13 GB caps vs unbounded; prod: 15.5 CPU / 23 GB vs unbounded; healthchecks 100% vs 89%.                                                                                                                                                                  |
| 6. Migration risk       | **Lower**                                         | higher               | Killing codex strands 2 portable items (+1 skipped). Killing claude strands PRs #14-31 architecture — weeks of rewrite.                                                                                                                                                  |

---

## Detailed findings by axis

### Axis 1 — PRD coverage (both at parity; claude's implementations are more integrated)

Both versions implement the core PRD (§2 Scope, §6 Strategy System, §7 Frontend, §8 Backend API, §9 Schema).

**Both have:** `PortfolioService`, `RevisionService`, `deployment_identity` + `LiveDeployment.portfolio_revision_id` + `UniqueConstraint(portfolio_revision_id, account_id)`. The council-confirmed correction: these are NOT claude-only (see `codex-version/backend/src/msai/services/live/portfolio_service.py:20-97`, `codex-version/backend/src/msai/models/live_deployment.py:18-20`).

**Claude's integration-depth wins** (still real; different framing):

- Partial unique index on `(deployment_id, broker_trade_id) WHERE broker_trade_id IS NOT NULL` — dedup on Nautilus reconnection reconciliation (`backend/src/msai/models/trade.py:53-58` post-flatten)
- Data lineage on `Backtest` (nautilus_version, python_version, data_snapshot) for reproducibility (§14 NFR)
- `instrument_definitions` + `instrument_aliases` registry with alias-windowing by date and IB qualifier routing (PR #32 + #35) — claude-only
- `msai instruments refresh --provider interactive_brokers` CLI (PR #35) — claude-only
- `ib_login_key` + `gateway_session_key` on LiveDeployment / LiveNodeProcess (PR #30 multi-login gateway) — claude-only; **flagged by Contrarian as beyond PRD scope** (PRD §3 specifies a single `ib-gateway` container)

**Codex-only (residual after cross-port PRs #3-11):**

- 9 Playwright specs in `codex-version/frontend/e2e/` (2,021 LOC) — **confirmed 0 in claude**
- `LiveStateController(Controller)` Nautilus subclass at `codex-version/backend/src/msai/services/nautilus/live_state.py:58` — 5s snapshot publishing (957 LOC in one file)
- 36-command CLI in 7 sub-apps vs claude's flatter layout
- Dedicated `daily-scheduler` compose service vs claude's arq-cron-in-ingest-worker

**Both miss (PRD-scope gaps):**

- Live-path wiring onto instrument registry (CONTINUITY Next #1)
- `instrument_cache` → registry migration (CONTINUITY Next #2)
- Strategy config-schema extraction for UI form generation (CONTINUITY Next #3)
- Broker-side OCO/stop orders (§12)
- SMS alerting (§12)

### Axis 2 — Operational maturity (2x weight)

| Capability                            | Claude                                                                                                | Codex                                                          | Winner |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- | ------ |
| Live supervisor subprocess            | `live_supervisor/` dir, 4 modules (1,799 LOC)                                                         | No separate supervisor                                         | Claude |
| Kill-all defense                      | 4-layer: Redis halt flag 24h TTL + supervisor re-check + LiveCommandBus push + SIGTERM+flatten        | 2-step: `risk_engine.kill_all()` + `runtime_client.kill_all()` | Claude |
| Reconciliation on startup             | Explicit `LiveExecEngineConfig(reconciliation=True, reconciliation_lookback_mins=1440)`               | No explicit pre-start reconciliation                           | Claude |
| Trade dedup on IB reconnect           | Partial unique index on `(deployment_id, broker_trade_id)` + `audit_hook.write_trade_fill()` pipeline | Full unique constraint; DB persistence path opaque             | Claude |
| OrderFilled→trades pipeline           | `services/nautilus/audit_hook.py` on every msgbus event                                               | `live_state.py` extracts for WebSocket; persistence unclear    | Claude |
| Risk engine pre-live validation       | Singleton at `api/live.py:75`                                                                         | Same pattern at `api/live.py:45`                               | Tie    |
| WebSocket JWT first-message auth      | 5s timeout, receive_text, validate                                                                    | Identical                                                      | Tie    |
| Stale-import hygiene                  | `scripts/restart-workers.sh` (10s restart)                                                            | Requires full Docker rebuild                                   | Claude |
| Heartbeat / stale detection           | `heartbeat_monitor.py` + typed `HEARTBEAT_TIMEOUT`                                                    | No live-specific monitor                                       | Claude |
| Portfolio-per-account **integration** | LivePortfolio→Revision→Deployment wired through supervisor + command-bus; shipped real-money drill    | Schema + services present; not integrated end-to-end           | Claude |

### Axis 3 — Backend code quality

| Metric               | Claude | Codex  |
| -------------------- | ------ | ------ |
| Backend src LOC      | 32,520 | 17,055 |
| Test files           | 146    | 49     |
| Test functions       | 536    | 175    |
| mypy `strict = true` | yes    | no     |
| Ruff ignores         | 0      | 7      |
| Alembic revisions    | 23     | 9      |

Claude's codebase is ~1.9x larger and ~3x more tested, reflecting hardening shipped in PRs #14-31. Maintainer advisor confirms claude's module boundaries (thin entrypoint → supervisor loop → process manager → heartbeat monitor) tell a clearer story than codex's 957-line `live_state.py` monolith. Test names are also more scenario-specific in claude (`test_aapl_at_10am_eastern_is_in_rth`) vs codex's compressed names (`test_live_portfolio_crud_and_snapshot`).

### Axis 4 — Frontend UI quality

| Metric                  | Claude                   | Codex             |
| ----------------------- | ------------------------ | ----------------- |
| Frontend LOC (src only) | 9,191                    | 6,340             |
| Pages                   | 15                       | 14                |
| Component files         | 37                       | 20                |
| shadcn/ui primitives    | 15                       | 0                 |
| Typed API client        | 26 Response interfaces   | Raw fetch         |
| CSS design tokens       | 34 (dark-mode via oklch) | 7 (hand-rolled)   |
| WebSocket hook usages   | 1                        | 1                 |
| data-testid coverage    | 0                        | 0                 |
| **e2e specs**           | **0**                    | **9 (2,021 LOC)** |

Claude's UI is structurally more mature (shadcn + typed API + oklch design tokens). Codex's ONLY UI lead is the Playwright suite — fully portable within days.

### Axis 5 — Efficiency

| Dimension            | Claude             | Codex              |
| -------------------- | ------------------ | ------------------ |
| Dev containers       | 8 + 2 broker-gated | 9 + 1 broker-gated |
| Prod containers      | 7 + 1 broker-gated | 7 + 1 broker-gated |
| Total CPU cap (dev)  | 10.5               | unbounded          |
| Total mem cap (dev)  | 13 GB              | unbounded          |
| Total CPU cap (prod) | 15.5               | unbounded          |
| Total mem cap (prod) | 23 GB              | unbounded          |
| Healthchecks         | 8/8 (100%)         | 8/9 (89%)          |

### Axis 6 — Migration risk (decisive)

**Killing Codex:** 2 real ports + 1 optional skip:

1. Playwright e2e suite — copy, retarget baseURL, wire auth fixture (1-2 days)
2. CLI sub-app taxonomy — diff and merge (1 day)
3. `daily-scheduler` container — optional (SKIP unless ops prefers separate container)

**Killing Claude:** weeks of rewrite + re-exposes real money to untested paths:

1. Portfolio-per-account supervisor integration (PR #29-31 wiring)
2. Instrument registry + alias windowing + IB qualifier path (PR #32 + #35)
3. Live-stack hardening: supervisor + command bus + heartbeat + reconciliation + 4-layer kill-all (PRs #12-27)
4. Trade dedup + OrderFilled→trades pipeline + data lineage (PRs #15, #19, #21)
5. 536 tests of regression coverage

**Asymmetry is decisive.**

---

## Council verdict (2026-04-19, xhigh chairman synthesis)

### Recommendation

Keep `claude-version`; kill `codex-version` after porting only the portable residuals. Operational maturity deserves the 2x weight because live money has already moved through Claude's stack, and its supervisor/heartbeat/command-bus/reconciliation/kill-all layers are guarding real failure modes, not decorative complexity. `LiveStateController` should not be ported; Claude's DB-hydrated reconnect is sufficient and simpler. Migration asymmetry is decisive: Codex's remaining wins are days to port, while recreating Claude's live hardening in Codex is a multi-week rewrite with fresh production risk.

### Decision on the 3 original council questions

1. **Is ops-maturity 2x weight justified?** — **Yes.** Real money has moved through claude's stack. The 2026-04-16 U4705114 drill is concrete evidence the hardening is load-bearing, not ceremony.
2. **Should LiveStateController be ported?** — **No. DEFERRED / not-to-port.** Claude's DB-hydrated reconnect (PR #24) solves the same problem simpler, and the controller is 957 LOC in a single file.
3. **Is migration asymmetry decisive?** — **Yes.** Killing codex = 2 portable items. Killing claude = weeks of rewrite re-exposing real money.

### Consensus points

- Claude is the only implementation with real-money validation and the only shippable stack today.
- Claude's operational primitives are load-bearing for a live trading system, even if they add code and process surface area.
- Codex's Playwright suite is a real asset and portable within days.
- The scorecard had a factual error (portfolio-per-account claimed as claude-only). **This doc's Axis 1 row now reads correctly.**

### Minority report (Contrarian / Codex — OBJECT)

The Contrarian objected on substantive grounds and the dissent is preserved here:

> **Who:** The Contrarian (engine: Codex, persona: devil's advocate)
> **What they said:**
>
> 1. The PRD specifies a single `ib-gateway` container and a backend-spawned trading subprocess (`docs/plans/2026-02-25-msai-v2-design.md:95-106`, `:333-351`). Claude introduced multi-login routing, `ib_login_key → host:port` static router, persisted `gateway_session_key` per subprocess — **topology expansion beyond stated PRD scope**.
> 2. Claude's supervisor complexity is **partly self-inflicted**: startup requires a dedicated watchdog because heartbeat begins before `node.build()`; liveness authority is split between watchdog and HeartbeatMonitor; projection adds another Redis-Streams/PEL/DLQ subsystem.
> 3. Claude's instrument registry is a full security-master lifecycle; the PRD only explicitly promises `/market-data/symbols`. Could be valuable later; today looks like scope creep.
> 4. Non-trivial pieces of what the scorecard claimed as "claude-only deep wins" are also present in codex (confirmed).
>
> **Why overruled on keep/kill:** The deciding evidence is operational, not stylistic. Claude is the only stack with verified real-money drills and mature failure-path controls, and migration asymmetry still favors keeping it.
>
> **Why NOT dismissed:** Carrying cost should be revisited after the scorecard is corrected (done in this rev) and more runtime evidence is gathered. An **architecture-governance review in 6 months** is added below as a follow-up.

### Blocking objections (noted, not overruled)

- **Resolved in this rev:** the false "portfolio-per-account = claude-only" claim has been corrected in Axis 1.
- **Deferred (not overruled):** the PRD-scope objection around multi-login gateway + instrument-registry. The council did not fully quantify the 6-month carrying cost of claude's extra topology vs selectively porting claude hardening into codex. Tracked as "architecture-governance review" below.

### Missing evidence

- No apples-to-apples 6-month maintenance-cost comparison between claude's current topology and a codex-plus-ported-hardening path.
- No production-like incident or load data for codex, because it is not the operating stack.
- No quantified UX comparison between codex's 5-second push snapshots and claude's reconnect hydration beyond advisor judgment.

---

## Port list (superseded 2026-04-19 by option C — see postscript)

~~All 4 items are dropped.~~ Codex-version is deleted directly. No residuals ported. See [Postscript 2026-04-19 — option C adopted](#postscript-2026-04-19--option-c-adopted-no-port).

Original table (kept for audit trail):

| #   | Item                                      | Original intent                                                         | Final disposition                                |
| --- | ----------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------ |
| 1   | Playwright e2e suite (9 specs, 2,021 LOC) | Port specs; retarget baseURL; align selectors; add data-testids         | **DROPPED** — UI drift too large; see postscript |
| 2   | CLI sub-app taxonomy                      | Diff + port missing sub-apps                                            | **DROPPED** — not investigated; out of scope     |
| 3   | `daily-scheduler` container               | Compose delta from codex-version                                        | **SKIP** (as originally ratified — optional)     |
| 4   | `LiveStateController`                     | Claude's DB-hydrated reconnect (PR #24) solves the same problem simpler | **SKIP** (as originally ratified — council SKIP) |

---

## Archival plan (this PR — option C)

Executing now on branch `feat/playwright-e2e-port` (branch name is historical — it carries the option-C deletion, not any port work):

1. Tag the pre-delete commit as `codex-final` before merge (preserves history; revival via `git checkout codex-final` if anything is missed)
2. `git rm -r codex-version/` — removes ~17K LOC of the codex implementation
3. Remove `codex-version` rows from root `CLAUDE.md` "Two Competing Implementations" table, "Running Both Versions Side-by-Side" section, file-structure diagram, key-commands, and E2E Configuration matrix — reflect single-stack operation
4. Drop ports `3400/8400/5434/6381` from any docs that still reference them
5. Update `playwright.config.ts` default `baseURL` from `http://localhost:3000` to `http://localhost:3300`
6. Tidy up 15 Feb-25 baseline screenshot PNGs at the repo root (`claude-*.png` + `codex-*.png`) — unreferenced, 2.4 MB
7. Open PR to main

## Follow-ups (not blocking)

- **Architecture-governance review (2026-10-19, 6-month cadence)** — revisit: (a) does claude's multi-login gateway fabric earn its complexity against actual multi-account operational load? (b) is the instrument registry + alias windowing justified by live-path usage (CONTINUITY Next #1) or still scope creep? Decision author: whoever is operating the stack then.
- **Playwright regression coverage** — none shipped. Future feature work that changes user-facing behavior should author claude-native specs in `tests/e2e/specs/` using `getByTestId` — don't re-litigate the codex port.

---

## Postscript 2026-04-19 — option C adopted (no port)

### What happened

Started the Playwright port (`/new-feature playwright-e2e-port`) on branch `feat/playwright-e2e-port`. Plan-review loop iteration 1 (Claude + Codex in parallel) converged on **NEEDS_FIX** with 5 shared P1 findings. The core issue: the two versions diverged more on UI than the scorecard captured.

### Discoveries from plan review

1. **Copy drift is severe.** Of 15 critical codex copy strings that the specs assert on (`"Backtest Runner"`, `"Research Console"`, `"Strategy Registry"`, `"Interactive Brokers status"`, `"Daily Universe"`, `"Launch Research Jobs"`, `"Walk-Forward Windows"`, `"Model portfolio allocation"`, `"Turn research winners…"`, `"Broker Truth"`, `"Deploy Strategy"`, etc.), **only 1** (`"Active Strategies"`) exists in claude's frontend. Claude's pages have minimalist single-word headings (`"Backtests"`, `"Research"`, `"Portfolios"`) where codex has descriptive phrases.

2. **Some claude pages lack headings altogether.** `data-management`, `graduation`, and `live-trading` have no `<h1>` in their top-level `page.tsx`.

3. **Features behind codex specs don't exist in claude.** `strategies-authoring.spec.ts` tests a scaffold form ("Module Name", "Create Strategy" button). Claude's `/strategies` page is a registry/list view only — no scaffold UI. Same pattern on research-console (Walk-Forward Windows, Launch Research Jobs), live-paper (Interactive Brokers status, Broker Truth, Deploy Strategy), data-refresh (Daily Universe).

4. **Route delta broader than expected.** Beyond `/live` → `/live-trading`, also `/data` → `/data-management`.

5. **Mock payload shape incompatibility.** Claude's API returns wrapped responses (`StrategyListResponse.items`, `BacktestHistoryResponse.items`, `LiveStatusResponse.deployments`). Codex specs' `page.route()` mocks return bare arrays. Every mock in every spec needs surgery.

6. **Auth refactor scope was bigger than planned.** Codex flagged `frontend/src/components/layout/app-shell.tsx:16-19` (post-flatten path) also has MSAL coupling via `useIsAuthenticated()` with its own dev bypass. Plan's `providers.tsx`-only refactor was insufficient.

### Three options considered

| Option                             | Action                                                                                                                         | Effort      | Trade-off                                                                          |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ----------- | ---------------------------------------------------------------------------------- |
| A. Scope down                      | Port 2-4 specs whose UI substantially exists; defer the rest                                                                   | 2-3 days    | Reduced regression coverage; un-portable specs documented as follow-ups            |
| B. Scope up                        | Build missing UI in claude (scaffold form, Research Console sections, IB status pane, Daily Universe) so specs port faithfully | 1-2 weeks   | Becomes a product-feature ship, not a test port                                    |
| **C. Abandon port, delete anyway** | **Accept that codex's specs assert on UI that never made it to claude.** Delete codex-version without porting.                 | **< 1 day** | Zero Playwright coverage until claude-native specs are written for future features |

### Decision — option C

**Rationale:**

- The specs were asserting on **codex's UI, not claude's**. They were never regression coverage for claude — they'd have been coverage for a ghost version.
- Building the missing UI (option B) is a product-feature decision, not a test-infrastructure decision. If operators want "Interactive Brokers status" or a "Daily Universe" editor, that's a new-feature backlog item.
- Scope-down (option A) would ship 2-4 thin specs on unstable ground (the salvageable specs still need `data-testid` adds, mock-shape rewrites, and auth refactor). The effort-to-value ratio is poor.
- The council's original migration-asymmetry argument still holds: codex has no real-money validation, and deleting it doesn't orphan anything that's load-bearing on claude.

### What this changes vs the council verdict

- **Council verdict unchanged:** keep claude-version, kill codex-version.
- **Council's port-list:** superseded. Item #1 (Playwright port) and item #2 (CLAUDE sub-app taxonomy — not yet looked at) are both dropped. Items #3 (daily-scheduler container) and #4 (LiveStateController) remain **SKIP** as before.
- **Archival plan unchanged:** tag `codex-final` on the pre-delete commit, `git rm -r codex-version/`, update root `CLAUDE.md` to single-stack operation, update `playwright.config.ts` default baseURL to claude's `:3300`.

### Audit trail preserved on the abandoned port branch

The failed-port planning artifacts are preserved under the same branch (`feat/playwright-e2e-port`) that ships this deletion:

- `docs/plans/2026-04-19-playwright-e2e-port.md` — the plan that was rejected in iter-1 review
- `docs/research/2026-04-19-playwright-e2e-port.md` — the research brief
- Council context in `docs/decisions/scratch/council-context.md`

Future re-attempts at a Playwright suite should start fresh against claude's actual UI, not try to revive these artifacts.
