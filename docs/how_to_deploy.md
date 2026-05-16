# How to Deploy MSAI v2

This is the **top-level orientation doc** — what infrastructure runs where, how local dev works, and how prod deploys flow from `git push` to a running container on the Azure VM. Operational deep-dives live in `docs/runbooks/`; this file points at them.

---

## TL;DR

| What you want to do           | How                                                                                                                                                                                  |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Run locally with hot-reload   | `docker compose -f docker-compose.dev.yml up -d`                                                                                                                                     |
| Ship a code change to prod    | `git push origin main` — CI auto-builds + deploys                                                                                                                                    |
| Roll back to an older SHA     | `gh workflow run deploy.yml -f git_sha=<7-char-sha>`                                                                                                                                 |
| Re-deploy current SHA (force) | `gh workflow run deploy.yml`                                                                                                                                                         |
| First deploy to a fresh VM    | `gh workflow run deploy.yml -f bootstrap=true`                                                                                                                                       |
| Rehearse before risky change  | `gh workflow run deploy.yml -f resource_group=msaiv2-rehearsal-<date> -f vm_public_ip=... ...` (rehearsal inputs; `DEPLOYMENT_NAME` is not overrideable — see §Pre-deploy rehearsal) |

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

- Changed `Dockerfile.dev`, `pyproject.toml`, `uv.lock`, `package.json`, or `pnpm-lock.yaml`
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
# Rollback to a specific SHA (must still be present in ACR)
gh workflow run deploy.yml -f git_sha=<7-char-sha>

