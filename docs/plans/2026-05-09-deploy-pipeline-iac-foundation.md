# Deployment Pipeline Slice 1 — IaC Foundation

**Goal:** Provision the foundational Azure infrastructure (Bicep declares VM + NSG + Premium SSD + ACR + KV + Log Analytics + AMA + Blob backup container + GH OIDC federated credential + role assignments) so subsequent slices can build CI/CD and operations on top. NO application deploys. After this slice merges, an operator can run `./scripts/deploy-azure.sh` and end up with a reproducible, idempotent platform; the systemd unit that materializes `/run/msai.env` from Key Vault is shipped but not yet enabled (Slice 3 enables it).

**Architecture:** Single `infra/main.bicep` file (~600 lines), section-ordered (params → vars → identity → networking → storage/data → registry/secrets → vm+extensions → role assignments → outputs), using symbolic-name implicit dependencies — no `dependsOn:` arrays, no AVM modules (overkill at this scale per research brief topic 1). The VM uses system-assigned managed identity for runtime KV/Blob access; a separate user-assigned managed identity (`msai-gh-oidc`) holds the federated credential for GH Actions push-to-main (declared in Slice 1, AcrPush role assignment lives in Slice 2). AMA is installed as a VM extension and connected to a Log Analytics workspace via an explicit DCR + DCR-association (research brief topic 3-4: AMA does NOT auto-create the DCR). The boot-time `render-env-from-kv.sh` script implements a 10-attempt exponential-backoff retry loop (research brief topic 5: doubled from the standard 5-attempt IMDS guidance to absorb post-deployment MI propagation outliers).

**Tech Stack:** Azure Bicep (single-file shape), Azure CLI (`az deployment group create` + `az deployment group what-if`), Bash 5+ with `curl` + `jq` for the boot-time secret renderer, systemd units (Type=oneshot, Restart=on-failure), GitHub OIDC (exact-match subject claim per research brief topic 2: `repo:marketsignal/msai-v2:ref:refs/heads/main`, `aud=api://AzureADTokenExchange`).

---

## Approach Comparison

The strategic approach was settled by two pre-ratified council verdicts; this section is a **citation, not a fresh comparison**.

### Chosen Default

**Approach A** from the slicing verdict — 4-PR incremental series, with Hawk's re-ordering applied (observability + backup target ship in Slice 1, not deferred). Slice 1 = Bicep IaC + ACR + KV + Blob backup target + Log Analytics + AMA + GH OIDC federated credential + VM managed identity.

### Best Credible Alternative

**Approach B** (one big PR with everything end-to-end) — one Codex advisor (Contrarian) preferred this on the grounds that the risk lives in the interaction between Bicep + OIDC + ACR + KV + SSH + DR, not in any single layer. Folded as Blocking Objection #4: Slice 3 cannot merge until the full deploy path is rehearsed end-to-end.

### Scoring (fixed axes — taken from the slicing verdict's analysis)

