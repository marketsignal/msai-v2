# PRD Discussion: Deployment Pipeline Slice 2 — CI Image Publish

**Status:** In Progress
**Started:** 2026-05-10
**Participants:** Pablo, Claude

## Original Scope (council-ratified — slicing verdict §Slice 2)

> GH Actions workflow on push-to-main + Azure OIDC federation + `docker build -f backend/Dockerfile .` and `docker build ./frontend` with `NEXT_PUBLIC_*` build-args + push to ACR with `${git-sha}` immutable tags. **No deploy step.**
>
> **Acceptance:** Workflow runs green on a no-op commit; ACR shows `msai-backend:abc1234` and `msai-frontend:abc1234`.

## Scope Brief (from session prompt)

- `.github/workflows/build-and-push.yml` — push-to-main → `azure/login@v2` (OIDC) → `docker build backend + frontend` → push to ACR with `${git-sha}` tags
- One Bicep edit: AcrPush role assignment on `msai-gh-oidc` user-assigned MI
- No deploy step
- Watch-outs surfaced by user:
  - ACR login server is at `properties.outputs.acrLoginServer.value` (wired in `main.bicep`)
  - GH OIDC client ID is at `properties.outputs.ghOidcClientId.value`
  - Federated credential subject: `repo:marketsignal/msai-v2:ref:refs/heads/main` (correct)
  - Audience: `api://AzureADTokenExchange`

## Inherited from Slice 1 (already in `infra/main.bicep`)

- `msai-gh-oidc` user-assigned MI (resource `ghOidcMi`)
- Federated credential `gh-actions-main` (issuer = `token.actions.githubusercontent.com`, subject = `repo:marketsignal/msai-v2:ref:refs/heads/main`, audience = `api://AzureADTokenExchange`)
- ACR Basic SKU (`adminUserEnabled: false`, OIDC-only push)
- Outputs: `acrLoginServer`, `ghOidcClientId`, `keyVaultUri`, `keyVaultName`, `vmPublicIp`, `vmPrincipalId`, `logAnalyticsWorkspaceId`, `backupsStorageAccount`, `backupsContainerName`

What's missing (and is Slice 2's job to add):

- AcrPush role assignment from `ghOidcMi` to `acr` — flagged in main.bicep:90 comment "AcrPush role assignment lives in Slice 2"

## Settled by council verdict (no need to re-discuss)

- ACR (not GHCR) — Hawk + Contrarian + Maintainer 3/5 (architecture verdict §3)
- GH-hosted runner (not self-hosted) — 4/5 (architecture verdict §3, lateral-movement risk)
- OIDC federation (not service principal secret) — Hawk + Contrarian + Maintainer (architecture verdict §3)
- `${git-sha}` immutable tags (no `latest` for production rollback discipline) — slicing verdict §Slice 2
- No deploy step — Slice 3's job
- No nightly backup, no alert rules — Slice 4's job

## Discussion Log

### Q1 — Source of frontend NEXT*PUBLIC*\* build-args

**Question:** Where do `NEXT_PUBLIC_AZURE_TENANT_ID`, `NEXT_PUBLIC_AZURE_CLIENT_ID`, `NEXT_PUBLIC_API_URL` come from in the workflow? Public values, not secrets.

**Answer:** **GitHub Repo Variables** (`vars.*`). Workflow references `${{ vars.NEXT_PUBLIC_AZURE_TENANT_ID }}` etc. Pablo sets them in repo Settings → Secrets and variables → Actions → Variables before the first run. Rotation = settings change, not PR. `NEXT_PUBLIC_API_URL` stays placeholder until Slice 3 picks DNS.

### Q2 — Workflow triggers

**Answer:** `push: branches: [main]` AND `workflow_dispatch:`. The dispatch trigger is essential for the acceptance smoke without writing a synthetic commit each time.

### Q3 — Image tagging

**Answer:** **Short SHA only** (first 7 chars of `${{ github.sha }}`). No `latest`. Strict immutability; matches slicing verdict literal acceptance (`msai-backend:abc1234`); aligns with Slice 3's per-deploy explicit-SHA reference and 5-tag rollback retention.

### Q4 — Build cache

**Answer:** **GitHub Actions cache via buildx** (`cache-from: type=gha`, `cache-to: type=gha,mode=max` on `docker/build-push-action@v5`). 3-10× faster rebuilds; free; repo-scoped.

### Q5 — Topic 3 research-brief revision

**Answer:** **Folded into this Slice 2 PR.** Single small doc change to `docs/research/2026-05-09-deploy-pipeline-iac-foundation.md` topic 3: replace the bogus `streams: ['Microsoft-Heartbeat']` pattern with the validated `kind: 'Linux'` + `Microsoft-Syslog` data source. Cite the AMA-on-Linux stream enum confirmation (`Microsoft-Event`, `Microsoft-InsightsMetrics`, `Microsoft-Perf`, `Microsoft-Syslog`, `Microsoft-WindowsEvent` — no `Microsoft-Heartbeat`). Reference `feedback_ama_dcr_kind_linux_required.md`. Won't bloat the PR (one file, ~20 lines).

