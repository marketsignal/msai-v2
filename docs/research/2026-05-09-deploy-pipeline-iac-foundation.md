# Research: deploy-pipeline-iac-foundation

**Date:** 2026-05-09
**Feature:** Slice 1 of 4 — Provision Azure foundational IaC (Bicep + ACR + KV + Blob + Log Analytics + AMA + GH OIDC + system-assigned MI) for the deployment pipeline. No application deploys.
**Researcher:** research-first agent

> **Scope note.** This is infrastructure work. There are no `package.json` / `pyproject.toml` deltas. The "external libraries/APIs" researched here are Azure resource providers, GitHub OIDC token claims, and `az` CLI behavior. The seven topics below come verbatim from the slicing-verdict's "Missing Evidence" section plus two bonus operational checks (Premium SSD pricing, deterministic naming).

---

## Libraries / APIs Touched

| Surface                                           | Our pinned form                         | Latest stable / current guidance                                                                                                                                                                                    | Breaking changes vs our assumed shape                                                  | Source                                                                                                                                                                                                                                                                                                            |
| ------------------------------------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bicep authoring                                   | n/a (greenfield)                        | Modular structure; AVM optional; `uniqueString(resourceGroup().id)` for naming                                                                                                                                      | n/a — we have nothing yet                                                              | [MS Learn — Bicep best practices](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/best-practices) (2025-12-10)                                                                                                                                                                               |
| GitHub Actions OIDC → Azure federated credential  | n/a (greenfield)                        | Standard subject grammar; **flexible/wildcard subjects are PREVIEW only**                                                                                                                                           | n/a                                                                                    | [MS Learn — Connect from Azure OpenID Connect](https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect), [GitHub OIDC discussion #172176](https://github.com/orgs/community/discussions/172176) (2026-05-09)                                                                   |
| Azure Monitor Agent (AMA, Linux)                  | n/a                                     | Resource type `Microsoft.Compute/virtualMachines/extensions`, publisher `Microsoft.Azure.Monitor`, type `AzureMonitorLinuxAgent`, typeHandlerVersion `1.21+`, apiVersion `2021-11-01` (or newer); MMA fully retired | Legacy Log Analytics agent (MMA): **backend shuts down 2026-03-02** — no fallback path | [MS Learn — AMA install/manage](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-manage) (2026-02-18), [MS Learn — MMA retirement](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-migration) (2026-04)                                          |
| AMA → Data Collection Rule association            | n/a                                     | Resource type `Microsoft.Insights/dataCollectionRuleAssociations` apiVersion `2021-09-01-preview` (still the current shipping API per 2026-02-18 docs)                                                              | n/a                                                                                    | [MS Learn — AMA install/manage §Configure (preview)](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-manage) (2026-02-18)                                                                                                                                                        |
| VM system-assigned managed identity / IMDS        | n/a                                     | IMDS endpoint `http://169.254.169.254/metadata/identity/oauth2/token` with `Metadata: true` header; recommended retry: ExponentialBackoff 5 attempts, 2s→30s                                                        | None breaking; IMDS API version `2018-02-01+` still the recommendation                 | [MS Learn — Use MI on VM](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-to-use-vm-token) (2025-11-11), [MS Learn — How MI works on VM](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-managed-identities-work-vm) (2026-02-19) |
| Azure managed disks (Premium SSD vs Standard SSD) | Architecture verdict pinned Premium SSD | Standard SSD adequate for ~1 GB/day write; Premium SSD justified by IOPS floor + low-latency for DuckDB scans                                                                                                       | n/a                                                                                    | [Azure Pricing — Managed Disks](https://azure.microsoft.com/en-us/pricing/details/managed-disks/) (2026-05-09), [MS Learn — Disk types](https://learn.microsoft.com/en-us/azure/virtual-machines/disks-types)                                                                                                     |
| Storage account naming                            | n/a                                     | `${prefix}${uniqueString(resourceGroup().id)}` is current best practice in 2026                                                                                                                                     | n/a                                                                                    | [MS Learn — Bicep best practices §Names](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/best-practices), [MS Learn — Name generation pattern](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/patterns-name-generation)                                                |

---

## Per-Topic Analysis

### 1. Bicep current idioms — single file vs modules (~10–15 resources)

**Question (verbatim):** For an infra deployment of ~10–15 Azure resources (VM, NSG, disk, storage, ACR, KV, Log Analytics, AMA extension, federated credential, role assignments), what's the current best practice — one `main.bicep` or split into `infra/modules/*.bicep`? What does the Bicep team itself recommend in 2026?

**Findings:**

1. The official **Bicep best-practices doc** (last updated 2025-12-10) is opinionated about **naming, parameters, variables, outputs, and child-resource patterns** but is **deliberately silent on a hard "split into modules at N resources" threshold**. It says modules are for **encapsulating complexity and enabling reuse**, not for arbitrary line-count splits. Source: [MS Learn — Bicep best practices](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/best-practices) (accessed 2026-05-09).
2. The community/Azure-team Discussion #10926 ("Best practices for organizing a fairly large infrastructure project with Bicep") and the 2026-02 post on Azure Bicep modules both treat **modularization as the default once you cross "more than one logical concern in a single file"** — networking + compute + identity + secrets is already three concerns. Sources: [GitHub Discussion #10926](https://github.com/Azure/bicep/discussions/10926), [iamachs.com — Master Bicep Modules](https://iamachs.com/blog/azure-bicep/part-4-master-modules-guide).
3. **Implicit dependencies via symbolic names** are now the strong recommendation. The best-practices doc says explicitly: "Prefer using implicit dependencies over explicit dependencies… you can access any resource in Bicep by using the symbolic name. For example, `toyDesignDocumentsStorageAccount.id`." That means a single `main.bicep` of ~600 lines is perfectly idiomatic if the cross-references read naturally — `keyVault.id`, `vm.identity.principalId`, `acr.id` — without `dependsOn:` arrays.
4. **Outputs are NOT the recommended bridge** between modules when the consumer can use the `existing` keyword. From the same doc: "Instead of passing property values around through outputs, use the existing keyword to look up properties of resources that already exist."
5. **Azure Verified Modules (AVM)** went GA for Bicep in **January 2026** ([ADTmag — AVM GA 2026-01-20](https://adtmag.com/articles/2026/01/20/microsoft-makes-bicep-azure-verified-modules-for-azure-landing-zones-generally-available.aspx)). AVM is **scoped at the Landing Zone Accelerator level** — it solves multi-subscription, multi-tenant, multi-region governance problems. For a single-RG, single-VM Phase 1 deploy, AVM imports add transitive parameters and version-pin pressure with **no payback** at this scale. Source: [Azure Verified Modules](https://azure.github.io/Azure-Verified-Modules/) (accessed 2026-05-09).

**Sources:**

1. [MS Learn — Learn best practices when developing Bicep files](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/best-practices) — accessed 2026-05-09 (page last updated 2025-12-10)
2. [GitHub Azure/bicep Discussion #10926](https://github.com/Azure/bicep/discussions/10926) — accessed 2026-05-09
3. [iamachs.com — Master Bicep Modules](https://iamachs.com/blog/azure-bicep/part-4-master-modules-guide) — accessed 2026-05-09
4. [ADTmag — Microsoft Makes Bicep AVM Generally Available](https://adtmag.com/articles/2026/01/20/microsoft-makes-bicep-azure-verified-modules-for-azure-landing-zones-generally-available.aspx) — accessed 2026-05-09
5. [Azure Verified Modules home](https://azure.github.io/Azure-Verified-Modules/) — accessed 2026-05-09

**Design impact:**

For Slice 1's ~15 resources scoped to one RG and one VM, **a single `infra/main.bicep` is the correct shape**, with two qualifications: (a) keep it strictly section-ordered (params → vars → identity → networking → storage/data → registry/secrets → vm+extensions → role assignments → outputs) so cross-references read as a top-down dependency chain via symbolic names; (b) **do NOT introduce AVM** for this slice — its sweet spot is Landing Zone scale. If the file crosses ~600 lines OR if Slice 4 adds per-environment variants (dev/stage/prod) we revisit module split then. **No "modules to be cute" refactor** mid-Slice 1.

**Test implication:**

Phase 5 acceptance test should `az bicep build infra/main.bicep` (lint clean, no warnings) and `az deployment group what-if --resource-group msaiv2_rg --template-file infra/main.bicep` (zero diff after a clean apply). The what-if-zero-diff check is the cleanest proof that the file is genuinely idempotent — symbolic-name implicit dependencies make this property robust as long as we avoid `reference()` and string-concatenated `resourceId()` calls.

**Open risks:**

- The Bicep team has not published a definitive "this many resources = split" rule. Reasonable senior reviewers may push for module split during Slice 4. Pre-empt by tagging the design "monolithic by intent for Phase 1; revisit at file > 600 lines OR multi-env."
- AVM Bicep registry is GA but the **per-resource modules** (e.g., `avm/res/storage/storage-account`) carry their own param surface. We could regret skipping them if Slice 4 wants alert-rule defaults that AVM bakes in. Verify at implementation time.

---

### 2. GitHub OIDC federated credential subject claim format

**Question (verbatim):** For a GH Actions workflow that triggers on `push` to `main`, what is the exact `subject` claim format the federated credential must accept?

**Findings:**

1. **Push-to-branch (no environment) subject grammar:** `repo:<OWNER>/<REPO>:ref:refs/heads/<BRANCH>`. For our case: `repo:marketsignal/msai-v2:ref:refs/heads/main`. Sources: [MS Learn — Connect from Azure OpenID Connect](https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect) (accessed 2026-05-09); confirmed in [Firefly — Federated identity credentials](https://www.firefly.ai/blog/how-to-secure-deployments-to-azure-with-github-actions-using-federated-identity-credentials).
2. **Environment-bound subject grammar:** `repo:<OWNER>/<REPO>:environment:<NAME>`. The GitHub OIDC docs only show one example and it's this one (`"repo:octo-org/octo-repo:environment:prod"`). [docs.github.com — OpenID Connect](https://docs.github.com/en/actions/concepts/security/openid-connect) (accessed 2026-05-09).
3. **Other subjects:** `repo:<OWNER>/<REPO>:pull_request` (any PR — note: hyphen, no `:ref:` suffix); `repo:<OWNER>/<REPO>:ref:refs/tags/<TAG>` (tag push). Source: [Firefly](https://www.firefly.ai/blog/how-to-secure-deployments-to-azure-with-github-actions-using-federated-identity-credentials) (accessed 2026-05-09).
4. **Audience claim (`aud`):** Default is **`api://AzureADTokenExchange`** — this is what the official Azure-OIDC-from-GH guide and Microsoft samples both pin. No reason to override for our use case. Source: [MS Learn — Connect from Azure OpenID Connect](https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect) (accessed 2026-05-09).
5. **Wildcards / "flexible" subjects:** Azure shipped **"Flexible federated identity credentials"** as a **preview** feature (community discussion #172176, 2025–2026). It uses a claims-matching expression language: `claims['sub'] matches 'repo:myorg/myapp:ref:refs/heads/*'`. **It is NOT GA**. We must use exact-match subject claims for Slice 1. Sources: [GitHub Community Discussion #172176](https://github.com/orgs/community/discussions/172176) (accessed 2026-05-09); [GitHub Community Discussion #47298](https://github.com/orgs/community/discussions/47298) on the older "no available entity" UX issue.
6. **Environment NOT required.** The federated credential works fine without a GitHub Environment binding when subject = `ref:refs/heads/main`. We can add an Environment later (e.g., `production`) for additional approval gates without changing the credential — we'd just add a SECOND federated credential with `environment:production` subject. That's the standard pattern for "promote to prod with manual approval."

**Sources:**

1. [Microsoft Learn — Authenticate to Azure from GitHub Actions by OpenID Connect](https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect) — accessed 2026-05-09
2. [GitHub Docs — Configuring OpenID Connect in Azure](https://docs.github.com/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-azure) — accessed 2026-05-09
3. [GitHub Docs — OpenID Connect concepts](https://docs.github.com/en/actions/concepts/security/openid-connect) — accessed 2026-05-09
4. [GitHub Community Discussion #172176 — Flexible federated identity credentials (preview)](https://github.com/orgs/community/discussions/172176) — accessed 2026-05-09
5. [Firefly — How to Secure Deployments to Azure with GitHub Actions Using Federated Identity Credentials](https://www.firefly.ai/blog/how-to-secure-deployments-to-azure-with-github-actions-using-federated-identity-credentials) — accessed 2026-05-09

**Design impact:**

The Bicep `Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials` (or App Registration variant) for Slice 1 needs **one credential, exact-match**:

- `issuer = "https://token.actions.githubusercontent.com"`
- `subject = "repo:marketsignal/msai-v2:ref:refs/heads/main"`
- `audiences = ["api://AzureADTokenExchange"]`

No GitHub Environment is declared in Slice 1. When Slice 3 wants a manual-approval gate before the first prod deploy, we add a second federated credential with `subject = "repo:marketsignal/msai-v2:environment:production"` and switch the deploy-job's workflow YAML to `environment: production`. This is forward-compatible — Slice 1's credential stays.

**Test implication:**

Phase 5 acceptance test should be a no-op workflow run on a throwaway branch merged to `main` that does nothing but `azure/login@v2` with `client-id` / `tenant-id` / `subscription-id` and asserts `az account show` succeeds. This proves the credential's subject claim matches what GH actually issues. Use **a low-risk action** (`az group show msaiv2_rg`) — do NOT run any deployment in this acceptance.

**Open risks:**

- If the workflow ever needs to fire from a `pull_request` event (e.g., Slice 2 will run image builds on PR for validation), the existing credential will reject — `pull_request` subjects don't match `ref:refs/heads/main`. Slice 2 will need a second credential. Document this for the Slice 2 PRD.
- "Flexible federated identity credentials" being preview-only means we **cannot** pre-emptively use a wildcard like `ref:refs/heads/*` to cover both `main` and future release branches. Each branch needs its own credential entry. Re-verify GA status before Slice 3.

---

### 3. Azure Monitor Agent (AMA) vs legacy Linux Monitoring Agent (MMA)

**Question (verbatim):** Is AMA still the current best practice in 2026 for Linux VM observability, or has Azure deprecated/replaced it?

**Findings:**

1. **MMA is fully retired and the backend dies imminently.** The Log Analytics agent (MMA / OMS) was retired **2024-08-31**. Microsoft is running a **12-hour upload pause on 2026-01-26 as a validation test** ahead of the **permanent backend shutdown on 2026-03-02**. After 2026-03-02, MMA agents cannot upload data at all — the cloud ingestion services are gone. Source: [MS Learn — Migrate to Azure Monitor Agent](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-migration) (accessed 2026-05-09); confirmed by [Windows Forum — MS pauses MMA uploads ahead of AMA migration](https://windowsforum.com/threads/microsoft-pauses-legacy-mma-uploads-for-12-hours-ahead-of-ama-migration.398937/).
2. **AMA is the only supported path in 2026.** Both [MS Learn — AMA install/manage](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-manage) (last updated 2026-02-18) and the [AMA migration guide](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-migration) name AMA explicitly. There is no successor announced.
3. **AMA capabilities for our use case (heartbeat + syslog):** AMA covers heartbeat natively (via DCR with `Microsoft-Heartbeat` data flow) and syslog natively (Linux syslog facilities → DCR). Optional perf counters (`Microsoft-Perf` data flow) are also available; we only enable heartbeat in Slice 1. Source: [MS Learn — AMA install/manage §Configure](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-manage) (2026-02-18).
4. **Minimum viable DCR for heartbeat-only** is a single DCR resource with `dataSources: { extensions: [...] }` empty and `dataFlows: [{ streams: ["Microsoft-Heartbeat"], destinations: ["msaiLogAnalytics"] }]`, plus a `dataCollectionRuleAssociation` linking it to the VM. We do **NOT** need the `AgentSettings` DCR (that's for agent-cache-size tuning, currently preview-only and Resource-Manager-only).
5. **AMA does NOT auto-create the DCR.** From the MS Learn install page: "[VM extension install method] doesn't create a DCR, so you must create at least one DCR and associate it with the agent before data collection begins." This is the single most-missed step in AMA Bicep templates.

**Sources:**

1. [MS Learn — Migrate to Azure Monitor Agent from Log Analytics agent](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-migration) — accessed 2026-05-09
2. [MS Learn — Install and Manage the Azure Monitor Agent](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-manage) — accessed 2026-05-09 (page updated 2026-02-18)
3. [MS Learn — Prepare for retirement of Log Analytics agent (Defender for Cloud)](https://learn.microsoft.com/en-us/azure/defender-for-cloud/prepare-deprecation-log-analytics-mma-agent) — accessed 2026-05-09
4. [Windows Forum — MS Pauses Legacy MMA Uploads](https://windowsforum.com/threads/microsoft-pauses-legacy-mma-uploads-for-12-hours-ahead-of-ama-migration.398937/) — accessed 2026-05-09

**Design impact:**

`infra/main.bicep` ships **AMA + a heartbeat-only DCR + a DCR association** as a unit. The Log Analytics workspace gets a single SKU=`PerGB2018`. We do **not** mention MMA anywhere — there is no fallback, only AMA. Slice 4's alert rules will sit on the DCR's heartbeat stream + a synthetic `/health` probe.

**Test implication:**

Phase 5 acceptance:

1. After deploy, query the Log Analytics workspace: `az monitor log-analytics query -w <workspace-id> --analytics-query 'Heartbeat | where TimeGenerated > ago(15m) | project Computer, OSType' -t PT15M` should return at least one row within 15 minutes of VM provisioning.
2. `az vm extension show -g msaiv2_rg --vm-name msai-vm -n AzureMonitorLinuxAgent --query 'provisioningState'` should be `"Succeeded"`.
3. `az monitor data-collection rule association list --resource <vm-resource-id>` should show exactly one association to the heartbeat DCR.

**Open risks:**

- AMA install can fail silently if outbound TCP/443 to AMA endpoints is blocked. NSG rule for the VM **must** allow outbound 443 to `*.ods.opinsights.azure.com`, `*.oms.opinsights.azure.com`, `*.monitoring.azure.com`. (See topic 4 for endpoint list.) The NSG default-allow-internet-out rule covers this; if Slice 4 ever tightens the egress NSG, AMA breaks first. Document in the runbook.
- **Linux crypto-policy gotcha:** AMA does not work on Linux when the systemwide crypto policy is set to `FUTURE` mode (RHEL/CentOS 8+). Ubuntu 24 LTS (our target) does not set this by default. Verify in pre-flight: `update-crypto-policies --show` returns `DEFAULT`.

---

### 4. AMA installation via Bicep VM extension

**Question (verbatim):** What's the current Bicep idiom for AMA on a Linux VM?

**Findings:**

1. **Resource type & apiVersion (per-VM, not VMSS):** `Microsoft.Compute/virtualMachines/extensions@2021-11-01` (or newer — `2024-07-01` is the latest GA at the time of writing). Source: [MS Learn — AMA install/manage §Resource Manager template](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-manage) (2026-02-18).
2. **Extension shape (Linux, system-assigned MI):**
   ```bicep
   resource amaLinux 'Microsoft.Compute/virtualMachines/extensions@2024-07-01' = {
     parent: vm
     name: 'AzureMonitorLinuxAgent'
     location: location
     properties: {
       publisher: 'Microsoft.Azure.Monitor'
       type: 'AzureMonitorLinuxAgent'
       typeHandlerVersion: '1.21'  // see extension-versions doc for current
       autoUpgradeMinorVersion: true
       enableAutomaticUpgrade: true
     }
   }
   ```
   Sources: [MS Learn — AMA extension versions](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-extension-versions); [PSRule — Azure.VM.AMA](https://azure.github.io/PSRule.Rules.Azure/en/rules/Azure.VM.AMA/).
3. **DCR association resource type:** `Microsoft.Insights/dataCollectionRuleAssociations@2021-09-01-preview` (still the current shipping API per 2026-02-18 docs — Microsoft has not promoted to a non-preview version). Quoted directly from the MS Learn page above.
4. **Managed identity prerequisite — answered authoritatively:** [MS Learn — AMA requirements](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-requirements) (last updated 2026-01-07): **"Managed identity must be enabled on Azure virtual machines. Both user-assigned and system-assigned managed identities are supported."** System-assigned is recommended for "initial testing and small deployments" — exactly our Phase 1 case. The MI must be enabled **before** the AMA extension reaches a healthy state.
5. **Order-of-operations risk in Bicep:** When both `Microsoft.Compute/virtualMachines` (with `identity: { type: 'SystemAssigned' }`) and the AMA extension live in the same template, Bicep's symbolic-name dependency (`parent: vm`) ensures the VM finishes before AMA starts. **But the MI principal-ID is not necessarily propagated to Entra by the time AMA tries to authenticate.** AMA's automatic-upgrade and self-recovery loops handle this gracefully — extension provisioning will retry. If we manually trigger the extension before propagation, expect a 5-minute settling period. See topic 5 below.
6. **Outbound endpoints required for AMA:** The Linux agent communicates with `*.ods.opinsights.azure.com`, `*.oms.opinsights.azure.com`, `*.monitoring.azure.com`, and `*.handler.control.monitor.azure.com`. Source: [MS Learn — AMA network configuration](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-network-configuration) (linked from AMA install/manage). Phase 1 NSG default-allow-out covers this.

**Sources:**

1. [MS Learn — Install and Manage the Azure Monitor Agent](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-manage) — accessed 2026-05-09 (last updated 2026-02-18)
2. [MS Learn — Azure Monitor Agent Requirements](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-requirements) — accessed 2026-05-09 (last updated 2026-01-07)
3. [MS Learn — Azure Monitor Agent extension versions](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-extension-versions) — accessed 2026-05-09
4. [MS Learn — Resource Manager template samples for agents](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/resource-manager-agent) — accessed 2026-05-09

**Design impact:**

`infra/main.bicep` orders resources as: `vm` (with `identity: { type: 'SystemAssigned' }`) → `dataCollectionRule` (heartbeat) → `amaExtension` (parent=vm, depends implicitly via parent) → `dataCollectionRuleAssociation` (scope=vm, dataCollectionRuleId=dcr.id, depends implicitly on both). No `dependsOn:` arrays needed. No user-assigned identity for AMA in Slice 1 — the system-assigned identity handles AMA's outbound auth, and `userAssignedIdentities` would add complexity for no Phase-1 benefit.

**Test implication:**

Phase 5 acceptance: in addition to the heartbeat KQL query (topic 3), assert `az vm extension show -g msaiv2_rg --vm-name msai-vm -n AzureMonitorLinuxAgent --query 'provisioningState' -o tsv` returns `Succeeded` within 10 minutes of `az deployment group create` finishing. If it returns `Failed` or stays in `Creating`, dump `/var/log/azure/Microsoft.Azure.Monitor.AzureMonitorLinuxAgent/CommandExecution.log` from the VM via SSH for triage.

**Open risks:**

- The DCR-association API is still labeled `preview` in the apiVersion (`2021-09-01-preview`). It has been preview for years and has not had a breaking change, but a future GA bump will require a Bicep update. Pin the version explicitly and revisit at Slice 4.
- AMA ships `enableAutomaticUpgrade: true` so the typeHandlerVersion drifts. We pin a floor (`1.21`) but production may run something newer. This is desired behavior — verify at implementation time that nothing in the runbook references a specific minor version.

---

### 5. VM system-assigned managed identity propagation latency

**Question (verbatim):** After `az deployment group create` finishes provisioning a VM with `identity: { type: 'SystemAssigned' }`, how long until the identity can authenticate against Key Vault?

**Findings:**

1. **No Microsoft-published numerical SLA exists** for system-assigned MI propagation latency. The official docs describe the lifecycle ([How MI works on VM](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-managed-identities-work-vm)) but do not publish "X seconds." Empirical reports cluster at **30–90 seconds**, with **occasional outliers at 3–5 minutes** when Entra is under load.
2. **The official IMDS retry strategy is documented and authoritative.** From [MS Learn — Use MI on VM §Retry guidance](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-to-use-vm-token):
   - **404 Not Found:** "IMDS endpoint is updating. Retry with Exponential Backoff."
   - **410 Gone:** "IMDS is going through updates and will be available in a maximum of 70 seconds."
   - **429 Too Many Requests:** Throttle. Exponential backoff.
   - **5xx Transient:** Retry after ≥ 1 second; faster retries trigger 429.
   - **4xx (other):** **Don't retry. These are design-time errors.**
3. **The recommended retry curve** from the same doc is **5 attempts: delay 0s, 2s, 6s, 14s, 30s** (Min back-off 0, Max back-off 60s, Delta 2s, ExponentialBackoff). Total budget ~52s. For our boot-time KV-render script, **double this** (10 attempts, ~5 minute total budget) to absorb the post-deployment propagation tail.
4. **Differentiating "not propagated yet" from "permanently denied":**
   - `404` from IMDS itself = MI not yet attached → retry.
   - `200` from IMDS + access token + then `403 Forbidden` from Key Vault `getSecret` = **the MI exists but the RBAC role assignment hasn't propagated** → retry (RBAC propagation is a separate ~30s window).
   - `200` from IMDS + access token + then `401` or `404` from Key Vault = wrong KV name or wrong tenant → **don't retry; fix config**.
5. **`az identity show` does NOT wait for propagation.** It returns whatever ARM has indexed at the moment of the call — which can lag IMDS readiness. **Don't use `az identity show` as a readiness probe.** The only reliable readiness probe is "actually call IMDS for a token AND actually call Key Vault."
6. **Token caching:** The IMDS subsystem caches tokens internally; once you've gotten one successful 200, subsequent calls are fast. But you **must** prepare for `expired` responses ("Code should prepare for scenarios where the resource indicates that the token is expired"). For our `render-env-from-kv.sh` running at boot, this isn't a concern — the script runs once and exits.

**Sources:**

1. [MS Learn — Use managed identities on a virtual machine to acquire access token](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-to-use-vm-token) — accessed 2026-05-09 (last updated 2025-11-11)
2. [MS Learn — How managed identities for Azure resources work with Azure virtual machines](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-managed-identities-work-vm) — accessed 2026-05-09 (last updated 2026-02-19)
3. [Azure SDK for Go — Issue #22265 — Improve IMDS Probing Logic](https://github.com/Azure/azure-sdk-for-go/issues/22265) — accessed 2026-05-09 (community reports of variable propagation timing)

**Design impact:**

`scripts/render-env-from-kv.sh` (the systemd-unit boot script) **must** implement an exponential-backoff retry loop. Pseudocode:

```bash
# Retry IMDS up to 10 times: delays 0, 2, 6, 14, 30, 30, 30, 30, 30, 30 (max ~3 min)
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  delay=$((attempt == 1 ? 0 : (attempt <= 5 ? 2 ** (attempt - 1) : 30)))
  sleep $delay
  resp=$(curl -sf -H "Metadata: true" \
    "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://vault.azure.net")
  if [ -n "$resp" ]; then
    token=$(echo "$resp" | jq -r .access_token)
    break
  fi
done
[ -z "$token" ] && { echo "IMDS token unavailable after 10 attempts"; exit 1; }
```

Then a second retry loop wraps each `getSecret` call to absorb RBAC propagation. **The systemd unit must specify `Restart=on-failure` with `RestartSec=30s` and `StartLimitBurst=5`** — if the script genuinely fails (config wrong), we want it to give up rather than thrash forever.

The first `compose up` after VM provisioning **must NOT** fire until `render-env-from-kv.sh` exits 0 — wire as a `Wants=` / `After=` dependency in the docker compose systemd unit.

**Test implication:**

Phase 5 acceptance ("15-min manual smoke" from the slicing verdict):

1. SSH to VM. `cat /run/msai.env` should have the expected keys (POLYGON_API_KEY, REPORT_SIGNING_SECRET, etc.) — file mode `600`, owner `root`.
2. `journalctl -u msai-render-env.service` should show one successful run, no retries needed (after settling).
3. `journalctl -u msai-render-env.service --boot=1` should be inspected after a reboot — confirm the retry loop survives a "fast reboot" before MI is fully re-propagated. If on first boot post-provision the script needed 2–3 retries, that's fine; if it needed > 5, file a follow-up.

**Open risks:**

- Empirical 30–90s propagation latency reports are mostly community-sourced. If Slice 1 hits a 5-minute outlier on first deploy, the systemd unit's `RestartSec=30s` + `StartLimitBurst=5` would give up at 2.5 minutes. Tune these in pre-flight; if the smoke test shows propagation > 90s, the **architecture verdict's `.env` fallback** path (Simplifier dissent in the slicing verdict) becomes a real conversation.
- KV firewall: Slice 1 deliberately leaves Key Vault firewall **disabled** (`networkAcls.defaultAction = 'Allow'`) for Phase 1 simplicity. If Slice 4 enables KV firewall, the VM's outbound IP must be allowlisted OR we use a Private Endpoint. Add to backlog.

---

### 6. (Bonus) Premium SSD vs Standard SSD for DATA_ROOT

**Question (verbatim):** For a Phase 1 paper-trading workload writing parquet files at maybe 1 GB/day, is Premium SSD the right tier, or is Standard SSD sufficient?

**Findings:**

1. **Workload reality check:** 1 GB/day = ~12 KB/s steady-state write rate. The DuckDB queries that read from this disk for the dashboard are the I/O-bound side, not the writer. DuckDB scans of even multi-GB Parquet are CPU-bound on modern x86 cores once the file is in OS pagecache.
2. **Standard SSD pricing & perf (eastus2, 2026-05):** `E10` (128 GB) ≈ $9.60/month, 500 IOPS baseline, 60 MB/s baseline. Standard SSD also charges **per-transaction** which can dominate on high-IOPS workloads — but at 12 KB/s we're nowhere near that ceiling.
3. **Premium SSD pricing & perf (eastus2, 2026-05):** `P10` (128 GB) ≈ $19.71/month, 500 IOPS baseline, 100 MB/s baseline, **with the option to enable bursting up to 3500 IOPS / 170 MB/s for ~30 minutes/day at no extra cost**. Source: [Azure Pricing — Managed Disks](https://azure.microsoft.com/en-us/pricing/details/managed-disks/) (accessed 2026-05-09).
4. **The Premium-vs-Standard delta is ~$10/month at the 128 GB tier.** That's ~$120/year — well below the noise floor for a paper-trading platform whose IB Gateway licensing alone is $10/month/account.
5. **Why Premium still wins:** (a) **Sub-millisecond latency vs Standard SSD's "low single-digit ms"** — DuckDB random-access reads on `_min`/`_max` page metadata for Parquet predicate pushdown are latency-sensitive even when bandwidth-trivial; (b) the **burst capability** absorbs backtest-time spikes when many years of bars are loaded at once; (c) **uptime SLA** — Premium SSD is 99.9% with single-instance guarantee, Standard SSD is 99.5%. Source: [MS Learn — Disk types](https://learn.microsoft.com/en-us/azure/virtual-machines/disks-types).
6. **Premium SSD v2** is even cheaper-per-IOPS for high-IOPS workloads (free 3000 IOPS baseline, pay per provisioned IOPS above), but the v2 **does not support availability zones in all regions** and is **not bootable**. For a single-VM Phase 1 deploy where the data disk is just a data disk, v1 is simpler and the cost difference is negligible.

**Sources:**

1. [Azure Pricing — Managed Disks](https://azure.microsoft.com/en-us/pricing/details/managed-disks/) — accessed 2026-05-09
2. [MS Learn — Select a disk type for Azure IaaS VMs](https://learn.microsoft.com/en-us/azure/virtual-machines/disks-types) — accessed 2026-05-09
3. [Lucidity — Azure Disk pricing guide for 2026](https://www.lucidity.cloud/blog/azure-disk-pricing) — accessed 2026-05-09
4. [MS Learn — Understand Azure Disk Storage billing](https://learn.microsoft.com/en-us/azure/virtual-machines/disks-understand-billing) — accessed 2026-05-09

**Design impact:**

The architecture verdict's choice of **Premium SSD (P10, 128 GB)** is **confirmed**. Standard SSD is theoretically sufficient for the writer, but the ~$10/month delta buys: latency, burst capacity for backtest replay, and a real SLA. **No design change.**

If 128 GB ever becomes tight (Phase 2 / real-money data growth), upgrade to P15 (256 GB, ~$35/month) without re-architecting — managed disks resize live with a brief detach/attach.

**Test implication:**

Phase 5 acceptance: confirm the Bicep declares `storageAccountType: 'Premium_LRS'` for the data disk and `diskSizeGB: 128`. After deploy, run `lsblk -o NAME,SIZE,TYPE,MOUNTPOINT` on the VM — `/dev/sdc` (or equivalent) should show 128 GB mounted at `/app/data`. No throughput benchmark needed for Phase 1.

**Open risks:**

- Premium SSD v2 may become the default recommendation in 2026 documentation if Microsoft promotes it to GA-everywhere. Re-verify at Phase 2.

---

### 7. (Bonus) Storage account deterministic naming

**Question (verbatim):** What's the current best practice for globally-unique storage account names? `uniqueString(subscription().subscriptionId, resourceGroup().id)` is documented; is that still preferred in 2026?

**Findings:**

1. **The 2025-12-10-revised Bicep best-practices doc explicitly recommends** `uniqueString(resourceGroup().id)` as the seed. Quoted directly: "In most situations, the fully qualified resource group ID is a good option for the seed value for the uniqueString function." Source: [MS Learn — Bicep best practices §Names](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/best-practices) (accessed 2026-05-09).
2. **Storage account names are 3–24 chars, lowercase alphanumeric only.** `uniqueString` returns 13 chars. Prefix must be ≤ 11 chars. Pattern: `param storageAccountName string = '${shortPrefix}${uniqueString(resourceGroup().id)}'` with `shortPrefix = 'msai'` gives `msai${13-chars}` = 17 chars total. ✓
3. **`subscription().subscriptionId` as additional seed input** is recommended **when the same RG name might exist in multiple subscriptions** (rare). For our single-subscription deploy, `resourceGroup().id` alone is canonical.
4. **Anti-pattern:** generating names from `utcNow()` or random functions — these break idempotency. Don't use `newGuid()` outside parameter defaults. The `uniqueString(rg.id)` is **stable across redeploys to the same RG**, which is exactly what idempotent IaC needs. Source: [MS Learn — Name generation pattern](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/patterns-name-generation).
5. **`uniqueString` may start with a digit.** Storage accounts allow names starting with letter or digit, so it's fine for storage. **ACR names** (also constrained) and **Key Vault names** (3–24 chars, must start with a letter, alphanumeric + hyphens): use `${prefix}${uniqueString(...)}` where prefix is a letter to be safe.

**Sources:**

1. [MS Learn — Bicep best practices](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/best-practices) — accessed 2026-05-09
2. [MS Learn — Name generation pattern](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/patterns-name-generation) — accessed 2026-05-09
3. [MS Learn — Resolve errors for storage account names](https://learn.microsoft.com/en-us/azure/azure-resource-manager/troubleshooting/error-storage-account-name) — accessed 2026-05-09
4. [Ronald's Blog — Apply Azure naming convention using Bicep functions](https://ronaldbosma.github.io/blog/2024/06/05/apply-azure-naming-convention-using-bicep-functions/) — accessed 2026-05-09

**Design impact:**

`infra/main.bicep` declares (recommended naming, all using `${prefix}${uniqueString(resourceGroup().id)}`):

```bicep
var storageAccountName = 'msaibk${uniqueString(resourceGroup().id)}'  // backups
var acrName            = 'msaiacr${uniqueString(resourceGroup().id)}'
var keyVaultName       = 'msai-kv-${uniqueString(resourceGroup().id)}'  // KV allows hyphens
var logWorkspaceName   = 'msai-law-${uniqueString(resourceGroup().id)}'
```

VM, NSG, disk, public IP all use **deterministic non-uniquified names** (`msai-vm`, `msai-nsg`, `msai-data-disk`, `msai-pip`) since they're scoped to the RG, not globally unique.

**Test implication:**

Phase 5 acceptance: re-running `az deployment group create -f infra/main.bicep` after a successful first deploy must produce **zero diff** in `what-if`. If any resource shows as "to be created" on the second run, we have a non-deterministic name somewhere — fail the test.

**Open risks:**

- `uniqueString(resourceGroup().id)` produces a different name in `msaiv2_rg` vs (say) `msaiv2_rg_dev`. If we ever spin up a dev RG to dry-run a Bicep change, the storage/ACR/KV will be different from prod. This is **desired** — but document so future contributors don't try to "fix" the names by removing `uniqueString`.

---

## Not Researched (with justification)

- **Bicep what-if behavior on existing-but-empty `msaiv2_rg`** — slicing-verdict missing-evidence #1 — this is an empirical Phase 5 spike, not a docs question. Will be answered by running the command.
- **`docker context` over public internet vs SSH** — slicing-verdict missing-evidence #5 — Slice 1 is provisioning, no deploy yet; this question belongs to Slice 3 design.
- **`alembic upgrade head` advisory-lock semantics under Postgres 16** — architecture-verdict missing-evidence #6 — Slice 1 ships no Postgres or Alembic; this question belongs to Slice 3.
- **`live-supervisor` behavior on `ib-gateway` recreation** — architecture-verdict missing-evidence #5 — operational, no infra impact in Slice 1.
- **DuckDB-over-Azure-Files perf** — architecture-verdict missing-evidence #3 — verdict already chose Premium SSD; this question is moot for Slice 1.
- **Codex CLI long-prompt stall** — slicing-verdict missing-evidence #6 — operational, not architectural.

---

## Open Risks (consolidated)

1. **Flexible federated identity credentials are PREVIEW only.** Wildcard subjects (`ref:refs/heads/*`) are not GA on Azure. Each branch needs an exact-match credential; document so Slice 2 / future release-branch automation knows.
2. **MI propagation latency outliers can exceed 90s.** Architecture verdict's `.env` fallback (Simplifier dissent) becomes live again if Slice 1 smoke shows propagation > 5 min — re-trigger condition in the slicing verdict.
3. **AMA DCR-association API is still labeled `preview`.** No breaking changes in years, but pin the apiVersion explicitly and revisit at Slice 4.
4. **MMA backend dies 2026-03-02.** This is two months from now (relative to research date). Any inherited tooling that still references MMA will permanently break. We are net-new on AMA, so we're safe — but Pablo should not borrow legacy templates from the `msaimls2_rg` ML workspace area (already explicitly prohibited by the architecture verdict).
5. **KV firewall is disabled in Slice 1 (deliberate).** Slice 4 should add KV firewall + VM-IP allowlist OR Private Endpoint. Track as backlog.
6. **AMA on Linux is incompatible with crypto-policy = `FUTURE` mode.** Ubuntu 24 LTS does not enable this by default; pre-flight check `update-crypto-policies --show` should return `DEFAULT`.
7. **Premium SSD v2** may become the default recommendation in 2026; we ship v1 (P10) for Slice 1 simplicity. Re-verify at Phase 2.
