# PRD Discussion: Backtest Failure Surfacing

**Status:** In Progress
**Started:** 2026-04-20
**Participants:** Pablo, Claude

## Original User Stories

Derived from the verbal scope given at the tail of PR #38:

> "the errors via api, cli and UI should say so, or just download the data automatically, the system should be smart"

Agreed split:

- **This PR (Option b):** clear failure surfacing across API / CLI / UI for backtests.
- **Separate follow-up PR:** auto-ingest on missing data (needs its own council + design).

## Concrete failure observed on PR #38 dev stack

- User submits a backtest via the UI with `Instruments = ES.n.0`.
- `/backtests/run` returns 201 (submission OK).
- Worker log emits: `backtest_missing_data error="No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES. Run the data ingestion pipeline for this symbol before backtesting."`
- Worker writes that string to `backtests.error_message` (column exists).
- `GET /backtests/{id}/status` returns `{status: "failed", progress: 10, started_at, completed_at}` — NO error text.
- `GET /backtests/history` returns rows without error text either.
- UI badge shows `failed` with no tooltip, no details, no link to read more.
- CLI `msai backtest show <id>` prints the same info-free status JSON.

The failure reason exists in the DB but is dropped at every presentation boundary.

## Codebase starting state (as of branch-off commit `e47243d`)

| Surface                          | Has error_message?                                                         |
| -------------------------------- | -------------------------------------------------------------------------- |
| `backtests.error_message` column | YES (populated by worker on all 3 failure paths)                           |
| `BacktestStatusResponse` schema  | **NO** (fields: id, status, progress, started_at, completed_at)            |
| `BacktestListItem` schema        | **NO** (fields: id, strategy_id, status, start_date, end_date, created_at) |
| `BacktestResultsResponse` schema | **NO** (metrics + trades only)                                             |
| `msai backtest show` CLI         | Reads from `/status` → no error text                                       |
| `/backtests` UI page             | Renders status badge only                                                  |
| `/backtests/[id]` UI page        | Exists but currently only for "completed" rows                             |

## Worker failure taxonomy (current)

Three points where `_mark_backtest_failed(...)` is called in `backend/src/msai/workers/backtest_job.py`:

1. **Line 212** — `str(exc)` on any non-timeout, non-SIGTERM exception. Covers: missing data (the ES case), strategy import failure, NautilusTrader engine crash, config validation bypass, unexpected bugs.
2. **Line 220** — `f"Backtest timed out: {exc}"` for arq job timeout (wall-clock).
3. **Line 228** — `str(exc)` in an outer catch-all.

No structured error codes. No remediation hints.

## Questions to pin the design

### Q1 — Error shape: raw string or structured envelope?

Two options for how `/backtests/{id}/status` exposes failures:

- **(a) Raw string** — add `error_message: str | None` to the status response. Minimal change; whatever the worker wrote shows up verbatim.
- **(b) Structured envelope** — `error: {code: FailureCode, message: str, suggested_action: str | None}`. Requires the worker to classify failures into a small enum (`MISSING_DATA / IMPORT_ERROR / ENGINE_CRASH / TIMEOUT / UNKNOWN`). Enables UI affordances like "Click to ingest missing data" (deferred to the auto-ingest PR but the hook is there).

**My recommendation: (b).** The code-level classification is cheap (~4 regex/isinstance checks in `_mark_backtest_failed`), and it's the structural foundation for the auto-ingest follow-up. Without codes, the auto-ingest PR would have to regex-parse prose — not safe.

**Pick (a), (b), or something else?**

### Q2 — Remediation hints

For a `MISSING_DATA` failure where the user submitted `ES.n.0` and we can see the Parquet path was empty, should the server produce an exact remediation command?

