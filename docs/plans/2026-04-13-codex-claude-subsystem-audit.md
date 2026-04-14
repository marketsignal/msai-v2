# Codex vs Claude — Subsystem-by-Subsystem Audit

**Date:** 2026-04-13
**Context:** Post-hybrid-merge gap closure. Claude is the foundation; Codex features to port in order of priority.
**Method:** 4 parallel Explore agents audited 9 subsystems, produced structured finding per subsystem.

---

## Executive Summary

| #   | Subsystem                                            | Winner          | Scope | Priority | Decision                                     |
| --- | ---------------------------------------------------- | --------------- | ----- | -------- | -------------------------------------------- |
| 1   | Live command bus (Redis Streams + DLQ + idempotency) | **Claude**      | —     | —        | Keep Claude                                  |
| 2   | Strategy registry + IB canonicalization              | Codex           | M     | **P1**   | **Port (partial)**                           |
| 3   | Alerting / notifications                             | Codex           | S     | **P1**   | **Port**                                     |
| 4   | WebSocket / live state streaming                     | Mixed           | M     | **P1**   | **Port LiveStateController + view builders** |
| 5   | Daily scheduler                                      | Codex           | S     | **P1**   | **Port**                                     |
| 6   | Portfolio optimization                               | Codex           | L     | **P0**   | **Port (Claude is placeholder)**             |
| 7   | QuantStats report generation                         | Codex (partial) | S     | **P1**   | **Port intraday-to-daily normalization**     |
| 8   | Frontend Playwright e2e tests                        | Codex           | L     | **P0**   | **Port all 9 specs**                         |
| 9   | Operator CLI                                         | Codex           | M     | **P0**   | **Port 36-command sub-app structure**        |

**Total port surface:** ~2500 LOC across 3 P0 items and 5 P1 items.

---

## 1. Live Command Bus — Claude WINS (keep)

**Claude:** Redis Streams + consumer groups + PEL recovery + DLQ + dual idempotency + deployment identity (476 + 474 + 242 LOC).
**Codex:** arq jobs (fire-and-forget, no DLQ, no redelivery) — 216 LOC.

**Decision:** Do not port. Claude's command bus is production-grade and outclasses Codex's job-queue approach.

---

## 2. Strategy Registry + IB Canonicalization — PORT (P1, M)

**Claude:** Sync `TestInstrumentProvider.equity()`, NASDAQ default, no DB persistence, no continuous futures.
**Codex:** `NautilusInstrumentService` (605 LOC) with dual-provider routing (IB + Databento), `ResolvedInstrumentDefinition` dataclass, DB-backed `InstrumentDefinition` model, continuous futures regex `^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$` (e.g., `ES.Z.5`), Pydantic schema extraction for UI form generation, cached definitions with window-based refresh.

**Port (partial):**

- `ResolvedInstrumentDefinition` dataclass + `InstrumentDefinition` DB model
- Continuous futures helpers (`_raw_symbol_from_request`, `_resolved_databento_definition`)
- Multi-provider canonicalization (`canonicalize_live_instruments`, `canonicalize_backtest_instruments`)
- Config schema extraction (Pydantic `model_json_schema()` + defaults)

**Do NOT port wholesale:** integrate into existing `instruments.py` via async wrapper. Keep Claude's synchronous resolver for simple cases.

**Key files:** `codex-version/backend/src/msai/services/nautilus/instrument_service.py` (lines 32–106, 440–605), `codex-version/backend/src/msai/models/instrument_definition.py`.

---

## 3. Alerting — PORT (P1, S)

**Claude:** SMTP-only, fires and logs, no history, no API endpoint (109 LOC).
**Codex:** File-based JSON persistence, `/api/v1/alerts/` GET endpoint, paginated history (max 200), structured records (type, level, title, message, created_at), recovery event distinction (57 LOC + API + schema).

**Port:** Full port — small scope, high UX value.
**Key files:** `codex-version/backend/src/msai/services/alerting.py`, `api/alerts.py`, `schemas/alert.py`.

---

## 4. WebSocket / Live State Streaming — PORT LiveStateController (P1, M)

**Claude:** Per-deployment WS (`/api/v1/live/stream/{deployment_id}`), JWT first-message auth, exponential backoff reconnection, typed discriminated union, 30s heartbeat, Redis pub/sub (`msai:live:events:{deployment_id}`).
**Codex:** Single shared WS, no typed hook, no reconnection — but has `LiveStateController` (Nautilus Controller subclass) publishing runtime snapshots every 5s, `live_state_view.py` payload builders, Redis snapshot cache (`live_snapshot:*`).

**Port (partial):**

- `LiveStateController` (Nautilus `Controller` subclass with 5s snapshot interval) — Claude has no Nautilus event subscription; this closes a real gap
- `live_state_view.py` payload builders (status/positions/orders/trades)
- Redis snapshot persistence pattern for replay on reconnect

**Keep Claude's:** WS transport layer, typed React hook, per-deployment routing, exponential backoff.

**Key files:** `codex-version/backend/src/msai/services/nautilus/live_state.py` (lines 55–300), `services/live_state_view.py`, `services/live_updates.py`.

---

## 5. Daily Scheduler — PORT (P1, S)

