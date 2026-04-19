#!/usr/bin/env bash
# Nightly backup of Parquet data + PostgreSQL dump to Azure Blob Storage
# Usage: ./scripts/backup-to-blob.sh
# Recommended: add to crontab — 0 2 * * * /path/to/backup-to-blob.sh
set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/tmp/msai-backup-${TIMESTAMP}"
mkdir -p "$BACKUP_DIR"

echo "=== MSAI v2 Backup — ${TIMESTAMP} ==="

# 1. PostgreSQL dump
echo "Dumping PostgreSQL..."
docker exec msai-postgres-1 pg_dump -U msai msai > "$BACKUP_DIR/msai_db.sql"

# 2. Copy Parquet data
echo "Copying Parquet data..."
cp -r /app/data/parquet "$BACKUP_DIR/parquet"

# 3. Upload to Azure Blob
echo "Uploading to Azure Blob Storage..."
az storage blob upload-batch \
  --account-name msaistorage \
  --destination backups \
  --source "$BACKUP_DIR" \
  --destination-path "backup-${TIMESTAMP}"

# 4. Cleanup
rm -rf "$BACKUP_DIR"
echo "Backup completed: backup-${TIMESTAMP}"
