# PRD: Deployment Pipeline Slice 4 — Ops, Backup, Observability

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-05-10
**Last Updated:** 2026-05-10

---

## 1. Overview

Slice 4 (final slice) of the 4-PR deployment-pipeline series, ratified at [`docs/decisions/deployment-pipeline-slicing.md`](../decisions/deployment-pipeline-slicing.md) §Slice 4. Ships the operational layer on top of Slice 3's working deploy pipeline:

1. **Nightly backup automation** — systemd timer on the prod VM runs `scripts/backup-to-blob.sh` at 02:07 local (off-the-hour); Parquet tree mirrored via `azcopy --recursive` (10× faster than `az storage blob upload-batch --auth-mode login` per Slice 3 research §4 finding 5); 30-day retention.
2. **Log Analytics dashboards + alert rules** — KQL queries for deploy-failure, backend-5xx, container-restart, NSG-orphan-rule-age. Alerts route to operator (email + Webhook).
3. **Active-`live_deployments` hard refusal gate** — `deploy.yml` pre-flight queries the backend for active deployments; refuses with a clear message if any exist; operator must `msai live stop --all` first.

Plus folded-in IaC parity carry-overs from Slice 3:

4. **`Reader` on RG for VM MI** — enables `backup-to-blob.sh`'s Bicep-output lookup declaratively (Slice 3 granted manually).
5. **Idempotent re-apply of Slice 3 Bicep to prod RG** — lands `ghOidcNsgContributorAssignment` declaratively (Slice 3 granted manually).

This closes the 4-slice deploy-pipeline series and graduates MSAI v2 from "first prod deploy works" to "operationally hardened single-VM Phase 1 stack."

## 2. Goals & Success Metrics

### Goals

- **No-touch overnight backups.** Operator never needs to remember to run `backup-to-blob.sh` — systemd does it. Verified by restoring a recent backup to a throwaway Postgres and confirming schema parity.
- **Operator paged on real failures.** Container crashloops, deploy failures, orphan NSG rules >30 min, /health 5xx for >5 min — all alert. Tested by deliberately breaking each surface.
- **Deploys refuse to clobber live trading.** When `live_deployments` has active rows, `deploy.yml` exits non-zero with a clear message BEFORE pulling images. Tested by leaving a stale row and confirming refusal.
- **IaC parity restored.** Re-applying Slice 1+3 Bicep to prod is a no-op — all Slice 3 manual patches now declared in Bicep.
- **Backup retention enforced.** Blob lifecycle policy auto-deletes `backup-*` entries older than 30 days. Tested by inspecting the lifecycle rule + simulating with a backdated blob.

### Success Metrics

| Metric                               | Target                                                                                                     | How Measured                                                                                                                                   |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Nightly backup fires + lands in Blob | A `backup-<YYYYMMDD>T0207*` blob appears in `msai-backups` every 24h                                       | `az storage blob list ... --prefix backup-$(date -u +%Y%m%d)` returns ≥1 entry the morning after the first scheduled run                       |
| Restore-from-backup smoke            | Throwaway Postgres + `gunzip backup.sql.gz \| psql` succeeds; `\dt` returns same tables as prod            | Operator runs `docs/runbooks/restore-from-backup.md` (new) against a `msaiv2-restore-test-<date>` RG; tears down                               |
| `live_deployments` hard refusal      | Deploy job exits non-zero with `FAIL_ACTIVE_DEPLOYMENTS_REFUSAL` marker; no images pulled; no SSH executed | Trigger deploy with a sentinel active-deployment row; observe workflow refusal at the pre-flight step (BEFORE `Open transient SSH allow rule`) |
| `/health` alert fires within 5 min   | Azure Monitor email alert delivered to operator                                                            | Deliberately `docker stop msai-backend-1`; observe alert email                                                                                 |
| Container restart alert              | `>3 restarts in 10 min` for any prod container triggers alert                                              | Crashloop a sidecar by injecting a bad env; observe alert                                                                                      |
| Orphan NSG rule alert                | Any `gha-transient-*` rule >30 min old triggers alert                                                      | Manually create one + wait; observe alert; reaper cleans it up                                                                                 |
| IaC re-apply is a no-op              | `az deployment group what-if -f infra/main.bicep -g msaiv2_rg` shows 0 Modify/Create operations            | `tests/infra/test_bicep.sh` extended to grep what-if output for Modify/Create entries on a re-apply against prod                               |
| Acceptance: end-to-end ops drill     | Hawk's-gate restore + Contrarian's gate + active-deployment refusal + alert smoke all PASS                 | Documented in `docs/runbooks/slice-4-acceptance.md`; evidence in PR                                                                            |

