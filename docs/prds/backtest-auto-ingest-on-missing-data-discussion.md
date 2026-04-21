# PRD Discussion: Backtest Auto-Ingest on Missing Data

**Status:** Complete
**Started:** 2026-04-21
**Participants:** User (Pablo), Claude

## Original User Stories

Pablo, verbatim (2026-04-21, at PR #39 merge time):

> I noticed that when the back tests show and they show fail, you can click on it and it will show you the error. If the back test fails because it has no data, the system should automatically download the data that is needed and only show if the data is not available by data refresh. It's not good that it just shows the error, no way to remediate it. I don't want to use `show` to only show the error; I want to fix it by downloading the data if it's needed. Can you make sure that it does that?

**Follow-up (when asked if this PR should land ahead of the feature):** "Option 2 then" (i.e., land PR #39 with FailureCard as-is, ship auto-ingest as this follow-up PR).

## Codebase context already discovered (Phase 0 — discovery)

- **PR #39 shipped** the classifier + `Remediation(kind="ingest_data", symbols, asset_class, start_date, end_date, auto_available=False)` contract. This PR flips `auto_available → True` for the MISSING_DATA path and wires the auto-heal.
- **`ensure_catalog_data()`** (`services/nautilus/catalog_builder.py`) is the function that raises `FileNotFoundError("No raw Parquet files found for '<SYM>' under /app/data/parquet/<asset_class>/<SYM>. Run ...")` when data is absent. It fires in the outer worker path, BEFORE the backtest subprocess spawns. So auto-heal attaches cleanly there — no subprocess lifecycle surgery needed.
- **`enqueue_ingest(pool, asset_class, symbols, start, end, provider="auto", dataset=None, schema=None)`** already exists at `core/queue.py:147`. Delegates to the `run_ingest` arq job at `workers/settings.py:66` → `services/data_ingestion.run_ingest`. No new ingest plumbing needed.
- **arq worker config:** `max_jobs=2`, `max_tries=2` (one retry), shared queue for `run_backtest` + `run_ingest`. The shared queue is the main concurrency concern — if 2 failing backtests enqueue ingests while holding both slots, we deadlock.
- **`BacktestStatus`** is `String(50)` with values `pending | running | completed | failed`. No enum. Adding a new state would need migration (cheap) or sub-status field (cheaper — don't alter the main state machine).
- **Documented scope-defer from PR #39:** the UI's Run Backtest form doesn't send `config.asset_class`; worker defaults to `"stocks"`. So for futures-via-UI, the remediation command is wrong. This PR needs to close that — otherwise auto-heal routes futures symbols through Polygon, which won't have them.

## Discussion Log

_(questions below, awaiting Pablo's answers — will be appended here as Q&A)_

---

## Questions for Pablo (round 1)

The stories are clear in intent — "auto-download data when missing, only surface if that also fails." These questions are about the engineering decisions hiding in the gaps.

### State machine & visibility

**Q1 — Backtest state during auto-heal.** While the ingest is running and the backtest is queued for retry, what status should the row report?

- **(a)** Keep `running`. Add a separate `error_code=ingesting_data` or a new `substatus` text field so the UI can render "Data backfill in progress…" without confusing the state machine.
- **(b)** New first-class state `awaiting_data`. Cleaner semantically, but adds a migration + touches every status-filter query (`/history`, dashboards, watchdog).
- **(c)** Silent — stay `pending` until complete or final-fail. User sees a long `pending` but nothing else.

My recommendation: **(a)**. It's additive and reuses the existing envelope shape. `auto_available=true` + a new `Remediation.kind="ingest_in_progress"` variant (or a new progress field on the envelope) can report "Fetching AAPL 2024-01-01..2024-12-31 from Databento." Thoughts?

**Q2 — UI treatment.** Do you want a visible "data backfill in progress" badge on the list page and detail page, or should it be truly invisible (list row keeps its old `running` spinner, detail page shows no indication)?

- **(a)** Subtle indicator — e.g., small "Fetching data…" text or secondary badge alongside the running spinner.
- **(b)** Full FailureCard-style progress card that replaces the run-progress view during ingest.
- **(c)** Invisible — look indistinguishable from a normal run.

(I lean toward (a) — you'll want to know if a backtest is taking 15 minutes because of a Databento fetch vs. because the strategy is actually slow.)

### Retry & bounds

**Q3 — Retry budget.** How many auto-ingest attempts before we give up and surface the error to the user via FailureCard?

- **(a)** One try. If ingest fails, show FailureCard.
- **(b)** N tries with exponential backoff (e.g., 3 tries: 0s, 30s, 2min). Handles transient Databento/Polygon flakes.
- **(c)** Bounded wall-clock (e.g., "keep trying for up to 30 minutes").

And — **what's the max wall-clock you're willing to let a backtest sit in "ingesting"**? 5 minutes? 30 minutes? The ingest of 1 year of minute bars for 1 symbol typically completes in ~30 seconds per Databento, but a large range / many symbols can balloon.

**Q4 — Cost guardrails on auto-ingest.** The current `enqueue_ingest` will cheerfully pull whatever you give it. For auto-heal, should we cap scope to prevent accidental big bills?

- **(a)** No cap — trust the backtest request's date range and symbol list.
- **(b)** Soft cap on date range (e.g., auto-ingest max 2 years, longer ranges require manual `msai ingest`).
- **(c)** Soft cap on symbol count (e.g., max 5 symbols per auto-heal request).

(Relevant because Databento is pay-per-bar for historical, and a 10-year options request for 30 underlyings would be both slow and expensive.)

### Scope & classification

**Q5 — Close the asset_class scope-defer.** PR #39 documented that the UI doesn't send `asset_class`, so futures-via-UI routes to `stocks` in the remediation command. Auto-heal can't ship without fixing this — Databento routing is asset-class-dependent. Two options:

- **(a)** Add `asset_class` dropdown to the Run Backtest form. (stocks / futures / options / forex)
- **(b)** Derive `asset_class` server-side from the canonical instrument ID (`ES.n.0` → futures, `AAPL.NASDAQ` → stocks). The registry + `SecurityMaster` already knows this.

My recommendation: **(b)** — it's automatic, can't go wrong for users, works for programmatic API callers too. No new UI surface to maintain.

**Q6 — Scope of auto-heal classifications.** Only MISSING_DATA triggers auto-heal? Or also:

- **TIMEOUT** — probably not (timing out on a 1-year backtest isn't an "auto-heal" situation; user should shrink the run).
- **STRATEGY_IMPORT_ERROR** — definitely not (this is code, not data).
- **ENGINE_CRASH** — definitely not (this is a bug).
- **UNKNOWN** — definitely not.

Confirming: **auto-heal is MISSING_DATA only. All other failures keep today's FailureCard behavior unchanged.**

### Concurrency & dedupe

**Q7 — Dedupe ingest jobs.** If two failed backtests both need AAPL 2024-01-01..2024-12-31, do we:

- **(a)** Enqueue two ingest jobs. The service layer is likely idempotent (atomic Parquet writes — second run re-downloads and overwrites, or skips if files exist). But wastes API credits.
- **(b)** Dedupe at enqueue time — check Redis for "am I already ingesting this symbol/range?" lock.
- **(c)** Dedupe at the ingest service layer — check parquet existence before calling the provider.

My recommendation: **(c)** — the `ensure_catalog_data` check that classifies the miss in the first place should be replayed after the ingest runs, and a per-instrument "already ingested" short-circuit is probably already in there. Can verify during Phase 2 research.

**Q8 — Arq queue isolation.** `max_jobs=2` shared queue for `run_backtest` + `run_ingest`. If 2 failing backtests each enqueue an ingest, we have 2 backtests pending + 2 ingest pending + 2 workers → workers might pick up another backtest before the ingest runs, starving the heal. Options:

- **(a)** Accept the risk; raise `max_jobs` to 4 or 5.
- **(b)** Separate queue for ingest jobs (Redis + arq supports multiple queue names; just a worker split).
- **(c)** Priority — auto-heal ingests run at higher priority than manual ingests or new backtests.

My recommendation: **(a)** — simplest. Single-user system; 2 → 4 workers is cheap. If we see starvation in practice, upgrade to (b).

### Manual retry UX

**Q9 — Retry after auto-heal exhausted.** When auto-heal gives up (e.g., Databento has no data for that symbol + range), the FailureCard shows with the existing Remediation. Does the user need a **"Retry Backtest"** button that re-enqueues the same run, or do they just copy the backtest config and resubmit?

- **(a)** Add "Retry Backtest" button to FailureCard — one-click re-run once the user fixes the underlying issue (e.g., after they manually `msai ingest` the symbol).
- **(b)** No button — user navigates back to Run Backtest form, re-fills, submits. (What they do today.)

(a) is a nice ergonomics win that falls out of this PR cheaply. Worth including?

### Out-of-scope check

**Q10 — Anything I haven't covered?** Known candidates for defer-again, ordered by my guess at importance:

- Partial-range backfill (ingest only missing months within the requested range) — Phase 2 if partial-file-ingest is complex.
- Manual "force-refresh data" button on the backtest detail page — separate UX feature.
- Telemetry dashboard for auto-heal success rate — nice-to-have, not core.
- Cost visibility ("This auto-ingest cost $0.73") — nice-to-have, not core.

Anything missing from this list that you want in scope?

---

---

## Council Verdict (ratified 2026-04-21 by 5-advisor council + Codex chairman)

**Full chairman synthesis preserved in session transcript; summary below locks PRD scope.**

**10yr/1min interpretation:** Hybrid/tiered as long-term philosophy; implement **only lazy auto-heal** in THIS PR. Default omitted requests to `1min/10y`. Reject eager "every symbol × every asset type × 10yr" pre-seeding. Curated hot-universe seed job deferred to separate PR.

**Per-question verdicts (locked):**

- **Q1** — Keep `status="running"` + add first-class `phase="awaiting_data"` + `progress_message` on `/backtests/{id}/status`. No new top-level status; phase field avoids schema churn + reuse of failure-only envelope. _(Overruled Contrarian's new state; adopted Maintainer's refinement.)_
- **Q2** — Subtle "Downloading data…" indicator in existing status view, powered by polling `/backtests/{id}/status`. Full progress card deferred. _(Overruled Hawk/Contrarian's full card — substrate is status contract, not dashboard chrome.)_
- **Q3** — One auto-heal cycle, **30-minute wall-clock cap**. No recursive retry. On cap hit, fail cleanly with retryable-timeout message; in-flight ingest allowed to complete independently.
- **Q4** — Refined `(b)+(c)`: cap auto-heal to the requested symbol set + max **10 years** + asset-class-aware workload guardrails + **hard exclusion of options-chain fan-out**.
- **Q5** — Server-side derivation of `asset_class` from canonical instrument ID (registry lookup). No UI dropdown.
- **Q6** — Auto-heal only on `MISSING_DATA`. TIMEOUT/STRATEGY_IMPORT_ERROR/ENGINE_CRASH/UNKNOWN stay manual.
- **Q7** — **Redis lock + service-layer parquet short-circuit** (defense in depth). Existence-check-only loses on race conditions + duplicate provider spend.
- **Q8** — **Separate ingest queue** / worker lane. Shared `max_jobs=2` is the clearest operational risk. _(Overruled Simplifier/Pragmatist's "bump max_jobs" — this is a blocker, not a tuning knob.)_
- **Q9** — Defer "Retry Backtest" button to a future PR with a first-class retry endpoint + explicit attempt semantics. AI-first/self-heal makes human retry secondary.
- **Q10** — **OUT of this PR:** partial-range backfill, force-refresh button, telemetry dashboard, rich cost visibility. **IN as plumbing:** phase/progress fields on status, structured heal logs, guardrail/cost-estimate enforcement.

**Blocking objections accepted (must ship in this PR):**

1. Separate ingest queue/worker lane (kills shared-queue starvation risk).
2. Cost/workload guardrails (10y ceiling + symbol fan-out limits + no options expansion).
3. Dedupe (Redis lock + service-layer short-circuit).
4. Partial coverage verification (per-symbol time coverage, not just file existence).
5. Server-authoritative `asset_class` derivation.

**Missing evidence to resolve in Phase 2 research:**

- Actual Databento/Polygon billing math for 10y/1min pulls by asset class (options vs underlyings).
- Measured wall-clock ingest times at current API limits for 1y and 10y across realistic symbol counts.
- Whether catalog/coverage logic can validate per-symbol time coverage or only file existence.
- Whether deployed arq topology can add a separate ingest queue cleanly.
- Whether existing nightly cron already covers delta refresh for the future curated-universe seed job.
- Safe workload thresholds per asset class before latency/cost becomes unacceptable.

**Dissent preserved (minority report):**

- **Contrarian** wanted new top-level `awaiting_data` state — overruled (phase field sufficient).
- **Simplifier + Pragmatist** wanted bump max_jobs over separate queue — overruled (shared queue is the blocker).
- **Hawk + Contrarian** wanted full progress card + cost visibility in-scope — overruled (deferred to follow-up).
- **Simplifier + Hawk + Pragmatist** wanted immediate "Retry Backtest" button — overruled (deferred until first-class retry endpoint).

---

## Refined Understanding

### Personas

- **Platform agent (primary user per Pablo's AI-first directive):** Submits backtests via API/CLI; expects platform to self-heal transparently; polls `/backtests/{id}/status` for progress.
- **Human operator (Pablo):** Uses UI to monitor what the platform is doing; needs visibility into auto-heal progress without having to take action; still can intervene via CLI when auto-heal hits guardrails.
- **Cost-sensitive stakeholder (Pablo wearing operator hat):** Must not be surprised by provider bills. Workload guardrails + dedupe protect here.

### User Stories (Refined — to be fleshed out in `/prd:create`)

- **US-001** — As a platform agent, when I submit a backtest for a symbol/range whose data is missing, the platform auto-downloads the missing data and re-runs my backtest transparently; I see `status=running` + `phase=awaiting_data` + `progress_message` while the heal runs.
- **US-002** — As a human operator viewing the backtest detail page, I see a subtle "Downloading data…" indicator next to the normal running spinner when auto-heal is in flight, so I know why the run is taking longer than usual.
- **US-003** — As Pablo (cost stakeholder), auto-heal is hard-capped at 10 years of date range and a bounded symbol fan-out per request; options-chain expansion never happens automatically. If my request would exceed these, the backtest fails with a structured envelope pointing me to manual `msai ingest` with explicit scope.
- **US-004** — As a platform agent, concurrent backtest requests for the same missing symbol/range don't trigger duplicate downloads (Redis lock + service-layer short-circuit); I just wait for the in-flight heal to complete.
- **US-005** — As a human operator, when I submit a backtest via the Run form, the platform server-derives `asset_class` from my canonical instrument ID (`ES.n.0` → futures, `AAPL.NASDAQ` → stocks); I don't need to pick it.
- **US-006** — As a platform agent, if auto-heal exceeds the 30-minute wall-clock cap, my backtest fails cleanly with a retryable timeout message; any in-flight ingest allowed to complete in the background (cached for future backtests).
- **US-007** — As Pablo, structured logs + per-heal audit trail let me correlate "why did this cost me $X" without needing a dedicated dashboard in this PR.

### Non-Goals (explicit)

- ❌ Eager pre-seeding of "every symbol × every asset class × 10yr × 1min" up-front.
- ❌ Full progress-streaming (SSE/WS) endpoint.
- ❌ Dedicated auto-heal telemetry dashboard in the UI.
- ❌ Rich cost-visibility UI ("this backtest cost $0.73").
- ❌ Partial-range backfill optimization.
- ❌ "Retry Backtest" button on FailureCard.
- ❌ "Force refresh data" button.
- ❌ Auto-heal for non-`MISSING_DATA` FailureCodes.
- ❌ Auto-expanding options chains from auto-heal context.

### Key Decisions (locked by council)

- Phase field model: `phase="awaiting_data"` on `BacktestStatusResponse` (not a new top-level status).
- Retry policy: one cycle, 30-minute wall-clock cap, no recursive retries.
- Cost guardrails: 10-year date ceiling + asset-class-aware workload cap + explicit options-chain-fan-out rejection.
- Concurrency: separate `ingest_queue` with its own arq worker lane.
- Dedupe: Redis lock keyed by `(asset_class, sorted(symbols), start, end)` + service-layer parquet coverage short-circuit.
- Coverage verification: per-symbol time-range coverage check post-ingest, not just `os.path.exists`.
- `asset_class` source of truth: canonical instrument ID lookup (server-side). Close PR #39 scope-defer.

### Open Questions (remaining — to resolve in Phase 2 research)

- [ ] Actual provider billing for 10y/1min across asset classes.
- [ ] Wall-clock ingest measurements at current rate limits.
- [ ] Feasibility of coverage verification vs file-existence with current catalog code.
- [ ] arq topology: separate queue setup cost.
- [ ] Nightly cron reuse for curated-universe follow-up.
- [ ] Workload thresholds per asset class.

---

**Ready for `/prd:create backtest-auto-ingest-on-missing-data`.**
