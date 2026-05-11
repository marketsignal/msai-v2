# Runbook: Slice 4 Acceptance Procedure

**Purpose:** Verify Slice 4's three new operational surfaces work end-to-end on prod after merge.
**When:** Post-merge, after `iac-parity-reapply.md` completes successfully.
**Estimated time:** ~60 min (some alerts need 5-15 min to fire).
**Cost:** ~$0 (no new resources spun up; only triggers existing alert rules).

---

## Pre-conditions

- Slice 4 PR merged to `main`
- `iac-parity-reapply.md` runbook executed (Bicep re-applied; all Slice 4 resources Created)
- One Slice 4 deploy has happened on prod (via `gh workflow run deploy.yml -f git_sha=<slice-4-merge-sha>`) — this enables backup-to-blob.timer and installs azcopy
- `MSAI_API_KEY` GH Secret set (from KV's `msai-api-key`)

---

## Step 1: Manual backup smoke (~5 min)

**Purpose:** Confirm systemd timer + backup-to-blob.service + azcopy work end-to-end on prod without waiting until 02:07 UTC.

1. SSH to prod VM (via operator IP — same path Pablo always uses)
2. `sudo systemctl start backup-to-blob.service` (one-shot; runs the same script the timer fires)
3. `journalctl -u backup-to-blob.service --since '5 minutes ago' --no-pager | tail -30`
4. Verify blob landed:

   ```bash
   az storage blob list \
     --auth-mode login \
     --account-name msaibk4cd6d2obcxqaa \
     --container-name msai-backups \
     --prefix "backup-$(date -u +%Y%m%d)T" \
     --query "[].{name:name,size:properties.contentLength}" -o table
   ```

5. Expected: `postgres.sql.gz` + (if Parquet exists) `parquet/...` mirror. journal shows azcopy completion + "Backup complete".

**PASS criteria:** Blob present, journal shows exit 0.
**FAIL:** investigate per `backup-to-blob.sh` `set -euo pipefail` exit point.

---

## Step 2: Backup-failure alert smoke (~10 min)

**Purpose:** Confirm Azure Monitor alerts when `backup-to-blob.service` fails.

1. SSH to prod VM
2. Deliberately break the script: `sudo systemctl set-environment BAD_INJECTED=1 ; sudo bash -c 'echo "echo FAILED >&2 && exit 1" >> /opt/msai/scripts/backup-to-blob.sh'` — appends a force-fail line. **Remember to revert in Step 2.5.**
3. `sudo systemctl start backup-to-blob.service` — exits 1
4. Wait ~5-15 min for Azure Monitor to evaluate `msai-backup-failure-alert`
5. Check operator email at `pablo@ksgai.com`

   Subject expected to contain `msai-backup-failure-alert` (Azure Monitor default subject format).

6. **REVERT the deliberate break:** `sudo sed -i '$d' /opt/msai/scripts/backup-to-blob.sh` (removes the last line). Re-run `sudo systemctl start backup-to-blob.service` to confirm restored. (Long-term, `deploy-on-vm.sh` overwrites this file on next deploy — but don't rely on that.)

**PASS criteria:** Alert email received within 15 min. Script reverted + retest succeeds.

---

## Step 3: /health availability alert smoke (~15 min)

**Purpose:** Confirm external probe + alert fires on backend outage.

1. SSH to prod VM
2. `sudo docker compose --project-name msai -f /opt/msai/docker-compose.prod.yml --env-file /run/msai.env --env-file /run/msai-images.env stop backend`
3. Wait 5-10 min for ≥2 availability test locations to fail
4. Check operator email for `msai-health-availability-alert`
5. **Restore backend:** `sudo docker compose --project-name msai ... start backend`
6. Confirm alert auto-resolves within ~5 min after backend recovers (autoMitigate=true)

**PASS:** Alert email received; auto-resolves on recovery.
**FAIL:** Likely Application Insights ingestion lag — wait 10 more min and recheck. If still nothing, check `appInsights` resource in Azure Portal for availability-test results.

---

## Step 4: Active-`live_deployments` gate smoke (~10 min)

**Purpose:** Confirm `deploy.yml` refuses when active deployments exist.

1. Insert a sentinel via the public API (NOT raw DB write — uses the sanctioned interface):

   ```bash
   # Use msai CLI on the VM (preferred — already authenticated via internal compose net)
   ssh msaiadmin@platform.marketsignal.ai \
     'cd /opt/msai && sudo docker compose --project-name msai exec -T backend uv run msai live status'
   ```

   If no portfolio + deployment exists, create one via API (PRD §3 documents the path — `POST /api/v1/live-portfolios/` then `POST /api/v1/live/start-portfolio`). For Slice 4 acceptance specifically, EASIER: just use the existing prod portfolio if one exists; otherwise skip this Step and replace with a unit-test note (gate logic is shellcheck-clean per `tests/infra/test_workflow_deploy.sh`).

2. Confirm `active_count > 0`:

   ```bash
   curl -sH "X-API-Key: <KV msai-api-key>" https://platform.marketsignal.ai/api/v1/live/status | jq .active_count
   ```

3. Trigger deploy: `gh workflow run deploy.yml -f git_sha=<current-main-sha>`
4. `gh run watch <run-id>` — expect failure
5. Inspect log: should contain `FAIL_ACTIVE_DEPLOYMENTS_REFUSAL` at the `Refuse if active live_deployments` step
6. Verify subsequent steps (`Open transient SSH allow rule`, `Execute deploy`) are `skipped`
7. **Clear the gate:** `ssh msaiadmin@platform.marketsignal.ai 'cd /opt/msai && sudo docker compose --project-name msai exec -T backend uv run msai live stop --all'`
8. Re-trigger deploy → expect SUCCESS (proves the inverse path)

**PASS:** Step 6 failure with correct marker; Step 8 success.

---

## Step 5: Orphan-NSG-rule alert smoke (~35 min — longest because rule must age 30 min)

**Purpose:** Confirm reap-orphan-nsg-rules.yml backup + Azure Monitor alert both work.

1. Manually create a `gha-transient-acceptance-test-<date>` NSG rule:

   ```bash
   az network nsg rule create -g msaiv2_rg --nsg-name msai-nsg \
     --name "gha-transient-acceptance-$(date -u +%Y%m%d)" --priority 999 \
     --direction Inbound --access Allow --protocol Tcp \
     --source-address-prefixes 192.0.2.0/32 \
     --destination-port-ranges 22 \
     --description "Slice 4 acceptance: orphan-alert smoke. Reaper should delete within 15 min after age > 30 min." \
     --output none
   ```

2. Wait 30 min (rule must age past the threshold to trigger the alert)
3. Wait additional ~15 min for the alert evaluation OR the reaper to fire first
   - If `reap-orphan-nsg-rules.yml` cron fires first → rule deleted automatically → NO alert (correct behavior)
   - If reaper is broken → alert fires within 15 min after age > 30 min

4. **To test the alert specifically (without reaper interference):** before step 1, disable the reaper workflow with `gh workflow disable reap-orphan-nsg-rules.yml`. Re-enable after the test: `gh workflow enable reap-orphan-nsg-rules.yml`.

**PASS criteria:** Either the reaper deletes the rule (proves reaper) OR the alert fires (proves alert). Both is the goal long-term.

---

## Step 6: IaC parity drift-check

```bash
# Re-run what-if; expect no Modify/Create on existing resources
az deployment group what-if --name main -g msaiv2_rg \
  -f infra/main.bicep --parameters infra/main.bicepparam \
  --parameters "operatorIp=$(curl -sf https://ifconfig.me)" \
               "operatorPrincipalId=$(az ad signed-in-user show --query id -o tsv)" \
               "vmSshPublicKey=$(cat ~/.ssh/<your>.pub)" \
  --no-pretty-print | jq '[.changes[] | select(.changeType != "NoChange")]'
```

**PASS criteria:** Empty array (all NoChange). Slice 4 resources Created during step `iac-parity-reapply.md`; no further Creates expected.

---

## Step 7: Log evidence in CHANGELOG

Append to `docs/CHANGELOG.md` Slice 4 entry:

```
- Slice 4 acceptance 2026-MM-DD: 6/6 PASS.
  - Step 1: manual backup blob present in msai-backups
  - Step 2: backup-failure alert email received (subject: msai-backup-failure-alert)
  - Step 3: /health availability alert email received; auto-resolved on recovery
  - Step 4: deploy.yml refused with FAIL_ACTIVE_DEPLOYMENTS_REFUSAL; clear+retry succeeded
  - Step 5: <reaper deleted at <T+15min> | alert fired at <T+45min>>
  - Step 6: what-if shows 0 NoChange-deviating operations (drift-clean)
```

---

## Operational note

After Slice 4 acceptance passes, monitor:

- **First overnight backup** (02:07 UTC the night after Slice 4 deploy): blob should appear without operator action; backup-failure alert silent.
- **Email noise**: first week may show a few false-positive container-restart-heuristic alerts (Slice 4 research §3 — heuristic, not true counter). If >1/week, file an issue to revisit the KQL query.
