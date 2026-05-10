# Research: deploy-pipeline-ci-image-publish

**Date:** 2026-05-10
**Feature:** Slice 2 of 4 — Add `.github/workflows/build-and-push.yml` (OIDC → ACR push, backend + frontend at `<sha7>`) plus the missing AcrPush role assignment in `infra/main.bicep`. No deploy step.
**Researcher:** research-first agent

> **Scope note.** Same as Slice 1: this is infrastructure + CI work. There are no `package.json` / `pyproject.toml` deltas — the "external libraries/APIs" researched are GitHub Actions, Azure RBAC role definitions, and Azure Monitor Agent stream enums (the topic-3 correction). 12 topics, all required by the PRD's Open Questions and the prompt's research checklist.
>
> **Important correction folded into this brief:** Slice 1's research (`docs/research/2026-05-09-deploy-pipeline-iac-foundation.md` topic 3) cited a stream named `Microsoft-Heartbeat`, which is not in the AMA DCR stream enum. That bug surfaced at Slice 1 acceptance smoke (PR #53, 2026-05-10) and was fixed by switching to `kind: 'Linux'` + a `Microsoft-Syslog` data source — Heartbeat then flows automatically. Topics 11 and 12 below document the corrected facts and cite Microsoft's DCR-structure schema page so future slices can reference them directly.

---

## Libraries / APIs Touched