**Claude:** Hardcoded UTC arq cron (06:00 ingest, 21:30 PnL), no timezone config, no state tracking.
**Codex:** Dedicated async scheduler loop (60s poll), TZ-aware (default America/Chicago, configurable via `daily_ingest_timezone`), configurable `daily_ingest_hour`/`minute`, JSON state file to prevent duplicate same-day runs, `daily_ingest_enabled` flag.

**Port:** Full port — self-contained 110 LOC.
**Key files:** `codex-version/backend/src/msai/workers/daily_scheduler.py`, config additions in `core/config.py:70-74`.

---

## 6. Portfolio Optimization — PORT (P0, L) — CLAUDE IS BROKEN

**Claude:** Portfolio job is a **Phase 2 placeholder** that marks runs "completed" without executing. Schema-only, no rebalance, no optimization.
**Codex:** Full orchestration — parallel candidate backtests with `max_parallelism` compute slots, 5 objectives (equal_weight, maximize_profit, maximize_sharpe, maximize_sortino, manual), automatic weight normalization, leverage scaling via downside target, Redis lease heartbeat for stale detection, QuantStats tearsheet for portfolio-level results (457 LOC).

**Port:** Full port of `_resolve_allocations`, `_execute_candidate_backtests`, `run_portfolio_backtest`, effective leverage logic, compute slot management.

**Key files:** `codex-version/backend/src/msai/services/portfolio_service.py`, `workers/portfolio_job.py`, `services/compute_slots.py`.

---

## 7. QuantStats Reports — PORT intraday normalization (P1, S)

**Claude:** QuantStats via temp file with fallback when `quantstats` is missing (173 LOC).
**Codex:** Direct `qs.reports.html()`, **intraday-to-daily compounding** via `groupby.prod()` (line 71) — critical for intraday trading strategies; without it metrics are inflated (74 LOC).

**Port (partial):** `_normalize_report_returns()` intraday grouping logic. Keep Claude's fallback resilience.

**Key files:** `codex-version/backend/src/msai/services/report_generator.py`.

---

## 8. Frontend Playwright e2e — PORT ALL (P0, L)

**Claude:** **0 spec files, no e2e directory.**
**Codex:** 9 spec files, 2021 LOC total:

- `operator-journey.spec.ts` (731 LOC) — seed strategy → backtest → deploy paper → monitor → stop
- `research-jobs.spec.ts` (181 LOC), `research-promotion.spec.ts` (206 LOC), `research-console.spec.ts` (226 LOC)
- `strategies-authoring.spec.ts` (117 LOC)
- `live-paper.spec.ts` (173 LOC), `live-paper-real.spec.ts` (22 LOC)
- `graduation-portfolio.spec.ts` (197 LOC)
- `data-refresh.spec.ts` (168 LOC)
- `playwright.config.ts` (port 3401, API-key auth, CI retries)

**Port:** Full copy of `e2e/` directory + `playwright.config.ts`, adapt port (3300) + auth mode.

**Key files:** `codex-version/frontend/e2e/*.spec.ts` + `playwright.config.ts`.

---

## 9. Operator CLI — PORT sub-app structure (P0, M)

**Claude:** 7 flat commands (ingest, ingest_daily, data_status, live_start/stop/status, live_kill_all) in 186 LOC.
**Codex:** 36 commands in 7 sub-apps (strategy, backtest, research, live, graduation, portfolio, account) in 782 LOC:

- **strategy:** list, templates, scaffold, sync, validate
- **backtest:** run, sweep, walk-forward, analytics
- **research:** list, show, cancel, retry, capacity
- **live:** start, stop, status, kill-all (keep Claude's)
- **graduation:** list, show, create, stage, promote, validate
- **portfolio:** snapshot, allocate
- **account:** summary, portfolio, snapshot, health
- **data:** ingest, ingest-daily, data-status
- **system:** health

**Port:** Sub-app refactor + ~25 new commands. Keep Claude's already-tested live commands.

**Key files:** `codex-version/backend/src/msai/cli.py`.

---

## Recommended Port Order

### Phase 1 — Unblock (P0, ~1500 LOC)

1. **Portfolio optimization** (L) — unblocks a core broken feature
2. **Frontend Playwright e2e** (L) — unblocks safe refactoring
3. **CLI sub-app structure** (M) — enables backtest/research/graduation automation

### Phase 2 — Operational maturity (P1, ~1000 LOC)

4. **Daily scheduler** (S) — timezone + state tracking
5. **Alerting API + history** (S) — persist + expose alerts
6. **QuantStats intraday normalization** (S) — fixes metric accuracy
7. **Strategy registry + continuous futures** (M)
8. **LiveStateController + snapshot builders** (M)

### Phase 3 — Decision point

Re-audit after Phase 1+2. Remaining gaps to evaluate: research/Optuna depth, graduation workflow, compute slot granularity, walk-forward analysis.

---

## Notes

- Claude's command bus (Subsystem 1) is the one area where Claude meaningfully beats Codex. Keep it, do not regress.
- Several Codex wins are operational maturity (scheduling, alerting history, snapshot caching) — low-scope, high-leverage.
- The big one is **portfolio optimization**: Claude ships a placeholder. This is the most urgent gap.
- E2E test debt is the second-biggest risk; 0 spec files means refactoring the frontend is currently unsafe.
