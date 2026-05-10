# PRD: Deployment Pipeline Slice 1 — IaC Foundation

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-05-09
**Last Updated:** 2026-05-09

---

## 1. Overview

Slice 1 of the 4-PR deployment-pipeline series, ratified at `docs/decisions/deployment-pipeline-slicing.md`. Provisions the foundational Azure infrastructure for `msaiv2_rg` (eastus2, MarketSignal2 sub) so subsequent slices can build CI/CD and operations on top of a stable, reproducible platform. **No application is deployed in this slice.** Outcome: an operator (Pablo today, GH Actions later) can run `./scripts/deploy-azure.sh` and end up with a VM that has every supporting Azure resource (ACR, Key Vault, Log Analytics workspace, Blob backup container) wired up via managed identity, plus the systemd unit that will materialize `/run/msai.env` from Key Vault at boot when Slice 3 turns it on.

## 2. Goals & Success Metrics

### Goals

- **Provision idempotent, reviewable Azure infrastructure** — a single Bicep template + `az deployment group create` call yields the same resource graph every time, on a fresh `msaiv2_rg` or an already-provisioned one.
- **Eliminate the secret-on-runner failure mode from day one** — IB credentials and other production secrets live exclusively in Key Vault, accessed via VM system-assigned managed identity. No `.env` shipped over the wire, no secret pulled by the GH runner (closes the architectural blocker that motivated KV-from-day-one in the architecture verdict).
- **Land observability + DR target with the foundation, not after** — Hawk's blocking objection #1 from the slicing verdict: Log Analytics workspace + AMA on the VM, plus the `msai-backups` Blob container as the Slice 4 backup target, ship in Slice 1.

### Success Metrics

| Metric                                           | Target                                                                    | How Measured                                                                                                                                          |
| ------------------------------------------------ | ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bicep what-if validation                         | Clean (no errors, expected resource diff)                                 | `az deployment group what-if -g msaiv2_rg -f infra/main.bicep` against empty RG → all resources show as "Create"; against post-deploy RG → "NoChange" |
| KV secret retrieval from VM via managed identity | < 5s after VM boot (with retry tolerating up to 60s identity propagation) | Manual smoke: SSH to VM, `az login --identity`, `az keyvault secret show --vault-name <kv> --name dummy` returns success                              |
| Log Analytics heartbeat                          | VM appears in `Heartbeat` table within 15 min of provision                | Azure portal → Log Analytics workspace → Logs → `Heartbeat \| where Computer == 'msaiv2-vm'`                                                          |
| `scripts/deploy-azure.sh` re-run idempotency     | Re-running yields zero resource changes                                   | Run twice in succession; second run's deployment-group create reports `provisioningState: Succeeded` with no resource modifications                   |
| End-to-end provision time                        | < 15 min for a clean RG                                                   | `time ./scripts/deploy-azure.sh` end-to-end                                                                                                           |

### Non-Goals (Explicitly Out of Scope)

- ❌ No GitHub Actions workflow file (Slice 2)
- ❌ No `docker compose pull` / `up -d` deploys (Slice 3)
- ❌ No application image build or push (Slice 2)
- ❌ No SSH from GH runner to VM (Slice 3)
- ❌ No nightly backup cron job (Slice 4 — Slice 1 only ships the backup _target_ container)
- ❌ No alert rules or dashboards (Slice 4 — Slice 1 ships the workspace, not the alerts)
- ❌ No Azure Postgres Flexible Server (Phase 2 — paper-only Phase 1 stays containerized)
- ❌ No Helm, no Kubernetes (Phase 3+ migration; out of scope for current 6-month runway)
- ❌ No reuse of `msaimls2_rg` Azure ML workspace's ACR/KV/log resources (Contrarian Blocking Objection #5)
- ❌ No `.env` checked in or shipped over the wire — `/run/msai.env` is the only acceptable runtime form, and it's rendered at boot from KV

## 3. User Personas

### Pablo (developer/operator)

- **Role:** Solo operator. Owns the codebase + Azure subscription. Runs deployments by hand for the first weeks, then delegates to GH Actions.
- **Permissions:** Owner on MarketSignal2 sub (`68067b9b-943f-4461-8cb5-2bc97cbc462d`).
- **Goals:** Provision infra in one command; verify acceptance via `az` CLI; never commit a secret; have a clear escape path if the auto-deploy ever stops working.

### GitHub Actions runner (declared now, consumed in Slice 2)

