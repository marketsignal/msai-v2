#!/usr/bin/env bash
set -euo pipefail

ACCOUNT_NAME=${AZURE_STORAGE_ACCOUNT:?AZURE_STORAGE_ACCOUNT is required}
CONTAINER_NAME=${AZURE_STORAGE_CONTAINER:-msai-backups}
DATA_ROOT=${DATA_ROOT:-./data}
STAMP=$(date +"%Y%m%d-%H%M%S")
ARCHIVE="msai-backup-${STAMP}.tar.zst"

if ! command -v az >/dev/null 2>&1; then
  echo "Azure CLI is required" >&2
  exit 1
fi

tar --zstd -cf "${ARCHIVE}" "${DATA_ROOT}"
az storage blob upload \
  --account-name "${ACCOUNT_NAME}" \
  --container-name "${CONTAINER_NAME}" \
  --file "${ARCHIVE}" \
  --name "${ARCHIVE}" \
  --auth-mode login \
  --overwrite false

rm -f "${ARCHIVE}"
echo "Backup uploaded: ${ARCHIVE}"
