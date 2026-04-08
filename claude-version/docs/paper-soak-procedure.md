# Paper Soak Procedure (Phase 5.1)

> **Phase 5 is the release gate, not implementation.**
> Code is frozen on the branch that enters paper soak. Any
> code change restarts the 30-day clock.

---

## Purpose

The paper soak is the explicit "is this safe for real money"
gate between Phase 4 (production-grade resilience) and live
deployment. Phase 1–4 give us correct primitives. Paper soak
is how we verify those primitives behave the way we expect
under real broker conditions over real time.

---

## Pre-flight checklist

Before starting the soak clock, the following must all be
green:

- [ ] **All Phase 1–4 unit tests passing** (`uv run pytest tests/unit/`)
- [ ] **All Phase 1–4 integration tests passing** (`uv run pytest tests/integration/`)
- [ ] **Phase 1–4 E2E tests passing against real IB Gateway**
      (`MSAI_E2E_IB_ENABLED=1 uv run pytest tests/e2e/`)
- [ ] **Codex review batches 1–10 all closed clean** (no
      P0/P1/P2 outstanding)
- [ ] **Disaster recovery runbook** (`docs/runbooks/disaster-recovery.md`)
      reviewed by operator
- [ ] **IB paper account credentials** loaded in the deployment
      environment via Azure Key Vault (NOT committed to git)
- [ ] **Prometheus + alertmanager** wired against the
      `/metrics` endpoint (Phase 4 task 4.6)
- [ ] **Backups** of the Postgres `live_deployments` /
      `live_node_processes` / `order_attempt_audits` tables
      configured and verified (a restore test from
      yesterday's backup must work)

---

## Soak parameters

| Parameter                  | Value                                                                                                                            |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **Duration**               | 30 calendar days minimum                                                                                                         |
| **Account**                | IB paper trading account (separate from real)                                                                                    |
| **Initial strategy**       | EMA Cross on AAPL + MSFT, paper, 1 share trade size                                                                              |
| **Instrument expansion**   | Add ONE new instrument per week IF the prior week had zero P0/P1 incidents                                                       |
| **Concurrent deployments** | Start at 1, raise to 2 only after week 2 if clean                                                                                |
| **Daily PnL alerting**     | Email digest at 17:30 ET every trading day                                                                                       |
| **Halt budget**            | A single auto-halt (kill switch fired) requires investigation but does NOT restart the clock; multiple auto-halts in 7 days DOES |

---

## Monitoring requirements

### Always-on alerts (page on fire)

- API container down for >60s
- live-supervisor container down for >60s
- Trading subprocess `status='failed'` (any reason)
- IB Gateway disconnect lasting >2 minutes
- Reconciliation failure on startup
- Kill switch active (any reason: manual, IB disconnect, supervisor halt)
- Postgres connection failure
- Redis connection failure

### Daily review

- `order_attempt_audits` table — every row from the last 24h,
  filter by `status` and verify each `denied`/`rejected` row
  has the correct `reason`
- `live_node_processes` heartbeat freshness for all running rows
- Prometheus dashboard for the day's PnL, fill count, and
  rejection rate

### Weekly review

- Full audit of every fill
- Strategy PnL vs the corresponding backtest run on the same
  date range (Phase 2 task 2.11 parity contract)
- Container restart count (target: 0; tolerable: <3 per
  container per week)
- Disk usage trends (Parquet catalog growth + Postgres WAL)

---

## Incident classification

| Severity | Definition                                                                                                                                              | Action                                                                  |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| **P0**   | Real or paper money lost; orders submitted that should NOT have been; positions left open during a kill switch                                          | **STOP soak**, full root-cause analysis, code fix, RESTART 30-day clock |
| **P1**   | Order delivery failure that recovered automatically; partial fills not reconciled correctly; halt fired without operator click but for the right reason | Investigate within 24h, code fix if needed, RESTART 30-day clock        |
| **P2**   | Latency degradation; metrics gap; transient alert that recovered itself                                                                                 | Log + investigate; do NOT restart clock unless 3+ in a week             |
| **P3**   | Cosmetic / nit                                                                                                                                          | No action                                                               |

---

## Exit criteria

The soak is complete when ALL of:

- 30 consecutive calendar days have elapsed since the LAST P0
  or P1 incident
- ALL daily reviews completed by the operator with sign-off
- Weekly reviews recorded in a soak journal with no unresolved
  open items
- The release sign-off checklist (Phase 5.2,
  `docs/release-signoff-checklist.md`) is fully checked

---

## What "real money ready" does NOT mean

- It does NOT mean the system is bug-free. It means the bugs
  we've found are characterized and understood.
- It does NOT mean every code path has been exercised. Many
  Phase 4 recovery paths only fire under failures we'll
  rarely see.
- It does NOT mean paper PnL projects to real PnL. Slippage,
  rebates, and impact in real markets differ from IB's paper
  fill model.

The soak is a **risk reduction**, not a proof of correctness.
Real money allocation starts at $1,000 hard cap (per
`LiveRiskEngineConfig.max_notional_per_order`) regardless of
how the soak performed.