- **Role:** CI runtime that will exchange a federated OIDC token for an Azure access token (Slice 2 enables `docker push` to ACR; Slice 3 enables `ssh` to VM).
- **Permissions:** Per the federated credential declared in Slice 1 + the role assignments Slice 2 will add. Slice 1 only defines the federated credential resource; Slice 2 grants AcrPush.
- **Goals:** Push images to ACR without storing long-lived credentials anywhere.

### systemd on the VM (declared now, enabled in Slice 3)

- **Role:** Reads Key Vault via VM managed identity at boot, writes `/run/msai.env`, then chains into `docker compose up -d` (Slice 3 wires the chain).
- **Permissions:** VM system-assigned managed identity with Key Vault Secrets User on the KV scope.
- **Goals:** Materialize a complete production env file before the application stack starts.

## 4. User Stories

### US-001: Operator provisions a new MSAI v2 environment

**As an** operator (Pablo)
**I want** a single command that provisions every Azure resource MSAI v2 needs
**So that** environment standup is reproducible, version-controlled, and reviewable

**Scenario:**

```gherkin
Given the resource group msaiv2_rg exists in the MarketSignal2 subscription and is empty
And I have Owner role on the subscription
And I have azure-cli installed and `az account set --subscription 68067b9b-...` is active
When I run ./scripts/deploy-azure.sh
Then the script invokes `az deployment group create -f infra/main.bicep -g msaiv2_rg`
And every resource declared in infra/main.bicep is created with provisioningState=Succeeded
And no orphan resources remain in msaiv2_rg
And the script exits 0 in under 15 minutes
```

**Acceptance Criteria:**

- [ ] `infra/main.bicep` declares: VM (D4s_v6), NSG, Premium SSD data disk attached to the VM, Standard_LRS storage account + `msai-backups` Blob container, ACR Basic, Key Vault (with RBAC auth model + soft-delete enabled), Log Analytics workspace, AMA VM extension on the VM, GitHub OIDC federated credential, VM system-assigned managed identity
- [ ] Three RBAC role assignments wired to the VM managed identity: Key Vault Secrets User on the KV, AcrPull on the ACR, Storage Blob Data Contributor on the `msai-backups` container scope
- [ ] NSG inbound rules: SSH (port 22) from operator IP only, HTTP (80) and HTTPS (443) from anywhere, all other inbound denied
- [ ] `az deployment group what-if` against the empty RG reports the expected create set; against an already-provisioned RG reports `NoChange`
- [ ] `scripts/deploy-azure.sh` extension runs the bicep deploy and surfaces the deployment correlation ID on success/failure
- [ ] Re-running the script is idempotent (no resource modifications on the second run)

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| Operator IP changed since last deploy | NSG rule must be a parameter the script can update — script accepts `--operator-ip` flag, falls back to `curl ifconfig.me` |
| ACR name collision (globally unique) | Bicep parameter for `acrName` with deterministic default derived from sub ID; operator can override |
| Storage account name collision | Same pattern — parameter with deterministic default |
| Re-run after operator manually deleted a resource | Bicep recreates it; if it was a stateful resource (Premium SSD), data is lost — runbook must warn |
| Key Vault soft-delete window | If Bicep tries to recreate a recently-deleted KV with the same name, deployment fails — runbook documents `az keyvault purge` |
| Operator runs from a sub without Owner | Script fails fast with a clear "needs Owner role" message |

**Priority:** Must Have

---

### US-002: VM fetches secrets from Key Vault via managed identity

**As** the systemd unit on the VM (representing the platform)
**I want** to retrieve every required production secret from Key Vault using the VM's system-assigned managed identity
**So that** application services can start without any secret ever residing in repo, in `.env` files on the VM disk, or on the GH Actions runner

**Scenario:**

```gherkin
Given the VM has a system-assigned managed identity with Key Vault Secrets User on the KV
And the KV holds the required production secrets (REPORT_SIGNING_SECRET, AZURE_TENANT_ID, AZURE_CLIENT_ID, JWT_TENANT_ID, JWT_CLIENT_ID, IB_ACCOUNT_ID, POLYGON_API_KEY, DATABENTO_API_KEY, MSAI_API_KEY, etc.)
When the VM boots and systemd starts the msai-render-env unit
Then /usr/local/bin/render-env-from-kv.sh runs
And the script obtains a token via the IMDS endpoint (with retry + backoff for managed identity propagation)
And the script fetches every required secret by name
And the script writes /run/msai.env with mode 0600 and owner root:root
And the unit exits 0
And /run/msai.env contains key=value lines for every required secret
And no secret value appears in journalctl logs
```

**Acceptance Criteria:**

