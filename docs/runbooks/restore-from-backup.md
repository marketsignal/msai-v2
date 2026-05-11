# Runbook: Restore Postgres from Backup Blob

**Purpose:** Verify nightly Postgres backups are usable. Quarterly drill.
**Estimated time:** 20-30 min.
**Cost:** ~$0 (throwaway local container; no Azure resources spun up).

---

## 1. Pick a recent backup

```bash
az storage blob list \
  --auth-mode login \
  --account-name msaibk4cd6d2obcxqaa \
  --container-name msai-backups \
  --prefix backup- \
  --query "[].name" -o tsv \
  | sort | tail -5
```

Pick the most recent `backup-<UTC-iso>/postgres.sql.gz`. Copy the full blob name.

## 2. Download to a temp dir

```bash
BLOB="backup-20260511T020700Z/postgres.sql.gz"   # edit
mkdir -p /tmp/msai-restore && cd /tmp/msai-restore
az storage blob download \
  --auth-mode login \
  --account-name msaibk4cd6d2obcxqaa \
  --container-name msai-backups \
  --name "$BLOB" \
  --file postgres.sql.gz
ls -la postgres.sql.gz
```

## 3. Spin up a throwaway Postgres locally

```bash
docker run --name msai-restore-test --rm -d \
  -e POSTGRES_PASSWORD=restore-test \
  -e POSTGRES_USER=msai \
  -e POSTGRES_DB=msai \
  -p 15432:5432 \
  postgres:16-alpine
# Wait for ready
for i in 1 2 3 4 5 6; do
  docker exec msai-restore-test pg_isready -U msai 2>/dev/null && break
  sleep 2
done
```

## 4. Restore

```bash
gunzip -c postgres.sql.gz | docker exec -i msai-restore-test psql -U msai msai
```

## 5. Spot-check schema parity

```bash
docker exec msai-restore-test psql -U msai msai -c "\dt"
```

Expected: same tables as prod (`live_deployments`, `live_portfolios`, `live_portfolio_revisions`, `live_portfolio_revision_strategies`, `backtests`, `strategies`, `instrument_definitions`, `instrument_aliases`, `audit_log`, …). Count should match prod's `\dt` count.

```bash
# Pick one table and check row count looks sensible
docker exec msai-restore-test psql -U msai msai -c "SELECT count(*) FROM live_deployments"
```

## 6. Tear down

```bash
docker stop msai-restore-test     # --rm flag auto-removes the container
rm -rf /tmp/msai-restore
```

## 7. Log evidence

Append to `docs/CHANGELOG.md` under the most recent Slice 4 entry:

```
- Restore-from-backup drill 2026-MM-DD: PASS. Blob backup-<UTC>/postgres.sql.gz restored to local Postgres 16, \dt count = N (matches prod), spot-checked live_deployments row count = M.
```

---

## Failure modes

| Symptom                                           | Diagnosis                                               | Fix                                                                                                                         |
| ------------------------------------------------- | ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `az storage blob download` returns 403            | Missing `Storage Blob Data Reader` on operator          | `az role assignment create --role "Storage Blob Data Reader" --scope <storage-account-id> --assignee-object-id <pablo-oid>` |
| `gunzip: invalid`                                 | Blob corrupted in transit; OR azcopy upload was partial | Re-download (don't trust this blob); inspect next-most-recent backup; alert                                                 |
| `psql: relation "X" does not exist` after restore | Backup ran while schema was mid-migration               | Likely a migration race — pick the next-day backup                                                                          |
| Postgres container won't start                    | Port 15432 in use                                       | Change `-p 15432:5432` to `-p 15433:5432` (etc.)                                                                            |