### Required GitHub repo Variables (operator pre-merge step)

Listed here so they're surfaced in the PRD and the runbook. None are secrets.

| Variable                      | Source                                                                                                      | Example                                                     |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| `AZURE_TENANT_ID`             | MarketSignal Entra tenant                                                                                   | `2237d332-fc65-4994-b676-61edad7be319`                      |
| `AZURE_SUBSCRIPTION_ID`       | MarketSignal2 subscription                                                                                  | `68067b9b-943f-4461-8cb5-2bc97cbc462d`                      |
| `AZURE_CLIENT_ID`             | `az deployment group show -g msaiv2_rg -n main --query 'properties.outputs.ghOidcClientId.value' -o tsv`    | (UUID of msai-gh-oidc MI)                                   |
| `ACR_LOGIN_SERVER`            | `az deployment group show -g msaiv2_rg -n main --query 'properties.outputs.acrLoginServer.value' -o tsv`    | `msaiacr<hash>.azurecr.io`                                  |
| `NEXT_PUBLIC_AZURE_TENANT_ID` | Same as `AZURE_TENANT_ID` (frontend-side)                                                                   | `2237d332-fc65-4994-b676-61edad7be319`                      |
| `NEXT_PUBLIC_AZURE_CLIENT_ID` | Frontend Entra app registration client ID (separate from `AZURE_CLIENT_ID` which is the MI used by Actions) | TBD by operator                                             |
| `NEXT_PUBLIC_API_URL`         | Production API base URL (placeholder until Slice 3 sets DNS)                                                | `https://api.msai.example.com` (placeholder OK for Slice 2) |

## Refined Understanding

### Personas

- **Pablo (operator):** Sets repo Variables once before first run; triggers `workflow_dispatch` for acceptance smoke; verifies images appear in ACR.
- **GitHub Actions runner:** OIDC-federates to Azure as `msai-gh-oidc`, builds backend + frontend images, pushes to ACR.
- **ACR (consumer):** Receives `msai-backend:<sha7>` and `msai-frontend:<sha7>` tagged images; will be the read target in Slice 3.

### Refined User Stories

- **US-001:** As an operator, I want push-to-main to automatically build and publish backend + frontend images to ACR with immutable git-SHA tags, so the deploy step in Slice 3 has reproducible image references.
- **US-002:** As an operator, I want to be able to manually trigger the workflow (`workflow_dispatch`), so I can run an acceptance smoke and re-publish without a code change.
- **US-003:** As a security boundary, the workflow MUST authenticate to Azure exclusively via OIDC federation — no service principal client secret, no long-lived credential anywhere.

### Non-Goals (Explicit)

- ❌ No deploy step (SSH to VM, `docker compose pull`, etc.) — Slice 3
- ❌ No `latest` tag — rollback discipline
- ❌ No image scanning (Trivy, etc.) — Slice 4 ops, if at all
- ❌ No multi-arch builds — eastus2 VM is x86_64; arm64 not needed
- ❌ No PR-trigger build-and-push (existing `ci.yml` covers PR validation; only main pushes images)
- ❌ No image-retention policy / pruning — Slice 4 (will keep last 5 SHAs per slicing verdict §rollback)
- ❌ No e2e/smoke test against published images — Slice 3 will exercise the deploy + health checks

### Key Decisions

| #   | Decision                                                      | Rationale                                                                                      |
| --- | ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| 1   | OIDC federation, no SPN secret                                | Architecture verdict §3 — closes lateral-movement risk; no long-lived creds                    |
| 2   | ACR (not GHCR)                                                | Architecture verdict §3 — Azure RBAC parity, AKS continuity (3/5 advisors)                     |
| 3   | Short-SHA tags only                                           | Slicing verdict §Slice 2 acceptance literal; rollback discipline                               |
| 4   | `push: main` + `workflow_dispatch`                            | Strict main-only push semantics; dispatch enables acceptance smoke                             |
| 5   | GH Variables (not Secrets) for public values                  | Tenant/Client/Subscription IDs and ACR login server are all public; Variables is right surface |
| 6   | GHA buildx cache                                              | 3-10× faster rebuilds on no-op changes; no infra cost                                          |
| 7   | Fold topic-3 research-brief fix into this PR                  | Hygiene; flagged in user prompt; one-file ~20-line edit                                        |
| 8   | Single workflow file with 2 jobs (backend+frontend), parallel | Standard pattern; explicit; no matrix complexity                                               |

### Open Questions (None Remaining)

All resolved. Ready for `/prd:create`.

---

**Status: Complete.** Run `/prd:create deploy-pipeline-ci-image-publish` next.