- [ ] `scripts/render-env-from-kv.sh` exists, is executable (`chmod +x`), runs only on the VM (refuses to run on dev box without managed identity)
- [ ] Script uses `curl` against IMDS (`http://169.254.169.254/metadata/identity/oauth2/token?...`) with retry + backoff up to ~60s to tolerate identity propagation latency on a freshly-provisioned VM
- [ ] Script fetches every required production secret listed in CLAUDE.md `## Environment Variables` (excluding the ones derived at runtime — e.g., `DATABASE_URL`, `REDIS_URL`, `DATA_ROOT`, `ENVIRONMENT`)
- [ ] Output file `/run/msai.env` written with mode `0600`, owner `root:root`, atomic write via `mv` from `/run/msai.env.tmp`
- [ ] Systemd unit `msai-render-env.service` is `Type=oneshot`, runs `Before=docker-compose-msai.service`, `RemainAfterExit=yes`, has `Restart=on-failure` with `RestartSec=10`
- [ ] No secret value is logged to `journalctl` — script uses `set +x` while writing the env file
- [ ] Slice 1 ships the script + unit but does NOT enable the unit (Slice 3 enables it as part of the first deploy)

**Edge Cases:**
| Condition | Expected Behavior |
|-----------|-------------------|
| Managed identity not yet propagated (token fetch returns 404 within first 60s of VM provision) | Script retries with exponential backoff; total budget 60s; exits 1 with clear message after exhaustion |
| Required secret missing in KV | Script exits 1 naming the missing secret; does NOT write a partial env file |
| KV access denied (RBAC misconfigured) | Script exits 1 with the AAD error code; does NOT write a partial env file |
| Re-running the unit (e.g., after an operator updated a secret) | New `/run/msai.env` written atomically; existing readers continue with old file until they reload |
| VM rebooted | Systemd re-runs the unit before docker-compose comes back up |

**Priority:** Must Have

---

### US-003: Operator verifies infrastructure acceptance

**As an** operator (Pablo)
**I want** a documented 15-minute smoke check
**So that** I know the foundation is correctly wired before any further slice work

**Scenario:**

```gherkin
Given Slice 1 has been deployed to a fresh msaiv2_rg
When I follow the smoke checklist in docs/runbooks/vm-setup.md (Slice 1 section)
Then I confirm: KV holds at least one dummy secret, the VM can fetch it via managed identity from a manual SSH session, the Log Analytics Heartbeat table shows the VM, the Blob backup container exists, ACR exists, the federated credential is registered, and `az deployment group what-if` reports NoChange
And the entire smoke takes under 15 minutes
```

**Acceptance Criteria:**

- [ ] Runbook section in `docs/runbooks/vm-setup.md` titled "Slice 1 acceptance smoke" enumerates the seven `az` commands above
- [ ] Each command is copy-pasteable (no placeholders left as `<TODO>`)
- [ ] Runbook documents the expected output for each (so operator can spot deviations)

**Priority:** Must Have

## 5. Constraints & Policies

> Outcome-level only. Hard limits the product must respect. HOW we satisfy them is design.

### Business / Compliance Constraints

- IB credentials must never reside on the GitHub Actions runner. Architecturally enforced via the rule that secrets only flow Azure-side (Key Vault → managed identity → VM); the runner only ever has an OIDC token to push images to ACR.
- The MarketSignal2 subscription's existing ML workspace at `msaimls2_rg` is owned by a different project (Azure ML) and must not be reused — fresh resources for MSAI v2 only (Contrarian Blocking Objection #5).
- Sub: MarketSignal2 (`68067b9b-943f-4461-8cb5-2bc97cbc462d`); tenant: `2237d332-fc65-4994-b676-61edad7be319`. Pablo has Owner.

### Platform / Operational Constraints