| Axis                  | Default (A: 4-slice incremental)             | Alternative (B: one big PR)                                                    |
| --------------------- | -------------------------------------------- | ------------------------------------------------------------------------------ |
| Complexity            | M (4 PRs to coordinate, but each focused)    | H (single ~500-1000 line PR with 6 subsystems)                                 |
| Blast Radius          | L (each PR's scope is bounded)               | H (one bad merge breaks provisioning, OIDC, deploy, and observability at once) |
| Reversibility         | H (revert any one PR, prior PRs still stand) | M (revert undoes everything; partial rollback infeasible)                      |
| Time to Validate      | L (each PR has a narrow acceptance test)     | M (acceptance requires the full chain to work end-to-end)                      |
| User/Correctness Risk | L (review surface is small per PR)           | H (review surface is huge; reviewer fatigue → missed issues)                   |

### Cheapest Falsifying Test

Already executed (in the slicing council). The test was: "Can a senior reviewer carefully read a 600+ line single-PR diff covering 6 different Azure subsystems and catch issues that span boundaries?" Council verdict: 3-of-5 said no — incremental wins. Re-trigger condition for Approach B: if Slice 3 ships without a full end-to-end rehearsal, the Contrarian's position becomes active again (Blocking Objection #4 in the slicing verdict).

## Contrarian Verdict

**Result:** PRE-DONE / VALIDATE.

The slicing council ran 5 advisors (Simplifier, Hawk, Pragmatist as Claude; Contrarian, Maintainer as Codex). Final verdict at `docs/decisions/deployment-pipeline-slicing.md` ratified Approach A with 3-of-5 APPROVE/CONDITIONAL plus 7 blocking objections folded into the slice scope. The Contrarian's preference for B was preserved as a minority report and folded into Slice 3's gate (no merge until end-to-end rehearsal). Slice 1 is the canonical entry point.

**Re-running 3.1c** (`/council` Contrarian gate) here would either return VALIDATE (consistent with the prior verdict) or surface stale advisors who don't have the architecture+slicing context loaded. Per `feedback_skip_phase3_brainstorm_when_council_predone.md`, Phase 3.1/3.1b/3.1c are PRE-DONE. Phase 3.2 (this plan) and 3.3 (plan-review loop) still run fresh — the council is architecture-level; the plan needs file-level detail.

---

## Files

### New files

- `infra/main.bicep` — single-file template declaring all Slice 1 resources (~600 lines target).
- `infra/main.bicepparam` — production parameter file. For Slice 1 most params have defaults in `infra/main.bicep` (location, repoOwner, repoName, repoBranch, subscriptionId via `subscription()`, tenantId via `subscription()`), so the bicepparam can be near-empty. Per-operator inputs (`operatorIp`, `operatorPrincipalId`, `vmSshPublicKey`) are NOT in bicepparam — they pass at deploy time via `--parameters` flags in T10's `deploy_bicep_create()` / `deploy_bicep_whatif()`.
- `infra/cloud-init.yaml` — separated YAML for cleanliness; loaded via `loadTextContent('cloud-init.yaml')` in main.bicep's VM `customData` field. Contains the disk format/mount + `/etc/docker/daemon.json` data-root relocation + write_files for the boot script.
- `infra/README.md` — short doc (≤ 100 lines) explaining file structure, deploy command, and the Slice 1 → 2 → 3 → 4 progression.
- `scripts/render-env-from-kv.sh` — Bash boot-time script (Type=oneshot via systemd) that fetches secrets from KV via VM system-assigned MI and writes `/run/msai.env`. Pure Bash + curl + jq, no Python (research brief preference for ~30-line shell).
- `scripts/msai-render-env.service` — systemd unit file. Slice 1 ships the file but does NOT enable the unit (Slice 3 enables it as part of the first deploy).
- `tests/infra/test_bicep.sh` — shellcheck'd test harness running `az bicep build` (lint clean, no warnings) + `az deployment group what-if` (parses output for unexpected Delete operations only). Returns non-zero on any lint warning or unexpected delete.
- `docs/decisions/deployment-pipeline-slicing.md` — slicing council verdict (created during the prior session via `/council`, never committed; this PR commits it because the plan references it as load-bearing context per plan-review iter 2 P2 #12).

### Modified files

- `scripts/deploy-azure.sh` — **REWRITE.** The existing script (`scripts/deploy-azure.sh:1-34` as of branch-off) is a hardcoded `az vm create` flow targeting `msai-rg`/`eastus`/`D4s_v5` — three wrong values for Slice 1 (D4s_v5 quota is 0/0 in MarketSignal2; Slice 1 targets `msaiv2_rg`/`eastus2`/`D4s_v6`). Default behavior of the new script is `deploy_bicep` against `msaiv2_rg`. Legacy `az vm create` content is removed (preserved in git history). Plan-review iteration 1 fix to P1 finding #1 (was "extension" — wrong; "rewrite" is correct).
- `docs/runbooks/vm-setup.md` — TWO changes: (a) update §1 + §2 + §3 references from `msai-rg`/`eastus` → `msaiv2_rg`/`eastus2` (existing post-PR-#50 transition warning at line 5 already flagged §4-§5 as legacy; §1-§3 also need updates per plan-review iteration 1 P1 finding #2); (b) APPEND a new §X "Slice 1 acceptance smoke" section (7-step `az`-based smoke verifying provisioning, AMA heartbeat, KV access from VM via raw IMDS+REST curl — not `az login --identity`, since Ubuntu 24 LTS doesn't preinstall azure-cli — ACR existence, federated credential registered, what-if no-Create/Delete).

### Out-of-tree (not modified in this PR)

- No backend Python code changes.
- No `docker-compose.prod.yml` changes (PR #50 already shipped the deployable shape).
- No GH Actions workflow YAML (Slice 2).
- No application code changes of any kind.

---

## Tasks

> **Dispatch model:** This is a tightly-coupled set of changes (T1–T8 share `infra/main.bicep`) — **sequential mode**, one subagent at a time. T9–T11 could run in parallel after T1–T8 complete, but the file footprint is small enough that sequential execution stays simple and avoids cross-task interference.

### T1: Bicep skeleton — params, vars, naming pattern

**Writes:** `infra/main.bicep` (initial commit ~140 lines: `targetScope`, `param` block, `var` block with naming pattern, no resources yet).

**Test (Red):** `tests/infra/test_bicep.sh` runs `az bicep build infra/main.bicep` and expects success. Initially no resources, so what-if is empty diff against an empty RG.

**Implementation (Green):** Declare params: `location string = 'eastus2'`, `vmAdminUsername string = 'msaiadmin'`, `vmSshPublicKey string @secure()` (no default — passed at deploy time, see T10), `operatorIp string`, `operatorPrincipalId string` (plan-review iter 2 P1 #6 fix — Pablo's Entra object ID for KV data-plane role grant), `repoOwner string = 'marketsignal'`, `repoName string = 'msai-v2'`, `repoBranch string = 'main'`, `subscriptionId string = subscription().subscriptionId`, `tenantId string = subscription().tenantId`. Declare vars per research brief topic 7: `var storageAccountName = 'msaibk${uniqueString(resourceGroup().id)}'`, `var acrName = 'msaiacr${uniqueString(resourceGroup().id)}'`, `var keyVaultName = 'msai-kv-${uniqueString(resourceGroup().id)}'`, `var logWorkspaceName = 'msai-law-${uniqueString(resourceGroup().id)}'`. Deterministic non-uniquified names: `msai-vm`, `msai-nsg`, `msai-data-disk`, `msai-pip`, `msai-vnet`, `msai-subnet`, `msai-gh-oidc`. Strict `@description()` on every param.

**Refactor:** Validate params have correct length-bound annotations (`@minLength`, `@maxLength`) for KV (3-24), ACR (5-50), storage (3-24). Mark `operatorPrincipalId` with `@description('Object ID of the operator Entra user/SP that needs Key Vault data-plane access. Get via: az ad signed-in-user show --query id -o tsv')`.

### T2: Network — VNet + Subnet + Public IP + NSG

**Writes:** `infra/main.bicep` (append ~80 lines: vnet, subnet, NSG with 4 inbound rules, public IP).

**Test (Red):** `az bicep build` lint clean. `az deployment group what-if` reports the 4 new resources to create.

**Implementation (Green):**

- `Microsoft.Network/virtualNetworks@2024-01-01` with single subnet `10.0.0.0/24`.
- `Microsoft.Network/publicIPAddresses@2024-01-01` Standard SKU, static allocation.
- `Microsoft.Network/networkSecurityGroups@2024-01-01` with rules:
  - 100: Allow TCP 22 from `${operatorIp}/32` (inbound, priority 100)
  - 110: Allow TCP 80 from `*` (inbound, priority 110)
  - 120: Allow TCP 443 from `*` (inbound, priority 120)
  - Default deny all other inbound (NSG default)
- Subnet `networkSecurityGroup: { id: nsg.id }` association.

### T3: Storage account + Blob backup container

**Writes:** `infra/main.bicep` (append ~50 lines).

**Test (Red):** what-if shows storage account + child Blob service + container.

**Implementation (Green):**

- `Microsoft.Storage/storageAccounts@2024-01-01` Standard_LRS, `kind: 'StorageV2'`, `accessTier: 'Hot'`, `allowBlobPublicAccess: false`, `minimumTlsVersion: 'TLS1_2'`, `supportsHttpsTrafficOnly: true`.
- `Microsoft.Storage/storageAccounts/blobServices/default` (implicitly the only `blobServices`).
- `Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01` named `msai-backups`, `publicAccess: 'None'`.

### T4: Azure Container Registry

**Writes:** `infra/main.bicep` (append ~20 lines).

**Test (Red):** what-if shows ACR.

**Implementation (Green):**

- `Microsoft.ContainerRegistry/registries@2025-04-01` Basic SKU, `adminUserEnabled: false` (we only use OIDC federated identity for push, never admin password). **Plan-review iter 2 P2 #7 fix:** GA `2025-04-01` (or `2025-11-01`) used instead of `2023-11-01-preview`. Verify at implementation time which is the latest GA in our region.

### T5: Key Vault + diagnostic settings → Log Analytics

**Writes:** `infra/main.bicep` (append ~70 lines: KV + Log Analytics workspace + KV diagnosticSettings extension resource. Log Analytics is created here as a precondition for diagnostics; AMA's DCR points at the same workspace in T7.).

**Test (Red):** what-if shows KV + Log Analytics workspace + diagnostic settings.

**Implementation (Green):**

- `Microsoft.OperationalInsights/workspaces@2023-09-01` SKU `PerGB2018`, retention 30 days.
- `Microsoft.KeyVault/vaults@2025-05-01` (plan-review iter 2 P2 #8 fix: GA, was `2024-04-01-preview`):
  - `enableRbacAuthorization: true` (RBAC, not access policies — modern best practice)
  - `enableSoftDelete: true`, `softDeleteRetentionInDays: 90`
  - `enablePurgeProtection: false` for Phase 1 (allows clean re-deploys after RG nuke; Phase 2 enables it)
  - `networkAcls: { defaultAction: 'Allow', bypass: 'AzureServices' }` — KV firewall disabled for Slice 1 (research brief topic 5 open risk: re-tighten in Slice 4 with VM-IP allowlist or Private Endpoint)
  - `tenantId: tenantId`
- `Microsoft.Insights/diagnosticSettings@2021-05-01-preview` — **EXTENSION resource, NOT a child** (plan-review iter 2 P1 #2 fix). Use `scope: keyVault`, NOT `parent: keyVault`. Bicep example:
  ```bicep
  resource kvDiagSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
    scope: keyVault                                   // EXTENSION pattern
    name: 'kvDiagnostics'                             // diagnostic-setting name (free-form)
    properties: {
      workspaceId: logWorkspace.id
      logs: [
        { category: 'AuditEvent', enabled: true }
        { category: 'AzurePolicyEvaluationDetails', enabled: true }
      ]
    }
  }
  ```

### T6: VM + system-assigned managed identity + data disk attachment + cloud-init format/mount

**Writes:** `infra/main.bicep` (append ~140 lines: NIC, OS disk implicit, data disk standalone resource, VM with two-disk config + cloud-init that formats and mounts the data disk + relocates Docker data root).

**Test (Red):** what-if shows VM + NIC + data disk.

**Implementation (Green):**

- `Microsoft.Network/networkInterfaces@2024-01-01` referencing subnet + public IP.
- `Microsoft.Compute/disks@2024-03-02` named `msai-data-disk`, `diskSizeGB: 128`, `sku: { name: 'Premium_LRS' }`, `creationData: { createOption: 'Empty' }` (research brief topic 6: P10 confirmed).
- `Microsoft.Compute/virtualMachines@2024-07-01`:
  - `hardwareProfile: { vmSize: 'Standard_D4s_v6' }` (Ddsv6 family per quota; DSv5 was 0/0)
  - `osProfile: { computerName: 'msaiv2-vm', adminUsername: vmAdminUsername, linuxConfiguration: { disablePasswordAuthentication: true, ssh: { publicKeys: [{ path: '/home/${vmAdminUsername}/.ssh/authorized_keys', keyData: vmSshPublicKey }] } }, customData: <cloud-init-base64> }`
  - `storageProfile.imageReference`: Ubuntu 24.04 LTS Gen2 (`Canonical / 0001-com-ubuntu-server-noble / 24_04-lts-gen2`)
  - `storageProfile.osDisk`: `Premium_LRS`, `createOption: 'FromImage'`, `diskSizeGB: 64`
  - `storageProfile.dataDisks`: attach `dataDisk.id` at LUN 0
  - `networkProfile.networkInterfaces`: NIC reference
  - `identity: { type: 'SystemAssigned' }` — research brief topic 4 prerequisite for AMA + KV access

**Cloud-init customData (plan-review iter 2 P1 #3 fix — data disk attached but never mounted; PLUS iter 3 P1 #14 fix — Docker not installed because deploy-azure.sh rewrite removed the `curl get.docker.com | sh` step). Cloud-init must install Docker engine + compose plugin before Slice 3 can run.** Format the data disk, mount at `/var/lib/msai`, install Docker via official apt repo with `data-root: /var/lib/msai/docker` so volumes and image layers land on Premium SSD:

```yaml
#cloud-config
package_update: true
package_upgrade: false # don't auto-upgrade — keep predictable image
packages:
  - jq
  - curl
  - ca-certificates
  - gnupg
  - lsb-release
disk_setup:
  /dev/disk/azure/scsi1/lun0:
    table_type: gpt
    layout: true
    overwrite: false
fs_setup:
  - device: /dev/disk/azure/scsi1/lun0
    partition: 1
    filesystem: ext4
    overwrite: false
mounts:
  - [
      /dev/disk/azure/scsi1/lun0-part1,
      /var/lib/msai,
      ext4,
      "defaults,nofail,discard",
      "0",
      "2",
    ]
runcmd:
  # Step 1: data-disk landing site for Docker (must exist before dockerd first start)
  - mkdir -p /var/lib/msai/docker /etc/docker
  - |
    cat > /etc/docker/daemon.json <<'EOF'
    {"data-root": "/var/lib/msai/docker"}
    EOF
  # Step 2: install Docker engine + compose plugin via official apt repo
  - install -m 0755 -d /etc/apt/keyrings
  - curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  - chmod a+r /etc/apt/keyrings/docker.gpg
  - |
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
  - apt-get update
  - DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  - systemctl enable --now docker
  # Plan-review iter 5 P2 #22 fix: don't hardcode 'msaiadmin' — Bicep replaces this
  # placeholder with the actual vmAdminUsername at template-build time. Otherwise
  # overriding the param breaks usermod silently (cloud-init logs only).
  - usermod -aG docker __SLICE1_BICEP_VM_ADMIN_USERNAME__
write_files:
  - path: /usr/local/bin/render-env-from-kv.sh
    permissions: "0755"
    owner: "root:root"
    encoding: b64
    content: __SLICE1_BICEP_BASE64_OF_RENDER_SCRIPT__
  - path: /etc/systemd/system/msai-render-env.service
    permissions: "0644"
    encoding: b64
    content: __SLICE1_BICEP_BASE64_OF_UNIT__
```

**Plan-review iter 4 P1 #17 fix:** Use cloud-init's `encoding: b64` with single-line base64 content instead of multi-line `content: |`. Reason: `loadTextContent()` inserts the file verbatim; if we used `content: |` the script's lines wouldn't be indented to match cloud-init's YAML structure (cloud-init parses `|` as a block scalar requiring consistent indent). Base64 is a single-line YAML value that sidesteps indentation entirely. Pre-indenting source files (the alternative) breaks the single-source-of-truth property.

Bicep idiom:

```bicep
var cloudInitText = loadTextContent('cloud-init.yaml')
var renderScriptText = loadTextContent('../scripts/render-env-from-kv.sh')
var renderUnitText   = loadTextContent('../scripts/msai-render-env.service')
// Plan-review iter 5 P2 #22 fix: thread vmAdminUsername param into cloud-init.
var cloudInit = replace(
  replace(
    replace(cloudInitText, '__SLICE1_BICEP_BASE64_OF_RENDER_SCRIPT__', base64(renderScriptText)),
    '__SLICE1_BICEP_BASE64_OF_UNIT__', base64(renderUnitText)
  ),
  '__SLICE1_BICEP_VM_ADMIN_USERNAME__', vmAdminUsername
)

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  // ...
  properties: {
    osProfile: {
      // ...
      customData: base64(cloudInit)
    }
  }
}
```

This keeps `scripts/render-env-from-kv.sh` and `scripts/msai-render-env.service` as the single source of truth (operator can `cat` them directly for ops/manual smoke runs) while letting cloud-init plant them on first boot via base64-decode. **The unit is NOT enabled by cloud-init** — Slice 3 enables it via `systemctl enable --now msai-render-env.service` as part of the first deploy.

### T7: Log Analytics DCR + AMA VM extension + DCR association

**Writes:** `infra/main.bicep` (append ~80 lines).

**Test (Red):** what-if shows DCR + DCR association + AMA extension. After deploy, `az monitor log-analytics query` for `Heartbeat` returns rows (Phase 5 acceptance, not Phase 3 plan-review).

**Implementation (Green):**

- `Microsoft.Insights/dataCollectionRules@2022-06-01` named `msai-heartbeat-dcr`:
  - `properties.dataFlows: [{ streams: ['Microsoft-Heartbeat'], destinations: ['msaiLogAnalytics'] }]`
  - `properties.destinations.logAnalytics: [{ workspaceResourceId: logWorkspace.id, name: 'msaiLogAnalytics' }]`
  - `properties.dataSources` empty (heartbeat has no source-side config)
- `Microsoft.Compute/virtualMachines/extensions@2024-07-01` (parent: vm) named `AzureMonitorLinuxAgent`:
  - `publisher: 'Microsoft.Azure.Monitor'`
  - `type: 'AzureMonitorLinuxAgent'`
  - `typeHandlerVersion: '1.21'`
  - `autoUpgradeMinorVersion: true`
  - `enableAutomaticUpgrade: true`
- `Microsoft.Insights/dataCollectionRuleAssociations@2024-03-11` (plan-review iter 2 P2 #9 fix: research brief said `2021-09-01-preview` was the only available version, but Codex confirmed GA `2024-03-11` exists at https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/datacollectionruleassociations):
  - `name: 'msai-heartbeat-dcr-association'`
  - `scope: vm` (extension resource on VM scope)
  - `properties.dataCollectionRuleId: dcr.id`

**Refactor:** Confirm symbolic-name dependencies guarantee VM → DCR → AMA extension → DCR association ordering without explicit `dependsOn:`. The `parent: vm` on the extension makes that implicit; the association uses both `vm` (scope) and `dcr.id` (property) as references → both implicit.

### T8: User-assigned MI for GH OIDC + federated credential + role assignments

**Writes:** `infra/main.bicep` (append ~100 lines).

**Test (Red):** what-if shows user-assigned MI + federated credential + **4 role assignments** (3 on VM system-assigned MI: KV Secrets User, AcrPull, Blob Data Contributor; PLUS 1 on operator: KV Secrets Officer — plan-review iter 3 P2 #16 update from "3 role assignments"). After deploy, `az identity federated-credential list --identity-name msai-gh-oidc -g msaiv2_rg` returns the credential.

**Implementation (Green):**

- `Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31` named `msai-gh-oidc`. Slice 1 declares this; Slice 2 will add AcrPush role assignment, Slice 3 may add an SSH-related assignment. **apiVersion `2023-01-31`** — pinned because the research brief topic 2 didn't pin a version; verified at https://learn.microsoft.com/en-us/azure/templates/microsoft.managedidentity/userassignedidentities as a current GA release. **Verify at implementation time** that no newer GA exists; if so, bump.
- `Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31` (parent: gh-oidc-mi):
  - `name: 'gh-actions-main'`
  - `properties.issuer: 'https://token.actions.githubusercontent.com'`
  - `properties.subject: 'repo:${repoOwner}/${repoName}:ref:refs/heads/${repoBranch}'` (research brief topic 2: exact match, no wildcards — flexible federated credentials are PREVIEW only)
  - `properties.audiences: ['api://AzureADTokenExchange']`
- Three role assignments on the VM's **system-assigned MI** (NOT the GH OIDC MI). All use `Microsoft.Authorization/roleAssignments@2022-04-01`, `principalId: vm.identity.principalId`, `principalType: 'ServicePrincipal'`. Names are deterministic GUIDs.
  - **Key Vault Secrets User** on KV scope: `roleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')`, scope = `keyVault` (symbolic), name = `guid(vm.id, keyVault.id, 'kv-secrets-user')`.
  - **AcrPull** on ACR scope: role definition GUID `7f951dda-4ed3-4680-a7ca-43fe172d538d`, scope = `acr` (symbolic), name = `guid(vm.id, acr.id, 'acr-pull')`.
  - **Storage Blob Data Contributor** on the **container** scope (plan-review iter 2 P1 #4 fix — symbolic resource, NOT string scope; preserves Bicep dependency semantics): role definition GUID `ba92f5b4-2d11-453d-a403-e96b0029c9fe`, scope = `backupsContainer` (symbolic — declared in T3), name = `guid(vm.id, backupsContainer.id, 'blob-contributor')`. Bicep example:
    ```bicep
    resource blobContribAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
      scope: backupsContainer                                    // symbolic, NOT string interpolation
      name: guid(vm.id, backupsContainer.id, 'blob-contributor')
      properties: {
        principalId: vm.identity.principalId
        roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
        principalType: 'ServicePrincipal'
      }
    }
    ```

- **Plan-review iter 2 P1 #6 fix — operator data-plane access:** With `enableRbacAuthorization: true` on the KV (T5), even the subscription Owner cannot `az keyvault secret set/show` without a data-plane RBAC role. Add a fourth role assignment in T8:
  - **Key Vault Secrets Officer** on KV scope (data-plane read+write+delete): `roleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')`, scope = `keyVault`, `principalId: operatorPrincipalId` (the param from T1), `principalType: 'User'` (or 'ServicePrincipal' if operator is a deploy SP), name = `guid(operatorPrincipalId, keyVault.id, 'kv-secrets-officer')`. This lets Pablo (or the deploy SP) seed secrets via `az keyvault secret set` for the smoke test in T11 step 4 + later operational secret rotation.

### T9: render-env-from-kv.sh + systemd unit

**Writes:** `scripts/render-env-from-kv.sh`, `scripts/msai-render-env.service`.

**Test (Red):** Manual smoke test — run script on a real VM provisioned by T1-T8, verify it produces `/run/msai.env` with the expected keys (Phase 5 acceptance). Bashate / shellcheck the script.

**Implementation (Green):**

`scripts/render-env-from-kv.sh` (~50 lines target — ~30 logic + comments):

```bash
#!/usr/bin/env bash
# Boot-time secret renderer. Called by msai-render-env.service.
# Fetches secrets from Azure Key Vault via VM system-assigned managed identity,
# writes /run/msai.env (chmod 600, owner root). Exits 0 on success, 1 on retryable
# failure (systemd will Restart=on-failure), exits 1 with non-recoverable status
# on permanent config errors (StartLimitBurst eventually gives up).
#
# Required env (set in service unit ExecStart): KV_NAME, REQUIRED_SECRETS (comma-sep).
# Optional env: OPTIONAL_SECRETS (comma-sep) — logs warning + skips on missing
# (plan-review iter 4 P1 #18: defaults / broker-profile-only secrets must not block boot).
set -euo pipefail
: "${KV_NAME:?KV_NAME must be set}"
: "${REQUIRED_SECRETS:?REQUIRED_SECRETS must be set}"
OPTIONAL_SECRETS="${OPTIONAL_SECRETS:-}"

OUTPUT_FILE="/run/msai.env"
TMP_FILE="/run/msai.env.tmp"
IMDS_URL="http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://vault.azure.net"

# Retry IMDS up to 10 times with exponential backoff (research brief topic 5).
get_token() {
    local attempt resp delay token
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        if [[ "$attempt" -eq 1 ]]; then delay=0
        elif [[ "$attempt" -le 5 ]]; then delay=$((2 ** (attempt - 1)))
        else delay=30; fi
        sleep "$delay"
        if resp=$(curl -sf --max-time 10 -H "Metadata: true" "$IMDS_URL" 2>/dev/null); then
            token=$(echo "$resp" | jq -r .access_token 2>/dev/null)
            # Plan-review P1 #4: validate token is non-empty and not literal "null"
            # (malformed IMDS body could pass curl -sf but fail jq parse).
            if [[ -n "$token" && "$token" != "null" ]]; then
                echo "$token"
                return 0
            fi
        fi
    done
    echo "IMDS token unavailable after 10 attempts" >&2
    return 1
}

# Plan-review iter 3 P1 #13: Azure Key Vault secret names allow only alphanumerics and `-`
# (https://learn.microsoft.com/en-us/azure/key-vault/general/about-keys-secrets-certificates).
# We accept env-var-style names (with underscores) in REQUIRED_SECRETS, normalize to hyphens
# for the KV lookup, and write the original underscore name back to /run/msai.env.
# Mirrors the convention already used by backend/src/msai/core/secrets.py:112.
to_kv_name() { tr '[:upper:]_' '[:lower:]-' <<< "$1"; }

# Fetch one secret. Distinguish retryable (403 = RBAC propagating) from permanent (404 = wrong name).
# Plan-review iter 6 P1 #24 fix: previous version wrote curl response to /tmp/.kv_resp.json
# (world-readable, leak window during the rm). Replaced with in-memory capture via
# `-w '\n%{http_code}'` separator + bash parameter expansion to split body from HTTP code.
# No filesystem touch, no leak window.
get_secret() {
    local token="$1" env_name="$2" kv_name attempt resp http_code body
    kv_name=$(to_kv_name "$env_name")
    for attempt in 1 2 3 4 5; do
        sleep "$((attempt == 1 ? 0 : 10 * (attempt - 1)))"  # 0, 10, 20, 30, 40s = ~100s budget for RBAC
        # Capture body + HTTP status in a single curl call. Body lines never contain a
        # standalone numeric line of length 3 in the responses Azure KV returns (JSON only),
        # so splitting on the trailing "\n<3 digits>$" is unambiguous.
        resp=$(curl -s -w '\n%{http_code}' --max-time 10 \
            -H "Authorization: Bearer $token" \
            "https://${KV_NAME}.vault.azure.net/secrets/${kv_name}?api-version=7.4" || true)
        if [[ -z "$resp" ]]; then
            http_code="000"
            body=""
        else
            http_code="${resp##*$'\n'}"
            body="${resp%$'\n'*}"
        fi
        case "$http_code" in
            200) jq -r .value <<< "$body"; return 0 ;;
            403) continue ;;  # RBAC propagating, retry
            404) echo "Secret '$kv_name' (env: $env_name) not found in KV $KV_NAME (404)" >&2; return 2 ;;
            401) echo "KV auth failed (401) — fix MI or KV config" >&2; return 2 ;;
            *) echo "KV unexpected status $http_code for '$kv_name'" >&2 ;;
        esac
    done
    return 1
}

main() {
    local token
    token=$(get_token) || exit 1

    # Build env file atomically.
    : > "$TMP_FILE"
    chmod 600 "$TMP_FILE"
    local secret value
    # Plan-review iter 6 P1 #23 fix: declare required + optional as empty arrays before
    # populating, so the closing log line's ${#optional[@]} works under set -u even when
    # OPTIONAL_SECRETS is unset/empty.
    local -a required=() optional=()
    # Plan-review P1 #3: secret-leak protection comes from (a) /run/msai.env mode 0600,
    # (b) curl -s suppressing curl's own progress output, (c) NEVER echoing $value to
    # stdout/stderr, (d) responses captured in shell vars only — no temp files.

    # REQUIRED secrets — fail hard if any missing.
    IFS=',' read -ra required <<< "$REQUIRED_SECRETS"
    for secret in "${required[@]}"; do
        if value=$(get_secret "$token" "$secret"); then
            # Plan-review iter 6 P2 #25 fix: shell-quote the value with single quotes so
            # the resulting line is parseable by both `docker-compose --env-file` (POSIX
            # KEY=VALUE with optional 'single quotes' that strip exactly one outer pair)
            # AND systemd EnvironmentFile (same convention). Single-quote any embedded
            # single quotes via the standard '"'"' construct.
            printf "%s='%s'\n" "$secret" "${value//\'/\'\"\'\"\'}" >> "$TMP_FILE"
        else
            echo "Required secret '$secret' missing or unreachable; aborting." >&2
            rm -f "$TMP_FILE"
            exit 1
        fi
    done

    # OPTIONAL secrets — log warning + skip if missing (plan-review iter 4 P1 #18 fix).
    if [[ -n "${OPTIONAL_SECRETS:-}" ]]; then
        IFS=',' read -ra optional <<< "$OPTIONAL_SECRETS"
        for secret in "${optional[@]}"; do
            if value=$(get_secret "$token" "$secret" 2>/dev/null); then
                printf "%s='%s'\n" "$secret" "${value//\'/\'\"\'\"\'}" >> "$TMP_FILE"
            else
                echo "Optional secret '$secret' not present in KV; skipping." >&2
            fi
        done
    fi
    # Atomic move.
    mv "$TMP_FILE" "$OUTPUT_FILE"
    # Plan-review iter 5 P1 #21 fix: previous version logged ${#secrets[@]} but $secrets
    # was undeclared after splitting into $required + $optional arrays — under set -u
    # this exits nonzero AFTER successful render, triggering systemd Restart and a confusing
    # "looks succeeded but service Failed" diagnostic. Count both arrays.
    echo "Rendered $OUTPUT_FILE with ${#required[@]} required + ${#optional[@]:-0} optional secrets"
}
main "$@"
```

`scripts/msai-render-env.service` (~28 lines):

```systemd
[Unit]
Description=Render /run/msai.env from Azure Key Vault via system-assigned managed identity
# Block docker-compose-msai.service until env file is rendered.
Before=docker-compose-msai.service
After=network-online.target
Wants=network-online.target
# Plan-review iter 2 P2 #10 fix: StartLimit settings belong in [Unit], not [Service].
# Reference: man systemd.unit(5).
StartLimitIntervalSec=900
StartLimitBurst=5

[Service]
Type=oneshot
RemainAfterExit=yes
# These env vars are populated at deploy time (Slice 3) — Slice 1 ships placeholders.
Environment="KV_NAME=__SLICE3_FILLS_THIS__"
# Plan-review iter 4 P1 #18 fix: split REQUIRED (compose :?-guards or hard validators)
# from OPTIONAL (compose-defaulted or broker-profile-only). Renderer fails on missing
# REQUIRED secret; logs warning + skips on missing OPTIONAL. Cross-checked against
# docker-compose.prod.yml (PR #50): :?-guarded vars are MSAI_REGISTRY/MSAI_BACKEND_IMAGE/
# MSAI_FRONTEND_IMAGE/MSAI_GIT_SHA (image vars — not in KV, see image-vars note below) +
# REPORT_SIGNING_SECRET, AZURE_TENANT_ID, AZURE_CLIENT_ID, CORS_ORIGINS, POSTGRES_PASSWORD.
# JWT_* default to AZURE_*. TWS_USERID/PASSWORD are broker-profile only.
# IB_ACCOUNT_ID, POLYGON_API_KEY, DATABENTO_API_KEY, MSAI_API_KEY are optional.
#
# Image-version vars (all four — MSAI_REGISTRY, MSAI_BACKEND_IMAGE, MSAI_FRONTEND_IMAGE,
# MSAI_GIT_SHA — are NOT in either list. Plan-review iter 4 P2 #19 fix: previously omitted
# MSAI_FRONTEND_IMAGE; verified all four are :?-guarded in docker-compose.prod.yml). They
# come from Slice 3's deploy step (set at unit-invocation time in /run/msai-images.env or
# via direct Environment= in the docker-compose-msai.service unit).
Environment="REQUIRED_SECRETS=REPORT_SIGNING_SECRET,POSTGRES_PASSWORD,AZURE_TENANT_ID,AZURE_CLIENT_ID,CORS_ORIGINS,IB_ACCOUNT_ID,TWS_USERID,TWS_PASSWORD"
Environment="OPTIONAL_SECRETS=JWT_TENANT_ID,JWT_CLIENT_ID,POLYGON_API_KEY,DATABENTO_API_KEY,MSAI_API_KEY"
# Plan-review iter 5 P1 #20 fix: IB_ACCOUNT_ID is :?-guarded at docker-compose.prod.yml:143
# (always parsed, regardless of --profile). TWS_USERID/PASSWORD :?-guarded at lines 318-319
# and 383-384 — compose evaluates :? at config-parse time BEFORE profile filtering, so even
# without --profile broker, missing TWS_* would break `docker compose config`. All three are
# REQUIRED. Operator must seed KV with all required values before first deploy, even if
# broker profile is not active in the initial deploy.
ExecStart=/usr/local/bin/render-env-from-kv.sh
User=root
Group=root
Restart=on-failure
RestartSec=30

[Install]
# WantedBy is set but the unit is NOT enabled in Slice 1.
# Slice 3's deploy step runs `systemctl enable --now msai-render-env.service`.
WantedBy=multi-user.target
```

**Refactor:** Verify the script passes `shellcheck -e SC2034` clean. Verify `set +x` doesn't actually need to be present (we never `set -x` anyway — but it's defense-in-depth comment for future maintainers).

### T10: Rewrite scripts/deploy-azure.sh for Bicep deploy

**Writes:** `scripts/deploy-azure.sh` (REWRITE — see plan-review P1 #1: existing 34-line script targets wrong RG/region/VM size and conflicts with Slice 1).

**Test (Red):** `bash -n scripts/deploy-azure.sh` parses clean; `shellcheck scripts/deploy-azure.sh` clean; `./scripts/deploy-azure.sh --help` prints usage.

**Implementation (Green):** Replace the entire file. Default invocation runs `deploy_bicep`. Skeleton:

```bash
#!/usr/bin/env bash
# MSAI v2 — Azure Deployment Script (Bicep-driven, Slice 1 onward)
# Usage:
#   ./scripts/deploy-azure.sh                  # deploys infra/main.bicep to msaiv2_rg
#   ./scripts/deploy-azure.sh --what-if        # dry-run
#   ./scripts/deploy-azure.sh --help
#
# Pre-flight: az login + az account set --subscription MarketSignal2
set -euo pipefail

EXPECTED_SUB_ID="68067b9b-943f-4461-8cb5-2bc97cbc462d"  # MarketSignal2
RG="msaiv2_rg"
LOCATION="eastus2"
TEMPLATE="infra/main.bicep"
PARAM_FILE="infra/main.bicepparam"

usage() {
    cat <<EOF
Usage: $0 [--what-if|--help]
  (no flag)    Deploy infra/main.bicep to $RG (eastus2)
  --what-if    Run az deployment group what-if (dry-run; no changes applied)
  --help       Show this message

Environment:
  OPERATOR_IP  IPv4 to allowlist for SSH on the NSG. Defaults to current
               public IP via 'curl -s ifconfig.me' if not set.
EOF
}

preflight() {
    local current_sub
    current_sub=$(az account show --query 'id' -o tsv 2>/dev/null || true)
    if [[ -z "$current_sub" ]]; then
        echo "az not authenticated. Run: az login" >&2; exit 1
    fi
    if [[ "$current_sub" != "$EXPECTED_SUB_ID" ]]; then
        echo "Wrong subscription. Run: az account set --subscription $EXPECTED_SUB_ID" >&2
        echo "(Currently on: $current_sub)" >&2
        exit 1
    fi
    if ! az group show -n "$RG" >/dev/null 2>&1; then
        echo "Resource group '$RG' missing. Creating in $LOCATION..."
        az group create -n "$RG" -l "$LOCATION" -o none
    fi
}

resolve_operator_ip() {
    local ip="${OPERATOR_IP:-}"
    if [[ -z "$ip" ]]; then
        ip=$(curl -s --max-time 10 ifconfig.me || true)
    fi
    if [[ -z "$ip" ]]; then
        echo "Could not determine operator IP. Pass OPERATOR_IP=<x.x.x.x>." >&2
        exit 1
    fi
    echo "$ip"
}

# Plan-review iter 2 P1 #1 fix (extended): the Bicep template requires vmSshPublicKey
# (no default; @secure()). Read it from the operator's ~/.ssh/*.pub.
resolve_ssh_public_key() {
    local key=""
    for candidate in "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_rsa.pub" "$HOME/.ssh/id_ecdsa.pub"; do
        if [[ -f "$candidate" ]]; then
            key=$(<"$candidate")
            break
        fi
    done
    if [[ -z "$key" ]]; then
        echo "No SSH public key found in ~/.ssh/{id_ed25519,id_rsa,id_ecdsa}.pub" >&2
        echo "Generate one: ssh-keygen -t ed25519 -C msaiv2" >&2
        exit 1
    fi
    echo "$key"
}

# Plan-review iter 2 P1 #6 prerequisite: read the operator's Entra object ID
# so the Bicep can grant Key Vault Secrets Officer for data-plane access.
resolve_operator_principal_id() {
    local pid
    pid=$(az ad signed-in-user show --query id -o tsv 2>/dev/null)
    if [[ -z "$pid" ]]; then
        echo "Could not resolve signed-in user object ID. Are you logged in as a user (not an SP)?" >&2
        exit 1
    fi
    echo "$pid"
}

# Plan-review iter 2 P2 #11 fix: split create vs what-if. `az deployment group what-if` does NOT
# accept --query/-o table (it uses its own output renderer). Two clean branches, no ${op:+...} crud.
deploy_bicep_create() {
    local operator_ip operator_pid ssh_pubkey
    operator_ip=$(resolve_operator_ip);    echo "Operator IP: $operator_ip"
    operator_pid=$(resolve_operator_principal_id); echo "Operator principal ID: $operator_pid"
    ssh_pubkey=$(resolve_ssh_public_key);  echo "SSH key: ${ssh_pubkey:0:30}..."
    echo "Deploying $TEMPLATE to $RG (create)..."
    az deployment group create \
        --resource-group "$RG" \
        --template-file "$TEMPLATE" \
        --parameters "$PARAM_FILE" \
        --parameters operatorIp="$operator_ip" \
                     operatorPrincipalId="$operator_pid" \
                     vmSshPublicKey="$ssh_pubkey" \
        --query "{status: properties.provisioningState, correlationId: properties.correlationId}" \
        -o table
}

deploy_bicep_whatif() {
    local operator_ip operator_pid ssh_pubkey
    operator_ip=$(resolve_operator_ip)
    operator_pid=$(resolve_operator_principal_id)
    ssh_pubkey=$(resolve_ssh_public_key)
    echo "What-if $TEMPLATE against $RG..."
    az deployment group what-if \
        --resource-group "$RG" \
        --template-file "$TEMPLATE" \
        --parameters "$PARAM_FILE" \
        --parameters operatorIp="$operator_ip" \
                     operatorPrincipalId="$operator_pid" \
                     vmSshPublicKey="$ssh_pubkey"
}

main() {
    case "${1:-}" in
        --help|-h) usage; exit 0 ;;
        --what-if) preflight; deploy_bicep_whatif ;;
        "")        preflight; deploy_bicep_create ;;
        *) echo "Unknown flag: $1" >&2; usage; exit 1 ;;
    esac
}

main "$@"
```

### T11: Runbook updates — fix stale RG/region refs + append Slice 1 acceptance smoke

**Writes:** `docs/runbooks/vm-setup.md` (modify — TWO classes of change per plan-review P1 #2):

**(a) Fix stale references in §1-§3** (existing post-PR-#50 transition warning at line 5 already flagged §4-§5 as legacy; §1-§3 still need updates):

- Line 11 (Prerequisites): `msai-rg` → `msaiv2_rg`
- Line 24 (§1 first bullet): `msai-rg` in `eastus` → `msaiv2_rg` in `eastus2`
- Line 25 (§1 second bullet): note that the VM size text already says D4s_v6 (PR #50 fix), but `Standard_D4s_v6` in the bullet should be cross-referenced with the new flag-driven invocation: `./scripts/deploy-azure.sh` (no flags = deploy infra)
- Line 32 (§2): SSH command replaces `-g msai-rg -n msai-vm` → `-g msaiv2_rg -n msai-vm`. Also add: "After Slice 1 deploys, `vm-public-ip` is also surfaced in Bicep deploy outputs: `az deployment group show -g msaiv2_rg -n main --query 'properties.outputs.vmPublicIp.value' -o tsv`."
- §3 (env file): Add a sentence: "Once Slice 3 enables `msai-render-env.service`, this `.env` step is replaced by /run/msai.env materialized from Key Vault at boot. Until then, the manual `.env` flow is the operator's path for testing the prod compose without the full pipeline."

**(b) Append a new section** titled `## Slice 1 acceptance smoke (15 min)` with seven copy-pasteable commands. **Plan-review P2 #6 fix:** step 4 uses raw IMDS + KV REST curl instead of `az login --identity` + `az keyvault secret show`, because Ubuntu 24 LTS does not preinstall `azure-cli`. **Plan-review P2 #5 fix:** what-if expectation softened from "NoChange" to "no Create/Delete operations":

```bash
# 1) Bicep what-if reports no Create/Delete on second run (idempotency)
#    (Modify operations are acceptable — Azure adds default values not in Bicep,
#     so spurious modify diffs on re-deploy are normal and don't violate idempotency.)
./scripts/deploy-azure.sh --what-if 2>&1 | grep -E '^(Create|Delete):' || echo "PASS: no Create/Delete on re-deploy"

# 2) AMA extension provisioned successfully
az vm extension show -g msaiv2_rg --vm-name msai-vm -n AzureMonitorLinuxAgent --query 'provisioningState' -o tsv
# Expect: Succeeded (within 10 min of deploy)

# 3) Heartbeat in Log Analytics
WORKSPACE_NAME=$(az resource list -g msaiv2_rg --resource-type Microsoft.OperationalInsights/workspaces --query '[0].name' -o tsv)
WORKSPACE_ID=$(az monitor log-analytics workspace show -g msaiv2_rg -n "$WORKSPACE_NAME" --query customerId -o tsv)
az monitor log-analytics query -w "$WORKSPACE_ID" --analytics-query 'Heartbeat | where TimeGenerated > ago(15m) | project Computer' -o table
# Expect: at least 1 row (within 15 min of deploy)

# 4) KV access from VM via system-assigned MI — using raw IMDS + KV REST
#    (azure-cli is NOT preinstalled on Ubuntu 24 LTS; this matches what render-env-from-kv.sh does)
VM_IP=$(az deployment group show -g msaiv2_rg -n main --query 'properties.outputs.vmPublicIp.value' -o tsv)
KV_NAME=$(az resource list -g msaiv2_rg --resource-type Microsoft.KeyVault/vaults --query '[0].name' -o tsv)

# Seed a test secret (operator side)
az keyvault secret set --vault-name "$KV_NAME" --name dummy-test-secret --value test-value-ok -o none

# SSH and exercise IMDS + KV REST as the system-assigned MI
ssh msaiadmin@"$VM_IP" "
TOKEN=\$(curl -sf -H 'Metadata: true' 'http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://vault.azure.net' | jq -r .access_token)
[ -n \"\$TOKEN\" ] && [ \"\$TOKEN\" != null ] || { echo 'IMDS failed' >&2; exit 1; }
curl -sf -H \"Authorization: Bearer \$TOKEN\" 'https://$KV_NAME.vault.azure.net/secrets/dummy-test-secret?api-version=7.4' | jq -r .value
"
# Expect: test-value-ok
# (jq is preinstalled on Ubuntu 24 LTS; if not, install via cloud-init in the Bicep — see T6 refactor note.)

# 5) ACR exists, no admin user
ACR_NAME=$(az resource list -g msaiv2_rg --resource-type Microsoft.ContainerRegistry/registries --query '[0].name' -o tsv)
az acr show -g msaiv2_rg -n "$ACR_NAME" --query '{name:name, sku:sku.name, adminEnabled:adminUserEnabled}' -o table
# Expect: adminEnabled=False

# 6) Federated credential registered
az identity federated-credential list --identity-name msai-gh-oidc -g msaiv2_rg -o table
# Expect: 1 row, subject=repo:marketsignal/msai-v2:ref:refs/heads/main

# 7) Blob backup container exists
STORAGE_ACCOUNT=$(az resource list -g msaiv2_rg --resource-type Microsoft.Storage/storageAccounts --query '[0].name' -o tsv)
az storage container show --account-name "$STORAGE_ACCOUNT" --name msai-backups --auth-mode login -o table
# Expect: existence (your operator account needs Storage Blob Data Reader/Contributor role at the
# account or container scope; assign via `az role assignment create` if missing)
```

Plus a short "If something fails" subsection pointing to research brief sections 4-5 for AMA + MI troubleshooting.

**T6 refactor note (revised per P2 #6):** Add `cloud-init` block to the VM Bicep declaration that installs `jq` (and `curl` if not present) on first boot. This makes the IMDS+REST smoke step (#4 above) and the `render-env-from-kv.sh` script work on a fresh VM without manual operator setup:

```bicep
osProfile: {
  // ...
  customData: base64('''#cloud-config
package_update: true
packages:
  - jq
  - curl
  - ca-certificates
''')
}
```

`azure-cli` is intentionally NOT installed by default — the smoke step uses raw IMDS + KV REST (matches what render-env-from-kv.sh does, removing CLI as a dependency).

### T12: tests/infra/test_bicep.sh CI test

**Writes:** `tests/infra/test_bicep.sh`.

**Test (Red):** Initial commit fails (no script). Then the script itself must exit 0 on a clean Bicep file.

**Implementation (Green):**

```bash
#!/usr/bin/env bash
# CI test: lint Bicep + verify what-if produces no unexpected output.
# Note: what-if is read-only; safe to run in CI without RG access if we accept
# the fallback path (only `bicep build` lint). Full what-if requires Azure auth.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

echo "=== az bicep build infra/main.bicep ==="
az bicep build --file infra/main.bicep --stdout > /dev/null
# Lint must be clean — exit 0 with no warnings.

if [[ "${SKIP_WHATIF:-}" == "1" ]] || ! az account show >/dev/null 2>&1; then
    echo "Skipping what-if (no Azure auth or SKIP_WHATIF=1)"
    exit 0
fi

echo "=== az deployment group what-if (msaiv2_rg) ==="
# Plan-review iter 3 P1 #15 fix: T1 makes operatorPrincipalId and vmSshPublicKey REQUIRED params
# (no defaults, intentionally absent from main.bicepparam). The CI test must supply BOTH for
# what-if to even validate. Use env overrides — defaults work for CI smoke; in interactive
# operator mode T10's deploy_bicep_whatif() resolves them properly.
OPERATOR_IP="${TEST_OPERATOR_IP:-0.0.0.0}"
OPERATOR_PRINCIPAL_ID="${TEST_OPERATOR_PRINCIPAL_ID:-00000000-0000-0000-0000-000000000000}"
SSH_PUBKEY="${TEST_SSH_PUBKEY:-ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDummyKeyForCIWhatIfOnlyDoNotDeploy CI}"
az deployment group what-if \
    -g msaiv2_rg \
    -f infra/main.bicep \
    --parameters infra/main.bicepparam \
    --parameters "operatorIp=$OPERATOR_IP" \
                 "operatorPrincipalId=$OPERATOR_PRINCIPAL_ID" \
                 "vmSshPublicKey=$SSH_PUBKEY" \
    --no-pretty-print > /tmp/whatif.json

# Confirm valid JSON and no operations of type "Ignore" (which would mean Bicep
# diff doesn't match our expectation).
# Plan-review P2 #5: only fail on Delete (Create on first run is expected; Modify on
# subsequent runs is normal — Azure adds default values not in Bicep, producing spurious
# "Modify" diffs that don't violate idempotency).
if jq -e '.changes[] | select(.changeType == "Delete")' /tmp/whatif.json >/dev/null 2>&1; then
    echo "what-if: unexpected Delete operation. Review and adjust Bicep." >&2
    jq '.changes[] | select(.changeType == "Delete")' /tmp/whatif.json >&2
    exit 1
fi
echo "what-if: no Delete operations. Pass."
```

Make executable, shellcheck-clean.

### Dispatch Plan

| Task ID | Depends on | Writes (concrete file paths)                                       |
| ------- | ---------- | ------------------------------------------------------------------ |
| T1      | —          | `infra/main.bicep`, `infra/main.bicepparam`, `infra/README.md`     |
| T2      | T1         | `infra/main.bicep` (append)                                        |
| T3      | T2         | `infra/main.bicep` (append)                                        |
| T4      | T3         | `infra/main.bicep` (append)                                        |
| T5      | T4         | `infra/main.bicep` (append)                                        |
| T6      | T5         | `infra/main.bicep` (append)                                        |
| T7      | T6         | `infra/main.bicep` (append)                                        |
| T8      | T7         | `infra/main.bicep` (append)                                        |
| T9      | —          | `scripts/render-env-from-kv.sh`, `scripts/msai-render-env.service` |
| T10     | T8         | `scripts/deploy-azure.sh` (modify)                                 |
| T11     | T8, T10    | `docs/runbooks/vm-setup.md` (modify)                               |
| T12     | T8         | `tests/infra/test_bicep.sh`                                        |

**Sequential mode:** T1–T8 must run serially because they all append to `infra/main.bicep` (no append-only fast-path per workflow rules). T9 can run in parallel with T1-T8 (different files). T10–T12 require T8 done.

**Practical order (single-agent sequential is simplest):** T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10 → T11 → T12.

---

## Implementation Notes

### Resource ordering in `main.bicep` (top-down, matches symbolic-name reference chain)

```
1. params (location, vmAdminUsername, vmSshPublicKey, operatorIp, repoOwner, repoName, repoBranch, ...)
2. vars (storageAccountName, acrName, keyVaultName, logWorkspaceName, deterministic names)
3. ghOidcMi (user-assigned MI for GH Actions)
4. ghOidcCredential (federated credential, parent=ghOidcMi)
5. vnet + subnet + publicIP + nsg
6. storageAccount + blobService (default child) + backupsContainer
7. acr
8. logWorkspace
9. keyVault
10. kvDiagnosticSettings (extension resource, scope: keyVault — NOT a child)
11. dataDisk + nic + vm (with system-assigned MI + data disk attached + cloud-init customData)
12. dcr (heartbeat)
13. amaExtension (parent=vm)
14. dcrAssociation (extension resource, scope: vm, dataCollectionRuleId=dcr.id)
15. roleAssignments x4 (KV Secrets User on KV, AcrPull on ACR, Blob Data Contributor on container, KV Secrets Officer on KV for operator)
16. outputs (acrLoginServer, kvUri, vmPublicIp, ghOidcClientId, vmPrincipalId, logAnalyticsWorkspaceId)
```

### What outputs to emit from `main.bicep`

For Slice 2/3 to consume via `az deployment group show`:

- `acrLoginServer` — `acr.properties.loginServer` (e.g., `msaiacrXXXX.azurecr.io`)
- `keyVaultUri` — `keyVault.properties.vaultUri`
- `vmPublicIp` — `publicIP.properties.ipAddress`
- `ghOidcClientId` — `ghOidcMi.properties.clientId` (Slice 2's GH Actions consumes this)
- `vmPrincipalId` — `vm.identity.principalId` (for verification only)
- `logAnalyticsWorkspaceId` — `logWorkspace.properties.customerId`

### Test strategy

**Plan-review phase (Phase 3.3):** Static Bicep lint via `az bicep build --stdout`. No deploy. No Azure account needed.

**Phase 5 verify-app (T12):** Same `az bicep build` lint. Optionally `az deployment group what-if` if Azure auth is present.

**Phase 5 verify-e2e:** SKIP — this slice is pure IaC with no user-facing surface. The Slice 1 acceptance smoke (T11 runbook) is the operator's manual verification, not an automatable use case. Mark E2E checklist boxes as `N/A: IaC-only slice, no user-facing UI/API/CLI changes` per the "purely internal" exception in `rules/testing.md`.

**Phase 6 final acceptance:** Pablo runs the runbook smoke against `msaiv2_rg` and confirms 7-of-7 commands pass.

### What's deliberately deferred to later slices

- **GH Actions workflow YAML** (Slice 2) — Slice 1 declares the federated credential; Slice 2 adds `.github/workflows/build-and-push.yml` that exchanges the OIDC token, builds backend + frontend images, and pushes to ACR with `${git-sha}` tags.
- **AcrPush role assignment for GH OIDC MI** (Slice 2) — Slice 1 only grants AcrPull to the VM's system-assigned MI (for pulling images at deploy time); Slice 2 adds AcrPush on the user-assigned `msai-gh-oidc` MI.
- **SSH from runner to VM** (Slice 3) — Slice 1 does not configure SSH key federation; Slice 3 adds an SSH credential (or uses Azure Bastion + RBAC).
- **Enable msai-render-env.service** (Slice 3) — Slice 1 ships the service file but does not enable it. Slice 3's first deploy step runs `systemctl enable --now msai-render-env.service`.
- **Nightly backup cron job + alert rules** (Slice 4) — Slice 1 ships the backup container target; Slice 4 adds the cron + DR runbook.
- **KV firewall + Private Endpoint** (Slice 4) — Slice 1's KV is `defaultAction: Allow`; Slice 4 tightens to `Deny + allowedIpRule + servicePrincipalRule` once VM IP is known and stable.
- **Tighter NSG egress rules** (Slice 4) — Slice 1 leaves outbound default-allow (AMA needs `*.ods.opinsights.azure.com` etc.); Slice 4 can scope down to allowlisted FQDNs once monitoring matures.

### Out-of-scope work that PRD-style review WILL surface and we will defer

- Cost dashboard / alerts on Azure spend — Slice 4.
- Azure backup vault for VM-level snapshots (separate from app-level Postgres backups) — Phase 2.
- Multi-environment (dev/stage/prod) Bicep parameter files — Phase 2.
- Migration to Azure Verified Modules (AVM) — Phase 3+ if scale demands.
- Premium SSD v2 evaluation — Phase 2.
- `live_deployments` hard gate enforcement — Slice 4.

### Council-mandated re-evaluation triggers

Per the slicing verdict's "Minority Report":

- **If Slice 1's KV-render-env script proves harder than ~30 lines** (e.g., MI propagation has multi-day Azure-side delay) → reopen with a verdict-doc addendum (Simplifier dissent re-activates).
- **If Slice 3 ships without an end-to-end rehearsal** → Contrarian's preference for B becomes active again.

Neither condition applies to Slice 1 itself, but document them so the next session knows the watchlist.

---

## Acceptance Checklist (Phase 5/6 gate)

- [ ] `tests/infra/test_bicep.sh` exits 0 (lint clean, no unexpected Delete operations in what-if).
- [ ] `./scripts/deploy-azure.sh --what-if` against the empty RG reports the expected create set (~17 resources, no Delete).
- [ ] Manual deploy succeeds: `OPERATOR_IP=<x.x.x.x> ./scripts/deploy-azure.sh` (default flag = deploy) exits 0 in under 15 min.
- [ ] Re-running the deploy script produces no Create or Delete operations in what-if (Modify is acceptable; idempotency verified).
- [ ] All 7 acceptance-smoke commands in the runbook pass.
- [ ] Plan-review loop: 0 P0/P1/P2 from Claude + Codex (or fallback).
- [ ] Code-review loop: 0 P0/P1/P2 from Codex + PR Review Toolkit.
- [ ] `verify-app`: lint + tests pass (Bash + shellcheck on the new shell scripts; markdown lint on runbook).
- [ ] `verify-e2e`: N/A justification accepted (IaC-only slice, no user-facing UI/API/CLI surface).

---

## References

- **PRD:** `docs/prds/deploy-pipeline-iac-foundation.md`
- **Discussion:** `docs/prds/deploy-pipeline-iac-foundation-discussion.md`
- **Research brief:** `docs/research/2026-05-09-deploy-pipeline-iac-foundation.md`
- **Architecture verdict (locked):** `docs/decisions/deployment-pipeline-architecture.md`
- **Slicing verdict (locked):** `docs/decisions/deployment-pipeline-slicing.md`
- **Precursor PR (merged):** `6900210` — PR #50, `feat/prod-compose-deployable`
