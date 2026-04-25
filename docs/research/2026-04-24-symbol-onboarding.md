# Research: symbol-onboarding

**Date:** 2026-04-24
**Feature:** Thin orchestration layer composing PR #44 `/instruments/bootstrap` + existing `/market-data/ingest` arq path + `msai instruments refresh --provider interactive_brokers` into a single YAML-driven `POST /api/v1/symbols/onboard` + `msai symbols onboard` CLI with Databento preflight cost estimate and ceiling.
**Researcher:** research-first agent

---

## Libraries Touched

| Library                  | Our Version          | Latest Stable                     | Breaking Changes Since Ours                                                                                                                                 | Priority   |
| ------------------------ | -------------------- | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| databento (Python SDK)   | `>=0.43.0` (pinned)  | `0.75.0` (Apr 2026)               | `mode=` param on `metadata.get_cost` **deprecated in 0.65** (removed in a future release); several metadata list-method breaks at 0.17 (long pre-dating us) | **TIER 1** |
| PyYAML / ruamel.yaml     | **NOT installed**    | PyYAML 6.0.2; ruamel.yaml 0.18.10 | Net-new dependency — choose at plan time                                                                                                                    | **TIER 1** |
| arq                      | `>=0.26.0` (pinned)  | `0.28.0`                          | None material. Job-abort + `allow_abort_jobs` semantics stable; `Job.status()` lifecycle stable                                                             | **TIER 1** |
| FastAPI                  | `>=0.133.0` (pinned) | `0.133.x`                         | 202-polling pattern is textbook; no recent convention shift                                                                                                 | TIER 2     |
| Pydantic V2              | `>=2.10.0` (pinned)  | `2.10.x`                          | Policy: no breaking changes in minor V2 releases; `model_validator(mode="after")` stable                                                                    | TIER 2     |
| Typer                    | `>=0.15.0` (pinned)  | `0.15.x`                          | None material                                                                                                                                               | TIER 2     |
| Nautilus `BarAggregator` | `1.222.0+`           | `1.223.0+`                        | Covered by existing PR #16 autonomy contract — unchanged                                                                                                    | TIER 3     |
| SQLAlchemy 2.0           | `>=2.0.36`           | `2.0.36+`                         | No new patterns needed                                                                                                                                      | TIER 3     |
| tenacity                 | `>=9.1.0,<10`        | `9.1.x`                           | No new patterns needed (PR #44 established retry policy on Databento client)                                                                                | TIER 3     |
| Next.js 15 / React 19    | N/A                  | N/A                               | **N/A in v1** — UI deferred                                                                                                                                 | SKIP       |

---

## Per-Library Analysis

### 1. Databento Python SDK — `Historical.metadata.get_cost()` (TIER 1 — load-bearing for US-004)

**Versions:** ours = `>=0.43.0`; latest stable = `0.75.0` (2026-04-08).

**Method contract (from docs + SDK inspection):**

Historical endpoint: `GET /v0/metadata.get_cost`. Python SDK method: `client.metadata.get_cost(...)`.

Parameter shape (matches `databento-python` + the Historical REST reference):

- `dataset: str` — REQUIRED. Examples: `XNAS.ITCH`, `XNYS.PILLAR`, `ARCX.PILLAR`, `GLBX.MDP3`.
- `symbols: str | list[str]` — REQUIRED (unless `ALL_SYMBOLS` semantics used, which is dangerous for cost estimation — don't).
- `schema: str` — REQUIRED. For us this is `"ohlcv-1m"` (1-minute bar storage is canonical per the architectural pin).
- `start: str | datetime` — REQUIRED. ISO-8601.
- `end: str | datetime` — REQUIRED. ISO-8601.
- `stype_in: str` — `"raw_symbol"` is the default we'd use.
- `stype_out: str` — usually `"instrument_id"`.
- `mode: str` — **DEPRECATED as of 0.65.0** (CHANGELOG confirms). The SDK will still accept it but it will be removed. **Do not pass `mode=` in new code.**
- `limit: int | None` — optional.

**Return type:** `float` — a single USD cost estimate for the whole `(dataset, symbols, schema, window)` tuple. Not a structured breakdown.

**Billing / rate-limit:**

- Databento public documentation does **NOT** explicitly state whether `metadata.get_cost` is itself billable or free. The method is positioned throughout their docs and blog as the pre-flight cost-check tool ("use this before you request any data"), which strongly implies free-to-call, but **this is not guaranteed in writing**. Databento gives each team $125 of historical free credit, so marginal `get_cost` cost (if any) would not block us.
- Databento does **not publish Historical REST rate limits** (confirmed during PR #44 research — see `docs/research/2026-04-23-databento-registry-bootstrap.md`). The SDK auto-retries 429s on `batch.download` only, not `timeseries.get_range` or metadata methods.
- **For our US-004 preflight**: treat `metadata.get_cost` as a cheap/free call, but wrap it with the **same tenacity retry policy PR #44 already uses** against `BentoClientError`/5xx/429. Do NOT loop-call it once per symbol for a 50-symbol watchlist — batch one call per `(dataset, schema, window)` tuple and let Databento fan out the `symbols` list internally.

**Accuracy (the critical unknown flagged in the PRD):**

- Databento's own documentation does **NOT** publish an error band for `get_cost` vs actual billed cost. No "estimate is accurate within X%" SLA, no "median deviation" figure.
- Pricing is **per-byte-delivered**, not per-query. `metadata.get_cost` runs the same coverage + delivery-size computation the real `timeseries.get_range` uses, so the two should agree **to the byte** for a completed window, **unless**:
  1. The requested window crosses today's publication boundary (new data published mid-request — the exact class of failure PR #44 hit during the nightly E2E window).
  2. Databento re-bills on backfills / corrections (rare but possible on futures with late settlement prints).
  3. Ambiguous symbol resolution: `get_cost` computes size assuming every matched symbol lands; if one is delisted mid-window the actual byte count is smaller.
- **Operational pattern other teams use** (from the blog + Discord): call `get_cost` immediately before `get_range`, use the returned value as the fixed budget, and treat any >5% post-hoc delta as a Databento-side event (backfill, publication boundary).
- **For MSAI**: `estimate_confidence: "medium"` is the right default. Only flip to `"high"` when the window is fully historical (`end < today - 1d`) — matches PR #44's finding that `start=today` probes fail during the nightly publication window.

**Sources:**

1. [Historical.metadata.get_cost — official docs](https://databento.com/docs/api-reference-historical/metadata/metadata-get-cost) — accessed 2026-04-24 (page loaded nav-only; parameter details corroborated from SDK + CHANGELOG + PR #44 research).
2. [databento-python CHANGELOG](https://github.com/databento/databento-python/blob/main/CHANGELOG.md) — accessed 2026-04-24. Confirms `mode=` deprecation at 0.65.0; no other breaking changes to `get_cost` in 0.40→0.75 range.
3. [databento-python on PyPI](https://pypi.org/project/databento/) — accessed 2026-04-24. Current 0.75.0; supports Python 3.10–3.14.
4. [Databento metered pricing FAQ](https://databento.com/docs/faqs/usage-pricing-and-data-credits) — accessed 2026-04-24. $125 free credit, per-byte billing.
5. Prior MSAI research: `docs/research/2026-04-23-databento-registry-bootstrap.md` (PR #44) — rate-limit silence + tenacity policy + nightly-publication-window gotcha.

**Design impact:** **(a)** US-004 "confidence band" is a **declared classification**, not a computed confidence interval — `high` when `end < today - 1d` AND no continuous-futures symbols in batch, `medium` otherwise, `low` when any symbol outside Databento catalog. **(b)** Batch `get_cost` calls by `(dataset, schema, window)` tuple, not per-symbol — Databento fans out internally. **(c)** Do NOT pass `mode=` (deprecated). **(d)** Wrap `get_cost` in the PR #44 tenacity retry (reuse existing `DatabentoError` hierarchy). **(e)** `estimate_basis: "databento.metadata.get_cost(dataset=...,schema=ohlcv-1m,window=[start,end])"` is the right audit string.

**Test implication:** **(a)** Mock `client.metadata.get_cost` to return specific float values in unit tests; don't call the real API. **(b)** Integration test with fake Databento module (reuse PR #44 pattern at `_install_fake_databento`) asserts batching — single `get_cost` call for a 50-symbol equity watchlist sharing `(XNAS.ITCH, ohlcv-1m, 2023-01-01→2024-12-31)`. **(c)** E2E test with `end=today` must expect `estimate_confidence="medium"` (not `high`). **(d)** Regression test asserting the outbound call does NOT include `mode=`.

---

### 2. YAML parser — PyYAML vs ruamel.yaml (TIER 1 — net-new dependency)

**Versions:** ours = **NEITHER installed** (confirmed by grep of `backend/pyproject.toml`). PyYAML 6.0.2 (Aug 2024), ruamel.yaml 0.18.10 (2025).

**Trade-off:**

| Axis                | PyYAML 6.0.2                                                                                       | ruamel.yaml 0.18                                                        |
| ------------------- | -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Maturity            | Older, ubiquitous, used by ~everyone                                                               | Younger, derived from older PyYAML fork, actively maintained            |
| Safety              | `yaml.safe_load()` safe since always; **`yaml.load()` without `Loader=` deprecated and dangerous** | `YAML(typ="safe")` safe; default typ is round-trip (comments preserved) |
| YAML 1.2 compliance | Partial (still emits YAML 1.1 by default; int-like strings silently reparse)                       | Full YAML 1.2 compliance available                                      |
| Comment round-trip  | Lost on load                                                                                       | Preserved (useful for `msai symbols ... --edit` future features)        |
| Weight              | Lean, C-ext                                                                                        | Pure-Python default; install size similar                               |
| MSAI fit            | We only READ watchlists (never write back) → round-trip moot in v1                                 | Same but with headroom                                                  |

**Security (both safe when used correctly):**

- PyYAML: **always** use `yaml.safe_load()` (never `yaml.load(s)` or `yaml.load(s, Loader=yaml.FullLoader)` on untrusted input — FullLoader has had CVEs in 2020-2021). Deserialization attacks against YAML in Python are real.
- ruamel.yaml: use `YAML(typ="safe")`; avoid default typ on untrusted input. Note: ruamel's `safe` cannot parse YAML flow-style with unquoted collection values as a subtle divergence from PyYAML.

**Watchlist files are git-committed by Pablo** (single-user, no network ingestion path), so the adversarial-input threat model is low. But adversarial or not, `safe_load` is the only correct choice.

**Sources:**

1. [PyYAML yaml.load() deprecation wiki](<https://github.com/yaml/pyyaml/wiki/PyYAML-yaml.load(input)-Deprecation>) — accessed 2026-04-24. `yaml.load(input)` without Loader= raises warning and will be removed.
2. [ruamel.yaml on PyPI](https://pypi.org/project/ruamel.yaml/) — accessed 2026-04-24. v0.18.10 current; `typ="safe"` equivalent to PyYAML's `safe_load` without tag resolution.
3. [Semgrep: Fully loaded — testing vulnerable PyYAML versions](https://semgrep.dev/blog/2022/testing-vulnerable-pyyaml-versions/) — accessed 2026-04-24. CVE-2020-14343 / CVE-2020-1747 demonstrate FullLoader unsafe-on-untrusted.
4. [Real Python: YAML: The Missing Battery](https://realpython.com/python-yaml/) — accessed 2026-04-24. General PyYAML/ruamel.yaml guidance.

**Design impact:** Add **PyYAML 6.0.2** to `backend/pyproject.toml`. Reasons: (a) ubiquity across the Python stack reduces foreign-code surface, (b) MSAI never round-trips watchlists (read-only load), so ruamel's comment preservation is unused, (c) PyYAML C-ext is faster on cold-start `msai symbols onboard` which matters for a CLI. **Pin `safe_load` at every call site.** Helper `_load_watchlist_yaml(path: Path) -> WatchlistManifest` centralizes parsing + Pydantic validation.

**Test implication:** One test for `yaml.load(malicious_payload)` — confirm we're calling `safe_load` not `load` (adversarial YAML with `!!python/object/apply:os.system` tag must NOT execute). One test for comments/anchors/aliases (should parse without data loss since our schema doesn't use them).

---

### 3. arq — async-job patterns for long-running batched jobs (TIER 1)

**Versions:** ours = `>=0.26.0`; latest stable = `0.28.0`.

**Core findings from arq docs + PR #40 precedent:**

**(a) Job status lifecycle** (5 states):

- `deferred` — in queue, scheduled-time not reached
- `queued` — in queue, ready to run
- `in_progress` — worker picked it up
- `complete` — done; result available via `Job.result()`
- `not_found` — ID unknown to Redis

These are **job-level** only. arq does NOT natively track sub-progress.

**(b) Per-symbol progress tracking — there is NO built-in primitive.** Options:

- **Option A (recommended for MSAI v1): single parent arq job + custom progress table in Postgres.** The parent worker updates a `per_symbol JSONB` column on the `OnboardingJob` row as each symbol transitions (`registering → backfilling → qualifying_live → completed`). `GET /status` queries Postgres, not arq. This matches PR #40's `run_auto_heal` pattern (single parent job writes its state to DB).
- **Option B: N child arq jobs, parent tracks child job_ids.** Cleaner separation but adds per-child Redis round-trips + requires the parent to poll N job statuses. Overkill for 20-symbol typical batch.
- **Option C: arq middleware.** Possible but unspecified — no published recipe. Skip.

**(c) Partially-succeeded state.** arq has NO native `completed_with_failures` state — only `complete` or job-level failure. MSAI must model this **in the `OnboardingJob.status` column**, not via arq. Parent job returns success (`complete`) whenever it finishes iterating; per-symbol `failed` vs `ok` is a DB-level concern.

**(d) Cancellation semantics.** Setting `allow_abort_jobs=True` on worker config enables `Job.abort()`. Once called, `asyncio.CancelledError` is raised inside the job "at the next opportunity" (next `await` checkpoint). Known issue: `Job.abort()` hangs if `keep_result=0` — don't use the combo. **v1 does not expose cancel** per PRD non-goal, but this research note preserves future-safety.

**(e) Job ID.** Random by default (UUID); custom deterministic via `_job_id=` parameter on `enqueue_job()`. **For idempotency (US-002 acceptance criterion "submitting the same watchlist twice only re-enqueues uncovered ranges"), we SHOULD use a deterministic `_job_id` computed from `hash((watchlist_name, sorted_symbol_tuple, start, end))`** — matches PR #40 auto-heal's `AutoHealLock` pattern. If the prior job is still `in_progress`, the second submission returns the same `job_id` without re-enqueuing (arq's `_job_id` uniqueness enforcement).

**(f) tenacity + `asyncio.CancelledError`.** tenacity's `AsyncRetrying` default retry predicate does NOT re-raise `CancelledError` — it catches it and retries. This is **wrong** for cancellation. When we eventually add cancel (post-v1), the retry `stop=` or `retry=` must explicitly exclude `CancelledError`. Add a research note for the future-PR.

**Sources:**

1. [arq docs v0.28.0](https://arq-docs.helpmanual.io/) — accessed 2026-04-24. Job lifecycle, `allow_abort_jobs`, cancellation.
2. [arq PR #212 (Add method to cancel jobs)](https://github.com/python-arq/arq/pull/212/files) — accessed 2026-04-24. Abort uses `asyncio.CancelledError`.
3. [arq issue #363 (Job.abort hangs with keep_result=0)](https://github.com/python-arq/arq/issues/363) — accessed 2026-04-24. Known limitation.
4. [FastAPI + ARQ polling pattern (Muraya, 2025)](https://davidmuraya.com/blog/fastapi-background-tasks-arq-vs-built-in/) — accessed 2026-04-24. Standard polling architecture (POST → 202 → GET /status).
5. Prior MSAI research: `docs/research/2026-04-21-backtest-auto-ingest-on-missing-data.md` (PR #40) — `run_auto_heal` single-parent-job pattern ratified and shipped.

**Design impact:** **(1) Adopt Option A: one `run_symbol_onboarding` parent arq job + `OnboardingJob` Postgres table.** Matches PR #40 precedent; avoids the N-child-job fan-out complexity. **(2) Deterministic `_job_id`** = `f"onboard:{watchlist_name}:{content_hash}"` for idempotency. **(3) `OnboardingJob.status` column** carries the MSAI state (`pending / in_progress / completed / completed_with_failures / failed`) — arq's 5 states are for worker internals only. **(4) `per_symbol` JSONB column** (not a separate `onboarding_job_symbols` table) — single write per symbol-state-transition keeps the hot path one-row-update. **(5) Do NOT set `allow_abort_jobs=True` in v1** (non-goal). Leave default; revisit when cancel lands.

**Test implication:** **(a)** Integration test submitting the same watchlist twice shares `_job_id` and returns the same `job_id` on second call (idempotency). **(b)** Unit test for the `per_symbol` JSONB update ordering — per-symbol state transitions are serialized per-symbol (no race between `registering → backfilling` updates). **(c)** Test that parent arq job returns `complete` (not `failed`) even when all 20 symbols failed per-symbol — systemic failure distinction is enforced at the DB `status` column.

---

### 4. FastAPI 202 + polling conventions — dry_run query flag (TIER 2)

**Versions:** ours = `>=0.133.0`; latest stable = `0.133.x`.

**Finding:** FastAPI has no official opinion on `POST /resource?dry_run=true` vs `POST /resource/dry-run`. The community consensus (from fastapi-best-practices repo + Render + Muraya blog) emphasizes **separation of submission from status polling** but does not prescribe a dry-run idiom.

**Trade-off:**

| Axis     | `?dry_run=true` query flag                                                                                                   | Separate `/dry-run` endpoint                                                                       |
| -------- | ---------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Pros     | Single handler; shared request validation; one Pydantic model                                                                | Clearer OpenAPI schema; different response type is explicit                                        |
| Cons     | Response shape diverges based on a query param (harder to type in OpenAPI clients; `EstimateResponse                         | AcceptedResponse` union)                                                                           | Code duplication (validation runs twice, once per endpoint) |
| MSAI fit | Keeps the request validation + ceiling check in one function; Pydantic `@model_validator(mode="after")` runs unconditionally | Separate endpoint means separate route decorator, separate OpenAPI operation_id, better Swagger UX |

**Modern OpenAPI consumers (TypeScript codegen, Swagger UI):** slightly prefer separate endpoints because the response-type union behind a query flag forces `response_model=OnboardResponse | DryRunResponse`, which generates an awkward `type OnboardResponse200 = A | B` in TS clients.

**Sources:**

1. [FastAPI Concurrency/async docs](https://fastapi.tiangolo.com/async/) — accessed 2026-04-24. No prescription on dry-run shape.
2. [fastapi-best-practices (zhanymkanov)](https://github.com/zhanymkanov/fastapi-best-practices) — accessed 2026-04-24. Emphasizes Pydantic separation per operation.
3. [Muraya: Managing Background Tasks in FastAPI](https://davidmuraya.com/blog/fastapi-background-tasks-arq-vs-built-in/) — accessed 2026-04-24. 202 + polling pattern reference.

**Design impact:** **Use a separate endpoint: `POST /api/v1/symbols/onboard/dry-run`**. Rationale: (a) responses genuinely differ (estimate vs 202-accepted), (b) Pablo's OpenAPI-generated TS client will be cleaner, (c) matches MSAI's existing per-operation-endpoint precedent (PR #41's `/results` vs `/report-token` vs `/report`). **Do NOT use `?dry_run=true`.** The PRD's US-004 mentions both idioms — this research resolves to the dedicated endpoint.

**Test implication:** Two endpoints → two integration test classes. Dry-run endpoint uses `response_model=EstimateResponse`; execute endpoint uses `response_model=AcceptedResponse`. The ceiling-exceeded path is tested on the **execute** endpoint only (dry-run itself is read-only and doesn't enforce).

---

### 5. Pydantic V2 `model_validator(mode="after")` (TIER 2)

**Versions:** ours = `>=2.10.0`; latest stable = `2.10.x`.

**Finding:** The `model_validator(mode="after")` decorator is **stable and supported across Pydantic 2.x**. Per the Pydantic version policy: "We will not intentionally make breaking changes in minor releases of V2, and functionality marked as deprecated will not be removed until the next major V3 release."

One observed 2.x quirk: model_validators run "again when the model is embedded as an attribute in another class, on the already validated instance" — for our schema (flat request bodies, not nested), this is irrelevant.

MSAI already uses this pattern in PR #44 (`BootstrapResultItem.model_validator`) and PR #41 (`BacktestResultsResponse.model_validator`). Both shipped clean.

**Sources:**

1. [Pydantic V2 Validators docs](https://docs.pydantic.dev/latest/concepts/validators/) — accessed 2026-04-24. `mode="after"` semantics + cross-field usage.
2. [Pydantic Version Policy](https://docs.pydantic.dev/latest/version-policy/) — accessed 2026-04-24. No minor-release breaks in V2.
3. Prior MSAI usage: PR #44 `src/msai/schemas/instrument_bootstrap.py`; PR #41 `src/msai/schemas/backtest.py`.

**Design impact:** **No impact — our current usage is aligned.** Copy PR #44's exact pattern for `OnboardRequest.model_validator(mode="after")` enforcing `end >= start`, `symbols non-empty`, `max_estimated_cost_usd > 0`, `live_qualify implies IB config present at Settings` (defer the last one to request-time check, not validator — Settings is outside the schema).

**Test implication:** Standard cross-field negative tests (`end < start` → `ValidationError`, `symbols=[]` → error, `max_estimated_cost_usd=0` with a non-IB-only batch → error). Copy PR #44's `test_schemas_instrument_bootstrap.py` structure.

---

### 6. Typer CLI — sub-app + `--dry-run` + `--confirm-spend` (TIER 2)

**Versions:** ours = `>=0.15.0`; latest stable = `0.15.x`.

**Finding:** Typer 0.15 sub-apps + flags pattern matches `msai instruments bootstrap` from PR #44. No API shifts in recent minor versions.

**Cost-confirmation idioms in established CLI tools:**

- **terraform:** `terraform plan` (dry-run), `terraform apply` (prompts `yes`), `terraform apply -auto-approve` (skip prompt). No explicit spend ceiling flag.
- **gcloud:** most destructive commands prompt `[Y/n]` unless `--quiet` is passed. No spend ceiling idiom.
- **aws:** `--dry-run` is standard for EC2; no spend ceiling.
- **None of the major tools has an explicit "--confirm-spend $N" precedent.**

The PRD's `--confirm-spend $N` syntax is effectively MSAI-novel. Closest analogue is `terraform apply -auto-approve` (yes/no gate). Our refinement is parameterized-by-dollars, which aligns with the "never auto-spend on Databento" safety goal.

**Sources:**

1. [Typer docs](https://typer.tiangolo.com/) — accessed 2026-04-24 (not re-fetched; unchanged since PR #44).
2. [Terraform CLI reference](https://developer.hashicorp.com/terraform/tutorials/cli) — accessed 2026-04-24. Plan/apply/auto-approve pattern.
3. Prior MSAI usage: PR #44 `src/msai/cli.py` `instruments` sub-app.

**Design impact:** **(a)** `msai symbols` sub-app registered on the existing Typer root via `app.add_typer`, matches PR #44. **(b)** Flags: `--dry-run` (read-only, no mutation, no ceiling gate), `--max-cost $N` (sets ceiling for this run), `--confirm-spend $N` (required when estimate > default ceiling; user explicitly names the dollar amount they're ok with). **(c)** Exit codes: 0 for `completed` and `completed_with_failures` (per PRD US-005); non-zero for `failed` / `ESTIMATOR_UNAVAILABLE` / `COST_CEILING_EXCEEDED`. **(d)** `--json` suppresses stderr progress; stdout stays machine-readable.

**Test implication:** CliRunner test matrix: no-flag (prompts confirm? — PRD leaves this unspecified; recommend "fail closed if estimate > default ceiling and --confirm-spend is absent" with stderr showing exact flag to pass). `--dry-run` exits 0 and prints estimate even when over ceiling (dry-run is read-only). `--json` output is valid JSON via `json.loads()`.

---

### 7. Nautilus `BarAggregator` — lazy rebuild path (TIER 3)

**Versions:** ours = `1.222.0+`; latest = `1.223.0+`.

**Finding:** `BarAggregator` + `ensure_catalog_data` lazy rebuild was ratified by PR #16 (catalog rebuild detects raw parquet delta via per-instrument source-hash marker). No change to this contract in our pinned version range. The `backtest_data_available` readiness field depends on Parquet presence (checked by directory listing), not on Nautilus catalog rebuild. Catalog rebuild happens at **backtest-start time**, not at onboarding time — aligned with PR #40 "no eager re-materialize" decision.

**Sources:**

1. Prior MSAI research: `docs/research/2026-04-21-backtest-auto-ingest-on-missing-data.md` (PR #40) — catalog-rebuild-on-demand confirmed.
2. `docs/nautilus-reference.md` — ships with repo.

**Design impact:** **No impact — our current usage is aligned.** Onboarding does NOT trigger catalog rebuild; backtest-launch does (existing PR #16 path). Coverage check in `GET /instruments/{id}/readiness?window=...` is a pure Parquet directory + month-range scan, not a Nautilus API call.

**Test implication:** Standard coverage sufficient. Regression test that onboarding does NOT call `ensure_catalog_data` (confirm the lazy boundary).

---

### 8–10. SQLAlchemy 2.0 / tenacity / Next.js (TIER 3 — no change) — brief

- **SQLAlchemy 2.0.36+** — reuse PR #44's async session-per-symbol pattern in `DatabentoBootstrapService`. No new patterns needed for `OnboardingJob` table (standard `Mapped[]` + `async with AsyncSession`).
- **tenacity 9.1.x** — reuse PR #44's `AsyncRetrying` policy verbatim against `DatabentoError`. See Open Risks for `CancelledError` interaction note (future-PR concern).
- **Next.js 15 / React 19** — **N/A in v1** per PRD non-goal #1. Skip entirely.

---

## Specific Unknowns — Answers

### Q1: How accurate is `metadata.get_cost`?

**Answer:** Databento does NOT publish an error band. Pricing is per-byte-delivered; `get_cost` computes the same size function the real `get_range` uses, so the two agree **to the byte** for fully-historical completed windows. Divergence modes: (a) window crosses today's publication boundary, (b) backfill/correction after the fact (rare), (c) ambiguous symbol resolution changes between estimate and execution.

**Recommendation:** classify confidence as `high` only when `end < today - 1d` AND no ambiguous/continuous symbols in batch; `medium` as default; `low` when any symbol is outside Databento catalog (estimate treats as $0).

### Q2: `OnboardingJob` persistence strategy

**Answer:** **Dedicated Postgres table.** arq's Redis-backed job records (a) expire by default, (b) don't support the `completed_with_failures` partial state, (c) don't give operator-queryable history ("last 20 onboarding runs"). Matches PR #40 `auto_heal_runs` + PR #41 `backtests.series` precedent of "arq for compute, Postgres for state-of-record."

### Q3: `CoverageSnapshot` computation

**Answer:** **On-the-fly directory scan for v1.** 2000-symbol case is real but not imminent (Pablo is single-user; current watchlists are ~20–50 symbols); directory `stat` + month-file listing is fast (~100ms for 50 symbols). The cached-table alternative adds a consistency-tax (invalidation on ingest completion, stale-read window) that doesn't pay back at current scale. Revisit when an operator surfaces a >500-symbol watchlist. Matches how `run_auto_heal` decides coverage today (Parquet month-file existence).

### Q4: Prometheus metrics set (low cardinality — Scalability Hawk's constraint)

**Recommendation (final):**

- `msai_onboarding_cost_usd_total{watchlist, provider}` — counter (matches PRD)
- `msai_onboarding_symbols_total{status}` — counter where `status ∈ {ok, failed, already_covered, not_started}` (4-value label — low cardinality)
- `msai_onboarding_duration_seconds` — histogram (no labels → low cardinality; PRD said "if trivial," it is)

Do NOT add `per-symbol` labels (unbounded cardinality). Do NOT add `error_code` label (cardinality bounded but adds 10+ series per watchlist — defer).

### Q5: `trailing_5y` sugar expansion

**Answer:** **Client-side (CLI) expansion only; server never sees the string.** CLI computes `end = today_utc.date()`, `start = end - relativedelta(years=5)` using `dateutil.relativedelta` (already transitively in stack via pandas). Business-day adjustments and holiday calendars are NOT applied at sugar-expansion time — Databento handles that internally (queries that start/end on a non-trading day just return no rows for those days). QuantConnect precedent: their `SetStartDate(2020, 1, 1)` likewise does not holiday-snap.

### Q6: tenacity + `asyncio.CancelledError`

**Answer:** tenacity's default `AsyncRetrying` catches and retries `CancelledError` — wrong semantically. For future cancellation support, pass `retry=retry_if_not_exception_type((asyncio.CancelledError, KeyboardInterrupt, SystemExit))` to `AsyncRetrying`. **Not urgent for v1** (cancel is a non-goal), but flag as a must-do for the future cancellation PR.

---

## Not Researched (with justification)

- **Polygon** — v1 explicitly Databento + IB only (PRD non-goal #11).
- **DuckDB** — read-only, untouched by this feature.
- **Azure Entra / PyJWT** — auth mechanism unchanged from existing endpoints.
- **shadcn/ui + Recharts** — UI deferred (PRD non-goal #1).
- **`msai:ingest` arq queue internals** — consumed as-is from PR #40; no routing changes.
- **Alembic 1.14+** — standard `ADD COLUMN` / `CREATE TABLE` migrations for `OnboardingJob` + (possibly) `asset_universe.resolution` rename; no driver-level risk. PR #44's `b6c7d8e9f0a1` migration patterns apply verbatim.
- **Redis 7** — only used as arq broker; no direct manipulation.

---

## Open Risks

1. **Databento `get_cost` accuracy is literally unwritten.** Databento has no published SLA or error band. Ops: always store BOTH the estimate AND the post-hoc `actual_cost_usd` (from the `GetRangeResponse` size header × per-byte rate) on `OnboardingJob`, and emit a WARNING log + Prometheus counter when divergence > 5%. Creates a long-term dataset to calibrate our `estimate_confidence` bucketing.
2. **Ambiguous symbols inflate estimates.** `get_cost(symbols=["BRK.B"])` may match both share-class `BRK.B` and a derived instrument, inflating the estimate before `Historical.timeseries.get_range` resolves ambiguity and fails per-symbol. Plan mitigation: run `get_cost` AFTER per-symbol Databento symbol-resolution check (mirrors PR #44's ambiguity detection). If a symbol is ambiguous, return `estimate_confidence="low"` for that symbol's line item.
3. **`get_cost` mode= param deprecation.** The SDK still accepts `mode=`, but it will be removed. **Don't pass it.** If our pinned version jumps past the removal release post-v1, linting for stale usage prevents surprise breakage.
4. **arq `allow_abort_jobs` flip to default.** The docs say "`allow_abort_jobs=True` may become default in future." If a future arq version flips this, our resources governance changes (memory/time per worker accounting). Low risk; flag for dependency-update review.
5. **YAML 1.1 vs 1.2 trip hazards** (PyYAML default): `Yes`/`No`/`On`/`Off` become booleans; `01:23` becomes a base-60 integer. Our schema uses strings for symbols (`SPY`, `ES.n.0`) — no direct collision, but guard against it with Pydantic `StrictStr`.
6. **Databento nightly publication window** (found by PR #44 verify-e2e). Preflight with `end=today_utc.date()` MAY fail 4xx between UTC midnight and daily publication. Mitigation: CLI's `trailing_5y` sugar should default `end=today - 1d`, not `today`. Document in CLI help.
7. **On-the-fly coverage scan scales with symbol count × month count.** A 2000-symbol × 10-year watchlist = 240k `Path.stat()` calls on GET `/readiness?window=...`. Today we ship 20–50 symbol watchlists; revisit caching when a user hits >500 symbols in one watchlist.
8. **PyYAML is a net-new dependency** — adds a transitive libyaml (C) install. On Alpine-based Docker images this may require `apk add yaml-dev`. Our current `python:3.12-slim-bookworm` base has `libyaml-0-2` already; no change needed. Flag for future Dockerfile migrations.
9. **`OnboardingJob.per_symbol` JSONB hot-path updates.** Under Postgres MVCC, N serial row updates during a 20-symbol job = 20 tuple rewrites on the same row. At 20 symbols this is fine; at 2000 it creates vacuum pressure. Consider moving to a `onboarding_job_symbols` child table (one INSERT per symbol, no UPDATE) if watchlist size grows.
10. **Idempotency key collision risk.** Deterministic `_job_id` hash from `(watchlist_name, symbol_tuple, start, end)` MUST be stable across Python interpreter restarts — use `hashlib.blake2b` (matches PR #44 `compute_advisory_lock_key`), NOT Python's built-in `hash()` which is PYTHONHASHSEED-randomized.
11. **Databento rate limits are vendor-silent.** Mirrors PR #44 risk — if Databento enforces a metadata-call QPS cap we don't know about, a 50-symbol dry-run (batched to 1 call per dataset) is below any reasonable cap. A 2000-symbol multi-dataset dry-run could hit one. Tenacity retry + `max_concurrent=3` on the metadata calls (mirror `DatabentoBootstrapService`) is the right bounded behavior.

Sources:

- [Databento Historical.metadata.get_cost docs](https://databento.com/docs/api-reference-historical/metadata/metadata-get-cost)
- [databento-python on PyPI](https://pypi.org/project/databento/)
- [databento-python CHANGELOG](https://github.com/databento/databento-python/blob/main/CHANGELOG.md)
- [Databento pricing FAQ](https://databento.com/docs/faqs/usage-pricing-and-data-credits)
- [arq docs v0.28.0](https://arq-docs.helpmanual.io/)
- [arq PR #212 (cancel jobs)](https://github.com/python-arq/arq/pull/212/files)
- [arq issue #363 (abort hangs with keep_result=0)](https://github.com/python-arq/arq/issues/363)
- [PyYAML yaml.load() deprecation](<https://github.com/yaml/pyyaml/wiki/PyYAML-yaml.load(input)-Deprecation>)
- [ruamel.yaml on PyPI](https://pypi.org/project/ruamel.yaml/)
- [Semgrep: vulnerable PyYAML versions](https://semgrep.dev/blog/2022/testing-vulnerable-pyyaml-versions/)
- [FastAPI async docs](https://fastapi.tiangolo.com/async/)
- [Pydantic V2 Version Policy](https://docs.pydantic.dev/latest/version-policy/)
- [Pydantic V2 Validators](https://docs.pydantic.dev/latest/concepts/validators/)
- [FastAPI + ARQ pattern (Muraya)](https://davidmuraya.com/blog/fastapi-background-tasks-arq-vs-built-in/)
- Prior MSAI research: `docs/research/2026-04-23-databento-registry-bootstrap.md`, `docs/research/2026-04-21-backtest-auto-ingest-on-missing-data.md`