- VM size MUST be in the Ddsv6 family (`D4s_v6`) — DSv5 quota is 0/0 in MarketSignal2; Ddsv6 is 0/10. No `Standard_DS4_v5`.
- Region MUST be `eastus2` (consistent with `msaiv2_rg` location and existing `msaimls2_rg` in eastus2).
- Bicep + `az deployment group create` MUST be idempotent — re-running with no input changes must report `NoChange`.
- All resources scoped to `msaiv2_rg`; no cross-RG deployments.
- The pre-merged compose file `docker-compose.prod.yml` (PR #50) expects `MSAI_REGISTRY`, `MSAI_BACKEND_IMAGE`, `MSAI_GIT_SHA` env vars — Slice 1 must allocate the ACR whose login server will become `MSAI_REGISTRY` (consumed by Slices 2/3).
- `render-env-from-kv.sh` total Bash + systemd unit ≤ ~50 lines combined (slicing verdict §Slice 1 estimate ~30 lines for shell — leave room for retry loop comments).

### Dependencies & Required Integrations

- **Requires:** `az` CLI (operator's machine); active Owner-role subscription context; PR #50 (`6900210`) merged on main (precondition cleared).
- **Required integrations (named scope, not mechanism):**
  - **Azure Resource Manager** — Bicep is ARM-native (the design phase will confirm Bicep idioms via the research-first agent, not pivot to Terraform)
  - **Azure Active Directory (Entra ID)** — federated credential lives under an AAD application registration; managed identity binds to the same AAD tenant
  - **GitHub OIDC** — federated credential subject claim binds to `repo:<owner>/<repo>:ref:refs/heads/main` (research-first will confirm exact format)
  - **Azure Monitor / Log Analytics** — AMA installed via Bicep VM extension; data collection rule (DCR) attached to the workspace

## 6. Security Outcomes Required

- **Who can access what:**
  - Only the VM's managed identity can read secrets from Key Vault (Key Vault Secrets User RBAC, scoped to the KV)
  - Only the GitHub Actions OIDC token (with the matching subject claim) can push to ACR (AcrPush role assignment lives in Slice 2; Slice 1 declares the federated credential but does not yet grant AcrPush)
  - Only the operator's IP can SSH to the VM (NSG rule)
  - HTTP/HTTPS inbound is open from anywhere — application-level auth (Azure Entra ID JWT) handles user authorization once Slice 3 deploys the app
- **What must never leak:**
  - Secret values must never appear in `journalctl`, in Bicep deployment logs, or in `az deployment group show` outputs
  - The VM's managed identity client ID is not sensitive (it's not a credential), but the bearer tokens it issues are — `render-env-from-kv.sh` must not echo tokens
- **What must be auditable:**
  - All `deploy-azure.sh` runs produce a deployment correlation ID — runbook documents how to retrieve `az deployment group show` history
  - All KV secret retrievals are logged in Log Analytics via diagnostic settings on the KV (Slice 1 enables KV diagnostic settings → Log Analytics workspace)
- **Legal / regulatory outcomes:** N/A for paper-trading Phase 1. (Real-money Phase 2 will add SOC2-style audit retention requirements; out of scope here.)

## 7. Open Questions

> Questions to resolve in Phase 2 (research) and Phase 3 (design + plan-review)

- [ ] Bicep file structure: single `infra/main.bicep` (~300 lines) or split into modules (`infra/modules/network.bicep`, `infra/modules/compute.bicep`, etc.)? Decide in Phase 3 informed by research-first.
- [ ] AMA Data Collection Rule (DCR) scope: heartbeat-only for Slice 1, or include syslog + perf counters now? Default to heartbeat-only; defer perf counters to Slice 4 unless research shows DCR scope changes are non-trivial.
- [ ] Federated credential subject claim format: `repo:<owner>/<repo>:ref:refs/heads/main` is documented; is `environment:production` also needed for Slice 2's GH Environment-bound deploys? Research-first must answer.
- [ ] Storage account globally-unique naming: deterministic suffix from `subscription().subscriptionId` hash, or random suffix? Deterministic preferred for re-deploy idempotency.
- [ ] Should the federated credential's `audiences` be the Azure default (`api://AzureADTokenExchange`) or customized? Default unless research surfaces a reason.
- [ ] AMA install timing: Bicep `Microsoft.Compute/virtualMachines/extensions` resource at deploy time, or post-provision via `az vm extension set`? Bicep-time preferred for idempotency; research-first will confirm.

## 8. References

- **Discussion log:** `docs/prds/deploy-pipeline-iac-foundation-discussion.md`
- **Architecture verdict (locked):** `docs/decisions/deployment-pipeline-architecture.md`
- **Slicing verdict (locked):** `docs/decisions/deployment-pipeline-slicing.md`
- **Precursor PR (merged):** `6900210` — PR #50, `feat/prod-compose-deployable`
- **Related runbook:** `docs/runbooks/vm-setup.md` (will be extended in Phase 4 with Slice 1 acceptance smoke)

---

## Appendix A: Revision History

| Version | Date       | Author        | Changes                                |
| ------- | ---------- | ------------- | -------------------------------------- |
| 1.0     | 2026-05-09 | Claude + User | Initial PRD for Slice 1 IaC foundation |

## Appendix B: Approval

- [ ] Product Owner approval (Pablo)
- [ ] Technical Lead approval (Pablo)
- [ ] Ready for technical design
