# Council Decision Context

**Decision:** Which MSAI v2 implementation to keep, which to kill?

## The two versions

MSAI v2 is a personal hedge-fund trading platform (NautilusTrader engine + IB Gateway broker). Two implementations were built from the same PRD:

- **claude-version** (built by Claude Opus 4.6) at `/Users/pablomarin/Code/msai-v2/claude-version/` — HEAD `c6b42bb`, current direction-of-travel. Has absorbed many codex features via PRs #3–11, #32, #35.
- **codex-version** (built by OpenAI Codex GPT-5.3) at `/Users/pablomarin/Code/msai-v2/codex-version/` — the alternative.

**PRD:** `/Users/pablomarin/Code/msai-v2/docs/plans/2026-02-25-msai-v2-design.md` (637 lines).

**Full scorecard:** `/Users/pablomarin/Code/msai-v2/docs/decisions/which-version-to-keep.md`.

## Pre-council recommendation (to challenge)

Keep claude-version. Kill codex-version. Port 4 residual items from codex first.

## Scorecard summary

| Axis (weight)                  | Claude                                                                                  | Codex                                                             |
| ------------------------------ | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| PRD coverage (1x)              | 31/39 core + 8 unique                                                                   | 31/39 core                                                        |
| Ops maturity (2x — real money) | Winner                                                                                  | Behind                                                            |
| Backend code quality (1x)      | 32,520 LOC · 536 tests · mypy strict · 23 migrations                                    | 17,055 LOC · 175 tests · mypy loose (7 ignores) · 9 migrations    |
| Frontend UI quality (1x)       | 15 shadcn primitives · typed API client · 34 CSS design tokens · 37 components          | 0 shadcn · raw fetch · 7 tokens · 20 components                   |
| Efficiency (1x)                | Explicit CPU/mem caps (10.5 CPU / 13 GB dev, 15.5 CPU / 23 GB prod) · 100% healthchecks | Unbounded · 89% healthchecks                                      |
| Migration risk (1x)            | Kill-codex → 4 portable items orphaned                                                  | Kill-claude → PRs #14–31 architecture orphaned (weeks to rewrite) |

## Codex-only wins (all portable within days)

1. 9 Playwright e2e specs at `codex-version/frontend/e2e/` (2,021 LOC) — claude has 0
2. `LiveStateController(Controller)` at `codex-version/backend/src/msai/services/nautilus/live_state.py:58` — 5-second runtime-snapshot publishing pattern. Claude uses DB-hydrated reconnect snapshot instead (PR #24)
3. 36-command CLI organized into 7 sub-apps (research/graduation/portfolio) vs claude's ~8 flat commands (plus `instruments` sub-app post-PR #35)
4. Dedicated `daily-scheduler` container in docker-compose vs claude bundling scheduling into ingest-worker via arq cron

## Claude-only wins (architecturally deep)

1. Portfolio-per-account schema + `PortfolioService` + `RevisionService` + weight-validated composition (PR #29–31)
2. Instrument registry (`instrument_definitions` + `instrument_aliases`) + alias-windowing by backtest date + IB qualifier + `msai instruments refresh` CLI (PR #32 + #35)
3. Live-stack hardening:
   - `live_supervisor/` subprocess pattern (separate process, not FastAPI child) — 4 modules
   - `LiveCommandBus` (Redis Streams + consumer groups + PEL recovery + DLQ + dual idempotency + 7-field deployment identity)
   - `heartbeat_monitor.py` with stale-deployment detection (`HEARTBEAT_TIMEOUT` typed config)
   - `LiveExecEngineConfig(reconciliation=True, reconciliation_lookback_mins=1440)` explicit pre-start reconciliation
   - 4-layer kill-all defense (Redis halt flag 24 h TTL + supervisor re-check before spawn + push-stop via command bus + SIGTERM + Nautilus flatten)
4. Trade dedup via partial unique index on `(deployment_id, broker_trade_id) WHERE broker_trade_id IS NOT NULL` + `audit_hook.write_trade_fill()` OrderFilled→trades pipeline
5. `scripts/restart-workers.sh` for ~10 s stale-import hygiene without Docker rebuild
6. Data lineage on `Backtest` model (nautilus_version, python_version, data_snapshot) for reproducibility
7. 536 test functions across 146 files (vs 175/49 in codex)

## Real-world validation (claude-version only)

- First real-money drill 2026-04-16 14:52 UTC on account U4705114: AAPL BUY 1 @ $261.33 → SELL flatten @ $262.46 via `/kill-all`. Live-verified trade persistence (PR #21 side="SELL"), PnL extraction (PR #19 pnl=-0.88), WebSocket snapshot hydration (PR #24). Net drill cost: ~$0.88 + $2.01 commissions.
- First real backtest 2026-04-15 14:01 UTC: AAPL+SPY Databento 2024 full year, 258k bars, 4,448 trades, QuantStats HTML report.

## Critical decision points (address directly)

1. **Operational-maturity 2× weighting** — justified given real money is already transiting the claude stack, or is this post-hoc rationalization?
2. **LiveStateController** — port it as a nice-to-have, or accept that claude's DB-hydrated reconnect snapshot (PR #24) is sufficient and architecturally simpler?
3. **Migration asymmetry** — is "4 portable items from codex vs weeks of rewrite from claude" decisive enough to override any "codex has cleaner foundations" argument? Or is there a foundational-quality dimension we're missing?

## Your job

You have read-only access to both version trees. **Spot-check at least one specific claim** before forming your verdict (e.g., open a file in codex-version, verify a claimed absence, count a test file). Do not rubber-stamp. If the pre-council recommendation is wrong, say so.

## Output schema (follow exactly)

```
## [Your Advisor Name]

### Position
[One sentence: what you recommend and why]

### Analysis
[2-5 bullet points grounded in code/constraints. Cite file paths + line numbers where you spot-checked.]

### Blocking Objections
[Issues that MUST be resolved before proceeding. "None" if clean.]

### Risks Accepted
[Trade-offs knowingly accepted]

### Verdict
APPROVE | OBJECT | CONDITIONAL
```