- **(a) Generic hint** — `suggested_action: "No market data found for one or more instruments. Run the ingestion pipeline before retrying."`
- **(b) Specific command** — `suggested_action: "Run: msai ingest stocks ES 2025-01-02 2025-01-15"` (includes the exact CLI line with the failing symbols + window).
- **(c) None** — leave `suggested_action = null` for MVP; add in follow-up.

**Pick?**

### Q3 — Surface on history list, or detail only?

- **(a) Detail only** — the `/backtests` list still shows just the badge; user clicks a failed row to see the error on `/backtests/[id]`.
- **(b) Both** — the list badge becomes hover-able (tooltip with error_message); the detail page shows full structure.
- **(c) Inline on list** — a collapsible `<details>` under each failed row shows the reason without a click-through.

**Pick? (I lean (b) — tooltip on list + full structure on detail.)**

### Q4 — Sanitization

Worker errors may contain filesystem paths (`/app/data/parquet/stocks/ES`), import errors revealing internal module paths, stack traces, or (in rare cases) DB connection strings if a SQL error surfaces. For a single-user system this is harmless; for future multi-tenant it's a leak.

- **(a) Raw pass-through** — single-user dev/prod, no sanitization needed.
- **(b) Sanitize prefixes** — strip `/app/`, replace with `<DATA_ROOT>/`. Keeps the message useful without leaking container layout.
- **(c) Allow-list by failure code** — `MISSING_DATA` gets sanitized, `UNKNOWN` gets a generic "contact support" message without the raw exception.

**Pick? (I lean (a) — this is your box; raw is more useful for debugging.)**

### Q5 — Scope expansion?

Same bug almost certainly exists on:

- `/research/jobs/{id}/status` — research jobs have the same `error_message` column gap.
- `/live/deployments/{id}/status` — partially covered by `alerting_service`, but the API response itself may not expose the latest error.

Should this PR also cover those, or stay strictly backtest and file the others as follow-ups?

- **(a) Backtest only** — ship narrow, iterate.
- **(b) Backtest + research jobs** — both use the same worker pattern; the fix is parallel.
- **(c) All three surfaces** — larger blast radius, more to verify.

**Pick? (I lean (a) for speed; the other two land as 1-hour follow-ups each.)**

### Q6 — Historical rows (already-failed backtests)

Three existing `failed` rows on the dev stack have `error_message` populated. They'll Just Work once we expose the field. No migration or backfill needed.

Confirm no concern?

## Discussion Log

### 2026-04-20 — `/codex` verdict (Pablo delegated decision with prompt "decide, the system should be smart when possible and solve problems")

Codex gpt-5.4 @ xhigh reasoning reviewed Q1–Q6 and flagged one missing dimension. Pablo ratified the full set.

- **Q1 → structured envelope backed by a persisted stable failure code** on the `backtest` row. Classify at `_mark_backtest_failed` time, not via API-time regex. Envelope: `error: {code: FailureCode, message: str, suggested_action: str | None}`. Gives API/CLI/UI a real contract and keeps the auto-ingest follow-up clean.
- **Q2 → specific remediation command when the backend can derive it unambiguously, generic fallback otherwise**. "Smartest non-automated behavior in this PR". The same structured missing-data context will power the future "Ingest now" action.
- **Q3 → both list view + detail view**. List shows a short failure summary and failed rows navigate to detail; `/backtests/[id]` carries the full structured error + remediation because that's where copyable commands and future action buttons belong.
- **Q4 → code-aware sanitized public messages**. Strip/normalize filesystem paths, secrets, stack-trace noise from what API/CLI/UI see. Raw exception still goes to DB + structured logs. `UNKNOWN` gets a concise actionable "contact support / inspect logs" message, never useless.
- **Q5 → this PR stays backtest-scoped**. Research already exposes `error_message`; live has its own failure-kind contract via `alerting_service`. Widening this PR adds blast radius without proportional user value.
- **Q6 → no migration/backfill**, but historical rows need null-safe read handling. Old rows either classify best-effort on read OR fall back to `UNKNOWN` while still surfacing their stored `error_message`.
- **Q7 (NEW — Codex-identified, Pablo ratified) → machine-readable remediation metadata**. Add a structured `remediation: { kind: "ingest_data" | "contact_support" | "retry" | ..., symbols: list[str] | None, asset_class: str | None, start_date: date | None, end_date: date | None, auto_available: bool } | None` field alongside `suggested_action`. Lets the follow-up PR convert "run this command" to a one-click "Ingest now" button without redesigning the API contract.

