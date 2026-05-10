# PRD: Deployment Pipeline Slice 2 — CI Image Publish

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-05-10
**Last Updated:** 2026-05-10

---

## 1. Overview

Slice 2 of the 4-PR deployment-pipeline series, ratified at [`docs/decisions/deployment-pipeline-slicing.md`](../decisions/deployment-pipeline-slicing.md). Wires push-to-main on the `marketsignal/msai-v2` repo into Azure via OIDC federation, builds the backend and frontend Docker images, and pushes them to the Azure Container Registry provisioned in Slice 1, tagged with the immutable git SHA. **No deploy step.** When this ships, every commit on `main` lands two images in ACR — `msai-backend:<sha7>` and `msai-frontend:<sha7>` — that Slice 3 will pull onto the VM.

## 2. Goals & Success Metrics

### Goals

- **Continuous image publication on push-to-main.** Every commit that lands on `main` produces and publishes both application images to ACR with no operator action.
- **Zero long-lived credentials in CI.** Authentication to Azure happens exclusively via the OIDC federated credential declared in Slice 1 — no service principal client secret, no `AZURE_CREDENTIALS` JSON blob, no PAT.
- **Reproducible image references.** Every published image is tagged with the short git SHA only — strict immutability so Slice 3 can roll back by pinning a known-good SHA.
- **No surprise: the same Slice 1 IaC layer grants the AcrPush role.** The single missing role assignment from Slice 1's `main.bicep` (flagged at line 90: "AcrPush role assignment lives in Slice 2") gets added in this slice, keeping all production RBAC declarative.

### Success Metrics

| Metric                                        | Target                                                              | How Measured                                                                                                                 |
| --------------------------------------------- | ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Workflow green on a no-op commit              | Workflow run completes with `success` conclusion                    | `gh run list --workflow=build-and-push.yml --limit 1 --json conclusion -q '.[0].conclusion'` returns `success`               |
| Backend image published with short-SHA tag    | `msai-backend:<sha7>` exists in ACR after the run                   | `az acr repository show-tags --name <acr> --repository msai-backend --output tsv` lists `<sha7>` for the run's commit        |
| Frontend image published with short-SHA tag   | `msai-frontend:<sha7>` exists in ACR after the run                  | `az acr repository show-tags --name <acr> --repository msai-frontend --output tsv` lists `<sha7>`                            |
| OIDC federation succeeds                      | `azure/login@v2` step exits 0 with no SPN client secret in workflow | Workflow log shows OIDC token exchange; `git grep -i 'azure_credentials\|client_secret'` in `.github/` returns no matches    |
| `workflow_dispatch` trigger functional        | Operator can re-run from Actions UI without a code change           | `gh workflow run build-and-push.yml` succeeds; subsequent `gh run list` shows the dispatch run                               |
| Acceptance smoke E2E time                     | < 10 min for cold cache, < 5 min for warm cache                     | Workflow run duration on the no-op acceptance commit                                                                         |
| Re-pushing the same commit overwrites cleanly | Image digest changes only if Dockerfile/source changed              | Two consecutive `workflow_dispatch` runs on the same commit produce identical image digests (cache hit confirms determinism) |

### Non-Goals (Explicitly Out of Scope)

- ❌ No deploy step (SSH to VM, `docker compose pull`, `up -d --wait`) — Slice 3
- ❌ No moving `latest` tag — rollback discipline (Slice 3 references explicit SHA per deploy, last 5 retained per slicing verdict §rollback)
- ❌ No image scanning (Trivy, Snyk, etc.) — Slice 4 ops, if at all
- ❌ No multi-arch builds (linux/arm64) — eastus2 VM is x86_64
- ❌ No PR-trigger build-and-push — existing `ci.yml` covers PR validation; only pushes to `main` produce ACR images
- ❌ No image-retention / pruning policy — Slice 4
- ❌ No e2e/smoke against published images — Slice 3 will exercise the deploy + health checks
- ❌ No environment promotion (dev → staging → prod) — Phase 1 is single-environment
- ❌ No private endpoint or VNet integration on ACR — Phase 2; Slice 1 set ACR `publicNetworkAccess: Enabled`

## 3. User Personas

### Pablo (operator)

- **Role:** Solo operator. Owns the codebase + Azure subscription + GitHub repo settings.
- **Permissions:** Subscription Owner on MarketSignal2; admin on `marketsignal/msai-v2` GitHub repo.
- **Goals:** Set the eight repo Variables once before first run (the 7 originally enumerated PLUS `ACR_NAME`, added during Phase 3 plan-review iter-1 to fix `az acr login --name` short-name vs FQDN ambiguity); trigger `workflow_dispatch` for the acceptance smoke; verify the two short-SHA-tagged images appear in ACR; never see an Azure client secret.

