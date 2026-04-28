# Runbook: Instrument Cache → Registry Migration

**Migration revisions:** A (`d1e2f3g4h5i6` — additive `trading_hours` column) → B (`e2f3g4h5i6j7` — data migration + DROP `instrument_cache`)
**Maintenance window:** 5–15 minutes. Stack must be DOWN during the data migration.

## Preconditions

- [ ] All active live deploys are STOPPED (or expected to restart after the migration). Use `msai live status` to inventory.
- [ ] You have a `pg_dump` checkpoint of `instrument_cache`, `instrument_definitions`, and `instrument_aliases`.
- [ ] You have read PRD §9 binding decisions: [docs/prds/instrument-cache-registry-migration.md](../prds/instrument-cache-registry-migration.md).

## Step 1 — `pg_dump` checkpoint (REQUIRED — downgrade is data-lossy)

The Alembic downgrade is **schema-only**. Data restoration requires `pg_dump`. Without this checkpoint, a rollback to before this PR loses every `instrument_cache` row that wasn't already in the registry.

```bash
docker exec -it msai-postgres-dev pg_dump \
  -U msai \
  -t instrument_cache \
  -t instrument_definitions \
  -t instrument_aliases \
  msai > /tmp/pre-cache-mig-$(date +%Y%m%d-%H%M).sql
```

Verify the file is non-empty.

## Step 2 — Preflight gate

```bash
cd backend && uv run python scripts/preflight_cache_migration.py
```

**Expected:** `[ok] All N active deployment-member rows' instruments resolve through the registry. Preflight passed.` and exit 0.

**If preflight fails:** the script prints `msai instruments refresh --symbols X --provider interactive_brokers` for every missing alias. Run the suggested commands, then re-run preflight. Do NOT proceed to Step 3 until preflight exits 0.

## Step 3 — Stop the stack

```bash
docker compose -f docker-compose.dev.yml down
```

## Step 4 — Apply the migration

Bring up just postgres + redis so alembic can connect:

```bash
docker compose -f docker-compose.dev.yml up -d postgres redis
cd backend && uv run alembic upgrade head
```

**Expected output:**

```
INFO [alembic.runtime.migration] Running upgrade <prior> -> d1e2f3g4h5i6, add trading_hours...
INFO [alembic.runtime.migration] Running upgrade d1e2f3g4h5i6 -> e2f3g4h5i6j7, drop instrument_cache
[migration] copying N instrument_cache rows → registry
[migration] dropped instrument_cache table
```

**If migration aborts on a malformed row** (the migration is fail-loud on bad rows): the error message names the offending `canonical_id`. Inspect with:

```bash
docker exec -it msai-postgres-dev psql -U msai -d msai -c "SELECT canonical_id, asset_class, venue FROM instrument_cache WHERE canonical_id LIKE '%<bad>%'"
```

Fix at source (manual `UPDATE` or `DELETE`), then re-run `alembic upgrade head`.

## Step 5 — Bring up the rest of the stack

```bash
docker compose -f docker-compose.dev.yml up -d
./scripts/restart-workers.sh
```

The `restart-workers.sh` step is mandatory per `feedback_restart_workers_after_merges.md` — long-running worker containers cache imported modules at startup; without restart, they keep the OLD `models/instrument_cache.py` import in memory and crash on first DB call.

## Step 6 — Smoke test

```bash
curl -sf http://localhost:8800/health
```

Expected: `200 OK`.

```bash
docker exec -it msai-postgres-dev psql -U msai -d msai \
  -c "SELECT count(*) FROM instrument_definitions; SELECT count(*) FROM instrument_aliases;"
```

Expected: counts ≥ pre-migration `instrument_cache` row count.

```bash
docker exec -it msai-postgres-dev psql -U msai -d msai \
  -c "SELECT table_name FROM information_schema.tables WHERE table_name='instrument_cache'"
```

Expected: empty result (table dropped).

## Step 7 — Branch-local restart drill (per US-005)

Evidence required for the branch-local restart drill (US-005 verification).

1. Spawn a paper deploy that holds at least one open position (use `msai live start-portfolio` or `msai live status` to confirm an existing one).
2. Run Steps 3–6 above (compose down → migrate → compose up → workers restart).
3. Verify:
   - Deploy resumes (or is cleanly stopped via `msai live kill-all`).
   - Open position rehydrates correctly via `position_reader.py` → check `msai live positions`.
   - No log line in container logs references `instrument_cache` after restart:
     ```bash
     docker compose -f docker-compose.dev.yml logs backend worker | grep -i "instrument_cache" || echo "clean"
     ```
     Expected: prints `clean`.

Capture the drill output (stdout of each command above) and paste into the PR description as the US-005 restart-drill evidence.

## Rollback (data-lossy — only if Step 4 or later fails fatally)

```bash
# Restore the pg_dump from Step 1
docker exec -i msai-postgres-dev psql -U msai -d msai < /tmp/pre-cache-mig-YYYYMMDD-HHMM.sql

# Then re-apply the prior alembic head
cd backend && uv run alembic downgrade <PRIOR_HEAD>
```

## Post-merge tasks

- [ ] Update CHANGELOG with the migration note.
- [ ] Mark CONTINUITY's `## Done (cont'd N)` section.
- [ ] If a real-money deploy is on this stack, schedule a paper-week soak before the next live drill.
