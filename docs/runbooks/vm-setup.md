# VM Setup Runbook

Step-by-step guide to provision and configure the MSAI v2 production VM on Azure.

> ⚠️ **Runbook in transition (2026-05-09).** As of PR #50 (`feat/prod-compose-deployable`), `docker-compose.prod.yml` no longer builds images locally — it expects pre-built images pulled from a registry. The `scp -r backend/ frontend/` flow in §4 below is **legacy** and stops working as soon as PR #50 merges. The deployment-pipeline branch will rewrite §4 + §5 for the image-pull deploy path (Bicep IaC + GH Actions OIDC + ACR + SSH `docker compose pull && up -d --wait`). Until then, an operator following this runbook needs the **full required-env-var list in §3** and a way to build + push images manually. See [`docs/decisions/deployment-pipeline-architecture.md`](../decisions/deployment-pipeline-architecture.md) for the target shape.

## Prerequisites

- Azure CLI installed and authenticated (`az login`)
- SSH key pair available (`~/.ssh/id_rsa` and `~/.ssh/id_rsa.pub`)
- Access to the `msai-rg` resource group (or permission to create it)
- (Post-PR #50) Access to a container registry hosting `msai-backend` + `msai-frontend` images. Until the deployment-pipeline branch lands ACR provisioning, the operator can use any registry they have write access to (`docker.io/<your-org>`, GHCR, etc.).

## 1. Provision the VM

Run the deployment script from your local machine:

```bash
./scripts/deploy-azure.sh
```

This creates:

- Resource group `msai-rg` in `eastus`
- Ubuntu 24.04 VM (`Standard_D4s_v6`: 4 vCPU, 16 GB RAM, 256 GB disk). Note: Ddsv6 family has default subscription quota (10 vCPUs); the previous `D4s_v5` size is blocked on MarketSignal2 because the DSv5 family has zero default quota and would require a quota request.
- Ports 80 and 443 open
- Docker installed

## 2. SSH Into the VM

```bash
VM_IP=$(az vm show -g msai-rg -n msai-vm --query publicIps -o tsv)
ssh msai@$VM_IP
```

## 3. Configure Environment

Create the `.env` file on the VM with **every** variable the new prod compose `:?`-guards. Compose will refuse to start if any required value is missing:

```bash
cat > /home/msai/.env << 'EOF'
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
chmod 600 /home/msai/.env
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
docker compose --env-file /home/msai/.env -f docker-compose.prod.yml config > /dev/null \
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
scp docker-compose.prod.yml msai@$VM_IP:/home/msai/
```

On the VM, log into the registry, pull, and bring up:

```bash
docker login $MSAI_REGISTRY                # creds vary per registry
docker compose --env-file /home/msai/.env \
  -f /home/msai/docker-compose.prod.yml pull
docker compose --env-file /home/msai/.env \
  -f /home/msai/docker-compose.prod.yml up -d --wait --wait-timeout 120
```

The `migrate` one-shot service runs `alembic upgrade head` automatically before backend + workers start. `--wait` blocks until all healthchecks pass.

### Legacy local-build path (pre-PR #50 — kept for reference; does NOT work after PR #50 merges)

```bash
scp docker-compose.prod.yml msai@$VM_IP:/home/msai/
scp -r backend/ msai@$VM_IP:/home/msai/
scp -r frontend/ msai@$VM_IP:/home/msai/
scp -r strategies/ msai@$VM_IP:/home/msai/
```

On the VM, start the services:

```bash
cd /home/msai
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
0 2 * * * /home/msai/scripts/backup-to-blob.sh >> /var/log/msai-backup.log 2>&1
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