### GitHub Actions runner (the workflow itself)

- **Role:** Stateless CI runtime that, on every push to `main` or manual dispatch, exchanges a federated OIDC token for an Azure access token, builds both images, and pushes to ACR.
- **Permissions:** `id-token: write`, `contents: read` at the workflow level; AcrPush on the ACR via the `msai-gh-oidc` user-assigned managed identity.
- **Goals:** Authenticate without long-lived credentials; emit two reproducible images; fail fast and visibly when any step breaks.

### Slice 3's deploy job (downstream consumer, declared now, consumed later)

- **Role:** A future deploy workflow (Slice 3) that pulls images from ACR onto the VM by short-SHA reference.
- **Permissions:** AcrPull (already granted to the VM's system-assigned MI in Slice 1).
- **Goals:** Reference any committed `<sha7>` deterministically; never see `latest`; roll back by re-deploying a previous SHA from the last-5 retention window.

## 4. User Stories

### US-001: Operator pushes to main and images land in ACR

**As an** operator (Pablo)
**I want** every push to `main` to automatically build and publish both images to ACR
**So that** Slice 3's deploy step has reproducible, immutable image references with no manual intervention

**Scenario:**

```gherkin
Given Slice 1's `infra/main.bicep` is deployed to msaiv2_rg with the gh-oidc MI + federated credential
And the eight required GitHub repo Variables are set (AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID, AZURE_CLIENT_ID, ACR_NAME, ACR_LOGIN_SERVER, NEXT_PUBLIC_AZURE_TENANT_ID, NEXT_PUBLIC_AZURE_CLIENT_ID, NEXT_PUBLIC_API_URL)
And `infra/main.bicep` has been re-applied with the AcrPush role assignment added in this slice
When I commit and push a no-op change to `main`
Then GitHub Actions runs `.github/workflows/build-and-push.yml`
And the workflow exchanges an OIDC token for an Azure access token via `azure/login@v2`
And the workflow builds backend from `backend/Dockerfile` (build context = repo root)
And the workflow builds frontend from `frontend/Dockerfile` with the three NEXT_PUBLIC_* build-args populated from repo Variables
And the workflow pushes `msai-backend:<sha7>` and `msai-frontend:<sha7>` to ACR
And the workflow run concludes with `success`
```

**Acceptance Criteria:**

- [ ] `.github/workflows/build-and-push.yml` exists and triggers on `push: branches: [main]` AND `workflow_dispatch:`
- [ ] Workflow has `permissions: id-token: write, contents: read` at the job (or workflow) level
- [ ] Workflow uses `azure/login@v2` with `client-id`, `tenant-id`, `subscription-id` referenced from `${{ vars.* }}` — no `creds` JSON, no client secret
- [ ] Workflow uses `docker/login-action@v3` against `${{ vars.ACR_LOGIN_SERVER }}` with no admin password (OIDC-derived)
- [ ] Workflow has two parallel jobs (or matrix entries) — one each for backend and frontend
- [ ] Each job uses `docker/build-push-action@v5` with `cache-from: type=gha` and `cache-to: type=gha,mode=max`
- [ ] Each job tags the produced image as `${{ vars.ACR_LOGIN_SERVER }}/msai-{backend|frontend}:${{ <short-sha> }}` only — no `latest`
- [ ] Frontend job passes the three required `NEXT_PUBLIC_*` build-args; backend job passes none
- [ ] `infra/main.bicep` declares `acrPushAssignment` (or equivalent name) granting AcrPush role on `acr` to `ghOidcMi.properties.principalId`, with `principalType: 'ServicePrincipal'`
- [ ] After a push-to-main, `az acr repository show-tags --name <acr> --repository msai-backend` lists the short SHA
- [ ] Same check for `msai-frontend`
- [ ] No secret-typed value (`AZURE_CREDENTIALS`, client secret, ACR admin password) appears anywhere in the workflow file or repo Secrets
- [ ] `docs/research/2026-05-09-deploy-pipeline-iac-foundation.md` topic 3 is corrected: replace bogus `streams: ['Microsoft-Heartbeat']` with the validated `kind: 'Linux'` + `Microsoft-Syslog` data source pattern (per `feedback_ama_dcr_kind_linux_required.md`)

**Edge Cases:**

| Condition                                                         | Expected Behavior                                                                                                                                                                                              |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Backend build fails, frontend succeeds                            | Workflow conclusion = `failure`; only frontend image published. The two jobs are independent — partial success is acceptable; Slice 3 deploys won't run until both succeed (Slice 3's concern, not Slice 2's). |
| Repo Variables missing                                            | `docker/build-push-action` step fails fast with a clear error referencing the missing variable. The frontend Dockerfile already has fail-fast guards on each `NEXT_PUBLIC_*` ARG.                              |
| OIDC token exchange fails (federated credential subject mismatch) | `azure/login@v2` exits non-zero with the AAD error code; workflow halts before any build runs.                                                                                                                 |
| ACR push fails (AcrPush role not yet propagated)                  | `docker/login-action` or push step fails with 401/403. Operator re-runs `workflow_dispatch` after waiting ~30s for RBAC propagation.                                                                           |
| Two pushes land on `main` within a few seconds                    | Standard `concurrency` group cancels the in-progress run when a newer one starts (per workflow `concurrency: group: build-and-push-${{ github.ref }}, cancel-in-progress: true`).                              |
| Same commit re-dispatched manually                                | Workflow runs again, builds with cache hits, pushes the same digest under the same tag (idempotent re-push). Image manifest digest unchanged.                                                                  |
| Push to a branch other than `main`                                | Workflow does NOT run — branch filter on `push:`. Existing `ci.yml` runs PR validation; image publishing is `main`-only.                                                                                       |
| Run on a fork                                                     | Federated credential subject = `repo:marketsignal/msai-v2:ref:refs/heads/main`; forks present a different subject, OIDC exchange fails, no images pushed. Defense-in-depth even if the fork enables Actions.   |

**Priority:** Must Have

---

### US-002: Operator triggers acceptance smoke without a code change

**As an** operator (Pablo)
**I want** to manually re-run the workflow from the Actions UI
**So that** I can run the Slice 2 acceptance smoke (and any future re-publish) without writing a synthetic commit

**Scenario:**

```gherkin
Given the workflow file is on main with `workflow_dispatch:` declared
When I navigate to repo Actions → "Build and Push Images" → "Run workflow"
Or I run `gh workflow run build-and-push.yml`
Then a new workflow run starts on the current `main` HEAD
And the run completes the same way a push-to-main run would
And ACR shows the same `<sha7>` tag (idempotent re-push)
```

**Acceptance Criteria:**

- [ ] `on:` block has `workflow_dispatch:` alongside `push: branches: [main]`
- [ ] No `inputs:` are required for dispatch (smoke is parameter-free)
- [ ] `gh workflow run build-and-push.yml` from the operator's machine starts a run successfully
- [ ] Re-dispatching on the same SHA produces the same image digest (deterministic build given identical source + cache)

**Priority:** Must Have

---

### US-003: AcrPush role grant lands in IaC, not as a portal click

**As a** future operator (or another person taking over)
**I want** the AcrPush role assignment to be declared in `infra/main.bicep` and deployed via `scripts/deploy-azure.sh`
**So that** standing up a fresh `msaiv2_rg` from scratch produces a working CI pipeline without a hidden manual step

**Scenario:**

```gherkin
Given a fresh resource group msaiv2_rg
When I run ./scripts/deploy-azure.sh (passing --operator-ip and --ssh-public-key)
Then `az deployment group create -f infra/main.bicep` provisions all Slice 1 resources
And it ALSO provisions the AcrPush role assignment from msai-gh-oidc → msai-acr
And no manual `az role assignment create` step is required afterward
And `az deployment group what-if` against the post-deploy RG reports NoChange
```

**Acceptance Criteria:**

- [ ] `infra/main.bicep` declares one new `Microsoft.Authorization/roleAssignments@2022-04-01` resource scoping AcrPush to the ACR for the gh-oidc MI principal
- [ ] Role definition GUID for AcrPush (`8311e382-0749-4cb8-b61a-304f252e45ec`) declared as a `roleDefIdAcrPush` variable alongside the existing `roleDefIdAcrPull`, `roleDefIdKvSecretsUser`, etc.
- [ ] Assignment uses `guid()` for a deterministic name (consistent with the four Slice 1 assignments)
- [ ] Bicep `description` field on the assignment cites Slice 2 / CI image push
- [ ] `principalType: 'ServicePrincipal'` set explicitly (matches existing pattern; reduces propagation latency)
- [ ] Re-running `scripts/deploy-azure.sh` is idempotent (no resource modifications on second run)
- [ ] Slice 1's plan-file comment at `infra/main.bicep:90` ("AcrPush role assignment lives in Slice 2") removed or updated to note Slice 2 added it

**Edge Cases:**

| Condition                                                     | Expected Behavior                                                                                                                            |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Deploying Slice 2 Bicep before AcrPush role definition exists | Role def GUID is a built-in subscription-scoped resourceId; always exists. No precondition.                                                  |
| Operator hasn't re-run deploy-azure.sh after merging Slice 2  | First push-to-main fails on `docker push` with 401/403. Runbook explicitly requires the IaC re-apply as a post-merge step before first push. |

**Priority:** Must Have

---

## 5. Constraints & Policies

> Outcome-level only. Hard limits the product must respect.

### Business / Compliance Constraints

- **No long-lived Azure credentials in GitHub.** OIDC federation is the only authentication mechanism. No `AZURE_CREDENTIALS` GitHub Secret, no service principal client secret, no PAT. Architecturally enforced via Slice 1's federated credential resource and the workflow's `permissions: id-token: write`.
- **No reuse of `msaimls2_rg`'s ACR/KV/log resources** (Contrarian Blocking Objection #5 from the slicing verdict). Slice 2 only writes to `msaiacr<hash>.azurecr.io` provisioned in Slice 1.

### Platform / Operational Constraints

- **GH-hosted runners only** (`runs-on: ubuntu-24.04` matching `ci.yml`). Self-hosted runners rejected at architecture-verdict time (lateral-movement risk).
- **Image registry is ACR Basic SKU** as provisioned in Slice 1. ACR Basic supports OIDC token-based auth via `az acr login --expose-token` or `docker/login-action`'s ACR variant; no admin password required.
- **Build context split:** backend builds with build-context = repo root and `-f backend/Dockerfile` (matches local pattern in `docker-compose.dev.yml`); frontend builds with build-context = `./frontend` and `-f frontend/Dockerfile`. Confirmed against the existing Dockerfiles.
- **Tag format:** short SHA only (first 7 chars of `${{ github.sha }}`). Matches slicing verdict literal acceptance (`abc1234`).
- **Concurrency:** at most one in-progress build per ref; newer pushes cancel older runs (`cancel-in-progress: true`).
- **Push-to-main only.** PR builds remain in `ci.yml`; image publishing must NEVER fire from a PR or fork.

### Dependencies & Required Integrations

- **Requires:** Slice 1 (`feat/deploy-pipeline-iac-foundation`, PR #51 + #52 + #53) merged on main. Federated credential, ACR, gh-oidc MI all live.
- **Required integrations (named scope):**
  - **GitHub Actions OIDC issuer** (`token.actions.githubusercontent.com`) — federated credential authority
  - **Azure Active Directory (Entra ID)** — token exchange + MI principal
  - **Azure Container Registry** — image push target
  - **Azure Resource Manager** — Bicep target for the AcrPush assignment

### Operator Pre-Merge Setup (one-time)

Before the workflow can run successfully, Pablo sets **eight** GitHub repo Variables (Settings → Secrets and variables → Actions → **Variables** tab — NOT Secrets). All values are public; none are credentials.

> **Note:** the original PRD draft listed 7 variables. Phase 3 plan-review iter-1 (Codex) caught that `az acr login --name` expects the short registry name (e.g., `msaiacrXXX`), not the FQDN — `vars.ACR_LOGIN_SERVER` is FQDN-only. Resolution: split into `ACR_NAME` (short) for `az acr login --name` AND `ACR_LOGIN_SERVER` (FQDN) for `docker/login-action.registry` and image tags.

| Variable                      | Source                                                                                                                                                       |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `AZURE_TENANT_ID`             | Hardcoded — MarketSignal tenant `2237d332-fc65-4994-b676-61edad7be319`                                                                                       |
| `AZURE_SUBSCRIPTION_ID`       | Hardcoded — MarketSignal2 sub `68067b9b-943f-4461-8cb5-2bc97cbc462d`                                                                                         |
| `AZURE_CLIENT_ID`             | `az deployment group show -g msaiv2_rg -n main --query 'properties.outputs.ghOidcClientId.value' -o tsv`                                                     |
| `ACR_NAME`                    | `az acr list -g msaiv2_rg --query '[0].name' -o tsv` — short name (e.g., `msaiacrXXX`)                                                                       |
| `ACR_LOGIN_SERVER`            | `az deployment group show -g msaiv2_rg -n main --query 'properties.outputs.acrLoginServer.value' -o tsv` — FQDN (e.g., `msaiacrXXX.azurecr.io`)              |
| `NEXT_PUBLIC_AZURE_TENANT_ID` | Same as `AZURE_TENANT_ID`                                                                                                                                    |
| `NEXT_PUBLIC_AZURE_CLIENT_ID` | Frontend Entra app registration's client ID (separate AAD app from the gh-oidc MI; this is the SPA registration MSAL signs in against)                       |
| `NEXT_PUBLIC_API_URL`         | Production API base URL — placeholder until Slice 3 picks DNS (e.g., `https://api.msai.example.com` or `http://<vmPublicIp>:8000` for the placeholder build) |

The runbook will document this list and the exact `az` commands.

## 6. Security Outcomes Required

- **Who can push to ACR:**
  - Only the GitHub Actions OIDC token whose subject claim matches `repo:marketsignal/msai-v2:ref:refs/heads/main` AND that exchanges to the `msai-gh-oidc` MI AND has the AcrPush role assignment on the registry.
  - Any other principal (service principal, user, fork's Actions token) is rejected at the AAD or RBAC layer.
- **What must never leak:**
  - No Azure client secret in workflow YAML, in repo Secrets, in workflow logs.
  - No long-lived credential of any kind — every CI authentication must be a federated short-lived token.
  - The OIDC tokens themselves are short-lived (≤ 1h) and never logged. `azure/login@v2` masks them by default.
- **What must be auditable:**
  - Every image push appears in the ACR audit log (already enabled via Log Analytics workspace + diagnostic settings on the registry — Slice 4 wires the dashboards; Slice 1 wired the workspace).
  - Every workflow run is visible in `gh run list` and the Actions UI; the OIDC token exchange is visible in Entra sign-in logs filtered by the gh-oidc MI's object ID.
- **Legal / regulatory outcomes:** N/A for paper-trading Phase 1.

## 7. Open Questions

> Questions to resolve in Phase 2 (research) and Phase 3 (design + plan-review)

- [ ] Confirm the exact docker-action cache key shape that gives correct invalidation (e.g., does `cache-to: type=gha,mode=max` need a `scope:` to disambiguate backend vs frontend caches?). Phase 2 research.
- [ ] Confirm whether `docker/login-action@v3` against ACR with OIDC requires the ACR to be in `Premium` SKU or works on `Basic` (slicing verdict picked Basic). If Basic doesn't support OIDC token-based auth, fall back to `az acr login --name <acr> --expose-token` after `azure/login@v2` and feed the token into `docker/login-action`. Phase 2 research.
- [ ] Verify whether two parallel jobs both consuming the same gh-oidc OIDC token cause any AAD throttling or token-cache conflict. If so, run them sequentially or share via `azure/login` once at the workflow level (matrix-style). Phase 2 research.
- [ ] Confirm `${{ github.sha }}` truncation idiom in workflows — `${GITHUB_SHA::7}` in bash, `${{ github.sha }}` doesn't have a substring expression; the idiom is to compute it once in a setup step and emit as `outputs.short_sha`. Phase 3 plan.

## 8. References

- **Discussion log:** [`docs/prds/deploy-pipeline-ci-image-publish-discussion.md`](deploy-pipeline-ci-image-publish-discussion.md)
- **Architecture verdict (locked):** [`docs/decisions/deployment-pipeline-architecture.md`](../decisions/deployment-pipeline-architecture.md)
- **Slicing verdict (locked):** [`docs/decisions/deployment-pipeline-slicing.md`](../decisions/deployment-pipeline-slicing.md)
- **Slice 1 PRD:** [`docs/prds/deploy-pipeline-iac-foundation.md`](deploy-pipeline-iac-foundation.md)
- **Slice 1 plan (with Bicep that this slice extends):** [`docs/plans/2026-05-09-deploy-pipeline-iac-foundation.md`](../plans/2026-05-09-deploy-pipeline-iac-foundation.md)
- **Existing CI workflow (PR validation, NOT this slice):** `.github/workflows/ci.yml`

---

## Appendix A: Revision History

| Version | Date       | Author        | Changes                                                                  |
| ------- | ---------- | ------------- | ------------------------------------------------------------------------ |
| 1.0     | 2026-05-10 | Claude + User | Initial PRD for Slice 2 CI image publish (council-ratified scope locked) |

## Appendix B: Approval

- [ ] Product Owner approval (Pablo)
- [ ] Technical Lead approval (Pablo)
- [ ] Ready for technical design
