#!/usr/bin/env bash
set -euo pipefail

RESOURCE_GROUP=${RESOURCE_GROUP:-msai-rg}
VM_NAME=${VM_NAME:-msai-vm}
APP_DIR=${APP_DIR:-/opt/msai-v2}

if ! command -v az >/dev/null 2>&1; then
  echo "Azure CLI is required" >&2
  exit 1
fi

echo "Syncing project to ${VM_NAME}:${APP_DIR}"
az vm run-command invoke \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${VM_NAME}" \
  --command-id RunShellScript \
  --scripts "mkdir -p ${APP_DIR}"

rsync -az --delete ./ "${VM_NAME}:${APP_DIR}/"

ssh "${VM_NAME}" "cd ${APP_DIR}/codex-version && docker compose --env-file .env.prod -f docker-compose.prod.yml pull && docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build"

echo "Deployment complete"
