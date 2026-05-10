#!/usr/bin/env bash
# MSAI v2 — Postgres + Parquet backup to Azure Blob Storage.
#
# Slice 3 rewrite: reads target storage account + container from Bicep outputs (no
# hardcoded `msaistorage` from the pre-Slice-1 sketch). Authenticates via the VM's
# system-assigned managed identity (Slice 1 grants Storage Blob Data Contributor on
# the storage account). Streams pg_dump | gzip directly to a single blob — no temp
# file on disk.
#
# Usage (operator, on the prod or rehearsal VM):
#   sudo /opt/msai/scripts/backup-to-blob.sh
#   RESOURCE_GROUP=msaiv2-rehearsal-20260512 sudo /opt/msai/scripts/backup-to-blob.sh
#
# Hawk's Gate (council Plan-Review iter 1 of slicing verdict §Slice 3): operator
# MUST run this against the empty prod Postgres BEFORE the first `up -d --wait`
# and verify the dump appears in the msai-backups Blob container. Evidence in PR.
#
# Slice 4 carry-over (research §4 finding 5): replace the Parquet `cp -r` +
# `az storage blob upload-batch` step with `azcopy --recursive` for the nightly
# cron — `--auth-mode login` is ~10× slower than azcopy at scale.

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-msaiv2_rg}"
DEPLOYMENT_NAME="${DEPLOYMENT_NAME:-msai-iac}"

echo "=== MSAI v2 backup-to-blob — RG=$RESOURCE_GROUP deployment=$DEPLOYMENT_NAME ==="

# Resolve target storage account + container from Bicep outputs.
STORAGE_ACCT="$(az deployment group show \
    --name "$DEPLOYMENT_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query 'properties.outputs.backupsStorageAccount.value' \
    --output tsv 2>/dev/null || true)"

CONTAINER="$(az deployment group show \
    --name "$DEPLOYMENT_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query 'properties.outputs.backupsContainerName.value' \
    --output tsv 2>/dev/null || true)"

if [[ -z "$STORAGE_ACCT" || -z "$CONTAINER" ]]; then
    cat >&2 <<EOF
ERROR: Could not resolve Bicep outputs.

  backupsStorageAccount: '${STORAGE_ACCT:-<empty>}'
  backupsContainerName:  '${CONTAINER:-<empty>}'

Common causes:
  1. Deployment name mismatch — verify with:
     az deployment group list -g $RESOURCE_GROUP --query "[].name" -o tsv
     If your deployment is not named '$DEPLOYMENT_NAME', re-run with
     DEPLOYMENT_NAME=<actual> sudo \$0
  2. Resource group mismatch — verify RESOURCE_GROUP env var
  3. MI not authenticated — this script runs 'az login --identity'; if that
     fails the VM may not have system-assigned MI enabled (Slice 1 issue)
EOF
    exit 2
fi

echo "Target: $STORAGE_ACCT / $CONTAINER"

# Authenticate via VM system-assigned MI. Idempotent — succeeds even if already logged in.
az login --identity --output none

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PG_BLOB_NAME="backup-${TIMESTAMP}/postgres.sql.gz"

# 1. PostgreSQL — stream pg_dump | gzip directly to a blob (no temp file on disk).
echo "→ Streaming Postgres dump to ${PG_BLOB_NAME}"
PG_CONTAINER="$(docker ps --filter name=postgres --format '{{.Names}}' | head -n1)"
if [[ -z "$PG_CONTAINER" ]]; then
    echo "ERROR: no running container matching 'postgres' — is the stack up?" >&2
    exit 3
fi

docker exec "$PG_CONTAINER" pg_dump -U msai msai \
    | gzip \
    | az storage blob upload \
        --auth-mode login \
        --account-name "$STORAGE_ACCT" \
        --container-name "$CONTAINER" \
        --name "$PG_BLOB_NAME" \
        --file /dev/stdin \
        --overwrite \
        --output none

# 2. Parquet tree — `az storage blob upload-batch` via MI.
# NOTE (Slice 4): switch to `azcopy login --identity && azcopy cp --recursive` for
# nightly cron — ~10× faster than `az storage blob upload-batch --auth-mode login`
# at scale. Keep this path for Hawk's-gate (small dataset; one-shot operator run).
PARQUET_SRC="${PARQUET_SRC:-/var/lib/msai/docker/volumes/msai_app_data/_data/parquet}"
if [[ -d "$PARQUET_SRC" ]] && [[ -n "$(ls -A "$PARQUET_SRC" 2>/dev/null || true)" ]]; then
    echo "→ Mirroring Parquet tree from $PARQUET_SRC"
    az storage blob upload-batch \
        --auth-mode login \
        --account-name "$STORAGE_ACCT" \
        --destination "$CONTAINER" \
        --destination-path "backup-${TIMESTAMP}/parquet" \
        --source "$PARQUET_SRC" \
        --overwrite \
        --output none
else
    echo "→ Parquet src $PARQUET_SRC missing or empty; skipping (acceptable for Hawk's gate)"
fi

echo "=== Backup complete: backup-${TIMESTAMP} ==="
echo
echo "Verify with:"
echo "  az storage blob list --auth-mode login --account-name $STORAGE_ACCT \\"
echo "      --container-name $CONTAINER --prefix backup-${TIMESTAMP%T*} --output table"
