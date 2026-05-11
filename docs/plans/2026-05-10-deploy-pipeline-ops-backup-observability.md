# Deployment Pipeline Slice 4: Ops, Backup, Observability — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Use `- [ ]` for tracking.

**Goal:** Land Slice 4 (final slice of the 4-PR deploy-pipeline series). Three new operational surfaces — nightly backup cron via systemd timer + azcopy, Log Analytics scheduled-query alerts + availability test + Application Insights, active-`live_deployments` hard refusal gate in `deploy.yml` — plus folded-in IaC parity carry-overs from Slice 3 (Reader-on-RG for VM MI declaratively, idempotent prod Bicep re-apply runbook).

**Architecture:** Council-ratified Approach A, Slice 4 of 4 (per [`docs/decisions/deployment-pipeline-slicing.md`](../decisions/deployment-pipeline-slicing.md) §Slice 4). Six research-driven design adjustments from `docs/research/2026-05-10-deploy-pipeline-ops-backup-observability.md`: (1) azcopy uses `AZCOPY_AUTO_LOGIN_TYPE=MSI` env var (NOT deprecated `--identity` flag); (2) `Microsoft.Insights/scheduledQueryRules` pinned to `2022-06-15` GA (NOT 2023-12-01 preview which is unregistered in many regions); (3) container restart "alert" is a heuristic on the Syslog stream (Container Insights is K8s-only — AMA on a VM doesn't emit native per-container metrics); (4) systemd timer `OnCalendar=*-*-* 02:07:00 UTC` + `Persistent=true` + `RandomizedDelaySec=300`; (5) storage lifecycle policy singleton `name='default'` + `prefixMatch=['msai-backups/backup-']` + `daysAfterCreationGreaterThan: 30`; (6) availability test requires Application Insights component (`kind: 'standard'`; URL-ping retires 2026-09-30; 3 locations × 5min to stay <€10/mo).

**Tech Stack:** Bicep CLI 0.43.8, Azure Monitor (Log Analytics + scheduled query rules + Application Insights availability tests), systemd 255 (Ubuntu 24.04), azcopy v10.22+, Docker Compose v2.40+, GH Actions, bash.

---

## Approach Comparison

**PRE-DONE — council-ratified.** Slice 4 scope is locked at the slicing verdict [`docs/decisions/deployment-pipeline-slicing.md`](../decisions/deployment-pipeline-slicing.md) §Slice 4. No re-brainstorm; only implementation choices remain.

### Chosen Default

**Slice 4 of Approach A.** Three new operational surfaces + IaC parity carry-over folded in. All declarative (Bicep) except the systemd units + deploy.yml step. Single PR; first PR of the series with no operator-gated rehearsal before merge (acceptance is "next overnight backup fires + each alert tested under load after merge").

### Best Credible Alternative

**Split Slice 4 into 4a (backup) + 4b (alerts) + 4c (deploy gate) micro-PRs.** Tempting because each surface is independently testable. Rejected because (a) all three share `infra/main.bicep` edits — splitting creates rebase churn; (b) the IaC parity work is a one-shot redeploy regardless; (c) slicing verdict committed to one Slice-4 PR.

### Scoring (fixed axes)

| Axis                  | Default (one PR)                                                  | Alternative (3 micro-PRs)   |
| --------------------- | ----------------------------------------------------------------- | --------------------------- |
| Complexity            | M                                                                 | M                           |
| Blast Radius          | M (prod alerts wiring; misfire = noise, not outage)               | M                           |
| Reversibility         | M (Bicep what-if shows drift; each scheduledQueryRules deletable) | M                           |
| Time to Validate      | M (~hours; some alerts require time-based triggers)               | H (3× the same Bicep churn) |
| User/Correctness Risk | L (Slice 4 doesn't touch app code)                                | L                           |

### Cheapest Falsifying Test

`az deployment group what-if -f infra/main.bicep -g msaiv2_rg` (with new params if any). If it shows Modify operations on existing Slice 1/3 resources (other than the legitimate vmMiReaderAssignment Create), the IaC parity assumption is wrong. <5 min.

## Contrarian Verdict

**VALIDATE (no fresh council).** Slicing verdict already ratified this slice. The Contrarian's standing concern (Slice 3 Blocking Objection #4) was specifically about Slice 3's "first real deploy" — Slice 4 ops layer doesn't carry the same risk class. No rehearsal RG required for Slice 4 because the new surfaces are alert-rule additions (idempotent), a systemd timer (easy to revert), and a deploy.yml pre-flight step (off-by-default via the force flag).

Real-money posture and the active-`live_deployments` gate are the focus — the gate itself is the safety bar, not the surface being de-risked.

---

## Files

### Created

- **`scripts/backup-to-blob.service`** — systemd unit (Type=oneshot), runs `/opt/msai/scripts/backup-to-blob.sh` as root with `Environment=RESOURCE_GROUP=msaiv2_rg DEPLOYMENT_NAME=main`. ~20 lines.

- **`scripts/backup-to-blob.timer`** — systemd timer: `OnCalendar=*-*-* 02:07:00 UTC` + `Persistent=true` + `RandomizedDelaySec=300` (research §4 finding 4 — Persistent+OnCalendar+RandomizedDelay is the canonical combo). ~12 lines.

- **`scripts/install-azcopy.sh`** — idempotent install of azcopy v10.22+ to `/usr/local/bin/azcopy` via the official tarball (research §1 — azcopy is NOT in apt; install via tarball). Called by `deploy-on-vm.sh` on first deploy after Slice 4 merge AND added to Slice 1 cloud-init for future provisions. ~30 lines.

- **`infra/alerts.bicep`** — module file for all Slice 4 observability resources to keep `main.bicep` short:
  - Action Group `msai-ops-alerts` (email `pablo@ksgai.com`)
  - Application Insights component `msai-app-insights` (workspaceResourceId linked to Slice 1 Log Analytics)
  - Availability test `msai-health-ping` (`kind: 'standard'`, `geoLocations: 3` to stay <€10/mo per research §6)
  - 4 scheduled query alerts (pinned API `Microsoft.Insights/scheduledQueryRules@2022-06-15` per research §2):
    1. `/health` 5xx (Application Insights query against availability test results)
    2. backup-to-blob.service failure (KQL against Syslog stream)
    3. orphan `gha-transient-*` NSG rule >30min (AzureActivity table query)
    4. container restart heuristic (Syslog stream containing `restarted container` patterns; research §3 — this is a heuristic, not a true counter)
  - All alerts route to the action group; severities 1-3 per PRD AC

- **`docs/runbooks/restore-from-backup.md`** — operator walkthrough: pick recent blob → throwaway local Postgres container → `azcopy cp` or `az storage blob download` → `gunzip | psql` → `\dt` spot-check → tear down. ~100 lines.

- **`docs/runbooks/iac-parity-reapply.md`** — 1-page: run `az deployment group create -f infra/main.bicep -g msaiv2_rg` after merge; expected: only `vmMiReaderAssignment` Create + maybe Modify on already-existing-as-manual-grant resources (which is the parity). ~40 lines.

- **`docs/runbooks/slice-4-acceptance.md`** — Slice 4 acceptance procedure: (a) manually trigger backup-to-blob.timer + verify blob; (b) deliberate /health outage; (c) deliberate container restart; (d) deliberate stale live_deployments row → confirm deploy refusal; (e) re-apply Bicep → what-if clean. ~120 lines.

- **`tests/infra/test_alerts_bicep.sh`** — actionlint + grep assertions on `infra/alerts.bicep`: scheduledQueryRules API version `2022-06-15`, action group email, availability test geoLocations <= 3, lifecycle policy prefix. Mirrors existing `tests/infra/test_bicep.sh` style.

### Modified

- **`scripts/backup-to-blob.sh`** — switch Parquet mirror block from `az storage blob upload-batch --auth-mode login` to `azcopy cp --recursive` with `AZCOPY_AUTO_LOGIN_TYPE=MSI` env var (NOT `azcopy login --identity` — deprecated since v10.22 per research §1 finding 1). Postgres single-blob stream upload via `az storage blob upload --file /dev/stdin` stays (still the right tool for streaming).

- **`infra/main.bicep`** —
  1. Add `vmMiReaderAssignment` resource — Reader role on RG scope for `vm.identity.principalId` (Slice 3 manual-patch parity).
  2. Add storage account lifecycle policy: `Microsoft.Storage/storageAccounts/managementPolicies@2024-01-01` resource named `default` (singleton — research §5 finding 1) with `daysAfterCreationGreaterThan: 30` rule + `prefixMatch: ['msai-backups/backup-']` (research §5 finding 2; expiration based on creation, not modification, since blobs are immutable once written).
  3. Reference `infra/alerts.bicep` via `module alerts './alerts.bicep' = { ... }` — keeps main.bicep <800 lines.

- **`infra/cloud-init.yaml`** — install azcopy via tarball during provisioning (`runcmd` step after Docker install). Same approach as Slice 3's azure-cli install. NB: existing prod VM is patched manually via run-command in Phase 4 (see `scripts/install-azcopy.sh` task — applies to both fresh provision via cloud-init and existing-VM patch via deploy-on-vm.sh).

- **`scripts/deploy-on-vm.sh`** —
  1. Install azcopy if not present (idempotent — `which azcopy || /opt/msai/scripts/install-azcopy.sh`)
  2. Enable + start `backup-to-blob.timer` (`systemctl enable --now backup-to-blob.timer`); log status; non-fatal if start fails since deploys shouldn't block on timer state — but `FAIL_BACKUP_TIMER` marker added for audit.
  3. Stage `backup-to-blob.{service,timer}` from `/opt/msai/scripts/` → `/etc/systemd/system/`; `systemctl daemon-reload`.

- **`.github/workflows/deploy.yml`** —
  1. NEW pre-flight step `Refuse if active live_deployments` placed **after `Resolve inputs vs vars` and BEFORE `Azure login (OIDC)`** — fail fast before any Azure interaction (no OIDC token mint, no NSG rule churn). The step uses only `curl` + `jq` against the public hostname. Uses the response's `active_count` field directly (which the backend's `_node_manager.active_count` populates per `backend/src/msai/schemas/live.py:60`) — simpler than parsing `.deployments[].status`. Backend status enum is `{starting, running, stopped}` per `backend/src/msai/api/live.py:421,504,597,732` — there is no `ready` state (plan-review iter-1 P1 fix):
     ```yaml
     - name: Refuse if active live_deployments
       env:
         MSAI_HOST: ${{ steps.resolve.outputs.msai_hostname }}
         MSAI_API_KEY: ${{ secrets.MSAI_API_KEY }}
       run: |
         active=$(curl -sf -H "X-API-Key: $MSAI_API_KEY" \
           "https://${MSAI_HOST}/api/v1/live/status" \
           | jq -r '.active_count')
         if [[ "$active" != "0" ]]; then
           echo "FAIL_ACTIVE_DEPLOYMENTS_REFUSAL: $active deployments still active. Run 'msai live stop --all' first, then re-dispatch." >&2
           exit 1
         fi
     ```
  2. **No force-override flag.** Plan-review iter-2 P1: my original force+confirmation-token scheme was impractical because the run_id only exists post-dispatch, but the token had to embed the run_id; operator could never compute it pre-dispatch. Brutal-simplicity wins: real emergencies → operator clears the row with `msai live stop --all` first. If an emergency truly demands force (e.g. backend itself is down so `msai live stop --all` can't run), operator can temporarily comment out the gate step in deploy.yml on a hotfix branch + redeploy.
  3. Add `MSAI_API_KEY` as a GH Secret (operator action — copy from KV's `msai-api-key`).
  4. Caddy passes `X-API-Key` through unchanged by default (research §7 — no Caddyfile change).

- **`tests/infra/test_workflow_deploy.sh`** — grep assertions for the new active-live_deployments pre-flight step + force flag wiring.

- **`tests/infra/test_bicep.sh`** — grep assertions for `vmMiReaderAssignment`, lifecycle policy resource name `default`, `prefixMatch: ['msai-backups/backup-']`, alerts module import.

- **`docs/CHANGELOG.md`** — Slice 4 entry at finish (Phase 6.2).

### IB credentials operational follow-up (already complete pre-Slice-4)

The IB paper credentials (`tws-userid=marin1016test`, `tws-password=<extracted from .ibaccounts.txt>`, `ib-account-id=DUP733213`) + `databento-api-key` were seeded in KV before Slice 4 started. These were placeholders in Slice 3. `polygon-api-key` deferred — not found in `.env` and not blocking Slice 4 work.

---

## E2E Use Cases (Phase 3.2b)

Project type per CLAUDE.md: **fullstack**. Slice 4's user-facing surface is small (one new behavior — `deploy.yml` refusing on active deployments) but it IS user-facing per the testing rules. The rest is infra/ops — N/A for verify-e2e but executed during Slice 4 acceptance runbook.

#### UC-1: Deploy refuses when live_deployments has an active row

- **Interface:** CLI (`gh workflow run`) + API (X-API-Key check from runner)
- **Setup:** Insert a sentinel `live_deployments` row via the sanctioned interface — `POST /api/v1/live/start-portfolio` against a paper portfolio. Document the rollback (`msai live stop --all` after the test).
- **Steps:**
  1. `gh workflow run deploy.yml -f git_sha=$(git rev-parse --short=7 origin/main)`
  2. `gh run watch <run-id>` until completion
- **Verify:** Workflow conclusion = failure; logs contain `FAIL_ACTIVE_DEPLOYMENTS_REFUSAL`; the `Refuse if active live_deployments` step shows non-zero; subsequent steps (`Open transient SSH allow rule` and beyond) are `skipped`.
- **Persist:** N/A — this IS the persistence test for the gate.
- **Teardown:** `msai live stop --all` to clear the sentinel row.

#### UC-2: Clearing the gate by stopping deployments unblocks deploy

- **Interface:** CLI
- **Setup:** Same sentinel row from UC-1 still active.
- **Steps:**
  1. SSH to prod VM, run `cd /opt/msai && sudo docker compose --project-name msai exec backend uv run msai live stop --all` (or equivalent via API)
  2. Confirm `curl -sH "X-API-Key: …" https://platform.marketsignal.ai/api/v1/live/status | jq .active_count` returns `0`
  3. Re-dispatch `gh workflow run deploy.yml` (same SHA)
- **Verify:** Workflow proceeds past the gate; deploy completes per Slice 3 acceptance pattern.
- **Persist:** N/A — proves the gate's inverse path (clearing → success).

#### UC-3: Nightly backup blob appears (manual trigger)

- **Interface:** CLI (`ssh + sudo systemctl start backup-to-blob.service`)
- **Setup:** Prod VM has Slice 4 deployed; timer enabled. Pick a known-empty hour to trigger manually (not waiting for 02:07 UTC).
- **Steps:**
  1. SSH to prod VM (via the operator IP — no GH Actions involved here)
  2. `sudo systemctl start backup-to-blob.service`
  3. `journalctl -u backup-to-blob.service --since '5 minutes ago' --no-pager | tail -30`
- **Verify:** Service exits 0; journal shows `Backup complete: backup-<UTC-iso>`; `az storage blob list --auth-mode login --account-name msaibk4cd6d2obcxqaa --container-name msai-backups --prefix backup-$(date -u +%Y%m%d) --output table` includes the new blob.
- **Persist:** Re-list after 24h — blob still present (lifecycle is 30 days). After 30 days — gone (out of acceptance scope; documented for future).

#### UC-4: /health alert fires on outage

- **Interface:** Email (post-test)
- **Setup:** Slice 4 deployed; availability test active for ≥10 min.
- **Steps:**
  1. `ssh` to prod VM
  2. `sudo docker compose --project-name msai stop backend` (down for >5 min)
  3. Wait 7-10 min
- **Verify:** Email received at `pablo@ksgai.com` from Azure Monitor (subject contains the alert rule name).
- **Persist:** Bring backend back up: `sudo docker compose --project-name msai start backend`; confirm alert auto-resolves within 5 min.

(UC-5 = container restart heuristic, UC-6 = orphan-NSG-rule alert; left as "tested during Slice 4 acceptance runbook" rather than verify-e2e — verify-e2e is a developer tool, not an ops-drill harness.)

---

## Tasks

> **Concurrency model:** Mostly serial — `main.bicep` + `alerts.bicep` are coupled; runbooks reference each other. Dispatch one subagent at a time.

### T01 — `scripts/backup-to-blob.sh` Parquet azcopy switch

- [ ] Replace the `az storage blob upload-batch --auth-mode login` block with the azcopy v10.22+ pattern: `export AZCOPY_AUTO_LOGIN_TYPE=MSI; azcopy cp "$PARQUET_SRC" "https://$STORAGE_ACCT.blob.core.windows.net/$CONTAINER/backup-$TIMESTAMP/parquet" --recursive` (research §1 finding 1 — `--identity` flag deprecated).
- [ ] Update comment header: remove the Slice 4 carry-over note since we're doing it now.
- [ ] Keep the `pg_dump | gzip | az storage blob upload --file /dev/stdin` block unchanged (still optimal for streaming a single blob).

### T02 — `scripts/install-azcopy.sh`

- [ ] New idempotent script: skip if `/usr/local/bin/azcopy` exists AND `azcopy --version` returns ≥10.22; otherwise `curl -L https://aka.ms/downloadazcopy-v10-linux | tar xz -C /tmp && install -m 0755 /tmp/azcopy_linux_amd64_*/azcopy /usr/local/bin/azcopy` (NOT `curl | sh` per `.claude/hooks` safety rule).
- [ ] `chmod +x scripts/install-azcopy.sh` in git.

### T03 — `scripts/backup-to-blob.{service,timer}`

- [ ] `backup-to-blob.service`: `Type=oneshot`, `ExecStart=/opt/msai/scripts/backup-to-blob.sh`, `Environment=RESOURCE_GROUP=msaiv2_rg`, `Environment=DEPLOYMENT_NAME=main`, `User=root`, `StandardOutput=journal`.
- [ ] `backup-to-blob.timer`: `OnCalendar=*-*-* 02:07:00 UTC`, `Persistent=true`, `RandomizedDelaySec=300`, `Unit=backup-to-blob.service`, `WantedBy=timers.target`.
- [ ] Both files in `scripts/` directory (consistent with other systemd units like `scripts/msai-render-env.service`).

### T04 — `deploy-on-vm.sh` updates

- [ ] Source 3 new staged files at deploy time: `backup-to-blob.{service,timer}` and `install-azcopy.sh` (deploy.yml's "Stage compose file + Caddyfile + scripts on VM" step already scps `scripts/*.sh` — extend to scp `.service` + `.timer` files too).
- [ ] After the Caddy reload step:
  ```bash
  if ! /opt/msai/scripts/install-azcopy.sh; then
      echo "FAIL_AZCOPY_INSTALL" >&2
      # Non-fatal — backup can still run via az storage blob upload-batch fallback; alert
      # the operator instead of failing the deploy.
  fi
  cp /opt/msai/scripts/backup-to-blob.service /etc/systemd/system/
  cp /opt/msai/scripts/backup-to-blob.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now backup-to-blob.timer
  systemctl is-active backup-to-blob.timer || { echo "FAIL_BACKUP_TIMER" >&2; exit 1; }
  ```
- [ ] Add to header documentation under "Exit codes / failure markers" section.

### T05 — `deploy.yml` active-deployments gate

- [ ] Add `Refuse if active live_deployments` step after `Resolve inputs vs vars`, before `Azure login (OIDC)`. Reads `MSAI_API_KEY` Secret; curls `https://${{ steps.resolve.outputs.msai_hostname }}/api/v1/live/status`; checks `.active_count`; refuses if non-zero.
- [ ] **NO force-override flag** — plan-review iter-2: real emergencies use `msai live stop --all` first or a hotfix branch with the gate commented.

### T06 — Repo Secret seeding

- [ ] Set `gh secret set MSAI_API_KEY` from KV's `msai-api-key` value (operator action — script the read+set as part of `docs/runbooks/slice-4-acceptance.md`). Mirror Slice 3's `VM_SSH_PRIVATE_KEY` pattern.

### T07 — `infra/main.bicep` — vmMiReaderAssignment

- [ ] After `vmAcrPullAssignment` (Slice 1 line ~565), add:
  ```bicep
  resource vmMiReaderAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
    scope: resourceGroup()
    name: guid(vm.id, resourceGroup().id, 'reader')
    properties: {
      principalId: vm.identity.principalId
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')  // Reader
      principalType: 'ServicePrincipal'
      description: 'Slice 4: Reader on RG so backup-to-blob.sh can read Bicep outputs via az deployment group show. Was granted manually during Slice 3 first-deploy; landing in IaC for parity.'
    }
  }
  ```

### T08 — `infra/main.bicep` — Storage lifecycle policy

- [ ] After the `backupsContainer` block (Slice 1):
  ```bicep
  resource backupsLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2024-01-01' = {
    parent: storageAccount
    name: 'default'  // research §5 — singleton; no other name accepted
    properties: {
      policy: {
        rules: [
          {
            name: 'expire-backup-blobs-30d'
            enabled: true
            type: 'Lifecycle'
            definition: {
              filters: {
                blobTypes: ['blockBlob']
                prefixMatch: ['msai-backups/backup-']  // research §5
              }
              actions: {
                baseBlob: {
                  delete: { daysAfterCreationGreaterThan: 30 }  // research §5 — creation, not modification
                }
              }
            }
          }
        ]
      }
    }
  }
  ```

### T09 — `infra/alerts.bicep` (new module)

- [ ] New module file. Imports Slice 1's Log Analytics workspace by ID (param input from main.bicep).
- [ ] Action Group `msai-ops-alerts` (groupShortName `msai-ops`, email receiver `pablo@ksgai.com`).
- [ ] Application Insights component `msai-app-insights` (kind `web`, workspaceResourceId linked).
- [ ] Availability test `msai-health-ping`: `kind: 'standard'`, name `msai-health-ping`, request URL `https://platform.marketsignal.ai/health`, 5-min frequency, **3 geoLocations** to stay <€10/mo (research §6 finding 3). Mandatory `hidden-link:<appinsights-resource-id>` tag (research §6 finding 4).
- [ ] 4 `Microsoft.Insights/scheduledQueryRules@2022-06-15` (pinned per research §2 — `2023-12-01` preview is unregistered in many regions):
  1. `msai-health-availability-alert` — query Application Insights availability results for failed pings; severity 1; window 5 min.
  2. `msai-backup-failure-alert` — KQL: `Syslog | where SyslogMessage contains "backup-to-blob.service" and SyslogMessage contains "Failed"` over 24h; severity 1.
  3. `msai-orphan-nsg-rule-alert` — `AzureActivity | where OperationNameValue endswith "securityRules/write" | where ActivityStatusValue == "Succeeded" | extend rule_name=...` filtered to `gha-transient-*` AND no matching delete within 30 min; severity 2.
  4. `msai-container-restart-heuristic` — KQL on Syslog stream parsing Docker restart logs (research §3 — heuristic since AMA doesn't emit per-container metrics; documented as such); severity 2.
- [ ] Module emits outputs: `actionGroupId`, `appInsightsId`, `appInsightsConnectionString`, `availabilityTestId`.
- [ ] Comment header explains the 2022-06-15 API pin + the heuristic nature of #4.

### T10 — `infra/main.bicep` — alerts module reference

- [ ] After the existing observability section (Log Analytics + DCR), add:
  ```bicep
  module alerts './alerts.bicep' = {
    name: 'alerts-${uniqueString(resourceGroup().id)}'
    params: {
      location: location
      logAnalyticsWorkspaceId: logWorkspace.id
      operatorEmail: 'pablo@ksgai.com'  // hardcoded — single-operator project
      tags: tags
    }
  }
  ```
- [ ] Add `alerts.actionGroupId` to outputs for runbook reference.

### T11 — `infra/cloud-init.yaml` updates

- [ ] After the `azure-cli` install step, add azcopy install (tarball pattern from T02; inlined in cloud-init for simplicity — short).
- [ ] Plant `backup-to-blob.service` + `backup-to-blob.timer` files into `/etc/systemd/system/` via `write_files` (base64-encoded by Bicep at template-build time, same pattern as `msai-render-env.service`).
- [ ] Do NOT enable the timer in cloud-init — deploy-on-vm.sh's first run after Slice 4 merge handles enable. Rationale: avoids the timer firing on a freshly-provisioned VM that has no app stack yet.

### T12 — `tests/infra/test_bicep.sh` — Slice 4 assertions

- [ ] grep for `vmMiReaderAssignment`, `managementPolicies`, `prefixMatch: \['msai-backups/backup-'\]`, `daysAfterCreationGreaterThan: 30`, `module alerts`.

### T13 — `tests/infra/test_alerts_bicep.sh` (new)

- [ ] `az bicep build --file infra/alerts.bicep --stdout >/dev/null` (lint check).
- [ ] grep: `scheduledQueryRules@2022-06-15`, `kind: 'standard'`, `pablo@ksgai.com`, exactly 4 alert rule resources, `geoLocations` array length ≤ 3, hidden-link tag pattern.

### T14 — `tests/infra/test_workflow_deploy.sh` — Slice 4 assertions

- [ ] grep for the new step name `Refuse if active live_deployments` + `FAIL_ACTIVE_DEPLOYMENTS_REFUSAL` + `.active_count` + the absence of any `force_` flag (regression guard against re-introducing the impractical force scheme).

### T15 — `docs/runbooks/restore-from-backup.md`

- [ ] New file per PRD §4 US-005. Operator steps; expected outputs at each step; "if you see X, you forgot Y" troubleshooting.

### T16 — `docs/runbooks/iac-parity-reapply.md`

- [ ] New file. 1-page runbook for re-applying Slice 1+3+4 Bicep to prod RG idempotently.

### T17 — `docs/runbooks/slice-4-acceptance.md`

- [ ] New file. Acceptance procedure (5 sub-tests) — manual trigger backup, /health alert smoke, container restart smoke, active-deployment refusal, IaC parity what-if.

### T18 — CHANGELOG entry (Phase 6.2)

- [ ] Slice 4 entry per existing format.

---

## Dispatch Plan

Mostly serial. `infra/main.bicep` + `infra/alerts.bicep` are coupled (main.bicep references alerts module). `deploy-on-vm.sh` modified by T04 only. `deploy.yml` modified by T05 only. `backup-to-blob.sh` modified by T01 only.

| Task ID | Depends on    | Writes (concrete file paths)                                     |
| ------- | ------------- | ---------------------------------------------------------------- |
| T01     | —             | `scripts/backup-to-blob.sh`                                      |
| T02     | —             | `scripts/install-azcopy.sh`                                      |
| T03     | —             | `scripts/backup-to-blob.service`, `scripts/backup-to-blob.timer` |
| T04     | T02, T03      | `scripts/deploy-on-vm.sh`                                        |
| T05     | —             | `.github/workflows/deploy.yml`                                   |
| T06     | T05           | (operator action — `gh secret set`)                              |
| T07     | —             | `infra/main.bicep` (vmMiReaderAssignment)                        |
| T08     | T07           | `infra/main.bicep` (storage lifecycle)                           |
| T09     | —             | `infra/alerts.bicep`                                             |
| T10     | T08, T09      | `infra/main.bicep` (alerts module ref + outputs)                 |
| T11     | T03           | `infra/cloud-init.yaml`                                          |
| T12     | T10           | `tests/infra/test_bicep.sh`                                      |
| T13     | T09           | `tests/infra/test_alerts_bicep.sh`                               |
| T14     | T05           | `tests/infra/test_workflow_deploy.sh`                            |
| T15     | —             | `docs/runbooks/restore-from-backup.md`                           |
| T16     | T07           | `docs/runbooks/iac-parity-reapply.md`                            |
| T17     | T04, T05, T10 | `docs/runbooks/slice-4-acceptance.md`                            |
| T18     | All above     | `docs/CHANGELOG.md`                                              |

Sequential mode (per the dispatch-plan rules — tightly coupled).

---

## Implementation Notes

### Why `kind: 'standard'` availability test + not URL-ping?

URL ping retires 2026-09-30 per research §6 finding 1. Standard test requires an Application Insights component (new resource — Slice 4 adds it). Cost: 3 geoLocations × 5min interval ≈ €5-10/mo. Per Pablo's "don't optimize for cloud cost" feedback, this is well within tolerance. The alternative (Container Insights livenessProbe) doesn't apply because we're not on AKS.

### Why scheduledQueryRules `2022-06-15` GA?

Research §2 finding 2: `2023-12-01-preview` is documented in MS Learn but unregistered in many regions, causing deploy-time `API version 'X' is not supported` failures. The `2022-06-15` GA version is stable across all regions and supports all the alert features Slice 4 needs (criteria, dimensions, action groups). If a future contributor bumps to a newer preview, the pin survives until they confirm regional registration.

### Why container restart "heuristic" not real counter?

Research §3 finding 1: Container Insights is K8s-only. AMA on a VM emits Syslog stream + InsightsMetrics (host-level), but NOT per-container metrics. The "container restart alert" is therefore a KQL heuristic over the Syslog stream that matches Docker's restart log patterns. Acceptable for Phase 1 single-VM stack; revisit if false-positive rate exceeds 1/week.

### Why active-`live_deployments` gate via public endpoint?

Three options were considered: (a) SSH to VM + docker exec + Postgres query, (b) public API call with X-API-Key, (c) Azure Function intermediary. Option (b) wins because the API endpoint already exists (`/api/v1/live/status` per CLAUDE.md), MSAI_API_KEY is already in KV, and Caddy passes the header unchanged (research §7). Slice 3's deploy pattern proved the public-API surface is sufficient for runner-side probes.

### Why `OnCalendar=*-*-* 02:07:00 UTC` (off-the-hour)?

02:07 UTC = 22:07 ET (post-US market close) = 03:07 CET (operator timezone offset is OK; alerts will roll into morning routine). Off-the-hour at :07 avoids the storage-account scheduler stampede at :00. `RandomizedDelaySec=300` adds another 0-5 min of jitter on top.

### What about a "first deploy after merge" acceptance?

Slice 4 acceptance is **not** another rehearsal RG smoke. Reasons: (1) the surfaces are all additive (no compose/Caddy/SSH changes from Slice 3); (2) each new surface has its own acceptance smoke in `docs/runbooks/slice-4-acceptance.md`; (3) per the slicing verdict, Slice 4 was always meant to be tested under prod load (the "first overnight backup" gate). The Contrarian's-gate rehearsal was Slice 3-specific.

### What we deliberately do NOT alert on

- Successful deploys (no signal-to-noise)
- Postgres healthcheck transient flaps <2min (`/health` 5-min threshold covers the real issue)
- Caddy auto-LE renewal events (Caddy logs them; they're a normal background activity)
- Backup individual stage failures (only the overall service failure — if Postgres dumps but Parquet fails, the whole service exits non-zero)

### Sequencing of T07 + T08 against current prod state

Slice 3 left manual patches on prod:

- `Network Contributor` on NSG for ghOidcMi → declared in Slice 3 Bicep, granted manually before re-apply
- `Reader` on RG for VM MI → granted manually, **not** in Slice 3 Bicep

When `iac-parity-reapply.md` runbook executes after Slice 4 merge, Bicep will:

- `vmMiReaderAssignment` — Create (operator does the re-apply; the role assignment lands declaratively)
- `ghOidcNsgContributorAssignment` — No-op (already exists with matching properties)
- All other resources — No-op (drift-free)

If what-if shows any Modify on existing resources, that's a regression bug to investigate before applying.

### Test for the active-deployment gate without running the gate

The gate is hard to unit-test (it queries a live HTTPS endpoint). Two layers of coverage:

- **actionlint + grep** (T14) — the step exists with the right inputs and markers
- **Manual integration in slice-4-acceptance.md** (UC-1 in plan) — operator inserts a sentinel row and confirms refusal

Future: a synthetic dry-run mode in deploy.yml that hits a mock endpoint. Out of Slice 4 scope.
