# How to Deploy MSAI v2

This is the **top-level orientation doc** — what infrastructure runs where, how local dev works, and how prod deploys flow from `git push` to a running container on the Azure VM. Operational deep-dives live in `docs/runbooks/`; this file points at them.

---

## TL;DR

| What you want to do           | How                                                                                                                   |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Run locally with hot-reload   | `docker compose -f docker-compose.dev.yml up -d`                                                                      |
| Ship a code change to prod    | `git push origin main` — CI auto-builds + deploys                                                                     |
| Roll back to an older SHA     | `gh workflow run deploy.yml -f git_sha=<7-char-sha>`                                                                  |
| Re-deploy current SHA (force) | `gh workflow run deploy.yml`                                                                                          |
| First deploy to a fresh VM    | `gh workflow run deploy.yml -f bootstrap=true`                                                                        |
| Rehearse before risky change  | `gh workflow run deploy.yml -f resource_group=msaiv2-rehearsal-<date> -f vm_public_ip=... ...` (full override matrix) |

Health endpoints:

- Prod: `https://platform.marketsignal.ai/health`, `/ready`
- Local dev: `http://localhost:8800/health`

---

## Architecture at a glance

**Phase 1 deploy target** (per [`docs/decisions/deployment-pipeline-architecture.md`](decisions/deployment-pipeline-architecture.md)): single Azure VM running Docker Compose. No Kubernetes. The 4-slice deploy pipeline that gets us there shipped in PRs #51 → #61.

```
git push origin main
        │
        ▼
┌──────────────────────────────────────────────────────────────────┐
│ build-and-push.yml  (Slice 2 — .github/workflows/build-and-push) │
│   OIDC → ACR token → docker build → push                         │
│   Tags: msai-backend:<sha7>, msai-frontend:<sha7>                │
└──────────────────────────────────────────────────────────────────┘
        │ workflow_run on success
        ▼
┌──────────────────────────────────────────────────────────────────┐
│ deploy.yml  (Slice 3 — .github/workflows/deploy.yml)             │
│   Pre-flight: refuse if active live_deployments (Slice 4 gate)   │
│   OIDC → open transient NSG SSH rule for runner IP               │
│   scp scripts + Caddyfile + compose to VM                        │
│   ssh: sudo bash deploy-on-vm.sh <sha> <env-file>                │
│     → render /run/msai.env from Key Vault (managed identity)     │
│     → docker compose pull && up -d --wait                        │
│     → VM-local /health probe; rollback to last-good SHA on fail  │
│   Runner-side public probes: TLS chain, /health, frontend root   │
│   Cleanup job: delete transient SSH rule (always-runs)           │
└──────────────────────────────────────────────────────────────────┘
        │
        ▼
Azure VM (Ubuntu 24.04 LTS, Standard_D4ds_v6)
  docker-compose.prod.yml — backend, frontend, postgres, redis,
                            workers, caddy, [broker profile]
  /run/msai.env  ← Key Vault secrets, rendered at boot + each deploy
  /var/lib/msai  ← Premium SSD data disk (Parquet, postgres data, /docker root)
```

