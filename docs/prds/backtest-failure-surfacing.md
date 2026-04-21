# PRD: Backtest Failure Surfacing

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-04-20
**Last Updated:** 2026-04-20

---

## 1. Overview

When a backtest fails in the worker, the root-cause reason lives in `backtests.error_message` but is discarded at every presentation boundary — API response, CLI output, and UI badge all show only `status: "failed"` with no details. The user has to open container logs to find out why. This PR introduces a structured, classified failure envelope that flows unchanged from worker → DB → API → CLI and UI, with a machine-readable `remediation` hook that the forthcoming auto-ingest PR will turn into a one-click "Ingest now" button.

## 2. Goals & Success Metrics

### Goals

- **Primary:** Every `failed` backtest surfaces a human-readable reason on API + CLI + UI without requiring log access.
- **Secondary:** Failure classification uses a stable `FailureCode` enum so downstream automation (auto-ingest PR, alerting, telemetry) can branch without parsing prose.
- **Tertiary:** For the common `missing_data` case, the server derives and emits the exact `msai ingest ...` remediation command + structured remediation metadata.

### Success Metrics

| Metric                                                                                                      | Target                           | How Measured                            |
| ----------------------------------------------------------------------------------------------------------- | -------------------------------- | --------------------------------------- |
| Fraction of `failed` backtests with a non-`unknown` `FailureCode`                                           | ≥ 90% (on first common-path run) | Postgres query against seeded test data |
| `GET /backtests/{id}/status` response for a `failed` row contains `error.code` + `error.message`            | 100%                             | Contract test; E2E UC-BFS-001           |
| `msai backtest show <id>` prints the failure envelope (not just `status: failed`)                           | 100%                             | E2E UC-BFS-003                          |
| UI `/backtests` list-row badge tooltip shows first 150 chars of `error_public_message` on `failed` rows     | 100%                             | E2E UC-BFS-002 (Playwright)             |
| `remediation.kind == "ingest_data"` for a failure triggered by the "No raw Parquet files found" worker path | 100%                             | Integration test on `_classify_failure` |

### Non-Goals (Explicitly Out of Scope)

- ❌ **Auto-ingest on missing data** — separate follow-up PR with its own council. The `remediation.auto_available` field is the forward-compat hook but stays `false` everywhere in this PR.
- ❌ **Research-jobs + live-deployments failure surfacing** — same pattern, out of scope here. Filed as 1-hour follow-ups each.
- ❌ **Retry / re-run from the UI** — not added.
- ❌ **Multi-tenant sanitization rigor** — this is a single-user system; sanitization targets obvious leaks (filesystem paths, connection strings surfaced in exception messages) but doesn't try to defeat a determined adversary.
- ❌ **Alerting routing changes for backtest failures** — alerting currently fires from the live path only. Whether backtest failures should alert (at all, or by code) is a post-PR decision, not a PR requirement.
- ❌ **i18n of user-facing messages** — English only, consistent with the rest of the product.

## 3. User Personas

### Trader (Pablo)

- **Role:** Sole user — designs strategies, launches backtests, monitors portfolio.
- **Permissions:** Full access (single-user system).
- **Goals:** Diagnose why a backtest failed in <30 seconds without opening container logs or Postgres.

## 4. User Stories

### US-001: Failure reason on the backtest history list

**As the** Trader
**I want** the `failed` badge on each history row to expose a short, human-readable reason on hover
**So that** I can decide whether to retry without opening the detail page

**Scenario:**

```gherkin
Given a backtest submitted earlier has worker-status "failed" with error_code="missing_data"
When I load GET /api/v1/backtests/history
Then each failed item includes `error_code` and `error_public_message`
And when I navigate to the /backtests UI page
And hover the "failed" badge on that row
Then I see a tooltip with the first 150 chars of `error_public_message`
```

**Acceptance Criteria:**

- [ ] `BacktestListItem` schema (existing) or its replacement exposes `error_code: str | None` and `error_public_message: str | None`, both null for non-failed rows.
- [ ] `error_public_message` passes the sanitization pass (no raw filesystem paths like `/app/data/...`, no stack traces).
- [ ] Frontend `/backtests/page.tsx` renders the badge with a shadcn `Tooltip` primitive when the row is `failed` AND `error_public_message` is present.
- [ ] If `error_public_message` is longer than 150 chars, it's truncated with ellipsis in the tooltip; the full text is on the detail page.

**Edge Cases:**