### Non-Goals (Explicitly Out of Scope)

- ❌ Migration to managed Postgres / Postgres Flexible Server — Phase 2 (per architecture verdict §5b)
- ❌ Migration to AKS — Phase 2+ when 2-VM split happens
- ❌ Custom RBAC role replacing built-in Network Contributor — Phase 2 hardening per `docs/decisions/deploy-ssh-jit.md` "Deferred"
- ❌ Azure Policy deny on non-runner-IP `sourceAddressPrefix` — Phase 2 hardening
- ❌ Multi-region failover / hot standby — Phase 3+
- ❌ ACI-in-VNet jump as SSH primitive — escape hatch per Slice 3 ADR, only adopted if orphan-rule patterns emerge in production
- ❌ Frontend Entra SPA dedicated app reg — still using `msai-prod-spa` from Slice 3 pre-flight
- ❌ Production IB credentials in KV (`U4...` accounts) — paper-only Phase 1
- ❌ Polygon API key in KV — not requested by current ingestion paths
- ❌ Restore-from-backup automation (one-button DR) — Slice 4 documents the manual procedure; full automation is post-Phase-1

## 3. User Personas

### Pablo (operator)

- **Role:** Solo operator. Same role as Slice 3.
- **Slice 4-specific touchpoints:**
  - Sees nightly backup blobs accumulating in `msai-backups` container without lifting a finger
  - Receives email when `/health` is down >5 min, when a container crashloops, when an orphan NSG rule is detected
  - When attempting a deploy during an active live-trading session: gets a clear refusal message + the exact command to clear it (`msai live stop --all`)
  - Quarterly runs the restore-from-backup drill to verify backups are usable (manual; one runbook page)

### GitHub Actions `deploy.yml` (now gated)

- **New behavior:** Pre-flight step (after `azure/login@v2`, before `Open transient SSH allow rule`) queries the backend over the public endpoint with `X-API-Key`. If `/api/v1/live/status` shows any deployment in `running` / `starting` / `ready` state, fails fast with `FAIL_ACTIVE_DEPLOYMENTS_REFUSAL`.
- **No bypass flag.** (Plan-review iter-2 P1: the originally-proposed force+confirmation-token scheme was impractical — run_id is only known post-dispatch, but the token had to embed run_id; operator could never compute it pre-dispatch.) Real emergencies → operator stops deployments first (`msai live stop --all`) OR comments out the gate step in `deploy.yml` on a hotfix branch.

### `backup-to-blob.service` + `.timer` (new systemd units on VM)

- **Role:** Owns nightly backup execution. Timer at `OnCalendar=*-*-* 02:07:00` (UTC; off-the-hour to avoid scheduler stampede on the Azure storage backend).
- **Permissions:** Inherits VM system-assigned MI (Reader on RG + Storage Blob Data Contributor on storage account, both from Slice 1 + Slice 4 IaC).

### Azure Monitor (alert evaluator)

- **Role:** Evaluates KQL queries against Log Analytics workspace every 5-15 min; routes matched alerts to an Action Group.
- **Action Group:** Email to `pablo@ksgai.com` (single-operator stack). Could extend to webhook/Slack in Phase 2.

## 4. User Stories

### US-001: Nightly backup happens without operator action

**As an** operator
**I want** Postgres + DATA_ROOT mirrored to Blob every night at 02:07 UTC
**So that** I never lose more than 24h of data if the VM disk dies

**Acceptance Criteria:**

