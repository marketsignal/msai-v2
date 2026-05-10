# VM Setup Runbook

Step-by-step guide to provision and configure the MSAI v2 production VM on Azure.

> ⚠️ **Runbook in transition (2026-05-09).** §4 and §5 below describe the legacy `scp -r backend/ frontend/` deploy flow. After Slice 1 of the deployment-pipeline series merges (this PR), §1-§3 use Bicep IaC; after Slice 2-3 merge, §4-§5 will be replaced with the GH Actions OIDC + ACR + SSH `docker compose pull && up -d --wait` flow. See [`docs/decisions/deployment-pipeline-architecture.md`](../decisions/deployment-pipeline-architecture.md) and [`docs/decisions/deployment-pipeline-slicing.md`](../decisions/deployment-pipeline-slicing.md) for the target shape.

## Prerequisites

- Azure CLI installed and authenticated (`az login --tenant 2237d332-fc65-4994-b676-61edad7be319`)
- `az account set --subscription MarketSignal2` (sub ID: `68067b9b-943f-4461-8cb5-2bc97cbc462d`)
- SSH key pair available (`~/.ssh/id_ed25519.pub` preferred; `id_rsa.pub` fallback)
- `jq` and `curl` installed locally (used by the deploy script + smoke tests)
- Owner role on the MarketSignal2 subscription
- (Post-Slice 2-3) Access to a container registry hosting `msai-backend` + `msai-frontend` images. Slice 1 provisions ACR; Slice 2 wires GH Actions to push.

## 1. Provision the infrastructure

Slice 1 onward uses Bicep IaC. Run the deployment script from your local workstation (not from the VM):

```bash
./scripts/deploy-azure.sh --what-if    # dry-run; review the plan
./scripts/deploy-azure.sh              # apply
```

This deploys `infra/main.bicep` to `msaiv2_rg` in `eastus2` and creates:

- Ubuntu 24.04 LTS VM `msai-vm` (`Standard_D4ds_v6`: 4 vCPU, 16 GB RAM, local temp disk). Note: this is the Ddsv6 family (the documented `D...d...sv6` SKU with disk), which has 10 vCPUs default quota in MarketSignal2; the related Dsv6 SKU `D4s_v6` (no `d`) is a different family with separate quota. The previous `D4s_v5` size is blocked because the DSv5 family has zero default quota and would require a quota request.
- Premium SSD data disk (128 GB, mounted at `/var/lib/msai`; Docker data root relocated there)
- VNet + Subnet + NSG (SSH from operator IP only, ports 80/443 open)
- Azure Container Registry (Basic SKU)
- Azure Key Vault (RBAC mode)
- Standard_LRS storage account + `msai-backups` Blob container
- Log Analytics workspace + Azure Monitor Agent (heartbeat DCR)
- GitHub OIDC user-assigned managed identity + federated credential (push-to-main → ACR)
- VM system-assigned managed identity with Key Vault Secrets User + AcrPull + Storage Blob Data Contributor role assignments
- Operator user with Key Vault Secrets Officer (data-plane access for seeding/rotating secrets)
- Cloud-init installs Docker engine + compose plugin and plants `/usr/local/bin/render-env-from-kv.sh` + `/etc/systemd/system/msai-render-env.service` (NOT enabled yet — Slice 3 enables on first deploy)

Re-running the script is idempotent (Bicep what-if reports no Create/Delete on subsequent runs).

## 2. SSH Into the VM

```bash
VM_IP=$(az deployment group show -g msaiv2_rg -n main --query 'properties.outputs.vmPublicIp.value' -o tsv)
ssh msaiadmin@"$VM_IP"
```

## 3. Configure Environment

> **As of Slice 3, this `.env` flow is replaced by `/run/msai.env` rendered from Key Vault at boot via `msai-render-env.service`.** Until Slice 3 merges, the manual `.env` flow below remains the operator's path for testing the prod compose without the full pipeline.

