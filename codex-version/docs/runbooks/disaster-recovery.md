# Disaster Recovery Runbook

1. Provision replacement VM and install dependencies.
2. Download latest backup from Blob Storage.
3. Restore `data/` and PostgreSQL volume snapshot.
4. Start stack with `docker-compose.prod.yml`.
5. Validate `account/health`, `live/status`, and data integrity checksums.
6. Re-enable live trading only after risk engine checks pass.
