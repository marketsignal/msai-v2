# E2E Use Case — `msai backtest smoke` (CLI, smoke:fast)

**Feature:** Operational workflow smoke (PRD `docs/prds/ingest-backtest-smoke-test.md` v1.3).
**Maps to:** PRD US-001.
**Interface:** CLI.

---

## UC-SMK-001 — Operator runs the smoke from the prod VM CLI and reads metrics from stdout

**Actor:** Operator (Pablo) SSHed into the prod Azure VM, sitting at the project repo root with a working `.env` (Databento key, DB, Redis available) and the existing `MSAI_API_KEY` already exported.

**Scenario:** Pablo just deployed a new image and wants to confirm the ingest → portfolio-backtest → metrics pipeline still works end-to-end on real Databento data before he trusts it for tomorrow's trading session. He needs the structured risk metrics (return, P&L, Sharpe, Sortino, alpha vs SPY, beta vs SPY, max drawdown, trade-count per strategy) printed directly to stdout — not buried in a QuantStats HTML — so he can confirm in under three minutes and move on.

**Interface:** CLI

**Intent:** The operator fires a single command and, within minutes, sees a structured risk-metrics block plus a PASS line that tells him whether the pipeline is still healthy on real AAPL + SPY data.

**Setup:**

1. Pablo is SSHed into the prod VM at `/opt/msai-v2` with the existing deploy-credential session (no new credential model).
2. `MSAI_API_KEY` is exported (the dev/CLI shortcut documented in `CLAUDE.md ## Environment Variables`).
3. The `docker compose -f docker-compose.prod.yml` stack is running (backend, postgres, redis, workers — verified via `curl -sf http://localhost:8800/health` returns 200).
4. The Alembic migration has run on this VM (verified via `cd backend && uv run alembic current` returning the smoke migration revision id), so the four canonical `__smoke__/...` Strategy rows are seeded.
5. (Warm path) The Parquet files for AAPL + SPY 2024-12-01 → 2024-12-31 are already on disk under `DATA_ROOT/parquet/stocks/AAPL/2024/12.parquet` and `.../SPY/2024/12.parquet`.

**Steps:**

1. From the repo root, Pablo runs `cd backend && uv run msai backtest smoke`.
2. He watches stdout for the per-stage status lines (`ingest-check` → `ingest` → `backtest-submit` → `backtest-poll` → `report-check`).
3. Once the run reaches a terminal state, he runs `cd backend && uv run msai backtest smoke --json` to capture the same metrics block as a single JSON document on stdout (suitable for piping to `jq`).
4. He re-invokes `cd backend && uv run msai backtest smoke --config nightly` later in the day to confirm the deeper variant against the 2024 full-year window resolves the `smoke:nightly` config and lands a distinct run.

**Verification:**

- After step 1, stdout shows the per-stage status lines in order (e.g., `ingest-check: HIT`, `backtest-submit: portfolio_id=... run_id=...`, `backtest-poll: status=running`, ..., `backtest-poll: status=completed`).
- Stdout then prints a human-readable risk-metrics table with named rows: `Total Return`, `P&L ($)`, `Sharpe`, `Sortino`, `Alpha vs SPY`, `Beta vs SPY`, `Max Drawdown`, `Trade Count (smoke_market_order/AAPL)`, `Trade Count (smoke_market_order/SPY)`, `Trade Count (ema_cross/AAPL)`, `Trade Count (ema_cross/SPY)`, `Trade Count (total)`, `Benchmark: SPY`, `Backtest id: <UUID>`, `Report path: /app/data/reports/...`.
- The final line reads `PASS — PortfolioRun <UUID>` and the process exits 0.
- After step 3, `--json` stdout is a single parseable JSON document (`jq .total_return` returns a number; `jq .trade_count_total` returns ≥ 2; `jq .benchmark_symbol` returns `"SPY"`; `jq .smoke_config` returns `"fast"`). No human-readable framing precedes or follows the JSON.
- After step 4, the `--config nightly` invocation's stdout prints the same metrics block shape but with `smoke_config: nightly` and a wider date range; `Backtest id` is distinct from step 1's id.

**Persistence:** From the same shell (or a fresh SSH session), Pablo re-invokes `cd backend && uv run msai backtest smoke --json` and pipes through `jq -r '.id'` to capture the new run id. He then runs `curl -sf -H "X-API-Key: $MSAI_API_KEY" "http://localhost:8800/api/v1/backtests/history?smoke_only=true" | jq '.items[] | select(.smoke == true) | {id, smoke, smoke_config: .metrics.smoke_config}'` — the three runs from steps 1, 3, and 4 (all `smoke=true`) appear in the history with the captured ids, the original step-1 id is still listed with `smoke_config: "fast"`, and step-4's id is listed with `smoke_config: "nightly"`. The smoke runs persist across shell sessions and across a stack restart (`docker compose -f docker-compose.prod.yml restart backend`).

**Expected failure modes:**

- Backtest worker container not running → stage line reads `backtest-submit: FAIL — worker unreachable`; process exits non-zero with remediation hint pointing to `docker compose ps`.
- Databento auth missing / invalid on cold path → stage line reads `ingest: FAIL — DATABENTO_API_KEY not set or invalid`; process exits non-zero; no PortfolioRun row written.
- Fewer than 2 trades total (the deterministic floor — one `smoke_market_order` order each on AAPL and SPY guaranteed) → final line reads `FAIL — trade_count_total=<N> below floor 2`; process exits non-zero. This is the structural FAIL that catches order-submission / instrument-resolution breakage.

**Notes for verify-e2e:**

- Run on a host with the dev stack up (`docker compose -f docker-compose.dev.yml up -d`). The "prod VM" framing of the Actor is the production scenario; for local verification, the dev compose is the sanctioned equivalent.
- `MSAI_API_KEY=msai-dev-key` is the documented dev shortcut.
- Warm-path budget is ≤ 3 min p95; if the run exceeds 10 min wall-clock, classify as FAIL_INFRA and retry once.

---