| Surface                                       | Our pinned form (PRD / current repo)               | Latest stable (2026-05-10)                                                                                                                      | Breaking changes vs assumed shape                                | Source                                                                                                                                             |
| --------------------------------------------- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `azure/login`                                 | `@v2` (PRD)                                        | **v3.0.0** (2026-03-17)                                                                                                                         | None for OIDC inputs. v2 still works; v3 default is fine.        | [GitHub — azure/login releases](https://github.com/Azure/login/releases) (2026-05-10)                                                              |
| `docker/login-action`                         | `@v3` (PRD)                                        | **v4.1.0** (2026-04-02)                                                                                                                         | None affecting our use; ACR auth still username/password-based.  | [GitHub — docker/login-action](https://github.com/docker/login-action) (2026-05-10)                                                                |
| `docker/build-push-action`                    | `@v5` (PRD)                                        | **v7.1.0** (2026-04-10)                                                                                                                         | v7 requires Node 24 runner ≥ v2.327.1; drops deprecated env vars | [GitHub — docker/build-push-action releases](https://github.com/docker/build-push-action/releases) (2026-05-10)                                    |
| `docker/setup-buildx-action`                  | (not in PRD; required prerequisite)                | **v4.0.0** (2026-03-05)                                                                                                                         | None; v3 still supported (latest v3.8.0)                         | [GitHub — docker/setup-buildx-action](https://github.com/docker/setup-buildx-action/releases) (2026-05-10)                                         |
| `actions/checkout`                            | `@v4.2.2` (existing `ci.yml`)                      | **v4.2.2** (current)                                                                                                                            | n/a                                                              | (existing repo pin — confirmed in `.github/workflows/ci.yml:32`)                                                                                   |
| GitHub Actions OIDC issuer                    | `token.actions.githubusercontent.com`              | unchanged                                                                                                                                       | n/a                                                              | [GitHub Docs — OIDC reference](https://docs.github.com/en/actions/reference/openid-connect-reference) (2026-05-10)                                 |
| Azure built-in role: **AcrPush**              | GUID `8311e382-0749-4cb8-b61a-304f252e45ec`        | **confirmed correct**                                                                                                                           | n/a                                                              | [MS Learn — Built-in Container roles](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/containers) (2026-05-10)    |
| Azure Container Registry auth (Basic SKU)     | OIDC → MI → AcrPush                                | **`az acr login --expose-token` is the canonical OIDC-derived path**; `docker/login-action` accepts the resulting token as password             | n/a                                                              | [MS Learn — ACR authentication options](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication) (2026-05-10) |
| Azure Monitor Agent — DCR stream enum (Linux) | (Slice 1 brief said `Microsoft-Heartbeat` — wrong) | Valid streams listed in the data-source table on the DCR-structure page; **`Microsoft-Heartbeat` not in the enum**. Heartbeat flows implicitly. | n/a                                                              | [MS Learn — DCR structure](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/data-collection-rule-structure) (2026-05-10)      |

---

## Per-Topic Analysis

### A. GitHub Actions

#### 1. `azure/login@v2` — OIDC federation

**Findings:**

1. **Required inputs for OIDC mode:** `client-id`, `tenant-id`, `subscription-id`. The action treats the legacy `creds:` JSON parameter as ignored when these three are set — quoting the action README: _"If one of client-id and subscription-id and tenant-id is set, creds will be ignored."_
2. **Required workflow `permissions:` block:** `id-token: write` (allows the runner to mint an OIDC token) and `contents: read` (so `actions/checkout` can clone). The README says: _"In GitHub workflow, you should set permissions: with id-token: write at workflow level or job level."_
3. **`audience:` input** is optional; default is `api://AzureADTokenExchange` which matches the federated credential shape Slice 1 declared (`audiences: ['api://AzureADTokenExchange']` at `infra/main.bicep:106`). Do not override.
4. **`enable-AzPSSession`** input is `false` by default — leave it false, we don't use Az PowerShell.
5. **Latest stable is v3.0.0 (2026-03-17)**, but the existing PRD/architecture-verdict pins `@v2`. v2 is still fully supported. **Decision: keep `@v2` as PRD says** — there's no compelling reason to bump in this slice; v3 didn't change the OIDC input contract.

**Sources:**

1. [GitHub — Azure/login README](https://github.com/Azure/login) — accessed 2026-05-10
2. [MS Learn — Authenticate to Azure from GitHub Actions by OIDC](https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure-openid-connect) — accessed 2026-05-10 (cited in Slice 1 brief, still the canonical source)

**Design impact:**

The Slice 2 workflow declares (at the workflow level, since both jobs need it):

```yaml
permissions:
  id-token: write
  contents: read
```

Each job (or both, if shared) runs `azure/login@v2` with:

```yaml
- uses: azure/login@v2
  with:
    client-id: ${{ vars.AZURE_CLIENT_ID }}
    tenant-id: ${{ vars.AZURE_TENANT_ID }}
    subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID }}
```

No `creds:`, no `client-secret:`. **The plan must NOT pin `@v2.x.y` to a sub-minor** — pin the major (`@v2`) so security patches flow.

**Test implication:**

A successful `azure/login@v2` step is the contract test for the Slice 1 federated credential. If the credential's subject claim doesn't match what GitHub mints for `push: main`, the step exits non-zero with an AAD `AADSTS70021` error. The acceptance smoke from US-002 (`gh workflow run build-and-push.yml`) exercises this path — log inspection should confirm `Login successful` and an immediate `az account show` (implicit via the action) succeeds. Use `gh run view <run-id> --log-failed` if anything fails.

---

#### 2. `docker/login-action@v3` against ACR — the OIDC-derived path

**Findings (CRITICAL — diverges from PRD assumption):**

1. The PRD's acceptance criterion says _"Workflow uses `docker/login-action@v3` against `${{ vars.ACR_LOGIN_SERVER }}` with no admin password (OIDC-derived)"_ — this is **achievable but requires an intermediate step**. `docker/login-action` itself does not natively understand "OIDC-derived" auth; it always wants a `username` / `password` pair (plus optional `registry`).
2. **The canonical OIDC path on ACR Basic SKU** uses `az acr login --name <acrName> --expose-token` (after `azure/login@v2`) to mint a short-lived ACR access token. The MS Learn ACR-authentication page documents this exactly:
   ```bash
   TOKEN=$(az acr login --name <acrName> --expose-token --output tsv --query accessToken)
   docker login myregistry.azurecr.io \
     --username 00000000-0000-0000-0000-000000000000 \
     --password-stdin <<< $TOKEN
   ```
   The username is a sentinel UUID `00000000-0000-0000-0000-000000000000` (this is documented; not a typo); the password is the access token from `az acr login --expose-token`.
3. **ACR Basic SKU supports this fully.** The docs show the same flow for all SKUs (Basic / Standard / Premium); the SKU only constrains storage, replication, and private endpoint capabilities — not auth mechanism. Confirmed in [MS Learn — ACR authentication options](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication) (2026-05-10).
4. **Two viable workflow shapes:**
   - **Shape A (preferred — uses `docker/login-action`):** After `azure/login@v2`, run `az acr login --name <acrName> --expose-token` to capture `accessToken`, then call `docker/login-action@v3` with `username: 00000000-0000-0000-0000-000000000000` and `password: ${{ steps.acr-token.outputs.token }}`. This satisfies the PRD AC literally and gives `docker/login-action`'s post-action credential cleanup.
   - **Shape B (skips `docker/login-action`):** After `azure/login@v2`, call `az acr login --name <acrName>` (without `--expose-token`) — this writes credentials directly into `~/.docker/config.json`. Simpler, but **loses `docker/login-action`'s automatic credential cleanup at end of job**, which the docker-action README explicitly cites as a security benefit.
5. **Token TTL is 3 hours.** Plenty for any single workflow run. The `azure/login@v2`-issued Azure access token (which `az acr login` exchanges) is the parent token; both inherit the OIDC short-lived chain.
6. **What the PRD's acceptance criterion "no admin password" actually means:** zero `AZURE_CREDENTIALS` JSON, zero ACR admin password (Slice 1 has `adminUserEnabled: false` already), zero PAT. Both Shape A and Shape B satisfy this — the access token from `az acr login --expose-token` is OIDC-derived (it descends from the federated credential) and is short-lived.

**Sources:**

1. [MS Learn — Azure Container Registry authentication options](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication) — accessed 2026-05-10 (page updated 2026-02-27); see "Use `az acr login` without Docker daemon" section for the exact `--expose-token` flow.
2. [GitHub — docker/login-action README](https://github.com/docker/login-action) — accessed 2026-05-10. (Confirms the action takes username/password only; no native OIDC mode.)
3. [MS Tech Community — CI/CD as a Platform: Shipping Microservices...](https://techcommunity.microsoft.com/blog/azureinfrastructureblog/cicd-as-a-platform-shipping-microservices-and-ai-agents-with-reusable-github-act/4504550) — accessed 2026-05-10. (Real-world workflow example using exactly this pattern.)

**Design impact:**

**Adopt Shape A.** Step sequence per job:

```yaml
- uses: azure/login@v2
  with: { client-id: ..., tenant-id: ..., subscription-id: ... }

- name: Get ACR token
  id: acr-token
  run: |
    token=$(az acr login --name "${{ vars.ACR_NAME }}" --expose-token --output tsv --query accessToken)
    echo "::add-mask::$token"
    echo "token=$token" >> "$GITHUB_OUTPUT"
  # Note: az acr login --name expects the SHORT registry name (e.g. msaiacrXXX), NOT the FQDN.
  # The plan-review iter-1 (Codex) caught this — original example used vars.ACR_LOGIN_SERVER (FQDN)
  # which works inconsistently. Resolution: a separate vars.ACR_NAME (short) for `az acr login --name`
  # AND vars.ACR_LOGIN_SERVER (FQDN) for `docker/login-action.registry` and image tags below.

- uses: docker/login-action@v3
  with:
    registry: ${{ vars.ACR_LOGIN_SERVER }}
    username: 00000000-0000-0000-0000-000000000000
    password: ${{ steps.acr-token.outputs.token }}
```

Two notes for implementation:

- `::add-mask::` must fire **before** `>> $GITHUB_OUTPUT` so the masked value never appears in step logs. (Even if it leaks, it's a 3-hour ACR token — but discipline is free.)
- `vars.ACR_LOGIN_SERVER` is the FQDN (e.g. `msaiacrXXX.azurecr.io`) per Slice 1 output. **Correction (plan-review iter-1, 2026-05-10):** the original draft of this brief asserted `az acr login --name` "accepts either the short name or FQDN" — that was incorrect/inconsistent in practice. Resolution: introduce a separate `ACR_NAME` GH Variable (short name from `az acr list -g msaiv2_rg --query '[0].name' -o tsv`) used exclusively for `az acr login --name`, while `ACR_LOGIN_SERVER` remains the FQDN used for `docker/login-action.registry` and image tags. See PRD `## Operator Pre-Merge Setup` for the full 8-variable list.

**Decision NOT to revisit:** Shape B (skip `docker/login-action`) is materially simpler but loses the post-action cleanup. We keep Shape A to satisfy the PRD's literal AC and the security benefit.

**Test implication:**

The acceptance smoke must include a step that proves the docker login succeeded (the build-push-action will fail on push if not, but earlier evidence is helpful). Recommend `docker pull ${{ vars.ACR_LOGIN_SERVER }}/<existing-image>:<tag>` only if the registry has any image — for a fresh ACR, just rely on the build-push-action's `push: true` failing fast on auth error. **A deliberate failure mode worth scripting in the runbook:** if `az acr login --expose-token` returns exit 0 but with an empty `accessToken`, the AcrPush role assignment hasn't propagated yet — see topic 10.

---

#### 3. `docker/build-push-action` — version + GHA cache scope

**Findings:**

1. **Latest stable is v7.1.0 (2026-04-10).** v7.0.0 introduced breaking changes: Node 24 default runtime requires Actions Runner ≥ v2.327.1, ESM module switch, removed legacy export-build tool. The repo's `runs-on: ubuntu-24.04` runner is updated routinely and supports Node 24, so v7 is fine.
2. **PRD pins `@v5`.** v5 is no longer in the GitHub releases listing for this action — the visible majors are v6.x (latest `v6.19.2`, 2026-02-12) and v7.x. **Recommendation: bump to `@v6` for Slice 2.** Rationale: v5 is two majors behind, may stop receiving security patches; v6 has none of v7's runtime upgrade pressure (v7 specifically tracks Node 24); v6 is the conservative upgrade. Pin major only (`@v6`), not `@v6.19.2`. _This is the only PRD literal-version deviation in this brief and should be discussed with the user during plan review — see Open Risks._
3. **GHA cache backend (`type=gha`):** The `scope` parameter identifies the cache object. **Default scope is `buildkit`.** When two parallel jobs both write `type=gha,mode=max` without distinct scopes, _each build will overwrite the cache of the previous, leaving only the final cache_ (verbatim from [Docker — GitHub Actions cache](https://docs.docker.com/build/cache/backends/gha/)). **Backend and frontend MUST use distinct scopes** — e.g., `scope=backend` and `scope=frontend`.
4. **`mode=max`** exports all intermediate layers (vs `mode=min` which exports only the final stage). For our two multi-stage builds (frontend has a builder stage; backend's `Dockerfile` is single-stage but uv-based and benefits from layer cache on `requirements`/`uv sync`), `mode=max` is correct.
5. **`cache-from` scope must match `cache-to` scope** within a job. Cross-job sharing happens via the GHA cache key, not via the scope name itself.
6. **Token + URL inputs are auto-populated** by the action when run inside a GitHub Actions runner. We do not need to pass `url=` or `token=` explicitly.

**Sources:**

1. [Docker — GitHub Actions cache backend](https://docs.docker.com/build/cache/backends/gha/) — accessed 2026-05-10. _"Scope identifies which cache object a build uses... If you build multiple images, each build will overwrite the cache of the previous, leaving only the final cache."_
2. [GitHub — docker/build-push-action releases](https://github.com/docker/build-push-action/releases) — accessed 2026-05-10. v7.1.0 is the absolute latest; v6.19.2 is the latest v6.
3. [BuildKit issue #2885 — GHA cache with multiple images](https://github.com/moby/buildkit/issues/2885) — accessed 2026-05-10.

**Design impact:**

```yaml
- uses: docker/build-push-action@v6 # bump from PRD's @v5
  with:
    context: . # repo root for backend
    file: backend/Dockerfile
    push: true
    tags: ${{ vars.ACR_LOGIN_SERVER }}/msai-backend:${{ needs.short-sha.outputs.sha }}
    cache-from: type=gha,scope=backend
    cache-to: type=gha,mode=max,scope=backend
```

Frontend uses `scope=frontend` and `context: ./frontend`, plus the three `build-args` for `NEXT_PUBLIC_*`. **Distinct scopes are non-negotiable** — without them, the second job's cache overwrites the first.

**Test implication:**

Acceptance smoke: run the workflow twice on the same SHA. Second run should show _cache hits_ on every step in both jobs (look for `CACHED [stage]` in the build log). Successful caching is the metric for the PRD's "cold ≤ 10 min, warm ≤ 5 min" criterion. If only one job shows cache hits on the second run, the scope misconfiguration is back.

---

#### 4. `docker/setup-buildx-action` — required prerequisite

**Findings:**

1. **Yes, it is required** before `docker/build-push-action` when using `type=gha` cache. The Docker docs' minimal pattern always includes `docker/setup-buildx-action` between `actions/checkout` and the build step, because the GHA cache backend uses BuildKit's cache service, which only buildx exposes.
2. **Latest stable is v4.0.0 (2026-03-05).** v3 (`v3.8.0` latest) is still supported — both work.
3. No special inputs needed for our use; default driver (`docker-container`) is correct.

**Sources:**

1. [Docker — Cache management with GitHub Actions](https://docs.docker.com/build/ci/github-actions/cache/) — accessed 2026-05-10
2. [GitHub — docker/setup-buildx-action](https://github.com/docker/setup-buildx-action/releases) — accessed 2026-05-10

**Design impact:**

Both jobs include a `docker/setup-buildx-action@v3` step (pin v3 to match `docker/build-push-action@v6`'s contemporaneous release window — both shipped late 2024/early 2025; pairing reduces the risk of subtle BuildKit/buildx version drift). After `azure/login@v2`, before the build step.

**Test implication:** No test required; failing to include it manifests as `ERROR: failed to solve: cache-to: type=gha is not supported by the default builder` at first build. Acceptance smoke catches it immediately.

---

#### 5. GitHub Actions OIDC subject claim — `push` vs `workflow_dispatch`

**Findings:**

1. **Both triggers produce the same `sub` claim** when the dispatch is invoked from `main`. The `sub` is derived from the **branch ref**, not the trigger event. Per [GitHub Docs — OIDC reference](https://docs.github.com/en/actions/reference/openid-connect-reference): _"The subject claim includes the branch name of the workflow, but only if the job doesn't reference an environment, and if the workflow is not triggered by a pull request event."_
2. **For `push: branches: [main]`:** `sub = repo:marketsignal/msai-v2:ref:refs/heads/main`.
3. **For `workflow_dispatch` from `main`:** same `sub`. The `event_name` claim differs (`push` vs `workflow_dispatch`), but the federated credential **only** matches on `sub` — so a single Slice 1 credential satisfies both triggers.
4. **What would break it:** if the workflow ever uses `environment:` at the job level (e.g., for prod approval gates in Slice 3), the subject changes to `repo:OWNER/REPO:environment:<NAME>` and the Slice 1 credential rejects. Slice 2 must NOT introduce `environment:` — keep that for Slice 3 with a second federated credential.
5. **PR triggers** would produce `sub = repo:OWNER/REPO:pull_request` and would also reject — Slice 2's `on:` block has no `pull_request:`, so this can't happen.
6. **Forks:** the OIDC token's `sub` includes the upstream repo (`repo:marketsignal/msai-v2:...`) only when run on the upstream's runner; forks get a fork-prefixed subject and reject. This is automatic defense-in-depth.

**Sources:**

1. [GitHub Docs — OpenID Connect reference](https://docs.github.com/en/actions/reference/openid-connect-reference) — accessed 2026-05-10
2. [GitHub Docs — OpenID Connect concepts](https://docs.github.com/en/actions/concepts/security/openid-connect) — accessed 2026-05-10
3. [GitHub community discussion — OIDC custom claims](https://github.com/orgs/community/discussions/49966) — accessed 2026-05-10. Confirms `event_name` is a separate claim from `sub` and only certain claims (sub, ref, environment, repo) are matchable on Azure's federated credential.

**Design impact:**

The Slice 1 federated credential at `infra/main.bicep:99-109` (subject `repo:${repoOwner}/${repoName}:ref:refs/heads/${repoBranch}` = `repo:marketsignal/msai-v2:ref:refs/heads/main`) **already covers both `push: branches: [main]` and `workflow_dispatch:` from main**. No second credential needed for Slice 2.

**Test implication:**

The acceptance test deliberately exercises both: (1) push a no-op commit to `main` → workflow runs; (2) `gh workflow run build-and-push.yml` while no commits → workflow_dispatch run on `main` HEAD → also succeeds. Both must show `azure/login@v2` exit 0. If `workflow_dispatch` is invoked while on a feature branch via the Actions UI, expect failure — that's the federated-credential rejection working as intended; document in runbook.

---

#### 6. Short-SHA idiom

**Findings:**

1. **Canonical pattern in 2026:** bash parameter expansion `${GITHUB_SHA::7}` written into `$GITHUB_OUTPUT` from a `run:` step, surfaced as a job output. This is what GitHub's own Marketplace short-sha actions use under the hood, and it requires no third-party dependency.
2. **Verbatim canonical idiom:**
   ```bash
   echo "short_sha=${GITHUB_SHA::7}" >> "$GITHUB_OUTPUT"
   ```
3. **`${{ github.sha }}` does NOT support substring expressions** — you cannot write `${{ github.sha:0:7 }}` in YAML. Must compute in a `run:` step. Confirmed via repeated GitHub Marketplace short-sha action READMEs.
4. **Two consumption patterns:**
   - Same-job: `${{ steps.short.outputs.short_sha }}`
   - Cross-job: `needs.<job-name>.outputs.short_sha` after declaring `outputs:` on the producer job.
5. The 7-char convention is consistent with `git rev-parse --short` default and the slicing-verdict's literal acceptance string `abc1234`.

**Sources:**

1. [DEV Community — GitHub Actions and creating a short SHA hash](https://dev.to/hectorleiva/github-actions-and-creating-a-short-sha-hash-8b7) — accessed 2026-05-10
2. [Future Studio — How to Get the Short Git Commit Hash](https://futurestud.io/tutorials/github-actions-how-to-get-the-short-git-commit-hash) — accessed 2026-05-10

**Design impact:**

A small `compute-sha` job (or a step at the top of each build job) computes the short SHA once and emits it as an output:

```yaml
jobs:
  compute-sha:
    runs-on: ubuntu-24.04
    outputs:
      sha: ${{ steps.short.outputs.short_sha }}
    steps:
      - id: short
        run: echo "short_sha=${GITHUB_SHA::7}" >> "$GITHUB_OUTPUT"

  backend:
    needs: compute-sha
    ...
    steps:
      - uses: docker/build-push-action@v6
        with:
          tags: ${{ vars.ACR_LOGIN_SERVER }}/msai-backend:${{ needs.compute-sha.outputs.sha }}
```

Or — simpler, less DRY — compute it inline in each job's first step. Either is fine; the dedicated job is slightly cleaner and the cost (one ~2-second runner allocation) is negligible.

**Test implication:** Acceptance smoke verifies the published image tag matches `git rev-parse --short HEAD` of the run's commit (`az acr repository show-tags --name <acr> --repository msai-backend -o tsv | grep "<sha7>"`). If they ever differ, the SHA computation broke.

---

#### 7. `concurrency:` block

**Findings:**

1. **Canonical 2026 syntax** for cancel-in-progress on per-ref builds:
   ```yaml
   concurrency:
     group: build-and-push-${{ github.ref }}
     cancel-in-progress: true
   ```
2. The group expression typically uses `${{ github.ref }}` (or `${{ github.head_ref || github.ref }}` for PR-aware patterns; we don't need that since Slice 2 doesn't fire on PRs).
3. **`cancel-in-progress: true`** is appropriate for image-build workflows on `main`: the newer commit's image supersedes the older. **For deploy workflows (Slice 3)** the recommendation flips to `cancel-in-progress: false` to avoid mid-deploy interruption — different concern, different default.
4. The block can sit at the workflow level OR per-job level. Workflow-level is correct here since both jobs share the build identity.
5. `cancel-in-progress` and `queue: max` cannot coexist (validation error); we don't use queue.

**Sources:**

1. [GitHub Docs — Control concurrency of workflows and jobs](https://docs.github.com/actions/writing-workflows/choosing-what-your-workflow-does/control-the-concurrency-of-workflows-and-jobs) — accessed 2026-05-10
2. [Blacksmith — Protect prod, cut costs: concurrency in GitHub Actions](https://www.blacksmith.sh/blog/protect-prod-cut-costs-concurrency-in-github-actions) — accessed 2026-05-10

**Design impact:**

```yaml
concurrency:
  group: build-and-push-${{ github.ref }}
  cancel-in-progress: true
```

Workflow-level. Matches the PRD's Edge Cases row: _"Two pushes land on `main` within a few seconds → concurrency group cancels the in-progress run when a newer one starts."_

**Test implication:** Phase 5 manual smoke can simulate this by pushing two commits ~5 seconds apart on a temp branch (then merging to main as a single combined PR is the actual production path). Easier: confirm via `gh run list` that double-dispatch within 30s shows the older one as `cancelled`. Not blocking for first acceptance smoke.

---

### B. Azure / Bicep

#### 8. AcrPush role definition GUID

**Findings:**

1. **GUID `8311e382-0749-4cb8-b61a-304f252e45ec` is the current correct AcrPush built-in role.** Verified via:
   - Microsoft Learn's [Built-in roles for Containers](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/containers) page (which is authoritative; updated frequently).
   - The MicrosoftDocs/azure-docs JSON snapshot at `containers.md`.
2. **Role permissions:** `Microsoft.ContainerRegistry/registries/pull/read` + `Microsoft.ContainerRegistry/registries/push/write`. Push implies pull — convenient for the CI scenario where workflow may pull a base layer for caching before pushing.
3. The role is assignable at all scopes (subscription / RG / resource), and our scope is the ACR resource itself (matches existing Slice 1 pattern).
4. **No newer role replaces it.** Microsoft did add new "Container Registry Repository ..." roles for fine-grained ABAC repository permissions in 2024-2025, but those are scoped per-repository and require ABAC enablement. AcrPush remains the correct, simplest grant for "this principal can push any image to this registry."

**Sources:**

1. [MS Learn — Azure built-in roles for Containers](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/containers) — accessed 2026-05-10
2. [azadvertizer — AcrPush role detail](https://www.azadvertizer.net/azrolesadvertizer/8311e382-0749-4cb8-b61a-304f252e45ec.html) — accessed 2026-05-10 (confirms permissions + assignability)

**Design impact:**

Add to `infra/main.bicep` near line 85 (where existing role-def vars live):

```bicep
var roleDefIdAcrPush = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8311e382-0749-4cb8-b61a-304f252e45ec')
```

**Test implication:** None for the GUID itself. If we ever see a `RoleAssignmentNotFound` error at deployment, the GUID is the first thing to check.

---

#### 9. Role-assignment Bicep resource — user-assigned MI principal

**Findings:**

1. **The existing `vmAcrPullAssignment` resource at `infra/main.bicep:563-572` is the correct pattern** to mirror. Same `Microsoft.Authorization/roleAssignments@2022-04-01`, same `scope: acr`, same `principalType: 'ServicePrincipal'`, same `guid()`-named, same `description`-with-rationale.
2. **For user-assigned MI**, the principal-id reference is `ghOidcMi.properties.principalId` (NOT `ghOidcMi.identity.principalId` — that's for resources WITH an identity assigned, like a VM; user-assigned MI is itself a principal-bearing resource). Confirmed in multiple Azure-Samples and the [`azure/bicep` discussions #5825](https://github.com/Azure/bicep/discussions/5825).
3. **`principalType: 'ServicePrincipal'`** is correct for user-assigned MI — managed identities are a form of service principal in Entra ID. Setting principalType explicitly avoids the well-known Azure RBAC eventual-consistency issue where the role assignment "fails" because the principal "doesn't exist yet" — see [Bicep issue #836](https://github.com/microsoft/azure-container-apps/issues/836). Slice 1 sets it on every assignment; Slice 2 must too.
4. **`guid()` deterministic naming:** the existing pattern is `guid(<scope-resource-id>, <target-resource-id>, '<role-name>')`. For consistency with the four existing assignments, Slice 2 uses something like `guid(ghOidcMi.id, acr.id, 'acr-push')`.
5. **`scope: acr`** restricts the assignment to just the registry — narrowest reasonable scope; safer than RG- or subscription-scoped.

**Sources:**

1. [MS Learn — Use Bicep to create Azure RBAC resources](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/scenarios-rbac) — accessed 2026-05-10
2. [Bicep AzAPI reference — Microsoft.Authorization/roleAssignments](https://learn.microsoft.com/en-us/azure/templates/microsoft.authorization/roleassignments) — accessed 2026-05-10
3. (existing repo pattern: `infra/main.bicep:563-572`)

**Design impact:**

Add immediately after `operatorKvSecretsOfficerAssignment` (line 593) so all five role-assignments cluster together:

```bicep
resource ghOidcAcrPushAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(ghOidcMi.id, acr.id, 'acr-push')
  properties: {
    principalId: ghOidcMi.properties.principalId
    roleDefinitionId: roleDefIdAcrPush
    principalType: 'ServicePrincipal'
    description: 'Slice 2: GitHub Actions OIDC MI pushes images to ACR via build-and-push.yml'
  }
}
```

Plus update the section comment block `// T8 (continued): Role assignments` (line 547) to say "5 total" instead of "4 total."

Also remove the now-obsolete comment at line 90: _"Slice 1 declares both; AcrPush role assignment lives in Slice 2."_ — replace with: _"Slice 1 declares both; Slice 2 added the AcrPush role assignment below."_

**Test implication:**

Phase 5 acceptance must include `az deployment group what-if -f infra/main.bicep -g msaiv2_rg` showing exactly one resource added (the new role assignment) and zero resources modified. After apply, `az role assignment list --scope $acrId --assignee $ghOidcMiPrincipalId` should return one entry with `roleDefinitionName: AcrPush`. Then re-run `what-if` — must show NoChange (idempotency proof).

---

#### 10. Role-assignment propagation latency

**Findings:**

1. **No published numerical SLA from Microsoft.** Empirical reports cluster:
   - **Few-minute typical:** Most role assignments propagate in < 5 minutes.
   - **30s–2min common:** Documented in multiple Azure SDK + Azure Dev issues. [`azure/azure-dev` issue #8026](https://github.com/Azure/azure-dev/issues/8026) reports a "wait ~60 seconds" workaround.
   - **Up to 30-minute outliers:** under cache-pressure conditions (rare; documented as worst case in MS Q&A threads).
2. **`principalType: 'ServicePrincipal'` materially shortens the window** — without it, Azure tries to look up the principal in Entra and may cache a "not found" before the new MI propagates, then the role-assignment creation 503s. _We already do this everywhere in Slice 1, and Slice 2's new assignment continues the pattern._
3. **The race is mostly benign here** because the workflow doesn't run mid-deploy. Actual sequence: (1) PR #N merges, (2) operator runs `scripts/deploy-azure.sh` (deploys role assignment), (3) operator pushes a no-op commit OR runs `gh workflow run` for the acceptance smoke. The minimum gap between (2) and (3) is the operator's typing speed — already 30+ seconds in practice.
4. **First-push failure mode if propagation lags:** `azure/login@v2` succeeds (the federated credential is independent of role assignments) but `az acr login --expose-token` fails with HTTP 403 _AuthenticationFailed_ — `"The Microsoft Entra access token can't be used to authenticate to Azure Container Registry."* Workaround: re-run `gh workflow run build-and-push.yml` after 60 seconds. Document in runbook (US-003 already mentions this).

**Sources:**

1. [MS Q&A — Azure RBAC propagation latency](https://learn.microsoft.com/en-us/answers/questions/524792/azure-rbac-propagation-latency) — accessed 2026-05-10
2. [Azure-dev issue #8026 — RBAC propagation race causes 403 on first deploy](https://github.com/Azure/azure-dev/issues/8026) — accessed 2026-05-10
3. [azure-cli issue #20727 — `az role definition create` propagation delay](https://github.com/Azure/azure-cli/issues/20727) — accessed 2026-05-10

**Design impact:**

The PRD's US-003 already addresses this: runbook step says "first push-to-main after merging Slice 2 may fail; re-run after ~30s." **No design change required.** Do NOT add a sleep step in the workflow — that's an operator-side wait, not a CI-side wait, and we don't want to add silent dead time to every successful run.

**Test implication:**

The acceptance smoke (Phase 5.4 of `/new-feature`) should explicitly schedule the `workflow_dispatch` invocation **at least 60 seconds after** `scripts/deploy-azure.sh` returns. If first run still 403s, retry once at +60s. Classify two consecutive 403s as a real failure (likely role-def GUID typo or wrong principal-id resolution). **Do not retry indefinitely.**

---

### C. Azure Monitor Agent (Topic-3 revision facts for the Slice 1 brief)

> The Slice 2 plan does not change AMA configuration — that ships in Slice 1 and was fixed at PR #53 — but the Slice 1 research brief contained an incorrect claim about a `Microsoft-Heartbeat` stream. These two topics document the validated facts so future slices (4+) reference correct information directly. Slice 1's brief should be updated in this slice's PR per the PRD US-001 acceptance criterion.

#### 11. AMA-on-Linux valid stream enum

**Findings:**

1. **The canonical enum lives in the DCR-structure docs page's data-source table.** [MS Learn — DCR structure](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/data-collection-rule-structure) (page updated 2026-04-28). The valid streams emitted by built-in AMA data sources are:
   - `Microsoft-Event` (Windows event logs — `windowsEventLogs` data source)
   - `Microsoft-InsightsMetrics` (perf counters secondary stream — `performanceCounters` data source)
   - `Microsoft-Perf` (perf counters primary stream — `performanceCounters` data source)
   - `Microsoft-Syslog` (Linux syslog — `syslog` data source)
   - `Microsoft-CommonSecurityLog` (CEF security appliances — `syslog` data source with CEF)
   - `Microsoft-W3CIISLog` (IIS logs — `iisLogs` data source)
   - `Microsoft-PrometheusMetrics` (`prometheusForwarder`)
2. **`Microsoft-Heartbeat` is NOT in this enum.** It does not appear anywhere in the DCR-structure schema. Trying to declare it as a stream in a `dataFlows` entry produces an `InvalidStream` validation error from the DCR API — confirmed by the Slice 1 acceptance-smoke failure (PR #53, 2026-05-10) and by multiple MS Q&A threads where users hit the same error mistakenly thinking Heartbeat is a stream.
3. **The Slice 1 brief's topic 3 contained the bogus claim:** _"a single DCR resource with `dataSources: { extensions: [...] }` empty and `dataFlows: [{ streams: ['Microsoft-Heartbeat'], ... }]`"_ — this is incorrect and the assertion was caught at first-apply.
4. **Custom streams** start with `Custom-` and are declared via `streamDeclarations` (separate from `dataSources`). For our heartbeat-only use case, we don't need any custom stream.

**Sources:**

1. [MS Learn — Structure of a data collection rule (DCR) in Azure Monitor](https://learn.microsoft.com/en-us/azure/azure-monitor/data-collection/data-collection-rule-structure) — accessed 2026-05-10. _See "Valid data source types" table — it lists every stream the built-in `dataSources` types emit; `Microsoft-Heartbeat` is absent._
2. [MS Q&A — Custom Text Log DCR not Ingesting](https://learn.microsoft.com/en-us/answers/questions/2224302/custom-text-log-dcr-not-ingesting) — accessed 2026-05-10. _Shows the verbatim API error: "'Streams' stream 'X' must be a custom stream or one of: Microsoft-AACAudit, Microsoft-AACHttpRequest, ..." — full enum returned by the DCR validator._
3. [Bicep file at `infra/main.bicep:459-535`] — the Slice 1 fix-commit (PR #53) shows the validated correct shape: `kind: 'Linux'` + `dataSources.syslog: [{ streams: ['Microsoft-Syslog'], facilityNames: [...], logLevels: [...] }]`.

**Design impact:**

No Slice 2 design change to AMA — Slice 1 already shipped the corrected DCR. This research entry exists so the Slice 1 research brief gets corrected in the same PR (per PRD US-001 AC: _"Topic 3 is corrected: replace bogus `streams: ['Microsoft-Heartbeat']` with the validated `kind: 'Linux'` + `Microsoft-Syslog` data source pattern"_).

The corrected text for Slice 1 brief topic 3 should read:

> Minimum viable DCR for Linux VM heartbeat is a single DCR resource with `kind: 'Linux'` and `dataSources.syslog: [{ name: 'syslogBase', streams: ['Microsoft-Syslog'], facilityNames: [...], logLevels: [...] }]` plus a `dataCollectionRuleAssociation` linking it to the VM. Heartbeat then flows automatically once the agent is associated with any valid `kind: 'Linux'` DCR — no `Microsoft-Heartbeat` stream is declared (it is not a valid AMA stream; see topic 11 of the 2026-05-10 research brief).

**Test implication:**

Already covered by Slice 1's acceptance smoke (KQL: `Heartbeat | where TimeGenerated > ago(15m)`). No new test for Slice 2.

---

#### 12. Heartbeat emission — flows automatically

**Findings:**

1. **Heartbeat is implicit, not declared.** Once an AMA Linux agent is associated with **any** valid `kind: 'Linux'` DCR with **any** valid built-in data source (Syslog, Performance Counters, IIS) — even a minimal Syslog data source restricted to `auth`/`syslog` facilities at Warning+ severity — Heartbeat records flow to the workspace's `Heartbeat` table automatically.
2. **Confirmed by the Slice 1 acceptance smoke at PR #53.** Before the fix: `kind`-less DCR with stream `Microsoft-Heartbeat` → AMA's MCS endpoint returned 404 ("VM is not associated with the DCR"), Heartbeat never flowed. After the fix: `kind: 'Linux'` + Syslog data source → AMA Heartbeat records appeared within 15 minutes (per Slice 1 PRD acceptance metric).
3. **AMA must be restarted after a DCR change** for the new DCR to take effect — `sudo systemctl restart azuremonitoragent` flushes the agent's local DCR cache. Documented in the Slice 1 fix commit and in [MS Learn — AMA troubleshooting on Linux](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-troubleshoot-linux-vm). For a fresh VM provisioning (which is the Slice 1 acceptance path), this is automatic — first-boot startup pulls the DCR for the first time.
4. **The KQL acceptance query is unchanged:**
   ```kql
   Heartbeat
   | where TimeGenerated > ago(15m)
   | project Computer, OSType
   ```

**Sources:**

1. [MS Learn — Troubleshoot the Azure Monitor agent on Linux VMs](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/azure-monitor-agent-troubleshoot-linux-vm) — accessed 2026-05-10. _Confirms the Heartbeat KQL pattern + the systemctl restart workaround for DCR cache flush._
2. [MS Learn — Send Linux VM logs to Log Analytics with AMA](https://learn.microsoft.com/en-us/azure/azure-monitor/vm/data-collection-syslog) — accessed 2026-05-10. _Documents the Syslog-data-source-causes-Heartbeat side effect implicitly: every example DCR with Syslog produces both Syslog and Heartbeat records._
3. (existing repo: `infra/main.bicep:459-535` post-PR-#53 + `feedback_ama_dcr_kind_linux_required.md` in user MEMORY)

**Design impact:**

No Slice 2 design change. The corrected Slice 1 brief topic 3 should also note: _"Heartbeat is emitted automatically by AMA when associated with any valid `kind: 'Linux'` DCR — it is NOT a stream that gets declared in `dataFlows`."_ Risk-of-recurrence mitigation: document this prominently in the runbook so the next operator who cargo-cults a "minimum viable DCR" doesn't trigger the same regression.

**Test implication:**

Already covered.

---

## Not Researched (with justification)

- **Trivy / image scanning** — explicitly out of Slice 2 scope (Non-Goal #3 in PRD). Slice 4 ops if at all.
- **Multi-arch builds (linux/arm64)** — explicit Non-Goal #4. eastus2 VM is x86_64.
- **`docker compose pull` semantics on the VM** — Slice 3 concern (we only push images here, no pull-side).
- **Image-retention policies (last 5 retention)** — referenced by slicing verdict §rollback but enforcement is ACR side-config, Slice 4 ops.
- ~~**`az acr login` short-name vs FQDN normalization** — fast empirical check at implementation time; both forms work per docs, but using one consistently across the workflow is hygiene, not research.~~ **CORRECTED 2026-05-10 (plan-review iter-1):** the "both forms work" assumption was empirically inconsistent — `az acr login --name` expects the SHORT registry name only. Resolution moved to topic 2 + topic 9 + Open Risks #9: introduce a separate `ACR_NAME` GH Variable (short name) for `az acr login --name`, keep `ACR_LOGIN_SERVER` (FQDN) for `docker/login-action.registry` and image tags. **No longer** "Not Researched" — the question was actually settled by the iter-1 Codex review.
- **Self-hosted runners** — explicitly rejected at architecture-verdict time (lateral-movement risk). GH-hosted ubuntu-24.04 only.
- **Reusable workflows / composite actions** — premature for two jobs. Revisit at Slice 4 when more workflows exist.

---

## Open Risks (consolidated)

1. **PRD says `docker/build-push-action@v5`; v5 is no longer the latest visible major.** Recommendation: bump to `@v6` in this slice. Discuss with the user during plan review (Phase 3.3) — this is the only PRD literal-version deviation in this brief. If user prefers strict PRD adherence, `@v5` may still pull the latest sub-minor (action behavior unchanged for our use), but v5 may stop receiving security patches.

2. **PRD says `docker/login-action@v3` "with no admin password (OIDC-derived)."** Literal interpretation requires Shape A (use `az acr login --expose-token` to obtain a token, pass as password to `docker/login-action`). Shape B (`az acr login` directly, no `docker/login-action`) is materially simpler but loses post-action credential cleanup. **Adopt Shape A** to satisfy the PRD literally; document the intermediate `az acr login --expose-token` step in the workflow file with a comment so the next reader understands the indirection.

3. **First-push-after-deploy 403 due to RBAC propagation lag.** Empirical 30s–2min window. Document in runbook; do NOT add silent sleeps to the workflow. Acceptance smoke must wait ~60s between `scripts/deploy-azure.sh` and `gh workflow run`.

4. **Dual job → distinct GHA cache scopes are non-negotiable.** `scope=backend` and `scope=frontend`. Without this, the second job overwrites the first job's cache and the PRD's "warm cache ≤ 5 min" metric won't be reproducible. Verify by running the workflow twice on the same commit and confirming both jobs show layer cache hits the second time.

5. **`docker/build-push-action@v7` requires Node 24 runner ≥ v2.327.1.** Our `runs-on: ubuntu-24.04` is updated routinely so this is fine if/when we go to v7 in a future slice. v6 doesn't have this constraint. Sticking with v6 for Slice 2 sidesteps the runner-version pressure.

6. **Slice 1 brief's topic 3 must be corrected in the same PR** (per PRD US-001 AC). The corrected text replaces the bogus `Microsoft-Heartbeat` stream claim with the validated `kind: 'Linux'` + `Microsoft-Syslog` pattern (topics 11 and 12 above contain the correction-ready text). Failing to ship the correction leaves a future contributor a step away from regressing AMA via cargo-culted "minimum viable DCR."

7. **`environment:` keyword must NOT appear in Slice 2's workflow.** It would change the OIDC `sub` claim shape and reject against the Slice 1 federated credential. Slice 3 is where environments + a second federated credential land. Plan reviewer should grep the proposed workflow for `environment:` to confirm absence.

8. **Forks running this workflow** are already defended by the federated-credential's exact-match subject. Defense-in-depth is automatic; no extra config needed for Slice 2. (Re-verify at Slice 3 if any deploy job is added.)

9. **CORRECTED 2026-05-10 (plan-review iter-1).** Original wording asserted `az acr login --name` "accepts both short name and FQDN" — that was empirically inconsistent. Resolution: split into two repo Variables — `ACR_NAME` (short, used by `az acr login --name`) and `ACR_LOGIN_SERVER` (FQDN, used by `docker/login-action.registry` and image tags). PRD pre-merge setup documents both. Mixing the two forms across steps remains a footgun; runbook + workflow comments make the split explicit.