## Refined Understanding

### Personas

- **Trader (Pablo)** — single user. Needs to diagnose why a backtest failed without reading container logs.

### User Stories (Refined)

- **US-001:** As the trader, when a backtest I just launched fails, I see a short human-readable reason in the `/backtests` history row (e.g., tooltip on the `failed` badge) so I can decide whether to retry.
- **US-002:** As the trader, when I click the failed row I land on a detail view that shows: structured failure code, the sanitized human-readable message, an exact `suggested_action` command when derivable (e.g., `msai ingest stocks ES 2025-01-02 2025-01-15`), and structured `remediation` metadata for future automation.
- **US-003:** As the trader using the CLI, `msai backtest show <id>` prints the same structured failure envelope so I have parity across surfaces.
- **US-004:** As the trader hitting the API directly, `GET /api/v1/backtests/{id}/status` returns the structured failure envelope for any `failed` row, and `GET /api/v1/backtests/history` returns a compact `error_code + error_message` on each failed row.
- **US-005 (upstream bug):** As the trader, when the classifier can't confidently match a failure to a known `FailureCode`, I still see the sanitized root-cause message — never a blank badge.

### Non-Goals (explicit)

- **Auto-ingest on missing data** — separate follow-up PR with its own council. The `remediation.auto_available` field is the forward-compat hook but stays `false` everywhere this PR.
- **Research-jobs + live-deployments failure surfacing** — same pattern, but out of scope here. Filed as 1-hour follow-ups each.
- **Retry / re-run from the UI** — not added in this PR.
- **Multi-tenant sanitization rigor** — single-user system; sanitization targets obvious leaks (paths, secrets in exception messages) but doesn't try to defeat a determined adversary.

### Key Decisions

1. Failure classification happens in the **worker** (at `_mark_backtest_failed` time), persisted to new columns on `backtests`. API/CLI/UI are pure readers.
2. New DB columns: `error_code: String(32) NOT NULL DEFAULT 'unknown'`, `error_suggested_action: Text NULL`, `error_remediation: JSONB NULL`. Existing `error_message: Text NULL` stays raw (populated by worker); a new sanitization pass produces `error_public_message: Text NULL` served to the client.
3. `FailureCode` is a Python `StrEnum` — seed values: `missing_data`, `missing_strategy_data_for_period`, `strategy_import_error`, `engine_crash`, `timeout`, `config_rejected_at_worker`, `unknown`. Small, extensible, documented.
4. `ingest_data` remediation kind is the only one we populate machine-readable fields for in MVP (it's the common case). Others get `remediation: null` + a generic `suggested_action`.
5. UI surfacing: tooltip on list-view badge (150-char truncated `error_public_message`); full structure on `/backtests/[id]` with a `<code>` block for the `suggested_action` command + a copy-to-clipboard button.

### Open Questions (Remaining)

- [ ] Exact `FailureCode` value names — to be locked in the plan's TDD task list.
- [ ] Whether to emit an alert via `alerting_service` on each backtest failure (live path already does). My lean: YES for `engine_crash` + `timeout` + `unknown` (unexpected), NO for `missing_data` + `config_rejected_at_worker` (user error, already visible in UI). Pin in plan-review.
- [ ] Whether `GET /backtests/history` items include the full structured envelope or just `{error_code, error_public_message}` (bandwidth concern for big lists). Lean compact on list, full on detail.

Ready for `/prd:create`.