- [ ] `/etc/systemd/system/backup-to-blob.service` (Type=oneshot) runs `/opt/msai/scripts/backup-to-blob.sh` as root
- [ ] `/etc/systemd/system/backup-to-blob.timer` fires `OnCalendar=*-*-* 02:07:00`, `Persistent=true` (catches up missed runs)
- [ ] Timer enabled at deploy time by `deploy-on-vm.sh` (`systemctl enable --now backup-to-blob.timer`)
- [ ] `backup-to-blob.sh` updated: Parquet mirror uses `azcopy login --identity && azcopy cp <src> <dst> --recursive` (research finding §4)
- [ ] Storage account has lifecycle management policy: delete `backup-*` blobs older than 30 days
- [ ] Backup logs land in journald (`journalctl -u backup-to-blob.service`) AND in Log Analytics via Syslog DCR
- [ ] Failure: timer exits non-zero, systemd surfaces it, Log Analytics-routed alert fires

**Edge cases:**

| Condition                              | Expected                                              |
| -------------------------------------- | ----------------------------------------------------- |
| Postgres container down at backup time | `pg_dump` step fails fast, exit non-zero, alert fires |
| Storage account quota exhausted        | `azcopy` fails with quota error, alert fires          |
| VM rebooted mid-backup                 | `Persistent=true` makes systemd catch up on next boot |
| Multiple deploys in one day            | Each backup tagged `backup-<UTC-iso>`; no collisions  |

### US-002: Deploy refuses to clobber live trading

**As an** operator
**I want** `deploy.yml` to fail fast if any `live_deployments` row is in active state
**So that** I never accidentally restart the broker stack mid-trading-session

**Acceptance Criteria:**

- [ ] New pre-flight step in `deploy.yml` after `azure/login@v2`, before `Open transient SSH allow rule`: queries `https://${{ vars.MSAI_HOSTNAME }}/api/v1/live/status` with `X-API-Key: ${{ secrets.MSAI_API_KEY }}` (or `${{ vars.MSAI_API_KEY }}` if exposed as a non-secret)
- [ ] If response contains any deployment with `status` in `{running, starting, ready}`, exit 1 with single line `FAIL_ACTIVE_DEPLOYMENTS_REFUSAL` and a human-readable message: `"Active deployments present: <ids>. Run 'msai live stop --all' on the VM first, then re-deploy."`
- [ ] No force-bypass workflow input (plan-review iter-2 P1 — see §3 Persona note)
- [ ] The MSAI_API_KEY value lives in KV; new GH Secret `MSAI_API_KEY` is set to the same value for deploy.yml
- [ ] Probe surface is the same hostname Caddy already proxies — no new Bicep / NSG changes required

**Edge cases:**

| Condition                                                                     | Expected                                                                                                                      |
| ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Backend down entirely                                                         | `curl` fails — treat as "cannot determine state" — fail-closed (refuse deploy) with `FAIL_CANNOT_DETERMINE_LIVE_STATE` marker |
| Force flag without token                                                      | Workflow exits 1 at input validation                                                                                          |
| Force flag + valid token + active deployments                                 | Workflow continues with `WARN: forced deploy with active deployments — operator accepted risk` in summary                     |
| Stale `live_deployments` row (status=running but supervisor crashed long ago) | Refusal still fires (correct — operator must explicitly clear stale rows via `msai live kill-all`)                            |

### US-003: Alerts fire on real failures

**As an** operator
**I want** email alerts within 5 minutes when `/health` is down, a container crashloops, or an NSG orphan rule lingers
**So that** I notice problems before they become outages

**Acceptance Criteria:**

- [ ] Azure Monitor Action Group `msai-ops-alerts` with email to `pablo@ksgai.com`
- [ ] Scheduled query alert: `/health` returns non-200 for >5 min (via Container Insights or external uptime check — see plan-review decision)
- [ ] Scheduled query alert: any prod container with `>3 restarts in 10 min`
- [ ] Scheduled query alert: any NSG rule with name `gha-transient-*` AND `age > 30 min`
- [ ] Scheduled query alert: any `backup-to-blob.service` failure in last 24h
- [ ] All alert rules declared in Bicep (no portal-only state)
- [ ] Each alert has explicit severity (1=critical, 2=warning, 3=informational)

