# Deployment Pipeline Slice 2: CI Image Publish — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land `.github/workflows/build-and-push.yml` that builds backend + frontend on push-to-main + workflow_dispatch and pushes to ACR with short-SHA tags via OIDC, plus the AcrPush role assignment in `infra/main.bicep`. Fold in the Slice 1 research-brief topic 3 correction (Microsoft-Heartbeat → Microsoft-Syslog).

**Architecture:** Council-ratified Approach A (Slice 2 of 4). New workflow uses `azure/login@v2` (OIDC, no SPN secret), `az acr login --expose-token` to mint a 3-hour ACR token (required because ACR Basic SKU + `docker/login-action` does not natively understand OIDC), `docker/login-action@v3` with sentinel UUID + token, `docker/setup-buildx-action@v3`, `docker/build-push-action@v6` (bumped from PRD's `@v5` per research finding — v5 is two majors stale), with `type=gha` cache scoped per image (`scope=backend`, `scope=frontend`) to avoid mutual cache stomp. One Bicep resource added (AcrPush role assignment from `ghOidcMi` to `acr`) + one variable. **No deploy step — that's Slice 3.**

**Tech Stack:** GitHub Actions (ubuntu-24.04), azure-cli 2.83.0, Bicep CLI 0.43.8, actionlint, Docker buildx, Azure Container Registry Basic.

---

## Approach Comparison

**PRE-DONE — council-ratified.** The 4-slice decomposition (A/B/C) was compared and ratified at [`docs/decisions/deployment-pipeline-slicing.md`](../decisions/deployment-pipeline-slicing.md):

### Chosen Default

**Approach A — 4 incremental PRs.** Slice 2 ships only the CI image-publish workflow + the missing AcrPush role grant. Concretely:

- `.github/workflows/build-and-push.yml` (push-to-main + workflow_dispatch → OIDC → docker build backend + frontend → push to ACR @ `<sha7>` tags)
- One Bicep edit in `infra/main.bicep` (AcrPush on `msai-gh-oidc` MI scoped to `acr`)
- No deploy step

### Best Credible Alternative

**Approach B — vertical slice.** End-to-end deploy in fewer PRs (Bicep + workflow + SSH deploy + KV-render + smoke checks bundled). The Contrarian's preferred shape (CONDITIONAL).

### Scoring (fixed axes)

| Axis                  | Default A   | Alternative B |
| --------------------- | ----------- | ------------- |
| Complexity            | L           | M             |
| Blast Radius          | L           | M             |
| Reversibility         | H (deploy)  | L (deploy)    |
| Time to Validate      | L (≤30 min) | M (hours)     |
| User/Correctness Risk | L           | M             |

### Cheapest Falsifying Test

For Slice 2 specifically: a no-op commit on `main` that triggers the workflow. Acceptance is `gh run view --log` showing `success` plus `az acr repository show-tags --name <acr> --repository msai-{backend,frontend}` listing the short SHA. Fully automated, ~10 min cold cache.

## Contrarian Verdict

**VALIDATE** — The Contrarian's CONDITIONAL on B (vertical slice) was preserved in the slicing verdict as Blocking Objection #4 — Slice 3 cannot merge until full VM deploy path is rehearsed end-to-end. **That objection does not apply to Slice 2**, which is image-publish only and reversible (delete tags, re-tag, re-push). Slice 2 lands without re-firing the gate.

---

## Files

### Created

- **`.github/workflows/build-and-push.yml`** — single workflow file containing 3 jobs (compute-sha, backend, frontend) + workflow-level `permissions: id-token: write, contents: read` + `concurrency: cancel-in-progress: true` per ref + triggers `on: push: branches: [main]` and `workflow_dispatch:`. ~110 lines including comments.

### Modified

- **`infra/main.bicep`** — three edits:
  1. Add `roleDefIdAcrPush` variable in the role-def-vars block (around line 86).
  2. Add `ghOidcAcrPushAssignment` resource after `operatorKvSecretsOfficerAssignment` (around line 593).
  3. Update the comment at line 90 ("Slice 1 declares both; AcrPush role assignment lives in Slice 2.") and the comment block at line 547 (`// 4 total` → `// 5 total`).

- **`docs/research/2026-05-09-deploy-pipeline-iac-foundation.md`** — revise topic 3: replace the bogus `streams: ['Microsoft-Heartbeat']` claim with the validated `kind: 'Linux'` + `Microsoft-Syslog` data source pattern (per memory `feedback_ama_dcr_kind_linux_required.md` and Slice 1 PR #53 fix). Cite the corrected AMA-on-Linux stream enum.

- **`docs/runbooks/vm-setup.md`** — add a new H2 section `## Slice 2 acceptance smoke (10 min)` after the existing Slice 1 section (line 241+). Includes:
  - The 8 GH repo Variables to set + exact `az` command to retrieve each
  - `gh workflow run` and `gh run watch` commands for the smoke
  - `az acr repository show-tags` verification
  - Common failure modes (RBAC propagation 403, missing Variable, AcrPush not yet applied)

- **`tests/infra/test_bicep.sh`** — extend with a grep-assertion for the new `ghOidcAcrPushAssignment` resource (matches the existing string-grep style in the test).

### Build context for Docker

| Image           | Dockerfile path       | Build context   | Build args                                                                          |
| --------------- | --------------------- | --------------- | ----------------------------------------------------------------------------------- |
| `msai-backend`  | `backend/Dockerfile`  | `.` (repo root) | (none — backend reads runtime env only)                                             |
| `msai-frontend` | `frontend/Dockerfile` | `./frontend`    | `NEXT_PUBLIC_AZURE_TENANT_ID`, `NEXT_PUBLIC_AZURE_CLIENT_ID`, `NEXT_PUBLIC_API_URL` |

---

## A note on TDD discipline for this slice

Slice 2 produces a CI workflow + Bicep IaC. Neither has a useful unit-test loop:

- **Bicep:** the "test" is `az bicep build` (lint) and `az deployment group what-if` (dry-run against the live RG). The `tests/infra/test_bicep.sh` script wraps both. Adding a string-grep assertion is the closest analog to a unit test.
- **GH Workflow:** the "test" is `actionlint` (static schema check) and the live workflow run on a no-op commit (Phase 5.4 acceptance). There's no offline "run this workflow" simulator that's worth its setup cost for one workflow file.

So the discipline here is: **edit → static validate (actionlint or `az bicep build`) → push → run live → confirm**. This is honest TDD substitution, not an excuse to skip rigor.

---

## Tasks

### Task 1: Add AcrPush role assignment to infra/main.bicep

**Files:**

- Modify: `infra/main.bicep` (3 small edits)
- Modify: `tests/infra/test_bicep.sh` (extend grep-assertions)

**Why this task first:** It's the smallest change, exercises the local Bicep tooling, and unblocks the workflow (without it, `docker push` returns 403). T2/T3/T4 don't depend on this task running, but it's the natural starting point.

- [ ] **Step 1: Add the role-definition variable.** Find the existing role-def variable block in `infra/main.bicep` (around line 83-86). It currently has 4 vars (`roleDefIdKvSecretsUser`, `roleDefIdKvSecretsOfficer`, `roleDefIdAcrPull`, `roleDefIdBlobContributor`). Insert a new line after `roleDefIdAcrPull`:

```bicep
var roleDefIdAcrPush = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8311e382-0749-4cb8-b61a-304f252e45ec')
```

The GUID `8311e382-0749-4cb8-b61a-304f252e45ec` is the Azure built-in **AcrPush** role (verified in research brief topic 8: [MS Learn — Built-in roles for Containers](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/containers)). AcrPush implies AcrPull, so the gh-oidc MI gets pull-during-build for free.

- [ ] **Step 2: Update the Slice 1 forward-reference comment.** Find the comment at `infra/main.bicep:90`:

```bicep
// T8 (declared early): GH OIDC user-assigned managed identity + federated credential.
// Slice 1 declares both; AcrPush role assignment lives in Slice 2.
```

Replace with:

```bicep
// T8 (declared early): GH OIDC user-assigned managed identity + federated credential.
// Slice 1 declared both; Slice 2 added the AcrPush role assignment below (`ghOidcAcrPushAssignment`).
```

- [ ] **Step 3: Update the role-assignments section header.** Find the comment block at `infra/main.bicep:546-550`:

```bicep
// ─────────────────────────────────────────────────────────────────────────────
// T8 (continued): Role assignments
// 4 total: VM gets KV Secrets User + AcrPull + Blob Contributor (3 runtime grants),
// operator gets KV Secrets Officer (1 data-plane grant for seeding/rotating secrets).
// ─────────────────────────────────────────────────────────────────────────────
```

Replace with:

```bicep
// ─────────────────────────────────────────────────────────────────────────────
// T8 (continued): Role assignments
// 5 total: VM gets KV Secrets User + AcrPull + Blob Contributor (3 runtime grants),
// operator gets KV Secrets Officer (1 data-plane grant for seeding/rotating secrets),
// gh-oidc MI gets AcrPush (Slice 2 — CI image push from GitHub Actions).
// ─────────────────────────────────────────────────────────────────────────────
```

- [ ] **Step 4: Add the AcrPush role assignment resource.** Find `operatorKvSecretsOfficerAssignment` at `infra/main.bicep:585-593`. Append the following resource block immediately after its closing `}` (around line 594, before the Outputs comment block):

```bicep
resource ghOidcAcrPushAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(ghOidcMi.id, acr.id, 'acr-push')
  properties: {
    principalId: ghOidcMi.properties.principalId
    roleDefinitionId: roleDefIdAcrPush
    principalType: 'ServicePrincipal'
    description: 'Slice 2: GitHub Actions OIDC user-assigned MI pushes images to ACR via .github/workflows/build-and-push.yml'
  }
}
```

Pattern matches `vmAcrPullAssignment` at line 563 exactly (same scope/name/principalType pattern). `principalType: 'ServicePrincipal'` is required to dodge the well-known RBAC eventual-consistency 503 on user-assigned MI principals (research brief topic 9).

- [ ] **Step 5: Run `az bicep build` to verify syntax.**

```bash
cd /Users/pablomarin/Code/msai-v2/.worktrees/deploy-pipeline-ci-image-publish
az bicep build --file infra/main.bicep --stdout >/dev/null
```

Expected: silent (exit 0) — no warnings, no errors. If it fails, look at the line numbers in the error message and fix the indentation / brace mismatch. Common failure: stray comma after the last property in the role-assignment block.

- [ ] **Step 6: Extend tests/infra/test_bicep.sh with a grep-assertion for the new resource.**

Find the script and append after the `az bicep build` block:

```bash
echo "=== Slice 2 grep assertions ==="

# AcrPush role assignment exists for the gh-oidc MI
grep -q "resource ghOidcAcrPushAssignment 'Microsoft.Authorization/roleAssignments" infra/main.bicep \
  || { echo "FAIL: ghOidcAcrPushAssignment resource missing in infra/main.bicep" >&2; exit 1; }

grep -q "roleDefinitionId: roleDefIdAcrPush" infra/main.bicep \
  || { echo "FAIL: AcrPush role-def reference missing in role-assignment block" >&2; exit 1; }

grep -q "var roleDefIdAcrPush = subscriptionResourceId" infra/main.bicep \
  || { echo "FAIL: roleDefIdAcrPush variable missing" >&2; exit 1; }

echo "Slice 2 grep assertions clean."
```

Insert this **before** the `if [[ "${SKIP_WHATIF:-}" == "1" ]]` block so the assertions run even when `SKIP_WHATIF=1`.

- [ ] **Step 7: Run the test script locally.**

```bash
SKIP_WHATIF=1 bash tests/infra/test_bicep.sh
```

Expected output ending with: `Slice 2 grep assertions clean.` and exit 0.

- [ ] **Step 8: Commit.**

```bash
git add infra/main.bicep tests/infra/test_bicep.sh
git commit -m "feat(slice2): add AcrPush role assignment for gh-oidc MI on ACR"
```

---

### Task 2: Revise Slice 1 research brief — topic 3 (Microsoft-Heartbeat correction)

**Files:**

- Modify: `docs/research/2026-05-09-deploy-pipeline-iac-foundation.md` (topic 3 only)

**Why this task:** Slice 1's research-brief topic 3 stated `streams: ['Microsoft-Heartbeat']` as a valid AMA stream — but `Microsoft-Heartbeat` does not exist in the AMA stream enum. That bug surfaced at Slice 1 acceptance smoke (PR #53, 2026-05-10) and was fixed in `infra/main.bicep` by switching to `kind: 'Linux'` + `Microsoft-Syslog`. The brief itself was never updated. The PRD US-001 acceptance criterion explicitly requires the brief correction to ship in this PR. Memory feedback `feedback_ama_dcr_kind_linux_required.md` is the durable record.

- [ ] **Step 1: Locate topic 3 in the Slice 1 brief.**

```bash
cd /Users/pablomarin/Code/msai-v2/.worktrees/deploy-pipeline-ci-image-publish
grep -n "^### " docs/research/2026-05-09-deploy-pipeline-iac-foundation.md | head -20
grep -n "Microsoft-Heartbeat\|Heartbeat" docs/research/2026-05-09-deploy-pipeline-iac-foundation.md
```

Expected: shows the H3 heading for topic 3 (likely "AMA / Linux Monitoring Agent" or similar) and the line numbers where `Microsoft-Heartbeat` appears.

- [ ] **Step 2: Read the current topic 3 content** to see exactly what claims need revision.

```bash
sed -n '/^### .*[Aa]gent\|### .*AMA/,/^### /p' docs/research/2026-05-09-deploy-pipeline-iac-foundation.md
```

(Or just open the file and find topic 3.)

- [ ] **Step 3: Apply two concrete Edits to topic 3.** The bogus claims are at exact lines **112** and **113** of the brief (verified). Use the Edit tool with the following old_string / new_string pairs:

**Edit 3a (replaces the bullet 3 finding at line 112):**

`old_string`:

```
3. **AMA capabilities for our use case (heartbeat + syslog):** AMA covers heartbeat natively (via DCR with `Microsoft-Heartbeat` data flow) and syslog natively (Linux syslog facilities → DCR). Optional perf counters (`Microsoft-Perf` data flow) are also available; we only enable heartbeat in Slice 1. Source: [MS Learn — AMA install/manage §Configure](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-manage) (2026-02-18).
```

`new_string`:

```
3. **AMA capabilities for our use case (heartbeat + syslog):** AMA covers syslog natively (Linux syslog facilities → DCR). Heartbeat is **emitted automatically** by AMA once associated with any valid `kind: 'Linux'` DCR with at least one valid built-in data source — there is **NO `Microsoft-Heartbeat` stream**; that name does not exist in the AMA stream enum. The valid AMA stream enum is: `Microsoft-Event` (Windows event logs), `Microsoft-InsightsMetrics`, `Microsoft-Perf`, `Microsoft-Syslog` (Linux syslog), `Microsoft-CommonSecurityLog`, `Microsoft-W3CIISLog`, `Microsoft-PrometheusMetrics`. Optional perf counters (`Microsoft-Perf` data flow) are available; we only need Heartbeat in Slice 1, achieved implicitly via a minimal Syslog data source. Source: [MS Learn — DCR structure](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/data-collection-rule-structure) (accessed 2026-05-10) — see the "Valid data source types" table.
```

**Edit 3b (replaces the bullet 4 finding at line 113):**

`old_string`:

```
4. **Minimum viable DCR for heartbeat-only** is a single DCR resource with `dataSources: { extensions: [...] }` empty and `dataFlows: [{ streams: ["Microsoft-Heartbeat"], destinations: ["msaiLogAnalytics"] }]`, plus a `dataCollectionRuleAssociation` linking it to the VM. We do **NOT** need the `AgentSettings` DCR (that's for agent-cache-size tuning, currently preview-only and Resource-Manager-only).
```

`new_string`:

```
4. **Minimum viable DCR for Linux VM heartbeat** is a single DCR resource with `kind: 'Linux'` and `dataSources.syslog: [{ name: 'syslogBase', streams: ['Microsoft-Syslog'], facilityNames: [...], logLevels: [...] }]` plus the matching `dataFlows: [{ streams: ['Microsoft-Syslog'], destinations: [...] }]` and a `Microsoft.Insights/dataCollectionRuleAssociations` linking the DCR to the VM. **Heartbeat then flows automatically** — it is implicit, not declared as a stream. (Empirically verified at Slice 1 acceptance smoke / PR #53 2026-05-10: a `kind`-less DCR with declared stream `Microsoft-Heartbeat` returned AMA MCS endpoint 404s and zero Heartbeat flow; switching to `kind: 'Linux'` + Syslog restored Heartbeat within 15 min. AMA's negative-cache requires `sudo systemctl restart azuremonitoragent` after an in-place DCR change — not relevant on first-boot provisioning.) We do **NOT** need the `AgentSettings` DCR (that's for agent-cache-size tuning, currently preview-only and Resource-Manager-only).
```

**Edit 3c (extend the Sources block at lines 116-121 to add the DCR-structure citation):**

Locate the Sources block under topic 3 (lines 116-121, ending just before "**Design impact:**"). Append a new source as the next-numbered item:

`old_string`:

```
3. [MS Learn — Prepare for retirement of Log Analytics agent (Defender for Cloud)](https://learn.microsoft.com/en-us/azure/defender-for-cloud/prepare-deprecation-log-analytics-mma-agent) — accessed 2026-05-09
4. [Windows Forum — MS Pauses Legacy MMA Uploads](https://windowsforum.com/threads/microsoft-pauses-legacy-mma-uploads-for-12-hours-ahead-of-ama-migration.398937/) — accessed 2026-05-09
```

`new_string`:

```
3. [MS Learn — Prepare for retirement of Log Analytics agent (Defender for Cloud)](https://learn.microsoft.com/en-us/azure/defender-for-cloud/prepare-deprecation-log-analytics-mma-agent) — accessed 2026-05-09
4. [Windows Forum — MS Pauses Legacy MMA Uploads](https://windowsforum.com/threads/microsoft-pauses-legacy-mma-uploads-for-12-hours-ahead-of-ama-migration.398937/) — accessed 2026-05-09
5. **Correction (2026-05-10):** [MS Learn — Structure of a data collection rule (DCR) in Azure Monitor](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/data-collection-rule-structure) — canonical AMA stream-enum reference; confirms `Microsoft-Heartbeat` is **not** in the enum. See [`docs/research/2026-05-10-deploy-pipeline-ci-image-publish.md`](2026-05-10-deploy-pipeline-ci-image-publish.md) topics 11 + 12 for the full correction context. The Slice 2 PR (`feat/deploy-pipeline-ci-image-publish`) folds this correction in per `feedback_ama_dcr_kind_linux_required.md` in user MEMORY.
```

These three Edits collectively replace the bogus claims while preserving the rest of the topic-3 structure (Findings / Sources / Design impact / Test implication / Open risks), so other Slice 1 readers' references to topic 3 stay valid.

- [ ] **Step 4: Verify the corrected brief no longer claims Microsoft-Heartbeat is a stream.**

```bash
grep -n "Microsoft-Heartbeat" docs/research/2026-05-09-deploy-pipeline-iac-foundation.md
```

Expected: any remaining mentions are negative ("NOT a valid stream", "is NOT in the enum") — every occurrence either says it's wrong or is part of a quote of a prior incorrect assertion that's now contextualized as "what was thought before." Zero positive assertions of Microsoft-Heartbeat as a usable stream should remain.

- [ ] **Step 5: Commit.**

```bash
git add docs/research/2026-05-09-deploy-pipeline-iac-foundation.md
git commit -m "docs(research): correct Slice 1 brief topic 3 — Microsoft-Heartbeat is not a valid AMA stream"
```

---

### Task 3: Create the GitHub Actions workflow

**Files:**

- Create: `.github/workflows/build-and-push.yml`

**Why this is one Write step (not appended in pieces):** the file is a single declarative document. The original draft of this plan tried to append snippets (skeleton / backend job / frontend job) — but markdown formatters strip leading whitespace inside fenced code blocks, so the snippets ended up unindented and would have made `backend:` and `frontend:` invalid top-level workflow keys when copy-pasted (P1 finding from plan-review iter 1, Codex). Single Write of the complete file avoids the foot-gun.

**Important context:**

- Frontend Dockerfile has fail-fast guards on each `NEXT_PUBLIC_*` ARG (exit 1 if empty). Backend Dockerfile takes no build-args.
- `az acr login --name` expects the **short** registry name (e.g., `msaiacrXXX`), NOT the FQDN. This was a mismatch between the FQDN-only `vars.ACR_LOGIN_SERVER` and the `az` CLI's expectation. Resolved by adding a separate `vars.ACR_NAME` (operator sets both; runbook in Task 4 documents the retrieval).
- `docker/login-action.registry` and image tags use the FQDN (`vars.ACR_LOGIN_SERVER`).

- [ ] **Step 1: Create the complete workflow file.**

Use the Write tool to create `.github/workflows/build-and-push.yml` with the following exact content:

```yaml
name: Build and Push Images

# Slice 2 of 4 — see docs/decisions/deployment-pipeline-slicing.md.
# Builds backend + frontend Docker images on push-to-main (or manual dispatch),
# pushes to Azure Container Registry tagged with the short git SHA only.
# No deploy step — that's Slice 3.

on:
  push:
    branches: [main]
  workflow_dispatch:

# OIDC token exchange + checkout. No secrets needed.
permissions:
  id-token: write
  contents: read

# Newer commits on main supersede in-progress builds. Slice 3's deploy
# workflow will use cancel-in-progress: false instead.
concurrency:
  group: build-and-push-${{ github.ref }}
  cancel-in-progress: true

jobs:
  compute-sha:
    name: Compute short SHA
    runs-on: ubuntu-24.04
    outputs:
      sha: ${{ steps.short.outputs.short_sha }}
    steps:
      - id: short
        # GitHub Actions context expressions don't support substring; do it in bash.
        # See research brief topic 6.
        run: echo "short_sha=${GITHUB_SHA::7}" >> "$GITHUB_OUTPUT"

  backend:
    name: Build & push msai-backend
    needs: compute-sha
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4.2.2

      - name: Azure login (OIDC)
        uses: azure/login@v2
        with:
          client-id: ${{ vars.AZURE_CLIENT_ID }}
          tenant-id: ${{ vars.AZURE_TENANT_ID }}
          subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}

      - name: Get ACR access token
        id: acr-token
        # ACR Basic SKU + docker/login-action does not natively understand OIDC;
        # the canonical path is `az acr login --expose-token` to mint a 3-hour
        # ACR access token (research brief topic 2). The username is a documented
        # sentinel UUID; the password is the access token.
        # IMPORTANT: `az acr login --name` expects the SHORT registry name
        # (e.g. msaiacrXXX), NOT the FQDN — vars.ACR_NAME is the short name;
        # vars.ACR_LOGIN_SERVER is the FQDN used elsewhere.
        run: |
          token=$(az acr login --name "${{ vars.ACR_NAME }}" --expose-token --output tsv --query accessToken)
          echo "::add-mask::$token"
          echo "token=$token" >> "$GITHUB_OUTPUT"

      - name: Docker login to ACR
        uses: docker/login-action@v3
        with:
          registry: ${{ vars.ACR_LOGIN_SERVER }}
          username: 00000000-0000-0000-0000-000000000000
          password: ${{ steps.acr-token.outputs.token }}

      - uses: docker/setup-buildx-action@v3

      - name: Build & push msai-backend
        uses: docker/build-push-action@v6
        with:
          context: .
          file: backend/Dockerfile
          push: true
          tags: ${{ vars.ACR_LOGIN_SERVER }}/msai-backend:${{ needs.compute-sha.outputs.sha }}
          # Distinct cache scopes per image — without this, parallel builds
          # overwrite each other's cache (research brief topic 3).
          cache-from: type=gha,scope=backend
          cache-to: type=gha,mode=max,scope=backend

  frontend:
    name: Build & push msai-frontend
    needs: compute-sha
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4.2.2

      - name: Azure login (OIDC)
        uses: azure/login@v2
        with:
          client-id: ${{ vars.AZURE_CLIENT_ID }}
          tenant-id: ${{ vars.AZURE_TENANT_ID }}
          subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}

      - name: Get ACR access token
        id: acr-token
        run: |
          token=$(az acr login --name "${{ vars.ACR_NAME }}" --expose-token --output tsv --query accessToken)
          echo "::add-mask::$token"
          echo "token=$token" >> "$GITHUB_OUTPUT"

      - name: Docker login to ACR
        uses: docker/login-action@v3
        with:
          registry: ${{ vars.ACR_LOGIN_SERVER }}
          username: 00000000-0000-0000-0000-000000000000
          password: ${{ steps.acr-token.outputs.token }}

      - uses: docker/setup-buildx-action@v3

      - name: Build & push msai-frontend
        uses: docker/build-push-action@v6
        with:
          context: ./frontend
          file: frontend/Dockerfile
          push: true
          tags: ${{ vars.ACR_LOGIN_SERVER }}/msai-frontend:${{ needs.compute-sha.outputs.sha }}
          # NEXT_PUBLIC_* are baked into the JS bundle at build time; the frontend
          # Dockerfile has fail-fast guards on each (`exit 1` if empty).
          # Values come from repo Variables — see docs/runbooks/vm-setup.md.
          build-args: |
            NEXT_PUBLIC_AZURE_TENANT_ID=${{ vars.NEXT_PUBLIC_AZURE_TENANT_ID }}
            NEXT_PUBLIC_AZURE_CLIENT_ID=${{ vars.NEXT_PUBLIC_AZURE_CLIENT_ID }}
            NEXT_PUBLIC_API_URL=${{ vars.NEXT_PUBLIC_API_URL }}
          cache-from: type=gha,scope=frontend
          cache-to: type=gha,mode=max,scope=frontend
```

- [ ] **Step 2: Run actionlint on the complete file.**

```bash
actionlint .github/workflows/build-and-push.yml
```

Expected: silent (exit 0). actionlint may emit "could not find variable" hints for `vars.*` it doesn't have a config-defined values list for — that's a missing-context warning, NOT a hard error, not actionable here (the values come from GitHub repo settings, not the workflow file). If it reports any real schema errors (unknown keys, malformed YAML), fix them before continuing.

- [ ] **Step 3: Sanity-check that no secret-typed values appear in the workflow.**

```bash
grep -E "AZURE_CREDENTIALS|client_secret|client-secret|ACR_PASSWORD|secrets\.AZURE" .github/workflows/build-and-push.yml
```

Expected: zero matches. The only authentication is OIDC + the ephemeral ACR token from `az acr login --expose-token`. If anything matches, you've imported a service-principal-secret pattern by mistake.

- [ ] **Step 4: Confirm `environment:` does not appear** (would break OIDC subject claim per research brief topic 5).

```bash
grep -nE "^\s*environment:" .github/workflows/build-and-push.yml
```

Expected: zero matches.

- [ ] **Step 5: Confirm both jobs use distinct GHA cache scopes.**

```bash
grep -E "scope=(backend|frontend)" .github/workflows/build-and-push.yml | sort -u
```

Expected exactly 4 lines (research brief topic 3 — without distinct scopes, second job's cache silently overwrites the first):

```
          cache-from: type=gha,scope=backend
          cache-from: type=gha,scope=frontend
          cache-to: type=gha,mode=max,scope=backend
          cache-to: type=gha,mode=max,scope=frontend
```

- [ ] **Step 6: Commit.**

```bash
git add .github/workflows/build-and-push.yml
git commit -m "feat(slice2): add build-and-push workflow — OIDC → ACR with short-SHA tags"
```

---

### Task 4: Add Slice 2 acceptance smoke + GH Variables setup to runbook

**Files:**

- Modify: `docs/runbooks/vm-setup.md`

**Why this is mandatory:** the workflow won't run successfully on first push unless the operator has set the 8 GH repo Variables AND re-applied `infra/main.bicep` to grant AcrPush. Both are operator-side steps that the workflow itself can't perform. The runbook needs to enumerate them with copy-pasteable commands.

- [ ] **Step 1: Locate the insertion point.** The runbook has `## Slice 1 acceptance smoke (15 min)` starting at line 241, followed by `### If something fails` at line 325 — that H3 is part of Slice 1's troubleshooting and ends the file. **Do NOT insert Slice 2 between Slice 1's content and Slice 1's `### If something fails`** — that would reparent Slice 1 troubleshooting under Slice 2 (P3 finding from Codex iter-2). Instead, append Slice 2 at the very end of the file (after the last line of Slice 1's troubleshooting block).

```bash
cd /Users/pablomarin/Code/msai-v2/.worktrees/deploy-pipeline-ci-image-publish
grep -n "^### If something fails\|^## Slice " docs/runbooks/vm-setup.md
wc -l docs/runbooks/vm-setup.md
tail -5 docs/runbooks/vm-setup.md
```

Confirm the line numbers and that the last line is part of Slice 1's "If something fails" block.

- [ ] **Step 2: Append the Slice 2 section at the end of the file.** Use Read on the last few lines of `docs/runbooks/vm-setup.md` to capture the exact final paragraph, then Edit to insert the new section immediately after it (so Slice 2 becomes a sibling H2 of Slice 1, with its own scope and its own troubleshooting subsection).

The new section to insert:

````markdown
## Slice 2 acceptance smoke (10 min)

After merging Slice 2, the workflow `.github/workflows/build-and-push.yml` runs on every push to `main` and on manual `workflow_dispatch`. **Before the first run can succeed**, two operator-side steps are required:

### Step 1 — Re-apply Bicep to grant AcrPush

Slice 2 adds one Bicep resource (`ghOidcAcrPushAssignment`) granting AcrPush on the ACR to the `msai-gh-oidc` user-assigned MI. Re-run the deploy script:

```bash
cd /Users/pablomarin/Code/msai-v2
./scripts/deploy-azure.sh --operator-ip "$(curl -s ifconfig.me)" \
  --ssh-public-key-file ~/.ssh/id_ed25519.pub
```

Verify the role assignment landed:

```bash
GHOIDC_PRINCIPAL=$(az identity show -g msaiv2_rg -n msai-gh-oidc --query principalId -o tsv)
ACR_ID=$(az acr show -g msaiv2_rg --name "$(az acr list -g msaiv2_rg --query '[0].name' -o tsv)" --query id -o tsv)
az role assignment list --scope "$ACR_ID" --assignee "$GHOIDC_PRINCIPAL" \
  --query "[?roleDefinitionName=='AcrPush']" -o table
# Expect: one row with roleDefinitionName=AcrPush, principalType=ServicePrincipal
```

### Step 2 — Set GitHub repo Variables

The workflow reads **8 public values** from `${{ vars.* }}` (NOT `${{ secrets.* }}` — none of these are secrets). Set them in the repo: **Settings → Secrets and variables → Actions → Variables tab → "New repository variable"**.

> Why `ACR_NAME` AND `ACR_LOGIN_SERVER` (both): `az acr login --name` expects the **short** registry name (`msaiacrXXX`), while `docker/login-action.registry` and image tags use the **FQDN** (`msaiacrXXX.azurecr.io`). Operator sets both; runtime split avoids string-manipulation in YAML.

```bash
# Retrieve the runtime values
ACR_LOGIN_SERVER=$(az deployment group show -g msaiv2_rg -n main \
  --query 'properties.outputs.acrLoginServer.value' -o tsv)
ACR_NAME=$(az acr list -g msaiv2_rg --query '[0].name' -o tsv)
GH_OIDC_CLIENT_ID=$(az deployment group show -g msaiv2_rg -n main \
  --query 'properties.outputs.ghOidcClientId.value' -o tsv)

echo "Set these GitHub repo Variables (Settings → Secrets and variables → Actions → Variables):"
echo "  AZURE_TENANT_ID             = 2237d332-fc65-4994-b676-61edad7be319"
echo "  AZURE_SUBSCRIPTION_ID       = 68067b9b-943f-4461-8cb5-2bc97cbc462d"
echo "  AZURE_CLIENT_ID             = $GH_OIDC_CLIENT_ID"
echo "  ACR_NAME                    = $ACR_NAME"
echo "  ACR_LOGIN_SERVER            = $ACR_LOGIN_SERVER"
echo "  NEXT_PUBLIC_AZURE_TENANT_ID = 2237d332-fc65-4994-b676-61edad7be319"
echo "  NEXT_PUBLIC_AZURE_CLIENT_ID = <frontend Entra SPA app reg's client ID — separate AAD app from AZURE_CLIENT_ID, which is the gh-oidc MI>"
echo "  NEXT_PUBLIC_API_URL         = <production API base URL; placeholder until Slice 3 sets DNS, e.g. http://<vmPublicIp>:8000>"
```

Or via `gh` CLI (one command per var) — **all 8 must be set**, or the frontend Dockerfile fail-fast guards (`exit 1` on empty `NEXT_PUBLIC_*` ARG) kill the build:

```bash
gh variable set AZURE_TENANT_ID             --body "2237d332-fc65-4994-b676-61edad7be319"
gh variable set AZURE_SUBSCRIPTION_ID       --body "68067b9b-943f-4461-8cb5-2bc97cbc462d"
gh variable set AZURE_CLIENT_ID             --body "$GH_OIDC_CLIENT_ID"
gh variable set ACR_NAME                    --body "$ACR_NAME"
gh variable set ACR_LOGIN_SERVER            --body "$ACR_LOGIN_SERVER"
gh variable set NEXT_PUBLIC_AZURE_TENANT_ID --body "2237d332-fc65-4994-b676-61edad7be319"

# Replace these two with real values (or unblocking placeholders for the Slice 2 smoke):
gh variable set NEXT_PUBLIC_AZURE_CLIENT_ID --body "<your-frontend-entra-app-client-id>"
gh variable set NEXT_PUBLIC_API_URL         --body "http://placeholder.invalid"  # Slice 3 sets the real DNS

# Verify all 8 are set:
gh variable list --json name --jq '.[].name' | sort
# Expect (alphabetized):
#   ACR_LOGIN_SERVER
#   ACR_NAME
#   AZURE_CLIENT_ID
#   AZURE_SUBSCRIPTION_ID
#   AZURE_TENANT_ID
#   NEXT_PUBLIC_API_URL
#   NEXT_PUBLIC_AZURE_CLIENT_ID
#   NEXT_PUBLIC_AZURE_TENANT_ID
```

### Step 3 — Run the workflow

After waiting ~60 seconds for RBAC propagation (research brief topic 10), trigger the workflow manually:

```bash
gh workflow run build-and-push.yml
gh run watch                          # streams the latest run's logs
```

Or push a no-op commit to main to trigger the `push:` event:

```bash
# (only on main, after Slice 2 PR merges)
git commit --allow-empty -m "chore: Slice 2 acceptance smoke"
git push
```

### Step 4 — Verify images in ACR

```bash
ACR_NAME=$(az acr list -g msaiv2_rg --query '[0].name' -o tsv)
SHORT_SHA=$(git rev-parse --short HEAD)

az acr repository show-tags --name "$ACR_NAME" --repository msai-backend -o tsv | grep -q "^${SHORT_SHA}$" \
  && echo "✓ msai-backend:${SHORT_SHA} present" \
  || echo "✗ msai-backend:${SHORT_SHA} MISSING"

az acr repository show-tags --name "$ACR_NAME" --repository msai-frontend -o tsv | grep -q "^${SHORT_SHA}$" \
  && echo "✓ msai-frontend:${SHORT_SHA} present" \
  || echo "✗ msai-frontend:${SHORT_SHA} MISSING"
```

Both ✓ marks = acceptance pass.

### Common failure modes

- **`az acr login --expose-token` returns 403 `AuthenticationFailed`:** AcrPush role-assignment hasn't propagated. Wait 60 seconds and re-run `gh workflow run build-and-push.yml`. If it persists more than 5 min, double-check Step 1's `az role assignment list` query — the role assignment may not have actually landed (Bicep diff didn't include it, or the deploy script wasn't re-run after merging Slice 2).
- **Frontend job fails with `ERROR: --build-arg NEXT_PUBLIC_AZURE_CLIENT_ID is required (currently empty)`:** the GH repo Variable is unset or empty. Re-check `gh variable list` and set it.
- **`azure/login@v2` step fails with `AADSTS70021: No matching federated identity record found`:** the federated credential subject doesn't match the workflow's OIDC token subject. Confirm `infra/main.bicep` line 104 still says `repo:${repoOwner}/${repoName}:ref:refs/heads/${repoBranch}` and that the workflow ran on a `push: main` or `workflow_dispatch:` from main (not a feature branch via the Actions UI).
- **Workflow runs but only one image gets cached (slow rebuilds):** the `scope=backend` / `scope=frontend` cache disambiguation got removed. Without it, the second job's cache silently overwrites the first. Re-grep the workflow for `scope=`.
- **Two runs land within seconds and one is `cancelled`:** that's the `concurrency: cancel-in-progress: true` block working as intended. Newer commits on main supersede in-progress builds.
````

- [ ] **Step 3: Verify the runbook still parses cleanly.**

````bash
# Smoke check: H2 / H3 structure intact, no orphan code-fences
grep -cE '^(##|###) ' docs/runbooks/vm-setup.md   # should be > before-count
grep -cE '^```' docs/runbooks/vm-setup.md         # should be even (every fence closed)
````

If the second count is odd, you've left an unclosed code fence. Fix it.

- [ ] **Step 4: Commit.**

```bash
git add docs/runbooks/vm-setup.md
git commit -m "docs(runbook): Slice 2 acceptance smoke + GH Variables setup"
```

---

## Dispatch Plan

**Sequential mode** — small slice, all 4 tasks total < 30 min of work, file-disjoint enough that parallelism would save < 2 minutes.

| Task ID | Depends on | Writes (concrete file paths)                                 |
| ------- | ---------- | ------------------------------------------------------------ |
| T1      | —          | `infra/main.bicep`, `tests/infra/test_bicep.sh`              |
| T2      | —          | `docs/research/2026-05-09-deploy-pipeline-iac-foundation.md` |
| T3      | —          | `.github/workflows/build-and-push.yml`                       |
| T4      | T1, T3     | `docs/runbooks/vm-setup.md`                                  |

T4's `Depends on` is conceptual (T4 documents files T1 + T3 produced) — both file paths are fixed in advance, so T4 could run in parallel with them. But sequential is simpler and the time savings don't justify dispatch-plan complexity.

**Order:** T1 → T2 → T3 → T4. Single subagent at a time.

---

## Phase 5 — Quality Gates (will run after Task 4)

| Gate                            | Command                                                                                                                                                                                                                                                                                               | Expected                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bicep static                    | `SKIP_WHATIF=1 bash tests/infra/test_bicep.sh`                                                                                                                                                                                                                                                        | Lint clean + Slice 2 grep-assertions pass                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| Bicep what-if (Pablo's machine) | `bash tests/infra/test_bicep.sh` (no SKIP_WHATIF), then `jq -r '.changes[] \| select(.changeType == "Create") \| select(.resourceId \| test("/roleAssignments/")) \| (.after.properties.roleDefinitionId // "") + " " + .resourceId' /tmp/whatif.json \| grep "8311e382-0749-4cb8-b61a-304f252e45ec"` | `test_bicep.sh` exits 0 (script's contract: only fails on `Delete`; tolerates `Modify` from Azure-added defaults). The `jq` post-check filters Create entries to role assignments and matches against the AcrPush role-def GUID `8311e382-0749-4cb8-b61a-304f252e45ec` (NOT against the literal string `acr-push` — `guid()` produces an opaque UUID for the assignment's resourceId). On a fresh-not-yet-applied RG: prints exactly one line. After Slice 2 has been applied: prints zero lines (idempotency). |
| Workflow lint                   | `actionlint .github/workflows/build-and-push.yml`                                                                                                                                                                                                                                                     | Silent exit 0                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| No-secret check                 | `grep -E "AZURE_CREDENTIALS\|client_secret\|ACR_PASSWORD" .github/workflows/build-and-push.yml`                                                                                                                                                                                                       | Zero matches                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| No-environment check            | `grep -nE "^\s*environment:" .github/workflows/build-and-push.yml`                                                                                                                                                                                                                                    | Zero matches                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Acceptance smoke                | After merge: re-apply Bicep, set GH Variables, `gh workflow run build-and-push.yml`                                                                                                                                                                                                                   | Run concludes `success`; both `msai-backend:<sha7>` and `msai-frontend:<sha7>` present in ACR; ≤ 10 min                                                                                                                                                                                                                                                                                                                                                                                                         |

---

## Phase 5.4 E2E Use Cases

Slice 2 is **CI infrastructure** — it has no in-process API, no UI, no CLI flag. The "user-facing behavior" is the workflow's run conclusion + the resulting tags in ACR. The relevant `verify-e2e` use cases are operator-driven smoke tests; they exercise the same path as the runbook's Slice 2 acceptance smoke.

### UC-S2-001: Operator triggers workflow_dispatch and images appear in ACR

**Interface:** CLI (`gh`, `az`) — no API or UI to test.

**Setup:** Slice 2 PR merged on main; operator has re-applied Bicep (Task 4 Step 1); all 8 GH repo Variables (incl. `ACR_NAME`) set (Task 4 Step 2); ≥ 60s elapsed since deploy completed.

**Steps:**

1. `gh workflow run build-and-push.yml` (from the operator's machine, current branch = main)
2. `gh run watch` and capture the run ID

**Verification:**

1. Run conclusion = `success` (`gh run view <run-id> --json conclusion -q .conclusion` returns `"success"`)
2. `az acr repository show-tags --name <acr> --repository msai-backend -o tsv` lists `<sha7>` for the run's `${{ github.sha }}`
3. Same for `msai-frontend`
4. No `AZURE_CREDENTIALS` / `client_secret` appear in the workflow run logs

**Persistence:** Re-running `gh workflow run` on the same `main` HEAD produces the same image digests (cache hits). `az acr manifest list-metadata --name <acr>:msai-backend:<sha7>` shows the same digest both runs.

### UC-S2-002: Push-to-main trigger fires automatically

**Interface:** CLI + git.

**Setup:** Same as UC-S2-001.

**Steps:**

1. On `main`, `git commit --allow-empty -m "chore: Slice 2 trigger smoke"; git push origin main`
2. Wait ~10 seconds for GitHub to schedule the run
3. `gh run list --workflow build-and-push.yml --limit 1`

**Verification:**

1. The most-recent run's `event` column = `push`
2. Run conclusion = `success`
3. Images present in ACR with the new commit's `<sha7>`

**Persistence:** After the run completes, the new tags are immutable in ACR — `az acr repository show-tags` continues to list them.

### UC-S2-003: Workflow refuses non-main triggers

**Interface:** CLI.

**Setup:** Operator on a feature branch (not `main`).

**Steps:**

1. `git push origin feature/foo`
2. `gh run list --workflow build-and-push.yml --limit 1` — confirm no new run was triggered for the push event

**Verification:**

1. No workflow run created (the `push:` filter restricts to `branches: [main]`)
2. Even attempting `gh workflow run build-and-push.yml --ref feature/foo` succeeds in scheduling the run BUT the `azure/login@v2` step fails with `AADSTS70021` because the OIDC subject doesn't match the federated credential bound to `refs/heads/main`

**Persistence:** No tags written to ACR for non-main commits.

### Out of scope for Slice 2 E2E

- Image _correctness_ (does the backend image start? does the frontend serve?) — **Slice 3** verifies via `up -d --wait` + `/health` + `/ready` curls. Slice 2 only proves images are _publishable_.
- DNS / reverse proxy / TLS — **Slice 3+**.
- Image scanning / signature verification — Slice 4 ops if at all.

---

## Self-Review

**1. Spec coverage.** Skim each PRD section vs the plan:

- ✓ US-001 (push-to-main produces images) — Task 3 (workflow) + Task 1 (AcrPush)
- ✓ US-002 (workflow_dispatch) — Task 3 Step 1 (`workflow_dispatch:` in `on:` block)
- ✓ US-003 (AcrPush in IaC, not portal) — Task 1
- ✓ Topic 3 research correction — Task 2
- ✓ All all 8 GH repo Variables (incl. `ACR_NAME`) documented — Task 4 Step 2
- ✓ All non-goals respected — no deploy step, no `latest` tag, no PR-trigger, no multi-arch, no scanning, no environment promotion
- ✓ Build args match the frontend Dockerfile contract (3 NEXT*PUBLIC*\* with fail-fast) — Task 3 Step 5
- ✓ Backend takes no build-args — Task 3 Step 3 (no `build-args:` key on the backend job)

**2. Placeholder scan.** No "TBD", "TODO", "implement later", "similar to". Code blocks are complete and copy-pasteable. Every `grep`/`az`/`gh` command has expected output.

**3. Type / name consistency.**

- `roleDefIdAcrPush` (Task 1 Step 1) → referenced as `roleDefinitionId: roleDefIdAcrPush` in Task 1 Step 4 ✓
- `ghOidcAcrPushAssignment` (Task 1 Step 4) → grep-asserted in Task 1 Step 6 ✓
- Cache scope keys `backend`/`frontend` consistent across Task 3 Steps 3 and 5 ✓
- `vars.ACR_NAME` used for `az acr login --name` (short name); `vars.ACR_LOGIN_SERVER` used for `docker/login-action`'s `registry:` AND tag-prefix (FQDN). Iter-1 plan-review fix per Codex P1 — see "Important context" block in Task 3 ✓
- Sentinel UUID `00000000-0000-0000-0000-000000000000` matches research brief topic 2 ✓
- `compute-sha` job name + `needs.compute-sha.outputs.sha` cross-reference ✓

**4. Spec items with no task.** None — every PRD AC traces to a task step.
