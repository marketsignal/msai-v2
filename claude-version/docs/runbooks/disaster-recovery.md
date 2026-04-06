# Disaster Recovery Runbook

Procedure for recovering MSAI v2 from a VM failure or data loss scenario.

## Severity Levels

| Level | Scenario               | RTO    | RPO                         |
| ----- | ---------------------- | ------ | --------------------------- |
| P1    | VM completely lost     | 1 hour | Last nightly backup         |
| P2    | Database corruption    | 30 min | Last nightly backup         |
| P3    | Single container crash | 5 min  | No data loss (auto-restart) |

## P3: Single Container Crash

Docker Compose is configured with `restart: unless-stopped`. Containers recover automatically.

If a container is stuck in a restart loop:

```bash
# Check which container is failing
docker compose -f docker-compose.prod.yml ps

# View container logs
docker compose -f docker-compose.prod.yml logs <service-name> --tail=100

# Force restart the specific service
docker compose -f docker-compose.prod.yml restart <service-name>
```

## P2: Database Corruption

### 1. Stop the Application

```bash
docker compose -f docker-compose.prod.yml down
```

### 2. Remove Corrupted Data

```bash
docker volume rm msai_postgres_data
```

### 3. Identify the Latest Backup

```bash
az storage blob list \
  --account-name msaistorage \
  --container-name backups \
  --query "[].name" \
  --output table | sort | tail -5
```

### 4. Download the Backup

```bash
BACKUP_NAME="backup-YYYYMMDD_HHMMSS"  # Replace with actual backup name
mkdir -p /tmp/msai-restore
az storage blob download-batch \
  --account-name msaistorage \
  --source backups \
  --destination /tmp/msai-restore \
  --pattern "${BACKUP_NAME}/*"
```

### 5. Restore PostgreSQL

```bash
# Start only postgres
docker compose -f docker-compose.prod.yml up -d postgres

# Wait for it to be healthy
docker compose -f docker-compose.prod.yml exec postgres pg_isready -U msai

# Restore the dump
cat /tmp/msai-restore/${BACKUP_NAME}/msai_db.sql | \
  docker compose -f docker-compose.prod.yml exec -T postgres psql -U msai msai
```

### 6. Restore Parquet Data

```bash
# Copy Parquet files back into the app_data volume
docker cp /tmp/msai-restore/${BACKUP_NAME}/parquet msai-backend-1:/app/data/parquet
```

### 7. Restart All Services

```bash
docker compose -f docker-compose.prod.yml up -d
```

### 8. Verify

```bash
curl http://localhost:8000/api/v1/health
docker compose -f docker-compose.prod.yml logs --tail=20
```

## P1: VM Completely Lost

### 1. Provision a New VM

```bash
./scripts/deploy-azure.sh
```

### 2. Configure the New VM

Follow the [VM Setup Runbook](./vm-setup.md) steps 2-4.

### 3. Restore from Backup

Follow the P2 steps above (steps 3-8) to restore the database and Parquet data from Azure Blob Storage.

### 4. Verify Full System

- [ ] All containers running: `docker compose -f docker-compose.prod.yml ps`
- [ ] Backend health check passes: `curl http://localhost:8000/api/v1/health`
- [ ] Database has expected data: check table row counts
- [ ] Parquet data directory is populated: `ls -la /app/data/parquet/`
- [ ] IB Gateway is connected (if applicable)
- [ ] Frontend loads correctly
- [ ] Cron backup job is re-configured

## Backup Verification

Periodically (monthly) test the restore procedure on a throwaway VM to validate backup integrity:

```bash
# 1. Create a test VM
az vm create --resource-group msai-rg --name msai-test-restore \
  --image Ubuntu2404 --size Standard_B2s --admin-username msai --generate-ssh-keys

# 2. Run the restore procedure on the test VM
# 3. Verify data integrity
# 4. Delete the test VM
az vm delete --resource-group msai-rg --name msai-test-restore --yes
```

## Contacts

| Role            | Contact        |
| --------------- | -------------- |
| Primary on-call | (fill in)      |
| Azure admin     | (fill in)      |
| IB support      | ibkrguides.com |