Create the `.env` file on the VM with **every** variable the new prod compose `:?`-guards. Compose will refuse to start if any required value is missing:

```bash
cat > /home/msaiadmin/.env << 'EOF'
# Image-pull config (post-PR #50). Point these at whatever registry
# CI is building to. MSAI_GIT_SHA pins the deploy to an immutable
# image tag and is what the future deployment-pipeline workflow
# bumps on every push-to-main.
MSAI_REGISTRY=<registry hostname e.g. msai.azurecr.io or ghcr.io/<org>>
MSAI_BACKEND_IMAGE=msai-backend
MSAI_FRONTEND_IMAGE=msai-frontend
MSAI_GIT_SHA=<git sha or version tag, e.g. abc1234>

# Database
POSTGRES_PASSWORD=<generate-strong-password>

# Backend production secrets (ALL required)
REPORT_SIGNING_SECRET=<generate via `openssl rand -base64 48`>
AZURE_TENANT_ID=<Entra tenant id>
AZURE_CLIENT_ID=<Entra app client id>
CORS_ORIGINS=["https://your-domain.com"]

# IB / broker (required even if you only run the default profile —
# the IB_ACCOUNT_ID/TWS_* vars are referenced by backend + supervisor)
IB_ACCOUNT_ID=<DU... for paper, U... for live>
TWS_USERID=<your-ib-username>
TWS_PASSWORD=<your-ib-password>

# Optional
POLYGON_API_KEY=<your-polygon-key>
DATABENTO_API_KEY=<your-databento-key>
PUBLIC_API_URL=https://your-domain.com
MSAI_API_KEY=                              # leave empty unless using X-API-Key auth
JWT_TENANT_ID=                             # defaults to AZURE_TENANT_ID if empty
JWT_CLIENT_ID=                             # defaults to AZURE_CLIENT_ID if empty
TRADING_MODE=paper                         # or "live" (broker profile only)
IB_PORT=4002                               # 4002 paper, 4001 live
EOF
chmod 600 /home/msaiadmin/.env
```

Generate a strong password:

```bash
openssl rand -base64 32
```

