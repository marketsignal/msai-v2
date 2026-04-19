#!/usr/bin/env bash
# MSAI v2 — Azure VM Deployment Script
# Usage: ./scripts/deploy-azure.sh
set -euo pipefail

echo "=== MSAI v2 Azure Deployment ==="

# 1. Create resource group
az group create --name msai-rg --location eastus

# 2. Create VM (D4s_v5: 4 vCPU, 16 GB RAM)
az vm create \
  --resource-group msai-rg \
  --name msai-vm \
  --image Ubuntu2404 \
  --size Standard_D4s_v5 \
  --admin-username msai \
  --generate-ssh-keys \
  --os-disk-size-gb 256

# 3. Open ports
az vm open-port --resource-group msai-rg --name msai-vm --port 443 --priority 100
az vm open-port --resource-group msai-rg --name msai-vm --port 80 --priority 101

# 4. Install Docker
az vm run-command invoke \
  --resource-group msai-rg \
  --name msai-vm \
  --command-id RunShellScript \
  --scripts "curl -fsSL https://get.docker.com | sh && usermod -aG docker msai"

echo "=== VM created. SSH: ssh msai@$(az vm show -g msai-rg -n msai-vm --query publicIps -o tsv) ==="
echo "Next: scp docker-compose.prod.yml to VM and run docker compose up -d"
