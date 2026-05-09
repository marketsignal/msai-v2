# VM Setup Runbook

Step-by-step guide to provision and configure the MSAI v2 production VM on Azure.

## Prerequisites

- Azure CLI installed and authenticated (`az login`)
- SSH key pair available (`~/.ssh/id_rsa` and `~/.ssh/id_rsa.pub`)
- Access to the `msai-rg` resource group (or permission to create it)

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

Create the `.env` file on the VM:

```bash
cat > /home/msai/.env << 'EOF'
POSTGRES_PASSWORD=<generate-strong-password>
POLYGON_API_KEY=<your-polygon-key>
DATABENTO_API_KEY=<your-databento-key>
TWS_USERID=<your-ib-username>
TWS_PASSWORD=<your-ib-password>
PUBLIC_API_URL=https://your-domain.com
EOF
chmod 600 /home/msai/.env
```

Generate a strong password:

```bash
openssl rand -base64 32
```

## 4. Deploy the Application

Copy the production compose file and application code to the VM:

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