Generate the report-signing secret separately (must be ≥ 32 chars, never the dev default — the backend's Pydantic config validator at `backend/src/msai/core/config.py:295-307` raises at import time if either constraint is violated, so EVERY container that imports `msai.core.config.settings` crashloops on missing/invalid secret):

```bash
openssl rand -base64 48
```

Verify the env file resolves cleanly **before** trying to start anything:

```bash
docker compose --env-file /home/msaiadmin/.env -f docker-compose.prod.yml config > /dev/null \
  && echo "env file complete" || echo "env file incomplete — see error above"
```

## 4. Deploy the Application

> 🚧 **§4 deploy flow is in transition (post-PR #50).** The legacy
> `scp -r backend/ frontend/` block below **stops working** as soon as
> PR #50 merges, because compose now expects `image:` references to a
> registry, not local source directories. The deployment-pipeline
> branch will replace this section with a CI-driven `docker compose
pull && up -d --wait` flow. Until then, an operator can fall back to
> manually building + pushing images (see "Manual image-pull deploy"
> immediately below) or temporarily checkout the pre-PR-#50 SHA of
> `docker-compose.prod.yml` if local-build is required.

### Manual image-pull deploy (post-PR #50, pre-deployment-pipeline)

From a workstation with Docker login to your chosen registry:

```bash
# Build and push the backend image (build context is the REPO ROOT)
docker build -f backend/Dockerfile \
  -t $MSAI_REGISTRY/$MSAI_BACKEND_IMAGE:$MSAI_GIT_SHA .
docker push $MSAI_REGISTRY/$MSAI_BACKEND_IMAGE:$MSAI_GIT_SHA

# Build and push the frontend image (NEXT_PUBLIC_* are baked at build time)
docker build -f frontend/Dockerfile \
  --build-arg NEXT_PUBLIC_AZURE_CLIENT_ID=$AZURE_CLIENT_ID \
  --build-arg NEXT_PUBLIC_AZURE_TENANT_ID=$AZURE_TENANT_ID \
  --build-arg NEXT_PUBLIC_API_URL=$PUBLIC_API_URL \
  -t $MSAI_REGISTRY/$MSAI_FRONTEND_IMAGE:$MSAI_GIT_SHA \
  ./frontend
docker push $MSAI_REGISTRY/$MSAI_FRONTEND_IMAGE:$MSAI_GIT_SHA

# Copy ONLY the compose file + .env to the VM (no source code needed)
scp docker-compose.prod.yml msaiadmin@$VM_IP:/home/msaiadmin/
```

On the VM, log into the registry, pull, and bring up:

```bash
docker login $MSAI_REGISTRY                # creds vary per registry
docker compose --env-file /home/msaiadmin/.env \
  -f /home/msaiadmin/docker-compose.prod.yml pull
docker compose --env-file /home/msaiadmin/.env \
  -f /home/msaiadmin/docker-compose.prod.yml up -d --wait --wait-timeout 120
```

The `migrate` one-shot service runs `alembic upgrade head` automatically before backend + workers start. `--wait` blocks until all healthchecks pass.

### Legacy local-build path (pre-PR #50 — kept for reference; does NOT work after PR #50 merges)

```bash
scp docker-compose.prod.yml msaiadmin@$VM_IP:/home/msaiadmin/
scp -r backend/ msaiadmin@$VM_IP:/home/msaiadmin/
scp -r frontend/ msaiadmin@$VM_IP:/home/msaiadmin/
scp -r strategies/ msaiadmin@$VM_IP:/home/msaiadmin/
```

On the VM, start the services:

```bash
cd /home/msaiadmin
# Default profile: postgres, redis, migrate, backend, all arq workers,
# frontend. Safe for staging/CI deploys — does NOT touch a running
# trading session.
docker compose -f docker-compose.prod.yml up -d
```

To start the broker stack (`ib-gateway` + `live-supervisor`) for paper or live trading, opt in via the `broker` profile:

```bash
COMPOSE_PROFILES=broker docker compose -f docker-compose.prod.yml --env-file .env up -d
```

This mirrors the dev-compose pattern in `CLAUDE.md`. Re-deploying `ib-gateway` during an active live session silently disconnects the trading client (NautilusTrader gotcha #3 — duplicate `client_id`), so the deployment-pipeline workflow must NEVER auto-start the broker profile on push-to-main.

## 5. Verify Services

```bash
# Check all containers are running
docker compose -f docker-compose.prod.yml ps

# Check backend health
curl http://localhost:8000/health

# Check logs for errors
docker compose -f docker-compose.prod.yml logs --tail=50
```

## 6. Set Up TLS (Optional but Recommended)

Install Caddy as a reverse proxy for automatic HTTPS:

```bash
sudo apt-get install -y caddy

cat > /etc/caddy/Caddyfile << 'EOF'
your-domain.com {
    reverse_proxy localhost:3000
    handle_path /api/* {
        reverse_proxy localhost:8000
    }
}
EOF

sudo systemctl restart caddy
```

## 7. Set Up Nightly Backups

Add the backup script to crontab:

```bash
crontab -e
# Add this line (runs at 2:00 AM UTC daily):
0 2 * * * /home/msaiadmin/scripts/backup-to-blob.sh >> /var/log/msai-backup.log 2>&1
```

## 8. Post-Setup Checklist

- [ ] VM is running and accessible via SSH
- [ ] All Docker containers are healthy (`docker compose ps`)
- [ ] Backend health endpoint responds (`/health`)
- [ ] Frontend is accessible on port 3000
- [ ] IB Gateway is connected (check logs: `docker logs msai-ib-gateway-1`)
- [ ] `.env` file has correct credentials and is chmod 600
- [ ] Nightly backup cron job is configured
- [ ] TLS is configured (if using a public domain)

---

## Slice 1 acceptance smoke (15 min)

Run these checks after `./scripts/deploy-azure.sh` finishes provisioning. Validates all Slice 1 deliverables: Bicep idempotency, AMA + heartbeat, KV access from VM via managed identity, ACR, federated credential, Blob backup container.

```bash
# 1) Bicep what-if reports no Create/Delete on second run (idempotency)
#    (Modify operations are acceptable — Azure adds default values not in Bicep,
#     so spurious Modify diffs on re-deploy are normal and don't violate idempotency.)
#    Code-review iter 1 P2 #27 fix: parse JSON output, NOT pretty `+`/`-` text.
#    Azure CLI's default `what-if` pretty format does NOT emit `Create:`/`Delete:`
#    line prefixes, so a grep against pretty output silently misses real ops.
if ! az deployment group what-if -g msaiv2_rg -f infra/main.bicep \
  --parameters infra/main.bicepparam \
  --parameters operatorIp="$(curl -4 -fsS --max-time 10 https://api.ipify.org)" \
               operatorPrincipalId="$(az ad signed-in-user show --query id -o tsv)" \
               vmSshPublicKey="$(cat ~/.ssh/id_ed25519.pub 2>/dev/null || cat ~/.ssh/id_rsa.pub)" \
  --no-pretty-print -o json > /tmp/whatif.json; then
  echo "FAIL: az what-if command failed — see error above" >&2
elif ! jq -e '.changes' /tmp/whatif.json >/dev/null 2>&1; then
  # Code-review iter 2 P2 #31 fix: distinguish JSON-parse-failed (or missing .changes)
  # from "no Create/Delete found" — both produced false PASS in the previous version.
  echo "FAIL: what-if output not parseable JSON or missing .changes array" >&2
elif jq -e '.changes[] | select(.changeType == "Create" or .changeType == "Delete")' /tmp/whatif.json >/dev/null; then
  echo "FAIL: Create/Delete present on re-deploy — not idempotent" >&2
  jq '.changes[] | select(.changeType == "Create" or .changeType == "Delete")' /tmp/whatif.json
else
  echo "PASS: no Create/Delete on re-deploy"
fi

# 2) AMA extension provisioned successfully
az vm extension show -g msaiv2_rg --vm-name msai-vm -n AzureMonitorLinuxAgent \
  --query "provisioningState" -o tsv
# Expect: Succeeded (within 10 min of deploy)

# 3) Heartbeat in Log Analytics
WORKSPACE_NAME=$(az resource list -g msaiv2_rg \
  --resource-type Microsoft.OperationalInsights/workspaces --query "[0].name" -o tsv)
WORKSPACE_ID=$(az monitor log-analytics workspace show -g msaiv2_rg \
  -n "$WORKSPACE_NAME" --query customerId -o tsv)
az monitor log-analytics query -w "$WORKSPACE_ID" \
  --analytics-query "Heartbeat | where TimeGenerated > ago(15m) | project Computer" \
  -o table
# Expect: at least 1 row (within 15 min of deploy)

# 4) KV access from VM via system-assigned MI — using raw IMDS + KV REST.
#    (azure-cli is NOT preinstalled on Ubuntu 24 LTS; this matches what
#    render-env-from-kv.sh does, removing CLI as a dependency.)
VM_IP=$(az deployment group show -g msaiv2_rg -n main \
  --query "properties.outputs.vmPublicIp.value" -o tsv)
KV_NAME=$(az resource list -g msaiv2_rg \
  --resource-type Microsoft.KeyVault/vaults --query "[0].name" -o tsv)

# Seed a test secret (operator side) — note KV uses hyphens, not underscores
az keyvault secret set --vault-name "$KV_NAME" \
  --name dummy-test-secret --value test-value-ok -o none

# SSH and exercise IMDS + KV REST as the system-assigned MI
ssh msaiadmin@"$VM_IP" "
TOKEN=\$(curl -sf -H 'Metadata: true' 'http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://vault.azure.net' | jq -r .access_token)
[ -n \"\$TOKEN\" ] && [ \"\$TOKEN\" != null ] || { echo 'IMDS failed' >&2; exit 1; }
curl -sf -H \"Authorization: Bearer \$TOKEN\" 'https://$KV_NAME.vault.azure.net/secrets/dummy-test-secret?api-version=7.4' | jq -r .value
"
# Expect: test-value-ok

# 5) ACR exists, no admin user
ACR_NAME=$(az resource list -g msaiv2_rg \
  --resource-type Microsoft.ContainerRegistry/registries --query "[0].name" -o tsv)
az acr show -g msaiv2_rg -n "$ACR_NAME" \
  --query "{name:name, sku:sku.name, adminEnabled:adminUserEnabled}" -o table
# Expect: adminEnabled=False

# 6) Federated credential registered
az identity federated-credential list --identity-name msai-gh-oidc -g msaiv2_rg -o table
# Expect: 1 row, subject=repo:marketsignal/msai-v2:ref:refs/heads/main

# 7) Blob backup container exists
STORAGE_ACCOUNT=$(az resource list -g msaiv2_rg \
  --resource-type Microsoft.Storage/storageAccounts --query "[0].name" -o tsv)
az storage container show --account-name "$STORAGE_ACCOUNT" --name msai-backups \
  --auth-mode login -o table
# Expect: existence (your operator account needs Storage Blob Data Reader/Contributor
# role at the account or container scope; assign via `az role assignment create` if missing)
```

### If something fails

- **`az deployment group create` fails with `KeyVaultAlreadyExists` on a fresh redeploy:** Key Vault soft-delete reserves the deterministic vault name for 90 days even with `enablePurgeProtection: false`. List soft-deleted vaults: `az keyvault list-deleted --query "[?starts_with(name, 'msai-kv-')]" -o table`. To purge: `az keyvault purge --name <vault-name>`. To recover the prior vault (preserves secrets): `az keyvault recover --name <vault-name>`. The deploy script's preflight surfaces this warning automatically.
- **AMA extension stuck in `Creating` for > 10 min:** SSH to VM and check `/var/log/azure/Microsoft.Azure.Monitor.AzureMonitorLinuxAgent/CommandExecution.log`. Common cause: outbound TCP/443 blocked to `*.ods.opinsights.azure.com` / `*.handler.control.monitor.azure.com`.
- **Heartbeat table empty after 15 min:** Confirm AMA succeeded (step 2), then `az monitor data-collection rule association list --resource <vm-resource-id>` should show one association. If it doesn't, check the DCR + association resources in `infra/main.bicep`.
- **KV REST returns 403 from VM (step 4):** Managed identity propagation is normally 30-90s but can outlier to 3-5 min. Wait and retry. If persistent, check the `vmKvSecretsUserAssignment` role assignment exists: `az role assignment list --assignee $(az vm show -g msaiv2_rg -n msai-vm --query identity.principalId -o tsv) -o table`.
- **`az keyvault secret set` returns 403 from operator (step 4 seed):** Operator's Key Vault Secrets Officer role assignment hasn't propagated yet. Wait 30s and retry. If persistent, verify your principal ID matches what was passed to Bicep: `az ad signed-in-user show --query id -o tsv`.
- **`az storage container show` returns AuthorizationFailure (step 7):** Operator needs a data-plane Blob role on the account/container. Run `az role assignment create --role "Storage Blob Data Reader" --assignee <your-principal-id> --scope <storage-account-or-container-resource-id>`.

See [`docs/research/2026-05-09-deploy-pipeline-iac-foundation.md`](../research/2026-05-09-deploy-pipeline-iac-foundation.md) topics 4-5 for additional AMA + MI troubleshooting.
