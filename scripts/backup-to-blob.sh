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
# Slice 4 (this commit): Parquet mirror uses azcopy v10.22+ with the
# AZCOPY_AUTO_LOGIN_TYPE=MSI env var (research §1 finding 1 — `azcopy login --identity`
# was deprecated in v10.22). Postgres single-blob still uses `az storage blob upload`
# (correct tool for streaming pg_dump | gzip | --file /dev/stdin).
#
# azcopy install: see scripts/install-azcopy.sh; binary lives at /usr/local/bin/azcopy.
# cloud-init also installs it for fresh provisions.

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

# Post-upload sanity: code-review P1 caught that pipefail propagates pg_dump failure
# but `az storage blob upload` will happily upload a partial/empty gzipped stream —
# you discover the corruption at restore time. A gzip of an empty pg_dump is ~20B
# (just headers); an empty-DB dump is ~360B; populated-DB dumps are 1KB+. Floor of
# 100B catches "pg_dump failed before writing anything"; floor of 20KB is a stretch
# given Slice 3's empty-DB Hawk's-gate landed a 372B blob. Compromise: 100B floor
# to catch pg_dump's "no output" failure mode but not over-tighten and break the
# empty-DB Hawk's gate.
PG_BLOB_SIZE=$(az storage blob show \
    --auth-mode login \
    --account-name "$STORAGE_ACCT" \
    --container-name "$CONTAINER" \
    --name "$PG_BLOB_NAME" \
    --query 'properties.contentLength' -o tsv 2>/dev/null || echo 0)
if [[ "$PG_BLOB_SIZE" -lt 100 ]]; then
    echo "ERROR: Postgres backup blob is only ${PG_BLOB_SIZE} bytes — likely a partial / empty pg_dump." >&2
    echo "       Inspect via: az storage blob download --account-name $STORAGE_ACCT --container-name $CONTAINER --name $PG_BLOB_NAME --file -" >&2
    exit 5
fi
echo "→ Postgres blob OK: ${PG_BLOB_SIZE} bytes"

# 2. Parquet tree — azcopy via MI (research §1 finding 1: AZCOPY_AUTO_LOGIN_TYPE=MSI
# env var, NOT deprecated `--identity` flag). ~10× faster than az storage blob
# upload-batch at scale.
PARQUET_SRC="${PARQUET_SRC:-/var/lib/msai/docker/volumes/msai_app_data/_data/parquet}"
if [[ -d "$PARQUET_SRC" ]] && [[ -n "$(ls -A "$PARQUET_SRC" 2>/dev/null || true)" ]]; then
    if ! command -v azcopy >/dev/null 2>&1; then
        echo "ERROR: azcopy not on PATH — install via /opt/msai/scripts/install-azcopy.sh" >&2
        exit 4
    fi
    echo "→ Mirroring Parquet tree from $PARQUET_SRC via azcopy"
    export AZCOPY_AUTO_LOGIN_TYPE=MSI
    # Code-review P2 fix: azcopy's directory-copy semantics vary by version with
    # trailing-slash handling. Use explicit `<src>/*` + dest-with-slash so the
    # tree lands at `<dest>/<file-tree>` regardless of azcopy version. v10.22+
    # without this can produce `parquet/parquet/...` (double-nested basename).
    azcopy cp "${PARQUET_SRC%/}/*" \
        "https://${STORAGE_ACCT}.blob.core.windows.net/${CONTAINER}/backup-${TIMESTAMP}/parquet/" \
        --recursive \
        --output-level=essential
else
    echo "→ Parquet src $PARQUET_SRC missing or empty; skipping (acceptable for Hawk's gate)"
fi

echo "=== Backup complete: backup-${TIMESTAMP} ==="
echo
echo "Verify with:"
echo "  az storage blob list --auth-mode login --account-name $STORAGE_ACCT \\"
echo "      --container-name $CONTAINER --prefix backup-${TIMESTAMP%T*} --output table"