# List recently-built SHAs (every push-to-main SHA stays in ACR until
# manually deleted — there is NO automatic retention policy on the
# Basic-SKU ACR or in the workflow, despite earlier internal claims of
# "last 5 builds kept". Prune manually if storage cost becomes an issue.)
az acr repository show-tags -n <ACR_NAME> --repository msai-backend --orderby time_desc -o table
```

**Auto-rollback scope.** `scripts/deploy-on-vm.sh` auto-rolls-back to the previous SHA on any **VM-executed** probe failure: `docker compose pull`/`up`/`migrate`, VM-local `/health`, `/ready`, the VM-side HTTPS hostname probe (which can fail for DNS / NSG / Let's Encrypt reasons too, not just bad code — those still roll back), and `deploy-smoke.sh`. The runner-side public probes that fire AFTER the VM-side block returns success (TLS chain, public `/health`, frontend root in `deploy.yml`) fail the workflow but do **not** trigger auto-rollback — by that point the VM has accepted the new SHA as healthy. If a runner-side probe fails, run `gh workflow run deploy.yml -f git_sha=<previous>` manually.

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

The CLI's current output is a short success line — flatness fields are NOT carried in the entries `/api/v1/live/status` returns. Flatness lives only on the stop/kill-all response envelopes:

- `POST /api/v1/live/stop` → **200** for an actively-running deployment, with `broker_flat: bool` + `remaining_positions: list` in the body. The already-stopped shortcut (deployment was already terminal when the call arrived) ALSO returns 200 but with only `id` + `status` — no flatness fields — because there's no live child to ask. **504 `FLATNESS_UNKNOWN`** if the stop succeeded but the child never wrote a flatness report (operator must verify positions via IB portal).
- `POST /api/v1/live/kill-all` → **200** when every deployment came back flat, **207 (Multi-Status)** when `any_non_flat=true` or any publish failed partially. Body always includes `any_non_flat: bool` + `flatness_reports: list[dict]`.

To inspect flatness directly, capture body and status code separately (piping `curl -w` directly into `jq` mixes the status line into jq's input and breaks parsing):

```bash
# Per deployment — capture body to a temp file, status to a variable
STATUS=$(curl -s -o /tmp/stop.json -w "%{http_code}" -X POST \
  -H "X-API-Key: $MSAI_API_KEY" -H "Content-Type: application/json" \
  -d "{\"deployment_id\":\"<id>\"}" \
  https://platform.marketsignal.ai/api/v1/live/stop)
echo "HTTP $STATUS"   # 200 = stop accepted (read broker_flat from body); 504 = stop did not reach a clean terminal — see (d)/(e) below
jq '{broker_flat, remaining_positions, detail}' /tmp/stop.json
# NB: 200 alone does NOT mean flat. Five observable cases:
#   (a) 200 + broker_flat=true                                       → flat, safe to proceed.
#   (b) 200 + broker_flat=false                                      → residual positions; flatten in IB first.
#   (c) 200 + no flatness fields                                     → already-stopped shortcut (no live child).
#   (d) 504 + broker_flat=null + detail.error.code=FLATNESS_UNKNOWN  → stopped, no flatness report from child.
#   (e) 504 + detail.error.code=API_POLL_TIMEOUT                     → child never reached terminal in the poll window.
# In (c), (d), and (e), verify residual positions via IB portal before deploying.

# Or for the all-at-once kill path
STATUS=$(curl -s -o /tmp/kill.json -w "%{http_code}" -X POST \
  -H "X-API-Key: $MSAI_API_KEY" \
  https://platform.marketsignal.ai/api/v1/live/kill-all)
echo "HTTP $STATUS"   # 200 = all flat; 207 = any_non_flat / partial
jq '{any_non_flat, flatness_reports}' /tmp/kill.json
```

If `broker_flat=false` (or `any_non_flat=true`, which may also mean _unknown_-flatness rather than confirmed-non-flat), verify residual positions via the IB portal and flatten any that remain before re-attempting the deploy.

**Fresh-VM bypass:** if `curl` to `/api/v1/live/status` fails with DNS-resolution-error or connection-refused (exit code 6/7) — i.e., Caddy/backend aren't running yet — the gate normally **fails closed**. For a genuine fresh-VM bootstrap or DR rebuild, pass `-f bootstrap=true`. **Never use `bootstrap=true` for routine re-deploys** — broker subprocesses live in a separate compose profile and can keep trading even when the API listener is dead.

---

## Pre-deploy rehearsal (for risky changes)

Council-mandated for first deploys to new infra and any change touching `docker-compose.prod.yml`, `Caddyfile`, `scripts/deploy-on-vm.sh`, or `infra/main.bicep`. Deploy to a throwaway resource group first.

**Start from [`docs/runbooks/slice-3-rehearsal.md`](runbooks/slice-3-rehearsal.md) — that's the closest existing end-to-end procedure.** Caveat: the runbook predates Slices 3–4 hardening, so it covers the rehearsal Bicep apply, KV secret seeding, LE rate-limit pre-flight, image build, and the Contrarian's-gate NSG child-resource spike, but does NOT yet document the four temporary repo Variable/Secret swaps that Slices 3–4 introduced (`AZURE_CLIENT_ID` → rehearsal RG's UAMI client id; `DEPLOYMENT_NAME` → `msai-iac`; `VM_SSH_PRIVATE_KEY` → rehearsal private key; `ACR_NAME`+`ACR_LOGIN_SERVER` if pushing rehearsal images via build-and-push), nor the `-f bootstrap=true` dispatch flag. Use the orientation table below to fill those gaps until the runbook is updated.

Orientation only — what the rehearsal looks like at a high level:

| Step       | What                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | Where                                                           |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| Pre-flight | LE rate-limit headroom, DNS A record for `platform-rehearsal.marketsignal.ai`, operator IP current                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | runbook §Pre-flight                                             |
| 1          | `az group create msaiv2-rehearsal-<date>` (tagged `expires-by`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | runbook §1                                                      |
| 2          | Apply Slice 1 Bicep against the rehearsal RG with a fresh `~/.ssh/msai-rehearsal` keypair (DO NOT reuse prod key)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | runbook §2                                                      |
| 3          | Update DNS A record → rehearsal VM IP; `ssh-keyscan` for `VM_SSH_KNOWN_HOSTS`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | runbook §3                                                      |
| 4          | Seed rehearsal KV with secrets (report signing, postgres password, Entra IDs, CORS, IB stubs, `DATABENTO_API_KEY` for bootstrap/ingest, and an `MSAI_API_KEY` value that the rehearsal backend will read at boot). **`MSAI_API_KEY` is special** — the deploy gate authenticates via the GitHub repo Secret `${{ secrets.MSAI_API_KEY }}`, not via KV directly. Either set the KV secret to the SAME value as the existing repo Secret, OR temp-swap the repo Secret (and restore in step 10). The values MUST match or the gate's curl auth will 401 against the rehearsal stack.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | runbook §4 (partial — missing MSAI_API_KEY + DATABENTO_API_KEY) |
| 5          | Push images for the rehearsal SHA. The runbook literally writes `gh workflow run build-and-push.yml`, but `build-and-push.yml`'s jobs are guarded `if: github.ref == 'refs/heads/main'` — a feature-branch dispatch produces zero ran jobs. Use one of: (a) merge to main first then rehearse — defeats the pre-merge purpose; (b) build + push locally via two separate buildx commands (different contexts/Dockerfiles, and the frontend needs `NEXT_PUBLIC_*` build-args because Dockerfile fail-fasts on empty ones): `az acr login -n <rehearsal-acr-short>` then `docker buildx build --push -t <rehearsal-acr>.azurecr.io/msai-backend:<sha7> -f backend/Dockerfile .` and `docker buildx build --push -t <rehearsal-acr>.azurecr.io/msai-frontend:<sha7> -f frontend/Dockerfile --build-arg NEXT_PUBLIC_AZURE_TENANT_ID=... --build-arg NEXT_PUBLIC_AZURE_CLIENT_ID=... --build-arg NEXT_PUBLIC_API_URL=https://platform-rehearsal.marketsignal.ai ./frontend` (mirrors build-and-push.yml steps 72-82 and 119-134). Do NOT follow the runbook's literal step here. | runbook §5 (broken — follow this row instead)                   |
| 6          | **Contrarian's spike** — prove the NSG child-resource refactor survives a Bicep reapply BEFORE deploying                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | runbook §6                                                      |
| 7          | **Temporary swaps to repo Variables + Secrets** (each restored after teardown): `RESOURCE_GROUP`, `VM_PUBLIC_IP`, `MSAI_HOSTNAME`, `KV_NAME`, `DEPLOYMENT_NAME`, `VM_SSH_KNOWN_HOSTS`, `VM_SSH_PRIVATE_KEY`, `AZURE_CLIENT_ID` (the rehearsal RG's UAMI client id, not prod's — federated credentials are per-RG)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | NOT in runbook (gap) — see §rehearsal preface above             |
| 8          | `gh workflow run deploy.yml -f bootstrap=true -f git_sha=<sha7-from-row-5> -f acr_name=<rehearsal-acr-short> -f acr_login_server=<rehearsal-acr>.azurecr.io` — `git_sha` is REQUIRED after the local docker buildx path because deploy.yml's workflow_dispatch fallback resolves to `GITHUB_SHA::7` (i.e., whatever's currently on main); ACR overrides are REQUIRED if you pushed images to the rehearsal ACR in row 5(b) and didn't temp-swap `ACR_NAME` + `ACR_LOGIN_SERVER` in row 7 (deploy.yml otherwise pulls from the prod ACR per `vars.ACR_LOGIN_SERVER` default). The remaining `-f` overrides on top of these are only needed if you choose NOT to swap the matching repo Variables in step 7.                                                                                                                                                                                                                                                                                                                                                                  | runbook §8                                                      |
| 9          | Smoke probes — LE cert chain, public `/health`, frontend root, `msai live status` against the rehearsal stack. `/api/v1/live/kill-all` is mutating (sets persistent halt flag) and has no dry-run mode — exercise it only if your rehearsal includes a kill-switch verification step.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | runbook §9                                                      |
| 10         | `az group delete --no-wait` + restore all swapped Variables/Secrets                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | runbook §10                                                     |

**Two non-overrideable pins** worth calling out before you start (the runbook covers both, but they trip first-time rehearsers):

- `DEPLOYMENT_NAME` is hard-coded from `${vars.DEPLOYMENT_NAME}` into the staged env file (no workflow input). Either temp-swap the Variable to `msai-iac` for the rehearsal run, or accept that backup-to-blob.sh on the rehearsal VM resolves the wrong deployment — harmless if you're not running nightly backups during the rehearsal.
- `VM_SSH_PRIVATE_KEY` is loaded unconditionally from `${secrets.VM_SSH_PRIVATE_KEY}` (no workflow input). Temp-swap to the rehearsal-only private key before dispatch; restore prod key after teardown.

**Also worth knowing — three more pre-merge gotchas not in the runbook:**

- **ACR targeting.** `build-and-push.yml` pushes only to `${vars.ACR_LOGIN_SERVER}` (the prod ACR by default). For a rehearsal, either temp-swap `ACR_NAME` + `ACR_LOGIN_SERVER` Variables in step 7 (and restore in step 10), OR push directly to the rehearsal ACR with the explicit `docker buildx ... --push` commands in step 5(b). The `acr_name`/`acr_login_server` workflow_dispatch inputs on `deploy.yml` let the dispatch point at whichever ACR holds the image without swapping the repo Variables. The runbook does NOT document either pattern explicitly — operator picks per the row-5 / row-8 guidance above.

- **`gh workflow run deploy.yml` without `--ref` rehearses MAIN'S deploy.yml, not your branch's.** `gh workflow run` dispatches the version of the workflow file checked into the default branch — so pre-merge changes to `deploy.yml` itself, `docker-compose.prod.yml`, `Caddyfile`, or `scripts/deploy-on-vm.sh` (all staged to the VM by deploy.yml's scp step) won't actually be exercised. The rehearsal then proves nothing about those changes.

- **`--ref <branch>` triggers a different OIDC failure.** Adding `--ref feat/<your-branch>` makes the dispatch use the branch's workflow files, but the federated credential in `infra/main.bicep` pins the subject claim to `repo:<owner>/<repo>:ref:refs/heads/main` (default value of `repoBranch`). The OIDC token from a non-main ref will fail `azure/login@v2` with AADSTS70021. Workarounds: (a) re-Bicep the rehearsal RG with `--parameters repoBranch=feat/<your-branch>` so the federated cred accepts the feature branch; (b) merge the workflow/IaC changes first as a non-functional commit and rehearse from main. Both have trade-offs — discuss with the council before picking.

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

The Slice 2 acceptance step in [`docs/runbooks/vm-setup.md`](runbooks/vm-setup.md) walks through populating the 8 build-side variables (`AZURE_*`, `ACR_*`, `NEXT_PUBLIC_*`). The deploy-side variables and 2 secrets (`VM_*`, `NSG_NAME`, `KV_NAME`, `MSAI_HOSTNAME`, `MSAI_API_KEY`, `VM_SSH_PRIVATE_KEY`, etc.) are **not** auto-walked through by any runbook yet — [`docs/runbooks/slice-3-first-deploy.md`](runbooks/slice-3-first-deploy.md) verifies a sample via `gh variable list | grep ...` but assumes the operator has already set them. Populate them from Slice 1 Bicep outputs:

```bash
# Capture from the prod RG (one-time)
RG=msaiv2_rg
OUTS=$(az deployment group show --name main --resource-group "$RG" --query 'properties.outputs' -o json)
gh variable set RESOURCE_GROUP --body "$RG"
gh variable set VM_PUBLIC_IP --body "$(jq -r .vmPublicIp.value     <<<"$OUTS")"
gh variable set KV_NAME      --body "$(jq -r .keyVaultName.value <<<"$OUTS")"
gh variable set NSG_NAME     --body "$(jq -r .nsgName.value      <<<"$OUTS")"
gh variable set VM_SSH_USER  --body "msaiadmin"
gh variable set MSAI_HOSTNAME --body "platform.marketsignal.ai"
gh variable set MSAI_BACKEND_IMAGE  --body "msai-backend"
gh variable set MSAI_FRONTEND_IMAGE --body "msai-frontend"
gh variable set DEPLOYMENT_NAME     --body "main"
gh variable set VM_SSH_KNOWN_HOSTS  --body "$(ssh-keyscan -t ed25519 "$(jq -r .vmPublicIp.value <<<"$OUTS")" 2>/dev/null)"

# Secrets
gh secret set VM_SSH_PRIVATE_KEY < ~/.ssh/msai-prod    # private key matching the Bicep-deployed pubkey
gh secret set MSAI_API_KEY                              # then paste the X-API-Key value (also stored in KV)
```

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