### US-004: IaC parity — re-apply prod Bicep is a no-op

**As an** operator
**I want** the Slice 3 manual patches (Reader on RG for VM MI, Network Contributor on NSG for ghOidcMi) to land in Bicep
**So that** any future `az deployment group create` against prod produces a clean what-if (no drift)

**Acceptance Criteria:**

- [ ] `infra/main.bicep` declares `vmMiReaderAssignment` (Reader on RG scope, principalId = `vm.identity.principalId`)
- [ ] Slice 3 `ghOidcNsgContributorAssignment` is already in main.bicep but wasn't re-applied to prod — Slice 4 docs the re-apply procedure (`scripts/deploy-azure.sh` re-run for prod RG)
- [ ] `tests/infra/test_bicep.sh` extended to grep for `vmMiReaderAssignment` resource
- [ ] `az deployment group what-if` against prod after merge shows 0 Modify/Create on existing resources (only the new vmMiReaderAssignment is Created if it doesn't already exist)
- [ ] Runbook `docs/runbooks/iac-parity-reapply.md` documents the 1-command re-apply

### US-005: Quarterly restore-from-backup drill

**As an** operator
**I want** a runbook for restoring Postgres from a backup blob
**So that** I'm confident backups are actually usable

**Acceptance Criteria:**

- [ ] `docs/runbooks/restore-from-backup.md` walks the operator through:
  1. Pick a recent backup blob
  2. Provision a throwaway Postgres container locally (or in a throwaway Azure container)
  3. `az storage blob download` + `gunzip` + `psql -d msai_restore_test < backup.sql`
  4. `\dt` and spot-check table presence
  5. Tear down
- [ ] Runbook is exercised once during Slice 4 acceptance (evidence in PR)
- [ ] Calendar reminder: quarterly re-run (operator's calendar, not codebase concern)

## 5. Constraints & Assumptions

### Hard Constraints

- Phase 1 single-VM deploy target unchanged
- Forward-only migrations (additive-only per `.claude/rules/database.md`) — backup is just a safety net, not a rollback mechanism
- Email alerting only (no PagerDuty/Slack until Phase 2 or external ops need)
- Active-deployment gate trusts the backend's `/api/v1/live/status` as ground truth — if the DB shows stale rows, that's the operator's responsibility (Slice 4 doesn't auto-reconcile)
- Backup retention 30 days; not configurable per-deploy

### Assumptions

- VM MI's existing Storage Blob Data Contributor grant covers `azcopy login --identity` flow (research will confirm)
- `azcopy` is in the `azure-cli` apt repo OR installable as a separate package (research will confirm; cloud-init may need to install it)
- The backend's `/api/v1/live/status` endpoint returns the current state of `live_deployments` table (verified — exists in CLAUDE.md API list)
- Container Insights / Azure Monitor Agent (AMA) on the VM emits container metrics in a queryable form for the crashloop alert (research will confirm — Slice 1 AMA setup may need extension)

## 6. Open Decisions

### D-1: Where does the `/health` uptime check come from?

**Options:**

- A. Azure Monitor "Availability Test" pointed at `https://platform.marketsignal.ai/health` (external probe)
- B. Container Insights "Liveness" derived from compose healthcheck
- C. KQL query against backend access logs (no probe at all)

**Default (decide in Phase 2 plan-review):** A — external probe is the most representative of user-perceived health.

### D-2: `azcopy` install location?

**Options:**

- A. Extend cloud-init (Slice 1 redo)
- B. Install via `deploy-on-vm.sh` once at first deploy after Slice 4 merge
- C. Bake into a custom VM image

**Default:** B — `deploy-on-vm.sh` installs idempotently like Slice 3's deferred Docker fix. A is the long-term right answer but requires re-running cloud-init.

### D-3: Lifecycle policy granularity?

**Options:**

- A. Delete after 30 days (flat policy)
- B. Daily for 7d → weekly for 4w → monthly for 12m (tiered)

**Default:** A — flat 30-day policy for Phase 1. Tiered retention is Phase 2 when storage cost matters.

## 7. Dependencies

- **Slice 1+2+3 merged** (✓)
- **Slice 3 ADR `docs/decisions/deploy-ssh-jit.md`** — Slice 4 implements the "Deferred" Azure Monitor alert on orphan rule age
- **Operator: Azure Action Group destination** — email `pablo@ksgai.com` (assumed; confirm)

## 8. Phase 2 Research Targets

1. **azcopy v10.x with system-assigned MI** — confirm `azcopy login --identity` syntax; permission model; recursive cp performance vs. `az storage blob upload-batch`; CLI install path on Ubuntu 24.04
2. **Azure Monitor scheduled query alerts in 2026** — Bicep resource types (`Microsoft.Insights/scheduledQueryRules@2023-03-15-preview` vs. newer GA); KQL query best practices; action group wiring
3. **Container Insights vs Azure Monitor Agent on Ubuntu 24.04 for docker container metrics** — what's the canonical 2026 pattern? AMA with Container Insights extension? Per `feedback_ama_dcr_kind_linux_required.md` memory: AMA + DCR + Syslog stream worked for Heartbeat in Slice 1.
4. **systemd timer + Persistent=true** — confirm catch-up behavior on Ubuntu 24.04; randomized delay if applicable
5. **Storage account lifecycle policy in Bicep** — Microsoft.Storage/storageAccounts/managementPolicies — declarative pattern, `daysAfterCreationGreaterThan: 30` filter on `backup-*` prefix
6. **GH Actions accessing backend through Caddy with X-API-Key** — confirm the backend accepts X-API-Key header path (it does per CLAUDE.md "Key Design Decisions"); verify Caddy doesn't strip the header

## 9. Risks & Mitigations

| Risk                                                | Likelihood | Impact | Mitigation                                                                                                                         |
| --------------------------------------------------- | ---------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| Backup overlaps a long-running deploy               | L          | L      | 02:07 timer + deploy.yml typically morning-traffic; collision is benign (`pg_dump` is read-only against postgres replica state)    |
| azcopy memory blow-up on huge Parquet tree          | L          | M      | Phase 1 Parquet is small; if it ever grows >10GB, switch to streaming + chunking. Slice 4 doesn't pre-pay this cost                |
| Alert fatigue from flaky probes                     | M          | M      | Choose alert severity thresholds carefully (>5 min not >1 min); document mute-window pattern                                       |
| Bicep re-apply to prod IS NOT idempotent            | L          | H      | Run `what-if` first; if Modify/Create on existing resources, abort and investigate (per `tests/infra/test_bicep.sh` what-if check) |
| `live_deployments` gate has bypass that gets abused | L          | M      | Force flag requires confirmation token tied to SHA+run_id; logged in workflow summary; operator self-audit                         |
| Email lands in spam                                 | M          | M      | Add SPF/DKIM for action-group sender? Or use a non-Azure SMTP relay. Phase 2 if it matters                                         |

## 10. Operator Pre-Flight Checklist (before merge)

- [ ] Confirm email destination for action group (`pablo@ksgai.com` — confirm)
- [ ] Confirm 30-day retention is the right number (vs. 90)
- [ ] Action group + first alert rule will be created in `msaiv2_rg` (same RG; no new resources outside this RG)
- [ ] No other operator pre-flight items (this slice is mostly infra config + scripts; no DNS / Entra app reg changes)

## 11. Acceptance Criteria Summary

Slice 4 ships when:

1. systemd timer fires once on operator demand; resulting blob in `msai-backups` confirmed
2. Restore-from-backup drill runbook exercised once (evidence in PR)
3. Deliberate `/health` outage triggers email alert within 5 min
4. Deliberate container restart spam triggers crashloop alert
5. Active-`live_deployments` row causes `deploy.yml` to refuse; clearing the row + retrying succeeds
6. `tests/infra/test_bicep.sh` passes; `az deployment group what-if` on prod shows 0 Modify/Create on existing resources (only new resources Created)
7. Slice 4 PR merged; first scheduled overnight backup the night after merge lands in Blob without operator action
