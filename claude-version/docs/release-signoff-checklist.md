# Release Sign-Off Checklist (Phase 5.2)

> **Phase 5 is the release gate, not implementation.** This
> checklist is the final go/no-go for moving from paper soak
> to real-money trading. Every box must be ticked. No
> shortcuts. No exceptions.

---

## How to use this document

1. Print or copy it. The operator filling it out signs the
   bottom and stores the signed copy alongside the soak
   journal.
2. Each item has a verification command or document path.
   The operator runs / reviews it and ticks the box only
   after verifying the result themselves.
3. If any item is unchecked, real-money allocation is
   **NOT** authorized.

---

## A. Test suite

- [ ] **Unit tests pass** —
      `cd backend && uv run pytest tests/unit/`
- [ ] **Integration tests pass** —
      `cd backend && uv run pytest tests/integration/`
- [ ] **Phase 1 E2E test passes against real IB Gateway** —
      `MSAI_E2E_IB_ENABLED=1 uv run pytest tests/e2e/test_live_trading_phase1.py`
- [ ] **Phase 2 E2E test passes** (security master + parity)
- [ ] **Phase 3 E2E test passes** (streaming + kill switch)
- [ ] **Phase 4 E2E test passes** (recovery + IB disconnect)
- [ ] **Backtest determinism test passes** (Phase 2 task 2.11)
- [ ] **EMA save/load round-trip + restart-continuity tests pass**
      (Phase 4 task 4.5)
- [ ] **IB Gateway live boot smoke test** — operator
      MANUALLY starts one deployment against a **running**
      paper IB Gateway (NOT a fake/mock) and personally verifies: 1. The subprocess transitions
      `starting → building → ready → running` without error
      (check `live_node_processes.status` directly) 2. `trader.is_running == True` after `status = running`
      (check `/api/v1/live/status/{deployment_id}`) 3. IB account summary is populated
      (check `/api/v1/account/summary` returns real
      NetLiquidation / BuyingPower numbers, not zeros or
      defaults) 4. Submit ONE 1-share test order on a liquid symbol
      (e.g. SPY) and verify the fill event lands in
      `trades` table AND the IB Gateway account reflects
      the trade within 30s 5. `/api/v1/live/stop` cleanly terminates the subprocess
      (`status = stopped`, no positions left open,
      `live_node_processes.pid` not alive)
      **This is the authoritative test that the whole
      production path (supervisor → ProcessManager → payload
      factory → `_trading_node_subprocess` → `_build_real_node`
      → Nautilus TradingNode → IB adapter → IB Gateway → fill)
      actually works. No amount of unit/integration testing
      substitutes for this.**

## B. Code review

- [ ] **Codex review batches 1–10 closed clean** — all P0/P1/P2
      findings fixed; P3 findings either fixed or
      explicitly accepted in writing
- [ ] **Architecture review re-run on the latest commit** by
      Claude + Codex; no new P0/P1/P2
- [ ] **No `# TODO` / `# XXX` markers in the live order path**
      (`grep -rn "TODO\|XXX" src/msai/api/live.py src/msai/services/nautilus/`)
- [ ] **No `pytest.skip` / `xfail` decorators on Phase 1–4
      core path tests**

## C. Infrastructure

- [ ] **Postgres backups configured** — daily, retention 30
      days, restore from yesterday's backup verified manually
- [ ] **Parquet catalog backed up** to Azure Blob Storage
      (Phase 0 backup script)
- [ ] **Redis persistence enabled** (`appendonly yes` or
      RDB+AOF) so a Redis container restart doesn't lose the
      pending command bus + cache state
- [ ] **Docker Compose health checks** for every container
      reviewed and trip on the right conditions
- [ ] **Secrets in Azure Key Vault**, NOT in `.env` —
      verify `IB_ACCOUNT_ID`, `JWT_SECRET`, `POLYGON_API_KEY`
      etc. are loaded from KV and the Compose file does not
      contain them in plaintext

## D. Observability

- [ ] **Prometheus scrape configured** to hit `/metrics` every
      30s (Phase 4 task 4.6)
- [ ] **Alertmanager wired** for the always-on alerts listed
      in the paper soak procedure
- [ ] **Test alert path end-to-end** — manually trigger an
      alert (e.g., kill the API container) and confirm the
      operator's phone receives the notification within
      60 seconds
- [ ] **Audit log review** — operator has reviewed the last
      30 days of `order_attempt_audits` and confirms every
      `denied` row has a sensible `reason`

## E. Operator readiness

- [ ] **Operator confirms emergency contact for IB account**
      (phone + email + IB account number)
- [ ] **Operator has tested the kill-all UI flow** in the
      paper environment AND verified that all running
      deployments stop within 10 seconds
- [ ] **Operator has tested the resume flow** —
      `/api/v1/live/resume` followed by a successful `/start`
- [ ] **Operator has read the disaster recovery runbook**
      (`docs/runbooks/disaster-recovery.md`) end to end
- [ ] **Operator has the IB Gateway login credentials** in a
      password manager separate from the deployment env

## F. Real-money allocation guardrails

- [ ] **Initial allocation: $1,000 max** — hard cap in
      `LiveRiskEngineConfig.max_notional_per_order` for the
      one production deployment
- [ ] **Daily loss limit: 2% of allocation** ($20) — set in
      the deployment row's `RiskLimits.daily_loss_limit_usd`
      and verified by reading back via the API
- [ ] **Single-instrument cap: 100 shares** — set in
      `RiskLimits.max_position_per_instrument`
- [ ] **Allow_eth flag: false** for the equity strategy
- [ ] **Live deployment uses a SEPARATE IB account** from any
      manual / discretionary positions

## G. Soak journal

- [ ] **30-day paper soak journal** complete with daily
      review notes
- [ ] **Zero P0 incidents in the soak window**
- [ ] **Zero P1 incidents in the soak window**
- [ ] **Final soak summary** drafted and attached to this
      checklist

---

## Sign-off

| Field                   | Value                          |
| ----------------------- | ------------------------------ |
| Operator name           | ****\*\*****\_\_\_****\*\***** |
| Operator signature      | ****\*\*****\_\_\_****\*\***** |
| Date                    | ****\*\*****\_\_\_****\*\***** |
| Soak start date         | ****\*\*****\_\_\_****\*\***** |
| Soak end date           | ****\*\*****\_\_\_****\*\***** |
| Allocation cap (USD)    | $1,000                         |
| Initial deployment slug | ****\*\*****\_\_\_****\*\***** |

**By signing, the operator confirms:** every box above is
checked, and they have personally verified each item. The
operator also accepts that real-money trading carries
unbounded downside risk and that the platform's safeguards
(kill switch, daily loss limits, position caps) are
defenses-in-depth, NOT guarantees.

---

## What this checklist does NOT cover

- It does not cover regulatory or tax obligations.
- It does not cover broker-side risk controls (those are
  configured in IB's UI separately).
- It does not cover the operator's own risk tolerance or
  capital allocation strategy.
- It does not absolve the operator of monitoring
  responsibilities once trading goes live.

The platform is a tool. The decisions are the operator's.
