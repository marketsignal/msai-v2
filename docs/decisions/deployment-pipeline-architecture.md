# Decision: Deployment Pipeline Architecture (Phase 1 → 2-VM split → AKS)

**Date:** 2026-05-09
**Status:** **FINAL — council-ratified** (4 of 5 advisors APPROVE/CONDITIONAL; The Contrarian's OBJECT is preserved as a precursor-PR requirement, not an overrule)
**Decided by:** Engineering Council (`/council`) — 5 advisors (3 Claude, 2 Codex), Claude-as-chairman with engine-diversity caveat (Codex CLI silent-stalled on the synthesis prompt; the same `codex exec` long-prompt failure recorded 2026-04-28)
**Supersedes:** None
**Related plans/runbooks:** [`docs/runbooks/vm-setup.md`](../runbooks/vm-setup.md), [`docs/runbooks/disaster-recovery.md`](../runbooks/disaster-recovery.md), [`scripts/deploy-azure.sh`](../../scripts/deploy-azure.sh), [`docker-compose.prod.yml`](../../docker-compose.prod.yml)

---

## TL;DR

**Do NOT wire push-to-main yet.** The current `docker-compose.prod.yml` is structurally non-deployable (uses `build:`, missing Alembic in image, missing `ingest-worker`, missing `REPORT_SIGNING_SECRET` and Entra/CORS/MSAL envs). Land a precursor PR `feat/prod-compose-deployable` to fix those defects first; only after it merges does the deployment-pipeline branch open.

The shape of the eventual pipeline:

- **Provisioning:** Hand-provision Phase 1 via `scripts/deploy-azure.sh`; commit `infra/main.bicep` documenting the same shape declaratively. Don't run Bicep yet — it's the migration target, not the day-1 tool.
- **Deploy target:** Docker Compose on the single VM (`docker-compose.prod.yml`). No k3s, no Nomad.
- **CI/CD:** GitHub Actions GH-hosted runner + Azure OIDC + ACR + SSH to VM for `docker compose pull && migrate && up -d --wait`.
- **Secrets:** Azure Key Vault + VM system-assigned managed identity. Render a root-only `/run/msai.env` at boot.
- **Postgres:** Containerized + nightly `pg_dump` to Azure Blob (Phase 1). Defer Flexible Server to Phase 2.
- **Redis:** Containerized with AOF persistence. Volume backed up to Blob.
- **DATA_ROOT:** Premium SSD managed disk + nightly `azcopy` to Blob.
- **Observability:** Azure Log Analytics agent on the VM (managed identity).
- **Rollback:** Image rollback by git-SHA tag (last 5 in ACR). Forward-only migrations. Hard gate: deploy refuses if `live_deployments` has active rows.

---

## The 7 Sub-decisions

| #   | Decision                 | Choice                                                                                                                                                                                                                                                                                                                                                                                                                      | Aligned advisors                                                                  |
| --- | ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| 1   | Provisioning             | Hand-provision via `scripts/deploy-azure.sh` this week. Commit `infra/main.bicep` documenting the same shape declaratively. Don't apply Bicep yet.                                                                                                                                                                                                                                                                          | Bridge: Simplifier+Pragmatist (hand) + Hawk+Maintainer (Bicep)                    |
| 2   | Deploy target            | `docker compose -f docker-compose.prod.yml` on a single VM. No k3s/Nomad/`docker context`.                                                                                                                                                                                                                                                                                                                                  | 5/5                                                                               |
| 3   | CI/CD wiring             | GitHub Actions (GH-hosted runner) + Azure OIDC federated identity + ACR + SSH to VM for `docker compose pull && migrate && up -d --wait`. Reject self-hosted runner (lateral-movement risk). Reject GHCR in favor of ACR (Azure RBAC parity, AKS continuity).                                                                                                                                                               | Hawk+Contrarian+Maintainer (3/5 ACR), 4/5 GH-hosted                               |
| 4   | Secrets + config         | Azure Key Vault + VM system-assigned managed identity. ~30 lines of shell at boot fetches secrets and renders a root-only `/run/msai.env` consumed by compose.                                                                                                                                                                                                                                                              | Hawk+Contrarian+Maintainer (3/5)                                                  |
| 5a  | Migrations               | Add `alembic/` and `alembic.ini` to `backend/Dockerfile`. Add a one-shot `migrate` service in `docker-compose.prod.yml` with `restart: no`. App services `depends_on: condition: service_completed_successfully` of `migrate`. Forward-only migrations; destructive migrations require `workflow_dispatch` with explicit drain.                                                                                             | 5/5                                                                               |
| 5b  | Postgres                 | Containerized on Premium SSD managed disk + nightly `pg_dump` to Azure Blob (30-day retention). Defer Flexible Server to Phase 2.                                                                                                                                                                                                                                                                                           | Simplifier+Hawk+Pragmatist (3/5)                                                  |
| 5c  | Redis                    | Containerized with AOF persistence. Volume backed up to Blob.                                                                                                                                                                                                                                                                                                                                                               | 5/5                                                                               |
| 5d  | DATA_ROOT                | Premium SSD managed disk mounted at `/app/data`. Nightly `azcopy` to Blob. Reject Azure Files (DuckDB locking + perf surprise).                                                                                                                                                                                                                                                                                             | 4/5 (against Hawk)                                                                |
| 6   | AKS path                 | Compose file shape that translates 1:1 to Helm. Bicep file as IaC template (translates to AKS azcli). Secrets via KV + managed identity (translates to KV CSI driver). No managed Postgres yet.                                                                                                                                                                                                                             | Recommendation chooses against Codex side on Postgres                             |
| 7   | Observability + rollback | Deploy success: `docker compose up -d --wait --wait-timeout 120` + curl `/health` + curl `/ready` + `redis-cli XINFO GROUPS msai:live:commands`. Rollback: image rollback by git-SHA tag (last 5 in ACR), DB downgrades NOT supported. Hard gate: workflow refuses deploy if `live_deployments` has active rows; operator must `msai live stop --all` first. Logs: Azure Log Analytics agent (managed identity, ~$0.50/GB). | 5/5 success signal; Contrarian+Maintainer rollback discipline; Hawk observability |

---

## Consensus Points

- **5/5: Docker Compose on a single VM is the right deploy target.**
- **5/5: `alembic upgrade head` must be a single-runner, race-free deploy step that gates app startup.**
- **5/5: The current prod compose is not actually deployable** (every advisor identified ≥1 structural defect).
- **5/5: Broker profile (`ib-gateway` + `live-supervisor`) must NOT auto-deploy during an active live session** (NautilusTrader gotcha #3 — duplicate `client_id` silently disconnects).
- **5/5: Backups/DR are non-negotiable before first deploy.**
- **4/5: ACR over GHCR.**
- **3/5: Key Vault + managed identity from day one.**
- **3/5: Defer managed Postgres to Phase 2.**

---

## Blocking Objections (must resolve before first push-to-deploy)

### Bugs in the existing prod compose / Dockerfile (precursor PR `feat/prod-compose-deployable`)

1. **[Contrarian]** `backend/Dockerfile` does not copy `alembic/` or `alembic.ini`. `verify-paper-soak.sh:211` confirms migrations currently run from the host. → Add `COPY alembic/ alembic.ini` to backend image; add a `migrate` one-shot service in compose.
2. **[Contrarian]** `docker-compose.prod.yml` uses `build:` instead of `image:` from a registry. → Switch to `image: ${MSAI_REGISTRY}/msai-backend:${MSAI_GIT_SHA}` with `:?` guards.
3. **[Contrarian, Maintainer]** Prod compose has no `ingest-worker` service, but `IngestWorkerSettings` consumes `msai:ingest` queue (`queue.py:150`). → Add `ingest-worker` (and `job-watchdog` if part of operating model).
4. **[Contrarian]** `docker-compose.prod.yml:44` does not pass `REPORT_SIGNING_SECRET`; `config.py:284` hard-fails production on the dev default. → Inject from KV-rendered env file.
5. **[Contrarian]** Prod compose does not pass backend Entra settings, CORS origins, or frontend MSAL envs. → Same KV pipeline.
6. **[Maintainer]** Health docs reference `/api/v1/health`; app exposes `/health`. → Fix runbooks.

### Operational guardrails (must wire into the deploy workflow)

7. **[Simplifier, Pragmatist]** Auto-deploy must explicitly EXCLUDE the `broker` profile during active live sessions (gotcha #3).
8. **[Hawk]** No deploy success signal. Workflow MUST `--wait` + curl `/health` + curl `/ready` + `XINFO GROUPS msai:live:commands` before declaring success. `restart: unless-stopped` will hide a broken container indefinitely.
9. **[Hawk, Pragmatist]** No backup/DR plan. Nightly `pg_dump` + `app_data` rsync to Blob with retention BEFORE first deploy.
10. **[Hawk]** No observability beyond `docker logs`. Azure Log Analytics agent on the VM (managed identity, ~$0.50/GB).
11. **[Contrarian]** Rollback semantics undefined. Define: previous image tag + DB snapshot point + forward-only migration rule + hard gate when active live deployments exist.
12. **[Pragmatist]** Confirm Pablo has subscription-level Contributor on Azure (currently visible only on `pablovm/PABLOVM_RG`). Without it, `az group create msai-rg` fails.

---

## Minority Report

### The Contrarian (Codex) — VERDICT: OBJECT (preserved, not overruled)

> "The fatal flaw is assuming 'push to main deploys the stack' is mostly a transport problem. It is not. This app has broker sessions, live subprocesses, Redis Streams, local Parquet state, Alembic migrations, and Postgres audit tables. A bad deploy can strand live rows, lose queues, break auth, or require DB restore."

**Why I did NOT overrule:** Every concrete defect The Contrarian named (REPORT_SIGNING_SECRET injection, Alembic in image, ingest-worker, broker coupling, undefined rollback) is **a blocking precursor to wiring CI/CD at all**. The recommendation does not "approve and ship" — it **requires the precursor PR `feat/prod-compose-deployable` to fix the prod compose** before any pipeline lands. The OBJECT becomes a CONDITIONAL APPROVE only after that precursor PR ships clean.

**Re-trigger condition:** If the implementer attempts to wire the GitHub Actions workflow before fixing items 1-6 above, the Contrarian's OBJECT stands — the deploy will succeed-then-crash on first run because backend startup will fail on `REPORT_SIGNING_SECRET` validation.

### Hawk + Maintainer (CONDITIONAL) — partial dissent on managed Postgres

Both argued for **managed Postgres NOW**, citing: (a) Phase 2 swap is a real engineering cost (not just a connection-string change if migrations have drifted), (b) PITR matters even for paper-traded audit rows. The recommendation defers to Phase 2 instead. **Risk accepted:** if Phase 2 lands during real-money pressure, the Postgres swap will compete for attention. **Mitigation:** keep `database_url` the only swap point (already true via Pydantic config); test the swap in dry-run during a Phase 1 maintenance window.

### Pragmatist — partial dissent on ACR and Key Vault

Argued GHCR + `.env`-on-VM "ships Monday." Recommendation chose ACR + KV instead, accepting ~2 days of additional setup. **Risk accepted:** if Pablo's subscription perms are wrong (item 12), the ACR/KV path is blocked at the same Azure-permissions chokepoint that GHCR/`.env` would clear immediately. **Mitigation:** run the perm check first; if blocked, GHCR + `.env` is the cheap fallback for week 1 with a documented Phase 1.5 migration to KV/ACR.

---

## Missing Evidence

The council could not verify these — resolve before, or as the first step of, implementation:

1. **Azure subscription-level RBAC for Pablo.** Run: `az role assignment list --assignee pablo@ksgai.com --scope /subscriptions/<sub-id> --query "[?roleDefinitionName=='Contributor'].roleDefinitionName"`. (Verified inline in this decision — see "Verification" section below.)
2. **GHCR vs ACR cost/perf delta** for the actual MSAI image footprint — not assessed empirically.
3. **DuckDB-over-Azure-Files performance.** No advisor benchmarked it; consensus against was first-principles (network locking, latency).
4. **`docker-compose.prod.yml` env-var coverage audit.** Contrarian flagged 5 specific missing vars; full audit needs `diff <(env-from-config.py) <(env-from-prod-compose.yaml)`.
5. **Behavior of `live-supervisor` when `ib-gateway` is recreated underneath it.** NautilusTrader gotcha #3 silently disconnects on duplicate `client_id`; needs verification that the supervisor's heartbeat detects the dropped connection within an actionable window.
6. **`alembic upgrade head` advisory-lock semantics under Postgres 16** in containerized form.

---

## Next Step

**Open a precursor branch named `feat/prod-compose-deployable`** (NOT a deployment-pipeline branch yet).

- First commit: `backend/Dockerfile` adds `COPY alembic/ ./alembic/` and `COPY alembic.ini ./`.
- Second commit: `docker-compose.prod.yml` adds the `migrate` service, `ingest-worker` service, and pipes through every required env var (`REPORT_SIGNING_SECRET`, Entra `*_TENANT_ID`/`*_CLIENT_ID`, `CORS_ORIGINS`, frontend `NEXT_PUBLIC_MSAL_*`).
- Third commit: switch `build:` to `image:` placeholders (`${MSAI_REGISTRY}/${MSAI_BACKEND_IMAGE}:${MSAI_GIT_SHA}` with `:?` guards). Verify locally with `MSAI_REGISTRY=local MSAI_GIT_SHA=test docker compose -f docker-compose.prod.yml config` — should error cleanly on missing vars.
- Only AFTER this PR merges does the deployment-pipeline branch open.

---

## Verification (2026-05-09)

### Azure tenant + subscription target

The deploy goes to the **MarketSignal** Entra tenant (NOT KSGAI), per Pablo's correction during decision authoring:

| Field          | Value                                                                         |
| -------------- | ----------------------------------------------------------------------------- |
| Tenant         | `2237d332-fc65-4994-b676-61edad7be319` (MarketSignal)                         |
| Subscription   | `68067b9b-943f-4461-8cb5-2bc97cbc462d` (MarketSignal2)                        |
| User           | `pablo@marketsignal.ai`                                                       |
| Resource group | **`msaiv2_rg`** (eastus2) — already exists, empty, repurposed for this deploy |

### Subscription-level perms (item 12 / missing-evidence #1) — RESOLVED ✅

`az role assignment list --assignee pablo@marketsignal.ai --scope /subscriptions/68067b9b-943f-4461-8cb5-2bc97cbc462d --query "[].roleDefinitionName"`:

```
Owner
```

Pablo has **Owner at sub scope**. Item 12 closed; no Azure portal blocker.

### VM size correction — D4s_v5 → D4s_v6

`Standard DSv5 Family vCPUs` quota on MarketSignal2 is `0/0` in every region checked (eastus, eastus2, southcentralus, westus2, centralus). Filing a quota request adds days of latency.

`Standard Ddsv6 Family vCPUs` is `0/10` in eastus2 (and elsewhere). **D4s_v6** is one generation newer than D4s_v5, same 4 vCPU / 16 GB RAM / premium SSD support — ratified as the Phase 1 size with no quota request needed. Total Regional vCPUs = `0/10` so D4s_v6 fits well within the regional cap.

The runbook `docs/runbooks/vm-setup.md` will need updating to reflect D4s_v6 when the deployment-pipeline branch lands.

### Region

**eastus2** — chosen because:

- `msaiv2_rg` already exists empty in eastus2.
- Closer to IB servers (Greenwich, CT) than southcentralus / westus2 → lower live-trading latency.
- Ddsv6 quota available.

### Existing infra to AVOID

`msaimls2_rg` (southcentralus) on MarketSignal2 contains an **Azure Machine Learning workspace** (`Microsoft.MachineLearningServices/workspaces`) plus its auto-provisioned dependencies — Storage, Key Vault, Log Analytics, App Insights, ACR. **Do NOT reuse** any of those resources for the MSAI v2 deploy; ML workspace will surprise us with concurrent ACR/KV usage. Provision dedicated `msai-acr` / `msai-kv` inside `msaiv2_rg` instead.
