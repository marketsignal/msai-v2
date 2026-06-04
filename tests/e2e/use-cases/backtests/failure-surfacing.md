# Use Cases — Backtest Failure Surfacing

Graduated from PR `feat/backtest-failure-surfacing` after PASS 5/5 at 2026-04-21T12:07Z (report: `tests/e2e/reports/2026-04-21-12-07-backtest-failure-surfacing.md`).

Every use case assumes:

- Dev stack up (`docker compose -f docker-compose.dev.yml up -d`)
- `MSAI_API_KEY=msai-dev-key` in the environment
- `example.ema_cross` strategy registered (from `strategies/example/ema_cross.py`)
- UC-BFS-001 relies on the auto-heal **guardrail** (>10-year window rejected before any ingest), not on ES being un-ingested — so it is deterministic regardless of accumulated dev Parquet state (auto-ingest, PR #40, auto-heals normal cold paths).

---

## UC-BFS-001 — Failed backtest exposes structured envelope via API (guardrail short-circuit)

> **Changelog 2026-06-04 (UC-BFS-001 retarget):** the old cold-path
> `missing_data` trigger (submit a never-ingested symbol → fail) was
> superseded by auto-ingest (PR #40): the cold path now **auto-heals**, and a
> provider/ingest failure classifies as `engine_crash` (per UC-BAI-003) — so
> the original UC no longer deterministically produces `missing_data`. This UC
> is retargeted to the **guardrail short-circuit path** (a >10-year `ES.n.0`
> window), which fails BEFORE auto-ingest is ever enqueued and is therefore a
> deterministic `missing_data` envelope. This mirrors UC-BAI-002. The structured
> envelope contract (`code` / `message` / `suggested_action` / `remediation`)
> is what's under test here — the same contract regardless of which path raised it.

**Interface:** API

**Setup (ARRANGE):** authenticated client, the example EMA cross strategy id.

**Steps:**

1. `POST /api/v1/backtests/run` with `instruments=["ES.n.0"]`, `start_date=2013-01-01`, `end_date=2024-12-31` (~12 years — exceeds the 10-year auto-heal guardrail cap), any valid EMA config.
2. Wait ~5 seconds for the worker to process (guardrail short-circuit is near-instant — no provider round-trips).
3. `GET /api/v1/backtests/{id}/status`.

**Verification:**

- HTTP 200.
- `body.status == "failed"`.
- `body.error.code == "missing_data"`.
- `body.error.message` mentions the year cap (contains "year" and "10"), and does NOT contain `/app/` (sanitizer stripped container path).
- `body.error.suggested_action` starts with `"Run: msai ingest"`.
- `body.error.remediation.kind == "ingest_data"` with `symbols` array + `start_date` / `end_date` matching submission + `auto_available == false` (guardrail short-circuits before any auto-ingest enqueue).
- `body.error.remediation.asset_class == "futures"` (derived server-side from `ES.n.0`).

**Persistence:** GET again — identical envelope returned (stable read; idempotent).

---

## UC-BFS-002 — CLI `msai backtest show` prints envelope verbatim

**Interface:** CLI

**Setup:** a failed backtest exists (from UC-BFS-001 or any historical row).

**Steps:**

1. `docker exec msai-claude-backend /app/.venv/bin/python -m msai.cli backtest show <bt-id>`.

**Verification:**

- Exit code 0.
- Stdout JSON matches the API response byte-for-byte — the CLI is a pass-through, no translation layer. The `error` block is present with `code`, `message`, `suggested_action`, `remediation`.

**Persistence:** re-running the command prints identical output.

---

## UC-BFS-003 — History endpoint exposes compact error fields

**Interface:** API

**Setup:** at least one `failed` row in history.

**Steps:**

1. `GET /api/v1/backtests/history?page_size=10`.

**Verification:**

- HTTP 200.
- Each `failed` row has `error_code` + `error_public_message` populated.
- Non-failed rows have these fields absent (stripped by `response_model_exclude_none=True`).
- `suggested_action` and `remediation` are NOT in list items (bandwidth discipline — only on the detail endpoint).

**Persistence:** paginate to the next page — fields render consistently.

---

## UC-BFS-004 — Non-failed `/status` response omits the `error` key

**Interface:** API

**Setup:** submit a backtest and immediately fetch before worker processing.

**Steps:**

1. `POST /api/v1/backtests/run` (any valid config).
2. `GET /api/v1/backtests/{id}/status` within ~500ms.

**Verification:**

- HTTP 200.
- `body.status == "pending"` (or `"running"`).
- `"error" not in body` — key is ABSENT, not null. PRD contract per US-003.
- `"started_at" not in body` until worker flips `status` to `"running"` — same exclude-none behavior (TS types match with `started_at?`).

**Persistence:** eventually the worker transitions the row; re-fetching shows the failed envelope per UC-BFS-001.

---

## UC-BFS-005 — UI: history tooltip + nav link → FailureCard on detail page

**Interface:** UI (Playwright MCP)

**Setup:** Navigate to `http://localhost:3300/backtests` with at least one `failed` row visible.

**Steps:**

1. Confirm 0 console errors on page load (catches useAuth useEffect regressions).
2. Locate failed-row status cell — it renders as an accessible `<button>` element (not a plain span). The `aria-label` carries the first ~80 chars of the sanitized error message.
3. Verify the action-cell of a failed row contains a `View failure details` link (`data-testid="backtest-detail-link-<id>"`).
4. Click the link.
5. Verify `/backtests/<id>` renders a `<FailureCard>` (`data-testid="backtest-failure-card"`).
6. Check the following data-testid elements on the card:
   - `backtest-error-code` → `"MISSING_DATA"` (uppercased)
   - `backtest-error-message` → full sanitized message (no `/app/`)
   - `backtest-error-suggested-action` → `"Run: msai ingest stocks ..."`
   - `backtest-error-copy-button` → present + aria-labelled "Copy command"
   - Remediation details list → Symbols / Asset class / Date range rows

**Verification:** every data-testid resolves + content matches the API envelope.

**Persistence:** `page.reload()` — FailureCard re-renders with identical content.

---

## Expected failure modes when running against a dev stack

- **Stack down:** `curl /health` returns connection-refused. Bring up with `docker compose -f docker-compose.dev.yml up -d`.
- **Strategy not registered:** `api/v1/strategies/` returns empty. Add a sample strategy under `strategies/` and restart backend.
- **UC-BFS-001 returns `completed` or `engine_crash` instead of `missing_data`:** that means the window was inside the 10-year guardrail cap and auto-ingest (PR #40) took over. Keep the >10-year window (`2013-01-01`→`2024-12-31`) so the guardrail short-circuits BEFORE auto-ingest — that is the deterministic `missing_data` path now.
- **Radix Tooltip on mobile:** tooltip won't open on touch (WAI-ARIA design). Mobile users should reach the failure via UC-BFS-005 click-through. Flag FAIL_STALE if spec is ever rewritten to require mobile tooltip display.

## Known limitations (documented scope-defer in this PR)

- UI's Run Backtest form does NOT send `config.asset_class`; worker defaults to `"stocks"`. For a futures backtest launched via UI, `remediation.asset_class` will read `"stocks"` and `suggested_action` will say `msai ingest stocks` instead of `msai ingest futures`. Core feature (user sees WHY + which symbols) still works. Follow-up PR: UI dropdown OR server-side inference from resolved canonical instrument ID.
