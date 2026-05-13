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
- [ ] **IB Gateway end-to-end smoke test (automated)** —
      `./scripts/verify-paper-soak.sh`
      Zero manual steps. The script: 1. Validates `.env` has real (non-placeholder) paper
      credentials for `TWS_USERID` / `TWS_PASSWORD` /
      `IB_ACCOUNT_ID` and refuses to proceed otherwise 2. `docker compose up -d --wait` with the `live`
      profile — brings up postgres, redis, backend,
      backtest-worker, live-supervisor, and the ib-gateway
      sidecar container running `ghcr.io/gnzsnz/ib-gateway:stable`.
      `--wait` blocks until EVERY service's healthcheck
      passes, including ib-gateway's portable TCP probe
      that waits for IBC to log in and open port 4002
      (60-180 s start window) 3. Seeds the `smoke_market_order` strategy row via the
      same Python snippet the existing Phase 1 E2E harness
      uses 4. Runs `tests/e2e/test_live_trading_phase1.py` —
      drives POST `/api/v1/live/start` → status=running →
      fill in audit table → simulated backend crash +
      recovery → POST `/api/v1/live/stop` → status=stopped
      with zero open positions 5. On any failure, captures ib-gateway, live-supervisor,
      and backend logs to `./logs/paper-soak-*.log`
      **Scope caveat (Codex P2 iter1):** this step uses
      `docker-compose.dev.yml` in PAPER mode only. It exercises
      the full application path (supervisor → ProcessManager →
      payload factory → `_trading_node_subprocess` →
      `_build_real_node` → Nautilus TradingNode → IB adapter
      → IB Gateway → fill) but it does NOT exercise
      `docker-compose.prod.yml`-specific wiring. The prod
      compose file's live-mode port selection, resource
      limits, and (future) external secret mounts are
      validated by the next item below.
- [ ] **Production compose stack validation** — operator brings
      up the prod stack on the deployment host in paper mode
      (`COMPOSE_FILE=docker-compose.prod.yml TRADING_MODE=paper ./scripts/verify-paper-soak.sh`)
      AND in live mode
      (`COMPOSE_FILE=docker-compose.prod.yml TRADING_MODE=live IB_PORT=4003 IB_API_PORT=4001 ...`).
      Confirms that the prod healthcheck probes the right port
      for the selected mode and that both paper and live
      wiring actually boot on the production image. The
      verify script's `COMPOSE_FILE` env var override is
      reserved for this step (dev runs use the default
      `docker-compose.dev.yml`).

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

| Field                   | Value                                  |
| ----------------------- | -------------------------------------- |
| Operator name           | \***\*\*\*\*\***\_\_\_\***\*\*\*\*\*** |
| Operator signature      | \***\*\*\*\*\***\_\_\_\***\*\*\*\*\*** |
| Date                    | \***\*\*\*\*\***\_\_\_\***\*\*\*\*\*** |
| Soak start date         | \***\*\*\*\*\***\_\_\_\***\*\*\*\*\*** |
| Soak end date           | \***\*\*\*\*\***\_\_\_\***\*\*\*\*\*** |
| Allocation cap (USD)    | $1,000                                 |
| Initial deployment slug | \***\*\*\*\*\***\_\_\_\***\*\*\*\*\*** |

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