**Slice 4** (PR #58) layered on: nightly `pg_dump` → Blob backups, alert rules, and the active-`live_deployments` deploy gate (refuses to deploy if a broker is trading — operator clears state with `msai live stop <id>` per deployment, or `msai live kill-all --yes` for emergency).

> **Redis AOF caveat.** The architecture decision (§5c) called for Redis with AOF persistence + Blob backup, but the current `docker-compose.prod.yml` runs Redis without `appendonly yes` and without a named volume. Treat Redis state (idempotency keys, supervisor command stream PEL) as recoverable from Postgres + reconciliation, not durable across restarts. Closing this gap is a separate follow-up.

---

## Local development

**Use `docker-compose.dev.yml` for everything. Never rebuild images for code changes — hot reload handles it.**

```bash
# Start (backend, frontend, postgres, redis, workers — no broker)
docker compose -f docker-compose.dev.yml up -d

# With IB Gateway for live-trading work
COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml --env-file .env up -d

# Logs
docker compose -f docker-compose.dev.yml logs -f backend

# Stop
docker compose -f docker-compose.dev.yml down
```

Host ports: frontend `:3300`, backend `:8800`, postgres `:5433`, redis `:6380`.

**When to rebuild images:**

- Changed `Dockerfile.dev`, `pyproject.toml`, or `package.json`
- **NOT for `.py` / `.tsx` source changes** — those hot-reload via volume mounts (`./backend/src:/app/src`, etc.)

**After merges that touch worker code** (`src/msai/{services,workers,live_supervisor}`): run `./scripts/restart-workers.sh` to refresh stale Python imports without rebuilding.

Full dev setup, env vars, ports: [`CLAUDE.md`](../CLAUDE.md) §Running the stack.

---

## Production deploy — the happy path

**Just push to main.** That's it. The two-workflow chain handles the rest:

```bash
git checkout main && git pull
# ... your merged PR is here ...
# build-and-push.yml runs automatically on push
# deploy.yml fires automatically when build-and-push completes
```

Watch it land:

```bash
gh run list --workflow=build-and-push.yml --limit 3
gh run list --workflow=deploy.yml --limit 3
gh run watch  # follow the most recent
```

A successful deploy ends with three runner-side probes passing:

1. `https://platform.marketsignal.ai/health` → 200 (Caddy + backend reachable)
2. `https://platform.marketsignal.ai/` → 200 (frontend bundle served)
3. TLS cert issued by Let's Encrypt

The full happy-path is documented in [`docs/runbooks/slice-3-first-deploy.md`](runbooks/slice-3-first-deploy.md).

---

## Rollback

```bash
# Rollback to a specific SHA (must be still present in ACR — last 5 builds kept)
gh workflow run deploy.yml -f git_sha=<7-char-sha>

# List recently-built SHAs
az acr repository show-tags -n <ACR_NAME> --repository msai-backend --orderby time_desc -o table
```

**Auto-rollback scope.** `scripts/deploy-on-vm.sh` auto-rolls-back to the previous SHA only on **VM-local** failures: `docker compose pull`/`up`/`migrate`, VM-local `/health`, `/ready`, VM-local HTTPS `/health`, and `deploy-smoke.sh`. The runner-side **public** probes in `deploy.yml` (TLS chain, public `/health`, frontend root) fail the workflow but do **not** trigger auto-rollback — those failures usually mean DNS, NSG, or Let's Encrypt rate-limit, not bad code. If a public probe fails, run `gh workflow run deploy.yml -f git_sha=<previous>` manually.

Manual rollback is also right for "the new code is fine but we want to revert behavior."

**Forward-only migrations** (`docs/decisions/deployment-pipeline-architecture.md` §5a). Rolling back a deploy does **not** roll back the database schema. All migrations must be additive — see [`.claude/rules/database.md`](../.claude/rules/database.md) §Migrations.

---

## The active-deployments gate (Slice 4)

`deploy.yml` refuses to deploy if `/api/v1/live/status?active_only=true` reports any deployment in `starting`/`building`/`ready`/`running` state. This is a hard safety gate — broker subprocesses running through a deploy is the failure mode that loses money.

To clear the gate:

```bash
# 1. See what's active
msai live status

# 2a. Graceful — stop each active deployment by id (cancels orders, flattens positions, verifies broker_flat)
msai live stop <deployment_id>

# 2b. OR emergency — 4-layer halt of every active deployment (Redis halt flag + supervisor re-check + push-stop + SIGTERM+flatten)
msai live kill-all --yes
```

`live stop` and `kill-all` both surface `broker_flat` and `remaining_positions` in their response. If `broker_flat=false`, flatten manually via the IB portal before re-attempting the deploy.

**Fresh-VM bypass:** if `curl` to `/api/v1/live/status` fails with DNS-resolution-error or connection-refused (exit code 6/7) — i.e., Caddy/backend aren't running yet — the gate normally **fails closed**. For a genuine fresh-VM bootstrap or DR rebuild, pass `-f bootstrap=true`. **Never use `bootstrap=true` for routine re-deploys** — broker subprocesses live in a separate compose profile and can keep trading even when the API listener is dead.

---

## Pre-deploy rehearsal (for risky changes)

Council-mandated for first deploys to new infra and any change touching `docker-compose.prod.yml`, `Caddyfile`, `scripts/deploy-on-vm.sh`, or `infra/main.bicep`. Deploy to a throwaway resource group first.

The condensed flow (full procedure with pre-flight LE rate-limit check, KV seeding, smoke probes, and teardown is in [`docs/runbooks/slice-3-rehearsal.md`](runbooks/slice-3-rehearsal.md)):

```bash
RG=msaiv2-rehearsal-$(date -u +%Y%m%d)
az group create --name "$RG" --location eastus2 \
  --tags rehearsal=true expires-by="$(date -u -d '+1 day' +%Y-%m-%d)"

# Apply Slice 1 IaC against the rehearsal RG (NOTE: scripts/deploy-azure.sh
# is hard-coded to msaiv2_rg — for rehearsal use az deployment group create
# directly with a rehearsal-only ssh keypair)
ssh-keygen -t ed25519 -f ~/.ssh/msai-rehearsal -N '' -C "msai-rehearsal-$(date -u +%Y%m%d)"
OPERATOR_IP=$(curl -sf https://ifconfig.me)
OPERATOR_OID=$(az ad signed-in-user show --query id -o tsv)
az deployment group create \
  --name msai-iac \
  --resource-group "$RG" \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam \
  --parameters operatorIp="$OPERATOR_IP" operatorPrincipalId="$OPERATOR_OID" \
               vmSshPublicKey="$(cat ~/.ssh/msai-rehearsal.pub)"

# Capture outputs (the runbook also seeds KV secrets here)
OUTS=$(az deployment group show --name msai-iac --resource-group "$RG" --query 'properties.outputs' -o json)
VM_IP=$(jq -r .vmPublicIp.value     <<<"$OUTS")
KV_NAME=$(jq -r .keyVaultName.value <<<"$OUTS")
NSG_NAME=$(jq -r .nsgName.value     <<<"$OUTS")     # 'msai-nsg' per Bicep
ACR_NAME=$(jq -r '.acrLoginServer.value | split(".")[0]' <<<"$OUTS")
ACR_LOGIN=$(jq -r .acrLoginServer.value <<<"$OUTS")
KNOWN_HOSTS=$(ssh-keyscan -t ed25519 "$VM_IP" 2>/dev/null)

# Dispatch deploy.yml with full rehearsal-override matrix
gh workflow run deploy.yml \
  -f resource_group="$RG" \
  -f vm_public_ip="$VM_IP" \
  -f nsg_name="$NSG_NAME" \
  -f kv_name="$KV_NAME" \
  -f msai_hostname="platform-rehearsal.marketsignal.ai" \
  -f acr_name="$ACR_NAME" \
  -f acr_login_server="$ACR_LOGIN" \
  -f vm_ssh_known_hosts_var="$KNOWN_HOSTS" \
  -f bootstrap=true

# After success + smoke probes, tear down
az group delete -n "$RG" --yes --no-wait
```

The memory note `feedback_rehearsal_caught_real_bugs` captures why this is non-optional: the first-deploy rehearsal caught 8 production-blockers across 9 attempts.

---

## Backups + DR

- **Nightly `pg_dump`** → Azure Blob `msai-backups` container (systemd timer, `scripts/backup-to-blob.timer`).
- **Restore procedure:** [`docs/runbooks/restore-from-backup.md`](runbooks/restore-from-backup.md).
- **Full DR (rebuild VM from scratch):** [`docs/runbooks/disaster-recovery.md`](runbooks/disaster-recovery.md).
- **Hawk's gate (manual, post-merge):** operator runs `scripts/backup-to-blob.sh` against empty prod Postgres and verifies dump in Blob **before** the first real deploy. Evidence captured in PR #57 description.

---

## Required GitHub repo Variables + Secrets

CI needs 18 repo Variables and 2 Secrets. **Variables** are non-sensitive (visible to anyone with read on the repo); **Secrets** are encrypted (only deploy-time access). Set via `gh variable set NAME --body 'value'` and `gh secret set NAME`.

**Variables (non-sensitive):**

| Variable                      | Example                                 | Used by                                                                                                                                                                                              |
| ----------------------------- | --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AZURE_TENANT_ID`             | `2237d332-...`                          | OIDC auth                                                                                                                                                                                            |
| `AZURE_CLIENT_ID`             | UAMI client id                          | OIDC auth                                                                                                                                                                                            |
| `AZURE_SUBSCRIPTION_ID`       | `68067b9b-...`                          | OIDC auth                                                                                                                                                                                            |
| `RESOURCE_GROUP`              | `msaiv2_rg`                             | deploy targeting                                                                                                                                                                                     |
| `ACR_NAME`                    | `msaiacrXXXX` (short)                   | image push/pull                                                                                                                                                                                      |
| `ACR_LOGIN_SERVER`            | `msaiacrXXXX.azurecr.io`                | image tag prefix                                                                                                                                                                                     |
| `NSG_NAME`                    | `msai-nsg`                              | transient SSH rule (per `infra/main.bicep:76`; rehearsal Bicep output `nsgName`)                                                                                                                     |
| `KV_NAME`                     | `msai-kv-XXXX`                          | secret render                                                                                                                                                                                        |
| `VM_PUBLIC_IP`                | `20.x.x.x`                              | SSH target                                                                                                                                                                                           |
| `VM_SSH_USER`                 | `msaiadmin`                             | SSH user                                                                                                                                                                                             |
| `VM_SSH_KNOWN_HOSTS`          | multi-line ed25519 line                 | host-key trust                                                                                                                                                                                       |
| `MSAI_HOSTNAME`               | `platform.marketsignal.ai`              | runner probes                                                                                                                                                                                        |
| `MSAI_BACKEND_IMAGE`          | `msai-backend`                          | compose image name                                                                                                                                                                                   |
| `MSAI_FRONTEND_IMAGE`         | `msai-frontend`                         | compose image name                                                                                                                                                                                   |
| `DEPLOYMENT_NAME`             | `main` (prod) or `msai-iac` (rehearsal) | Azure deployment name used by `scripts/backup-to-blob.sh` for `az deployment group show` to resolve the storage account + container. Must match the `--name` passed to `az deployment group create`. |
| `NEXT_PUBLIC_AZURE_TENANT_ID` | `2237d332-...`                          | frontend build-arg                                                                                                                                                                                   |
| `NEXT_PUBLIC_AZURE_CLIENT_ID` | frontend app reg                        | frontend build-arg                                                                                                                                                                                   |
| `NEXT_PUBLIC_API_URL`         | `https://platform.marketsignal.ai`      | frontend build-arg                                                                                                                                                                                   |

**Secrets:**

| Secret               | What                                      |
| -------------------- | ----------------------------------------- |
| `VM_SSH_PRIVATE_KEY` | ed25519 private key for `msaiadmin@<VM>`  |
| `MSAI_API_KEY`       | X-API-Key for the active-deployments gate |

The Slice 2 acceptance step in [`docs/runbooks/vm-setup.md`](runbooks/vm-setup.md) walks through populating the 8 build-side variables (`AZURE_*`, `ACR_*`, `NEXT_PUBLIC_*`). The deploy-side variables and 2 secrets (`VM_*`, `NSG_NAME`, `KV_NAME`, `MSAI_HOSTNAME`, `MSAI_API_KEY`, `VM_SSH_PRIVATE_KEY`, etc.) are populated during the Slice 3 first-deploy procedure in [`docs/runbooks/slice-3-first-deploy.md`](runbooks/slice-3-first-deploy.md).

---

## When things go wrong

| Symptom                                                   | Likely cause                                      | Where to look                                                                                                                                                                              |
| --------------------------------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `build-and-push.yml` fails on OIDC                        | UAMI federated credential subject mismatch        | [`infra/main.bicep`](../infra/main.bicep) `federatedIdentityCredentials`                                                                                                                   |
| `deploy.yml` fails at "Open transient SSH allow rule"     | RBAC: gh-oidc MI lacks Network Contributor on NSG | re-apply Bicep                                                                                                                                                                             |
| `deploy.yml` fails at "Refuse if active live_deployments" | Active broker subprocess; or backend unreachable  | `msai live status` → `msai live stop <id>` per deployment (or `msai live kill-all --yes`); check `gh run view --log` for FAIL marker                                                       |
| Runner-side `/health` probe fails                         | DNS / Caddy / cert                                | `ssh msaiadmin@<VM>` → `sudo docker compose -f /opt/msai/docker-compose.prod.yml logs caddy backend`                                                                                       |
| `/health` returns 502 from Caddy                          | Backend container unhealthy                       | `docker compose logs backend`; check `/run/msai.env` rendered correctly                                                                                                                    |
| AMA heartbeat missing                                     | DCR misconfigured (Linux+stream)                  | [`feedback_ama_dcr_kind_linux_required`](#) — `sudo systemctl restart azuremonitoragent` after DCR fix                                                                                     |
| Orphan transient NSG rule                                 | Cleanup job failed                                | `reap-orphan-nsg-rules.yml` runs every 15 min (`cron: '7,22,37,52 * * * *'`) and reaps `gha-transient-*` rules older than 30 min — so cleanup lands 30-45 min after the orphan was created |

---

## Pointer index

- **Architecture decision:** [`docs/decisions/deployment-pipeline-architecture.md`](decisions/deployment-pipeline-architecture.md) — council verdict on Phase-1 VM + Compose, not k3s/AKS
- **Slicing decision:** [`docs/decisions/deployment-pipeline-slicing.md`](decisions/deployment-pipeline-slicing.md) — why 4 slices, what each does
- **SSH JIT decision:** [`docs/decisions/deploy-ssh-jit.md`](decisions/deploy-ssh-jit.md) — transient NSG rule pattern
- **VM provisioning:** [`docs/runbooks/vm-setup.md`](runbooks/vm-setup.md) — bootstrap a fresh VM from Bicep
- **First deploy:** [`docs/runbooks/slice-3-first-deploy.md`](runbooks/slice-3-first-deploy.md) — operator-mode procedure
- **Rehearsal RG:** [`docs/runbooks/slice-3-rehearsal.md`](runbooks/slice-3-rehearsal.md) — full Contrarian's-gate rehearsal procedure (the condensed version in §Pre-deploy rehearsal above derives from this)
- **Backup restore:** [`docs/runbooks/restore-from-backup.md`](runbooks/restore-from-backup.md)
- **Full DR rebuild:** [`docs/runbooks/disaster-recovery.md`](runbooks/disaster-recovery.md)
- **IaC re-apply:** [`docs/runbooks/iac-parity-reapply.md`](runbooks/iac-parity-reapply.md)
- **IB Gateway issues:** [`docs/runbooks/ib-gateway-troubleshooting.md`](runbooks/ib-gateway-troubleshooting.md)
- **CI workflows:**
  - [`.github/workflows/build-and-push.yml`](../.github/workflows/build-and-push.yml) — Slice 2
  - [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) — Slice 3 + 4
  - [`.github/workflows/reap-orphan-nsg-rules.yml`](../.github/workflows/reap-orphan-nsg-rules.yml) — orphan cleanup safety net
- **VM-side scripts** (staged on every deploy under `/opt/msai/scripts/`):
  - `scripts/deploy-on-vm.sh` — pulls images, renders env, runs `docker compose up -d --wait`, rolls back on VM-local probe failure
  - `scripts/deploy-smoke.sh` — VM-local probes
  - `scripts/backup-to-blob.sh` — nightly Postgres dump → Azure Blob
  - `scripts/deploy-azure.sh` — Bicep apply (operator workstation, not VM)

---

## Worktree note

Worktrees inherit `docker-compose.dev.yml`'s compose project name from the **working directory basename**. If you bring a stack up from `.worktrees/<name>/`, the running containers' project label is `<name>` — `docker compose -f docker-compose.dev.yml ps` from the main repo path will return empty until you stop the worktree's stack and bring a fresh one up from main. The `msai_postgres_data` volume is explicitly named (see `docker-compose.dev.yml` bottom) so it survives the rename.
