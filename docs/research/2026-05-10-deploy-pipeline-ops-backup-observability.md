# Research: deploy-pipeline-ops-backup-observability

**Date:** 2026-05-10
**Feature:** Slice 4 of 4 — nightly backup (systemd timer + azcopy), Log Analytics dashboards/alerts, active-`live_deployments` deploy gate, Storage lifecycle policy, IaC parity carry-overs from Slice 3
**Researcher:** research-first agent

> **Scope note.** Pure infra/ops/CI. No `package.json` / `pyproject.toml` deltas. External surfaces researched: AzCopy v10 + system-assigned MI auth, `Microsoft.Insights/scheduledQueryRules` (Bicep API version selection), AMA + Container Insights for docker on a VM, systemd `Persistent=true` + `RandomizedDelaySec`, `Microsoft.Storage/storageAccounts/managementPolicies` lifecycle, `Microsoft.Insights/webtests` standard availability tests, plus a quick re-check of Caddy header pass-through for the `X-API-Key` deploy gate. 6 priority topics from PRD §8.

---

## Surfaces Touched

| Surface                                                | Pinned form (PRD/repo)                                         | Latest stable (2026-05-10)                                   | Breaking changes vs assumed shape                                                                                                                            | Source                                                                                                                                                         |
| ------------------------------------------------------ | -------------------------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| AzCopy v10                                             | unpinned (PRD assumes available)                               | **v10.27.x**                                                 | `azcopy login` deprecated since v10.22 — use `AZCOPY_AUTO_LOGIN_TYPE=MSI` instead                                                                            | [Azure/azure-storage-azcopy wiki — azcopy_login](https://github.com/Azure/azure-storage-azcopy/wiki/azcopy_login) (2026-05-10)                                 |
| `Microsoft.Insights/scheduledQueryRules`               | new in Slice 4                                                 | GA `2022-06-15` / `2021-08-01`; preview `2024-01-01-preview` | `2023-12-01` documented but **not** registered in many regions (bicep-types-az#2318) — avoid                                                                 | [MS Learn — scheduledQueryRules reference](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/scheduledqueryrules) (2026-05-10)              |
| Azure Monitor Agent (AMA) on VM                        | already deployed Slice 1 (`kind: 'Linux'` DCR + Syslog stream) | unchanged                                                    | AMA does **not** emit per-container metrics standalone — Container Insights extension is K8s-oriented; for Compose-on-VM use Syslog/custom-log + KQL parsing | [MS Learn — Container Insights overview](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/container-insights-overview) (2026-05-10)            |
| systemd timer (`Persistent=true`)                      | new in Slice 4                                                 | systemd 255 on Ubuntu 24.04                                  | `Persistent=true` honored, but catch-up firing is **subject to `RandomizedDelaySec`** — frequent reboots can swallow runs (systemd#21166)                    | [Ubuntu manpage — systemd.timer](https://manpages.ubuntu.com/manpages/jammy/en/man5/systemd.timer.5.html) (2026-05-10)                                         |
| `Microsoft.Storage/storageAccounts/managementPolicies` | new in Slice 4                                                 | `2023-05-01` / `2024-01-01` GA                               | `prefixMatch` on container path; `daysAfterCreationGreaterThan` is the right knob (vs `daysAfterModification…`) for write-once backups                       | [MS Learn — managementPolicies reference](https://learn.microsoft.com/en-us/azure/templates/microsoft.storage/storageaccounts/managementpolicies) (2026-05-10) |
| `Microsoft.Insights/webtests` (standard test)          | new in Slice 4                                                 | GA, `kind: 'standard'`                                       | URL ping tests **retired 2026-09-30** — must use `standard` kind; requires App Insights resource as parent (`hidden-link` tag is mandatory)                  | [MS Learn — Application Insights availability tests](https://learn.microsoft.com/en-us/azure/azure-monitor/app/availability) (2026-05-10)                      |
| Caddy 2 reverse proxy (existing Slice 3)               | `caddy:2-alpine`                                               | v2.10.x                                                      | Custom headers (incl. `X-API-Key`) **passed through unchanged by default**; only `X-Forwarded-*` are sanitized                                               | [Caddy docs — reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) (2026-05-10)                                                     |

---

## Per-Topic Analysis

### 1. AzCopy v10 with system-assigned MI

**Findings:**

1. **`azcopy login --identity` works today but is deprecated.** Per the official wiki, "the `azcopy login` command will be deprecated starting release 10.22." The recommended 2026 pattern is the **auto-login env var**: `export AZCOPY_AUTO_LOGIN_TYPE=MSI` and run `azcopy copy …` directly. No interactive `login` step, no token cache files, no expiry juggling.
2. **System-assigned MI is the simplest target.** No additional env var needed when `AZCOPY_AUTO_LOGIN_TYPE=MSI` is set on a VM with system-assigned MI enabled. (User-assigned needs `AZCOPY_MSI_CLIENT_ID` or `AZCOPY_MSI_RESOURCE_STRING` and has had several known bugs — issues #2398, #2587, #2665 — best avoided.)
3. **Required role on the storage account: `Storage Blob Data Contributor`** for upload. Slice 1 already grants this to the VM MI on `msaibk4cd6d2obcxqaa`.
4. **Recursive copy is `azcopy copy <src> <dst> --recursive`.** Ubuntu 24.04 install path: download tarball from `aka.ms/downloadazcopy-v10-linux`, extract, drop binary in `/usr/local/bin/azcopy`. Not in the apt repo. Idempotent install in `deploy-on-vm.sh` is the right call (PRD D-2 default B).
5. **Performance:** AzCopy parallelizes by default and on large directory trees has consistently outperformed `az storage blob upload-batch` (matches Slice 3 research finding — `--auth-mode login` batch upload is single-threaded per blob and serial across blobs).

**Sources:**

1. [Azure/azure-storage-azcopy wiki — azcopy_login](https://github.com/Azure/azure-storage-azcopy/wiki/azcopy_login) — accessed 2026-05-10 ("`azcopy login` will be deprecated starting release 10.22")
2. [MS Learn — Authorize AzCopy with a managed identity](https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azcopy-authorize-managed-identity) — accessed 2026-05-10 (system-assigned vs user-assigned, env-var pattern)
3. [MS Learn — azcopy_login reference](https://learn.microsoft.com/en-us/azure/storage/common/storage-ref-azcopy-login) — accessed 2026-05-10

**Design impact:**

- Use `export AZCOPY_AUTO_LOGIN_TYPE=MSI` at the top of `backup-to-blob.sh`, not `azcopy login --identity`. Future-proofs against the v10.22+ deprecation.
- `deploy-on-vm.sh` installs azcopy idempotently: check `command -v azcopy` first; if missing, `curl -L https://aka.ms/downloadazcopy-v10-linux | tar xz` + `install -m 0755 …/azcopy /usr/local/bin/azcopy`. Pin a minor version in the URL once chosen, to avoid silent upgrades.
- Keep `az storage blob upload` for the small `pg_dump.sql.gz` file (one-shot, simple) and use `azcopy cp --recursive` only for the Parquet tree (the actual perf-sensitive path).

**Test implication:**

- Smoke test on rehearsal RG: as `msaiadmin` on the VM, run `AZCOPY_AUTO_LOGIN_TYPE=MSI azcopy cp /tmp/probe https://<sa>.blob.core.windows.net/msai-backups/probe-$(date -u +%s)/ --recursive` and confirm exit 0 + blob lands. Repeat after a `sudo systemctl restart …` to verify env var is sticky from `Environment=` in the unit, not the operator's shell.
- Failure-mode test: revoke `Storage Blob Data Contributor` from the VM MI temporarily; `azcopy cp` should fail with a clear authz error (not a hang).

---

### 2. Azure Monitor scheduled query alerts in 2026 (Bicep API version + KQL patterns)

**Findings:**

1. **API version pick: `2022-06-15` (GA) or `2024-01-01-preview`.** The `2023-12-01` version is documented but the resource provider is **not registered** in many regions (bicep-types-az#2318) and deployments fail with "API version unavailable". `2023-03-15-preview` works but is older preview. Recommendation: **pin to `2022-06-15`** for everything in Slice 4 unless we need a feature that landed later (we don't).
2. **`kind: 'LogAlert'`** is the value for Log Analytics KQL alerts. The resource shape: `scopes: [<workspaceId>]`, `criteria.allOf[]` carries the KQL `query`, `threshold`, `operator`, `timeAggregation`, `metricMeasureColumn`, `dimensions`.
3. **Action group wiring:** `actions.actionGroups: [<actionGroupId>]`; `actions.customProperties` for templated payload. One action group for all Slice 4 alerts is fine for solo operator (single email destination).
4. **KQL patterns we'll need:**
   - `/health` failure: a query against an `AppAvailabilityResults` (if availability test) or syslog/custom log table for backend access logs filtering 5xx, count over 5 min, threshold > N.
   - Container restart: parse Syslog records emitted by docker/containerd into KQL. AMA's Syslog stream captures `daemon.*` facility messages including container restart events; the alert is a count of `Health check failed` / `restarted` strings in the last 10 min.
   - Orphan NSG rule age: there is no AMA-emitted NSG-rule age signal — this needs an `AzureActivity` query for `Microsoft.Network/networkSecurityGroups/securityRules/write` with name pattern `gha-transient-*` and `now() - TimeGenerated > 30m` AND no matching delete event since.
   - Backup failure: query Syslog for `backup-to-blob.service` exit-non-zero entries.
5. **Severity levels** in scheduledQueryRules are `0` (critical) through `4` (verbose). PRD's "1=critical, 2=warning, 3=informational" maps fine to `1`/`2`/`3`.

**Sources:**

1. [MS Learn — scheduledQueryRules reference](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/scheduledqueryrules) — accessed 2026-05-10
2. [MS Learn — Create monitoring resources by using Bicep](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/scenarios-monitoring) — accessed 2026-05-10
3. [GitHub Azure/bicep-types-az#2318 — 2023-12-01 unavailable error](https://github.com/Azure/bicep-types-az/issues/2318) — accessed 2026-05-10
4. [MS Learn — Types of Azure Monitor alerts](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-types) — accessed 2026-05-10

**Design impact:**

- Pin `Microsoft.Insights/scheduledQueryRules@2022-06-15`, `Microsoft.Insights/actionGroups@2023-01-01`, `Microsoft.Insights/webtests@2022-06-15` in `infra/main.bicep` (or a dedicated `infra/observability.bicep` module). Document the 2023-12-01 trap in the bicep file as a comment so a future hand doesn't bump.
- One `actionGroups` resource (`msai-ops-alerts`), email receiver `pablo@ksgai.com`, used by all four scheduled query rules.
- Container-restart alert needs a KQL probe query stood up first (run interactively in the workspace) to confirm the Syslog stream actually carries the docker/containerd restart line on this VM. **Don't ship a query that hasn't returned ≥1 row in a manual test.**

**Test implication:**

- Unit: `bicep build` + `az deployment group what-if` shows 4 new `scheduledQueryRules` + 1 `actionGroup` Created on first apply, 0 Modify on re-apply.
- Integration smoke (PRD §11 acceptance): `docker stop msai-backend-1` → wait 6 min → operator email arrives. Repeat for crashloop (`while true; do docker restart msai-foo; sleep 10; done`) — confirm only one alert email per fire window (no storm).
- KQL probe queries pre-flight: each query returns ≥1 historical row in the last 7 days when run interactively; if not, the Syslog/AzureActivity pipeline isn't carrying what we think (bug, not test).

---

### 3. AMA vs Container Insights for Compose-on-VM container metrics

**Findings:**

1. **Container Insights is K8s/AKS-oriented.** All current MS Learn entry points for Container Insights describe enabling against AKS or Azure Arc-enabled K8s. There is **no documented Container Insights story for plain `docker compose` on an Ubuntu VM in 2026.** ([Container Insights overview](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/container-insights-overview), [oneuptime 2026-02 walkthrough](https://oneuptime.com/blog/post/2026-02-16-how-to-set-up-azure-monitor-for-containers-to-monitor-aks-cluster-health/view))
2. **AMA does not natively scrape per-container metrics.** AMA's data sources are Performance counters (host-level), Syslog, Windows event logs, and custom text/JSON logs. Per-container CPU/mem/restart-count is **not** an out-of-the-box stream. (Confirmed by [MS Q&A — Container Monitoring without Log Analytics Agent](https://learn.microsoft.com/en-us/answers/questions/1185915/container-monitoring-in-azure-monitor-without-log).)
3. **Two viable patterns for our case:**
   - **(A) Syslog + KQL parsing.** Docker's daemon emits container lifecycle events to journald/syslog; AMA's Syslog stream carries them; KQL parses for the "container ID restarted" patterns. This is what Slice 1 already wired (`kind: 'Linux'` DCR + Syslog facilities per `feedback_ama_dcr_kind_linux_required.md`). **Cheapest path. No new agent.**
   - **(B) Run cAdvisor or a Prometheus exporter sidecar + Azure Monitor managed Prometheus + Prometheus alert rules.** Production-grade per-container metrics. **Significantly larger blast radius.** Out of scope for Phase 1.
4. **Restart-count semantics.** In pattern (A), "restart" is detected by counting matching log lines in a window (`>3 restarts in 10 min`), not by a true container-restart-counter signal. False-positive risk on high-churn services. Mitigation: filter the KQL to `SyslogMessage matches /Container .* died|restarted/` and bind to specific container names (`msai-backend-1`, etc.).

**Sources:**

1. [MS Learn — Container Insights overview](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/container-insights-overview) — accessed 2026-05-10
2. [MS Learn — Tutorial: Collect guest logs from an Azure VM](https://learn.microsoft.com/en-us/azure/azure-monitor/vm/tutorial-collect-logs) — accessed 2026-05-10
3. [MS Learn — Collect Syslog events with AMA](https://learn.microsoft.com/en-us/azure/azure-monitor/vm/data-collection-syslog) — accessed 2026-05-10
4. [MS Q&A — Container Monitoring in Azure Monitor without Log Analytics Agent](https://learn.microsoft.com/en-us/answers/questions/1185915/container-monitoring-in-azure-monitor-without-log) — accessed 2026-05-10

**Design impact:**

- **Adopt pattern (A): extend Slice 1's existing DCR with one or two more Syslog facilities (`daemon`, possibly `user`) and write the alert KQL against the `Syslog` table.** No new extension, no new agent, no Container Insights enablement. Matches the `feedback_ama_dcr_kind_linux_required.md` learning that AMA + `kind: 'Linux'` + Syslog stream is the working stack.
- Acceptance criterion stands: "any prod container with >3 restarts in 10 min" — implemented as a KQL count over Syslog messages. Document this clearly in the runbook so a future operator doesn't expect a true restart counter.
- After any DCR change, **`sudo systemctl restart azuremonitoragent` on the VM** (per `feedback_ama_dcr_kind_linux_required.md`) — bake this into `deploy-on-vm.sh` or call it out in the runbook.

**Test implication:**

- Pre-flight KQL probe: in the workspace, run the proposed restart query against the last 24h. If it returns 0 rows AND we know there have been restarts, the Syslog facility is wrong — fix the DCR before shipping the alert.
- Smoke after DCR change: deliberately `docker restart msai-backend-1` 4 times in 5 min, wait the alert evaluation window, confirm email arrives. If not, dump the last 1h of `Syslog` for the host and inspect what AMA actually captured.

---

### 4. systemd timer `Persistent=true` on Ubuntu 24.04

**Findings:**

1. **`Persistent=true` works as advertised on Ubuntu 24.04 (systemd 255).** When the timer activates after a missed window, the service unit is triggered immediately if it would have fired during the inactive period.
2. **`RandomizedDelaySec` interacts badly with frequent reboots** (systemd#21166): the catch-up is subject to the random delay, and if the system shuts down before the delay expires, the run is lost. Our VM doesn't reboot frequently, but a deploy that includes `apt upgrade` + reboot followed by another deploy could stack two miss windows.
3. **`OnCalendar=*-*-* 02:07:00`** is interpreted as **UTC by default** unless the unit sets a `Timezone=` (newer systemd) or `OnCalendar` includes an explicit zone suffix. Ubuntu 24.04 systemd supports `OnCalendar=*-*-* 02:07:00 UTC` — be explicit.
4. **Missed-run dedup:** `Persistent=true` writes the last-trigger timestamp under `/var/lib/systemd/timers/stamp-backup-to-blob.timer`. If the VM is offline for 3 days, **only one** catch-up run fires on next boot (not three). Good for our case.
5. **`RandomizedDelaySec=300`** (5 min) is a healthy default for nightly cron-style work. The PRD's 02:07 "off-the-hour" already addresses scheduler-stampede on the storage backend; the random delay is belt-and-suspenders.

**Sources:**

1. [Ubuntu manpage — systemd.timer](https://manpages.ubuntu.com/manpages/jammy/en/man5/systemd.timer.5.html) — accessed 2026-05-10
2. [Arch Wiki — systemd/Timers](https://wiki.archlinux.org/title/Systemd/Timers) — accessed 2026-05-10
3. [systemd#21166 — Persistent timers + large RandomizedDelaySec on rebooted systems](https://github.com/systemd/systemd/issues/21166) — accessed 2026-05-10

**Design impact:**

- Unit file:

  ```ini
  [Timer]
  OnCalendar=*-*-* 02:07:00 UTC
  Persistent=true
  RandomizedDelaySec=300
  Unit=backup-to-blob.service

  [Install]
  WantedBy=timers.target
  ```

  Explicit `UTC` suffix; document why 02:07 (off-the-hour) and why 300s delay.

- Service unit `Type=oneshot`, `User=root` (needs to read postgres docker-volume contents), `Environment=AZCOPY_AUTO_LOGIN_TYPE=MSI`, `ExecStart=/opt/msai/scripts/backup-to-blob.sh`. Set `StandardOutput=journal` so logs land in journald and AMA Syslog stream picks them up.
- `deploy-on-vm.sh` writes the units under `/etc/systemd/system/`, runs `systemctl daemon-reload && systemctl enable --now backup-to-blob.timer`. Idempotent (re-write is safe).

**Test implication:**

- Smoke: `systemctl start backup-to-blob.service` on demand, observe `journalctl -u backup-to-blob.service`, confirm blob lands and the `lastrun` stamp is updated.
- Catch-up smoke: stop the timer (`systemctl stop`), `touch -d 'yesterday' /var/lib/systemd/timers/stamp-backup-to-blob.timer`, restart timer, observe a catch-up run within `RandomizedDelaySec`.

---

### 5. Storage account lifecycle policy in Bicep

**Findings:**

1. **Resource type:** `Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01` (also `2024-01-01`); a **singleton** — name is always `'default'`. Parent is the storage account.
2. **Filter shape for backup-only retention:**
   ```bicep
   filters: {
     blobTypes: [ 'blockBlob' ]
     prefixMatch: [ 'msai-backups/backup-' ]
   }
   ```
   `prefixMatch` is `<container>/<blob-prefix>`. For our case `msai-backups/backup-` matches every `backup-<UTC-iso>…` blob and excludes anything else dropped in the container.
3. **Action shape for "delete after 30d":**
   ```bicep
   actions: {
     baseBlob: {
       delete: { daysAfterCreationGreaterThan: 30 }
     }
   }
   ```
   `daysAfterCreationGreaterThan` is the right field for write-once backups. `daysAfterModificationGreaterThan` is wrong here because we don't modify blobs after upload.
4. **Versions / snapshots:** none in our flow; don't need `version` or `snapshot` rules.
5. **Drift / MS Q&A noted Internal Server Error** when the policy is declared with subtle schema mistakes (extra/missing fields). Ship via `az deployment group what-if` first; if the engine accepts it, the runtime apply is reliable.

**Sources:**

1. [MS Learn — Configure a lifecycle management policy](https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-policy-configure) — accessed 2026-05-10
2. [MS Learn — managementPolicies reference](https://learn.microsoft.com/en-us/azure/templates/microsoft.storage/storageaccounts/managementpolicies) — accessed 2026-05-10
3. [Azure/bicep-registry-modules — storage-account README](https://github.com/Azure/bicep-registry-modules/blob/main/avm/res/storage/storage-account/README.md) — accessed 2026-05-10

**Design impact:**

- One `managementPolicies` resource in `infra/main.bicep` parented to the existing storage account, with one rule (`name: 'expire-msai-backups-30d'`, `enabled: true`, type `'Lifecycle'`).
- Use `daysAfterCreationGreaterThan: 30` (PRD D-3 default A — flat 30d).
- `prefixMatch: ['msai-backups/backup-']` — strict prefix so the rule never deletes operator-uploaded probes/manual artifacts that don't match.

**Test implication:**

- Bicep `what-if` re-apply shows 0 ops once the policy exists.
- Lifecycle policies don't fire instantly (Azure evaluates ~daily). Acceptance can't wait 30d — verify by **simulating with a backdated blob**: `az storage blob upload --metadata createdOn=$(date -u -d '40 days ago' +…)` won't trick the engine because the engine uses the actual `Properties.creationTime`. Instead, manually inspect the rule via `az storage account management-policy show` and assert `enabled=true`, prefix matches, `daysAfterCreationGreaterThan=30`. Document that real expiration won't be observable until day 31 post-merge.

---

### 6. Azure Monitor availability tests (`Microsoft.Insights/webtests`)

**Findings:**

1. **URL ping tests retire 2026-09-30.** Use `kind: 'standard'` going forward. Standard tests support HTTPS, custom verbs, custom headers, body, TLS validity assertion.
2. **Resource needs an Application Insights parent.** `tags['hidden-link:<aiResourceId>'] = 'Resource'` is **mandatory** — without it Azure rejects deployment. The App Insights resource must be in the **same RG** as the webtest.
3. **We don't currently have an App Insights resource.** Slice 1 deployed a Log Analytics workspace (`2add1786-…`) but no `Microsoft.Insights/components`. Workspace-based App Insights is the 2026 standard — declared as `Microsoft.Insights/components@2020-02-02` with `kind: 'web'`, `WorkspaceResourceId: <existingLAWorkspaceId>`. **Adds one resource to Slice 4.**
4. **Locations:** standard tests require ≥1 location; MS recommends ≥5 distinct locations to dampen single-region false positives. Each location = one execution per frequency tick.
5. **Cost:** ~€0.0005 per execution. At 5 locations × every 5 min × ~8,640 ticks/month = ~€20/month. At 1 location × every 5 min = ~€4/month. **For Phase 1 single-operator, 1–3 locations every 5 min is plenty.** PRD §10 doesn't budget multi-region; default to 3 locations (NA + EU + APAC for global signal).
6. **Alert rule:** the webtest result feeds `AppAvailabilityResults` table; the alert can be a `scheduledQueryRules` over that table OR (simpler) an Application Insights metric alert (`Microsoft.Insights/metricAlerts`) on `availabilityResults/availabilityPercentage < 100` over 5 min.

**Sources:**

1. [MS Learn — Application Insights availability tests](https://learn.microsoft.com/en-us/azure/azure-monitor/app/availability) — accessed 2026-05-10
2. [MS Learn — Microsoft.Insights/webtests reference](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/webtests) — accessed 2026-05-10
3. [johnnyreilly — Azure standard availability tests with Bicep](https://johnnyreilly.com/azure-standard-tests-with-bicep) — accessed 2026-05-10
4. [Ronald's Blog — Track Availability with Standard Test (2026-01)](https://ronaldbosma.github.io/blog/2026/01/12/track-availability-in-application-insights-using-standard-test/) — accessed 2026-05-10

**Design impact:**

- Slice 4 adds:
  - `Microsoft.Insights/components` (workspace-based App Insights), 1 resource
  - `Microsoft.Insights/webtests@2022-06-15` with `kind: 'standard'`, target `https://platform.marketsignal.ai/health`, frequency 300s, 3 geo locations, expected status 200, with the `hidden-link` tag pointing at the App Insights component
  - `Microsoft.Insights/metricAlerts` on the webtest's availability metric (simpler than scheduledQueryRules for this one signal); routes to the same `msai-ops-alerts` action group
- This **resolves PRD D-1 default A** (external probe) and avoids the AMA-Container-Insights mess for the `/health` signal specifically.

**Test implication:**

- Smoke: deliberately `docker stop msai-backend-1`; `/health` returns connection-refused or 502 from Caddy; webtest fails next eval (within 5 min); metric alert fires; email arrives.
- Negative: keep stack healthy for 30 min, observe `availabilityResults/availabilityPercentage = 100` in Metrics Explorer, no alerts fire.
- Cost guardrail: monthly cost line item ≤ €10. Actual pricing visible in subscription Cost Management 24h after first eval.

---

### 7. (Bonus) Caddy passthrough of `X-API-Key` for the active-deployment gate

**Findings:**

1. **Caddy 2's `reverse_proxy` passes all incoming request headers through to the upstream by default**, with the only exceptions being `X-Forwarded-For` / `-Proto` / `-Host` (which Caddy sets/sanitizes for security). Custom headers like `X-API-Key` are **not** stripped.
2. No additional `header_up` directive needed in our `Caddyfile` for the deploy-gate's `curl https://platform.marketsignal.ai/api/v1/live/status -H 'X-API-Key: …'` to reach the backend.

**Sources:**

1. [Caddy docs — reverse_proxy directive](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) — accessed 2026-05-10
2. [Caddy community — reverse_proxy with custom header_up](https://caddy.community/t/reverse-proxy-with-custom-header-up/11459) — accessed 2026-05-10

**Design impact:** No Caddyfile change needed for Slice 4. Confirms PRD §8 finding 6.

**Test implication:** Single curl from the runner against rehearsal host with `X-API-Key` confirms the backend sees the header (via response shape — the unauth path returns 401, the authed path returns the live status JSON).

---

## Open Risks

1. **Container-restart KQL is heuristic, not a true counter.** Pattern-matching Syslog lines for "container … restarted" can miss/over-count under unusual log formats (e.g., if Docker logging-driver changes, or systemd-journald rotates). Mitigation: probe-test the query before shipping; document the limitation; accept Phase 1 alert may have minor false positive/negative rate. Phase 2 fix is cAdvisor + managed Prometheus.
2. **Lifecycle-policy 30-day expiration is not testable in Slice 4 acceptance.** We can verify the rule is declared correctly but real deletion only happens at day 31 post-merge. Add a calendar check (operator) on day 33 to confirm a backup-from-day-1 is actually gone.
3. **`scheduledQueryRules@2022-06-15` may be superseded mid-Phase-2** if MS finally GA-stabilizes 2023-12-01 and starts deprecating older versions. Low impact (Bicep API-version bumps are cheap), flag at Phase 2 review.
4. **AzCopy auto-login MSI quirks with user-assigned MI** (issues #2398, #2587, #2665) don't bite us — VM uses **system-assigned**. If we ever migrate to user-assigned, redo this research.
5. **Webtest cost creep** if a future hand bumps from 3 locations → 10 (~€60/mo). Add the location count to a Bicep parameter with a documented default and a comment about cost.
6. **`Microsoft.Insights/components` is a new resource type for this RG.** Requires a workspace-based App Insights schema (`WorkspaceResourceId` pointing at existing LA workspace). One-time setup — no migration concerns since we have no historical telemetry to preserve.
7. **DCR change in Slice 4 (extending Syslog facilities) requires AMA restart on the VM** — bake `sudo systemctl restart azuremonitoragent` into `deploy-on-vm.sh` post-DCR-update or a one-shot ops command. Per `feedback_ama_dcr_kind_linux_required.md`, AMA's negative-cache survives DCR updates without a restart.