| Condition                                                                         | Expected Behavior                                                                                           |
| --------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Historical row (pre-migration) with `error_message` populated but no `error_code` | Backfill reads as `error_code = "unknown"` on the fly; `error_public_message` is sanitized `error_message`. |
| Failed row with `error_message = null`                                            | Tooltip not rendered; badge shows "failed" alone (shouldn't happen in practice but safe).                   |
| `failed` badge on completed-but-empty backtest (0 trades)                         | Out of scope — that row is `completed`, not `failed`.                                                       |

**Priority:** Must Have

---

### US-002: Full structured envelope on the detail page

**As the** Trader
**I want** the `/backtests/[id]` page for a failed backtest to show the full structured failure envelope with a copyable `suggested_action` command
**So that** I can fix the underlying cause and re-run without retyping

**Scenario:**

```gherkin
Given a backtest with error_code="missing_data" and a derivable remediation command
When I navigate to /backtests/[id] for that row
Then I see:
  - a "Failure" section with error_code + error_public_message
  - a "Suggested action" block containing the exact CLI command (copy button)
  - optionally, a structured remediation preview (symbols / date range) for future auto-ingest
```

**Acceptance Criteria:**

- [ ] `/backtests/[id]` page renders a `<Card>` titled "Failure" when `status === "failed"`.
- [ ] `error_code` rendered as a monospace badge (e.g., `MISSING_DATA`).
- [ ] `error_public_message` rendered as plain text.
- [ ] `suggested_action` — if present — rendered in a `<pre><code>` block with a copy-to-clipboard button.
- [ ] `remediation` — if present and `remediation.kind === "ingest_data"` — renders symbols + date range as a small detail list (no "Ingest now" button in this PR).
- [ ] Failure envelope **is** visible for pre-migration `failed` rows (degraded gracefully to `error_code = "unknown"` + the raw `error_message`).

**Edge Cases:**

| Condition                                | Expected Behavior                                                                      |
| ---------------------------------------- | -------------------------------------------------------------------------------------- |
| `suggested_action` is null               | Entire "Suggested action" block is hidden — NO "No suggestions available" placeholder. |
| `remediation` is null                    | Entire "Structured remediation" block is hidden.                                       |
| Backtest row not `failed`                | "Failure" card not rendered at all.                                                    |
| `error_public_message` contains newlines | Rendered preserving whitespace (`whitespace-pre-wrap`).                                |

**Priority:** Must Have

---

### US-003: CLI parity with the API

**As the** Trader using the CLI
**I want** `msai backtest show <id>` to print the structured failure envelope
**So that** I have diagnostic parity across every surface without having to open the UI

**Scenario:**

```gherkin
Given a failed backtest with error_code="missing_data"
When I run "msai backtest show <id>"
Then stdout contains a `"error": { "code": "missing_data", "message": "...", "suggested_action": "...", "remediation": {...} }` block within the status JSON payload
```

**Acceptance Criteria:**

- [ ] CLI `msai backtest show <id>` output includes the same `error` envelope the API returns from `/backtests/{id}/status`.
- [ ] No CLI-side reformatting — the CLI prints the API response JSON verbatim (current behavior).
- [ ] If `status !== "failed"`, `error` field is absent (not `null`, absent).

**Edge Cases:**

| Condition          | Expected Behavior                                               |
| ------------------ | --------------------------------------------------------------- |
| API is down        | CLI errors out with existing network-error message (unchanged). |
| Backtest not found | CLI prints existing 404 handling (unchanged).                   |

**Priority:** Must Have

---

### US-004: API contract stability

**As** any API client (CLI, UI, future integrations)
**I want** the failure envelope to be part of the documented `/backtests/{id}/status` response schema
**So that** I can rely on stable field names and types across releases

**Scenario:**

```gherkin
Given a failed backtest in the DB
When I GET /api/v1/backtests/{id}/status
Then the response body conforms to `BacktestStatusResponse` which includes an optional `error: ErrorEnvelope | None` field
And `ErrorEnvelope` is documented in the OpenAPI schema with `code`, `message`, `suggested_action`, `remediation` fields
```

**Acceptance Criteria:**

- [ ] `ErrorEnvelope` Pydantic model defined in `backend/src/msai/schemas/backtest.py`.
- [ ] `BacktestStatusResponse.error: ErrorEnvelope | None` added.
- [ ] `BacktestListItem` gets compact fields `error_code: str | None` + `error_public_message: str | None` (NOT the full envelope — bandwidth).
- [ ] `GET /backtests/history` returns these fields populated on `failed` rows, null on non-failed rows.
- [ ] OpenAPI schema served at `/openapi.json` reflects the new models (automatic from FastAPI + Pydantic).

**Edge Cases:**

| Condition                                              | Expected Behavior                                                                            |
| ------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| Existing API consumer doesn't know about `error` field | Forward compatibility — adding optional fields is non-breaking. Confirmed via contract test. |
| `error.code` value the client hasn't heard of          | UI renders it verbatim; CLI pass-through. Codes are documented in `docs/architecture/`.      |

**Priority:** Must Have

---

### US-005: Machine-readable remediation metadata

**As a** future auto-ingest feature (upstream of this PR)
**I want** the `remediation` field to carry structured data (not just prose)
**So that** I can wire "Ingest now" UI actions without re-parsing CLI command strings

**Scenario:**

```gherkin
Given a backtest with error_code="missing_data" and symbols=["ES.n.0"] date_range=2025-01-02..2025-01-15
When the failure is classified
Then error_remediation = {
  "kind": "ingest_data",
  "symbols": ["ES.n.0"],
  "asset_class": "futures",
  "start_date": "2025-01-02",
  "end_date": "2025-01-15",
  "auto_available": false
}
And the API serializes it as-is
```

**Acceptance Criteria:**

- [ ] `remediation` stored as JSONB on the `backtests` table under `error_remediation`.
- [ ] Classifier populates `remediation` for `missing_data` code; null for other codes in this PR.
- [ ] `auto_available` is **always** `false` in this PR (MVP hook — future PR flips it when wiring succeeds).
- [ ] Schema typed via Pydantic `Remediation` model with `kind: Literal["ingest_data", "contact_support", "retry", "none"]`.
- [ ] No UI action taken on `remediation` in this PR beyond displaying the structured data (US-002 acceptance).

**Priority:** Must Have (the forward-compat hook Codex flagged — cheap now, enormous unblock later)

---

### US-006: Graceful null-safe handling of historical rows

**As the** Trader
**I want** previously-failed backtest rows (pre-migration) to still show useful failure information
**So that** the migration doesn't create a dead zone in history

**Scenario:**

```gherkin
Given a backtest row written before this PR with status="failed" + error_message populated
When I GET /backtests/{id}/status
Then response includes error = { code: "unknown", message: <sanitized error_message>, suggested_action: null, remediation: null }
```

**Acceptance Criteria:**

- [ ] Migration backfills `error_code = "unknown"` for existing `failed` rows (`UPDATE ... WHERE status='failed' AND error_code IS NULL`). Alternative: leave the column NULL and handle at read-time.
- [ ] Null-safe read path in the API — if `error_code` is null, treat as `unknown`.
- [ ] Null-safe read path — if `error_public_message` is null but `error_message` exists, sanitize-on-read + populate.
- [ ] Unit test covers the pre-migration shape path.

**Priority:** Must Have

---

## 5. Technical Constraints

### Known Limitations

- The worker's current `_mark_backtest_failed(backtest_id, error_message)` signature is minimal (`str` message only). This PR extends it to accept structured fields, which is a call-site change at 3 call-sites in `backend/src/msai/workers/backtest_job.py`.
- Sanitization is best-effort heuristic (path regex, stack-trace trimming). A future PR may formalize it.
- `FailureCode` classification relies on `isinstance(exc, ...)` + message regex. Worker failures without a recognizable exception type land as `unknown`. Acceptable per Q6 / US-006.

### Dependencies

- **Requires:** `backtests.error_message` column already exists and is populated (existing behavior).
- **Blocked by:** None.
- **Future consumer:** The auto-ingest follow-up PR will read `error_remediation.auto_available` and `error_remediation.symbols/...` to wire a UI button.

### Integration Points

- **Backend `backtest_job.py` worker:** classifier lives here; runs at `_mark_backtest_failed` time.
- **Backend `api/backtests.py`:** `/status` + `/history` endpoints add error envelope to responses.
- **Frontend `/backtests/page.tsx`:** badge tooltip with truncated message.
- **Frontend `/backtests/[id]/page.tsx`:** full failure card.
- **CLI `backtest_app.command("show")`:** unchanged — prints API response JSON verbatim, so it inherits the envelope for free.

## 6. Data Requirements

### New Data Models

- **`FailureCode` (Python StrEnum):** `missing_data`, `missing_strategy_data_for_period`, `strategy_import_error`, `engine_crash`, `timeout`, `config_rejected_at_worker`, `unknown`. Stored as `String(32)` in Postgres.
- **`Remediation` (Pydantic model):**
  ```python
  class Remediation(BaseModel):
      kind: Literal["ingest_data", "contact_support", "retry", "none"]
      symbols: list[str] | None = None
      asset_class: str | None = None
      start_date: date | None = None
      end_date: date | None = None
      auto_available: bool = False
  ```
- **`ErrorEnvelope` (Pydantic model):** `code: str`, `message: str`, `suggested_action: str | None`, `remediation: Remediation | None`.

### DB Schema Changes (Alembic migration)

Columns added to `backtests`:

| Column                   | Type         | Nullable | Default     | Index |
| ------------------------ | ------------ | -------- | ----------- | ----- |
| `error_code`             | `String(32)` | NO       | `'unknown'` | —     |
| `error_public_message`   | `Text`       | YES      | NULL        | —     |
| `error_suggested_action` | `Text`       | YES      | NULL        | —     |
| `error_remediation`      | `JSONB`      | YES      | NULL        | —     |

Data migration step: backfill `error_code='unknown'`, `error_public_message=<sanitize(error_message)>` for existing `status='failed'` rows.

### Data Validation Rules

- `error_code`: must be a member of `FailureCode` at write time (worker enforces).
- `error_public_message`: max 4 KB (text column, no explicit cap but classifier truncates to 1 KB).
- `error_remediation`: valid JSONB conforming to the `Remediation` schema (worker builds it via Pydantic → `.model_dump()`).

### Data Migration

- Alembic upgrade: add 4 columns, run backfill UPDATE, set `error_code` NOT NULL with DEFAULT.
- Alembic downgrade: drop the 4 columns. No data loss (the original `error_message` column stays).

## 7. Security Considerations

- **Authentication:** Unchanged — existing Entra ID JWT / `X-API-Key` required on all backtest endpoints.
- **Authorization:** Unchanged — single-user system.
- **Data Protection:** Sanitization pass strips obvious leaks before populating `error_public_message`:
  - Absolute container paths `/app/data/...` → `<DATA_ROOT>/...`
  - Absolute home paths `/Users/.../.../` → `<HOME>/...`
  - Python stack-trace lines `File "..../msai/...", line NN` → trimmed to last frame only
  - Known secret-shaped patterns (DSN, API-key, JWT) → `<redacted>`
- **Audit:** All failures already emit a structured log entry via `logger.error("backtest_missing_data", ...)`; this PR adds `error_code` to the log fields. No new audit-log entry types needed.
- **Secrets in `error_message`:** The raw `error_message` column still contains unsanitized text. If sensitive data ever lands there, DB backup dumps contain it. Acceptable for single-user system; flagged as a future concern.

## 8. Open Questions

- [ ] **Q-O1:** Should backtest failures with `error_code` in {`engine_crash`, `timeout`, `unknown`} fire an alert via `alerting_service`? My lean: YES (these are unexpected and the trader should be notified). Pin in plan-review.
- [ ] **Q-O2:** Should `GET /backtests/history` items return the full `ErrorEnvelope` (with `suggested_action` + `remediation`) or just `{error_code, error_public_message}`? Lean compact on list (bandwidth + user opens detail anyway), full on detail.
- [ ] **Q-O3:** Exact classifier regex patterns for `missing_data` detection — the worker's current message is `"No raw Parquet files found for '{symbol}' under /app/data/parquet/{asset_class}/{symbol}. Run the data ingestion pipeline for this symbol before backtesting."`. Classifier should match this string + extract `{symbol}` + `{asset_class}` to build `Remediation`. Lock in plan-review.
- [ ] **Q-O4:** Should the CLI get a human-friendly rendering mode (not raw JSON) for failed backtests? Deferred — current raw-JSON output is a CLI-wide convention; changing one command breaks consistency.

## 9. References

- **Discussion Log:** `docs/prds/backtest-failure-surfacing-discussion.md`
- **Related PRDs:** `docs/prds/strategy-config-schema-extraction.md` (immediately-prior PR; surfaces the top-level `{error: ...}` 422 envelope this PR's failure envelope is deliberately symmetric with).
- **Codebase hot-points:**
  - `backend/src/msai/workers/backtest_job.py` — 3 failure call-sites + `_mark_backtest_failed`.
  - `backend/src/msai/schemas/backtest.py` — response schemas.
  - `backend/src/msai/models/backtest.py` — `error_message` column (existing); 4 new columns added here.
  - `frontend/src/app/backtests/page.tsx` — list view.
  - `frontend/src/app/backtests/[id]/page.tsx` — detail view.
- **Convention reference:** `.claude/rules/api-design.md` — top-level `{error: {code, message, details}}` pattern; this PR's `ErrorEnvelope` deliberately mirrors it.

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                                                                                            |
| ------- | ---------- | -------------- | -------------------------------------------------------------------------------------------------- |
| 1.0     | 2026-04-20 | Claude + Pablo | Initial PRD. Incorporates 6 discussion Qs + Codex-flagged Q7 (machine-readable remediation field). |

## Appendix B: Approval

- [ ] Product Owner (Pablo) approval
- [ ] Technical design (Phase 3) entry — `/superpowers:brainstorming`
- [ ] Ready for technical design
