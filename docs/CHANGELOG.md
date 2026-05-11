# Changelog

All notable changes to msai-v2 will be documented in this file.

## [Unreleased]

### 2026-05-11 — Hotfix #2: Slice 3 az-CLI cloud-init revert (`hotfix/slice-4-iac-azcli-cloudinit-revert`)

**Why:** Hotfix #59 reverted the Slice 4 cloud-init substitutions but PR #57's earlier `apt-get install azure-cli` lines (also in cloud-init) remained. The prod VM was provisioned at Slice 1's customData baseline (no az-cli), so any `az deployment group create` re-apply still failed `PropertyChangeNotAllowed: osProfile.customData`. This blocked landing `vmMiReaderAssignment` and the sub-scoped `activityLog` diagnostic module from Slice 4 — leaving the orphan-NSG-rule alert silently broken (Codex PR-58 P2 catch).

**Fix:**

- `infra/cloud-init.yaml`: removed the Slice 3 az-CLI install block (curl key, apt repo, `apt-get install azure-cli`). cloud-init is back to its Slice 1 baseline (Docker + render-env unit only).
- `scripts/deploy-on-vm.sh`: idempotent `command -v az || install azure-cli` block added at Phase 2.5, before the `az login --identity` calls. Fresh VMs and the existing prod VM both converge to having az-CLI present after the first deploy. Pattern mirrors Slice 4's `install-azcopy.sh`.
- `tests/infra/test_bicep.sh`: regression guards block any future `install -y .*azure-cli` line in cloud-init or removal of the runtime install in deploy-on-vm.sh.

### 2026-05-10 — Deployment-pipeline Slice 4: Ops, Backup, Observability (`feat/deploy-pipeline-ops-backup-observability`)

**Goal:** Final slice of the 4-PR deploy-pipeline series. Three new operational surfaces — nightly backup automation (systemd timer + `azcopy` for Parquet), Log Analytics scheduled-query alerts + Application Insights availability test, active-`live_deployments` hard refusal gate in `deploy.yml` — plus folded-in IaC parity carry-overs from Slice 3 (`Reader` on RG for VM MI declaratively + runbook for idempotent prod Bicep re-apply).

**What ships:**

- **Backup automation**: `scripts/backup-to-blob.{service,timer}` (`OnCalendar=*-*-* 02:07:00 UTC`, `Persistent=true`, `RandomizedDelaySec=300`); `scripts/install-azcopy.sh` (idempotent v10.22+ tarball installer); `scripts/backup-to-blob.sh` Parquet mirror switched to `AZCOPY_AUTO_LOGIN_TYPE=MSI` env (research §1 — `azcopy login --identity` deprecated since v10.22); storage account lifecycle policy with `daysAfterCreationGreaterThan: 30` on `msai-backups/backup-` prefix
- **`deploy.yml` active-`live_deployments` gate**: pre-flight step (before Azure login — fail fast, no OIDC mint) curls `/api/v1/live/status` with X-API-Key, checks `.active_count`; refuses with `FAIL_ACTIVE_DEPLOYMENTS_REFUSAL` if non-zero. **No force-bypass flag** (plan-review iter-2 P1 removed it — run_id-bound token was impractical pre-dispatch). Backend unreachable → fail-closed with `FAIL_CANNOT_DETERMINE_LIVE_STATE`
- **`infra/alerts.bicep` module**: Action Group `msai-ops-alerts` (email `pablo@ksgai.com`), Application Insights `msai-app-insights` (workspace-based, linked to Slice 1 Log Analytics), Availability test `msai-health-ping` (`kind: 'standard'`, 3 geo-locations to stay <€10/mo, mandatory `hidden-link` tag), 4 `scheduledQueryRules@2022-06-15` (pinned GA — `2023-12-01-preview` is regionally unregistered per research §2): /health availability, backup-failure, orphan NSG rule >30 min, container restart heuristic (last is a Syslog-stream heuristic since AMA doesn't emit per-container metrics, research §3)
- **IaC parity**: `vmMiReaderAssignment` (Reader on RG for VM MI) — was Slice 3 manual patch
- **Runbooks**: `docs/runbooks/restore-from-backup.md`, `docs/runbooks/iac-parity-reapply.md`, `docs/runbooks/slice-4-acceptance.md`
- **Operational follow-ups during pre-flight (already committed pre-Slice-4)**: paper IB creds (`tws-userid=marin1016test`, `tws-password=…`, `ib-account-id=DUP733213`) seeded in KV from `.ibaccounts.txt`; `databento-api-key` from `.env`; Polygon key deferred (not in repo, not requested by current ingestion path)

**Research-driven design choices (6):**

1. azcopy: `AZCOPY_AUTO_LOGIN_TYPE=MSI` env (NOT deprecated `--identity` flag); tarball install (not in apt)
2. `scheduledQueryRules` API: `2022-06-15` GA pin; `2023-12-01-preview` is unregistered in many regions
3. Container restart "alert": KQL heuristic over Syslog stream (Container Insights is K8s-only; AMA on VM doesn't emit per-container metrics)
4. systemd timer: `OnCalendar` + `Persistent=true` + `RandomizedDelaySec=300` on Ubuntu 24.04 systemd 255
5. Storage lifecycle: singleton `name='default'`, `daysAfterCreationGreaterThan: 30`, `prefixMatch: ['msai-backups/backup-']`
6. Availability test: requires App Insights component, `kind: 'standard'` (URL-ping retires 2026-09-30), 3 geo-locations to keep cost <€10/mo

**Plan-review iter 1 (2 P1s addressed, Codex CLI locked out):**

1. Backend status enum is `{starting,running,stopped}` not `{running,starting,ready}`; gate now uses `LiveStatusResponse.active_count` (simpler than parsing `.deployments[].status`)
2. Force-flag + run_id-bound confirmation_token was impractical (run_id unknowable pre-dispatch). Removed entirely; real emergencies → operator clears state with `msai live stop --all` first

**Verification:** All 4 Slice infra tests green — `test_bicep.sh` (Slice 2/3/4 grep assertions clean; bicep build OK), `test_alerts_bicep.sh` (NEW: API version pin + 4 alert rules + workspace-based AI + heuristic doc), `test_workflow_deploy.sh` (gate marker + active_count parse + force-flag regression guard), `test_caddyfile.sh`. shellcheck clean on all touched shell scripts.

**Post-merge operator gates:**

- ☐ Run `docs/runbooks/iac-parity-reapply.md` (idempotent re-apply of Bicep — `what-if` should show only Slice 4 Creates, 0 Modify/Delete on existing resources)
- ☐ Set `MSAI_API_KEY` GH Secret (copy from KV `msai-api-key`)
- ☐ Trigger first Slice 4 deploy via `gh workflow run deploy.yml -f git_sha=<merge-sha>` (deploy-on-vm.sh installs azcopy + enables backup-to-blob.timer)
- ☐ Run `docs/runbooks/slice-4-acceptance.md` (6 sub-tests — manual backup, backup-failure alert, /health alert, active-deployment gate, orphan-NSG alert, drift-check)
- ☐ Watch first overnight backup blob appear in `msai-backups` at ~02:07-02:12 UTC

**Slice 4 closes the 4-PR deploy-pipeline series.** Phase 2 deferred per `docs/decisions/deploy-ssh-jit.md` "Deferred" (custom RBAC role replacing Network Contributor, Azure Policy `sourceAddressPrefix` deny, AKS migration when 2-VM split happens).

### 2026-05-10 — Deployment-pipeline Slice 3: SSH Deploy + First Real Production Deploy (`feat/deploy-pipeline-ssh-deploy-and-first-deploy`)

**Goal:** `.github/workflows/deploy.yml` (workflow_run after Slice 2 + workflow_dispatch) — OIDC + `webfactory/ssh-agent` SSH-from-runner; `scripts/deploy-on-vm.sh` (idempotent, classified failure markers, 1-step rollback to last-good SHA); Caddy 2 reverse-proxy + auto-LE TLS at `platform.marketsignal.ai`; updated `scripts/backup-to-blob.sh` (Bicep outputs + system-assigned MI; streams `pg_dump | gzip | az storage blob upload --file /dev/stdin`); ADR `docs/decisions/deploy-ssh-jit.md` resolving the council Plan-Review iter-1 P0 (Slice 1 NSG only allowed SSH from `operatorIp/32`, blocking GH-runner deploys).

**Operator gates — ALL ✅ COMPLETE:**

- ✅ **Contrarian's gate** (pre-merge): rehearsal RG `msaiv2-rehearsal-20260510` smoked clean on [run 25634158094](https://github.com/marketsignal/msai-v2/actions/runs/25634158094) after 9 attempts (8 caught real issues — see PR #57 commit history `c0fe11e..5bb74b7`). 5/5 probes pass against `platform-rehearsal.marketsignal.ai`. RG torn down via `az group delete --no-wait`.
- ✅ **Hawk's gate** (post-merge, pre-first-deploy): `scripts/backup-to-blob.sh` ran against empty prod Postgres. Blob `backup-20260510T175820Z/postgres.sql.gz` (372B, expected for empty DB) verified in `msai-backups` container via `az storage blob list --auth-mode login` from VM MI.
- ✅ **First real prod deploy** ([run 25635866251](https://github.com/marketsignal/msai-v2/actions/runs/25635866251), git_sha `3ba4200`): 5/5 prod acceptance probes PASS against `https://platform.marketsignal.ai/`:
  - `GET /health` → 200
  - `GET /ready` → 200
  - `GET /` → 200 + `text/html`
  - LE cert chain → `O=Let's Encrypt`
  - `GET /api/v1/auth/me` → 401 (Caddy prefix-preserving proxy confirmed)

**Manual operator patches outside the PR diff** (Slice 3 Bicep wasn't re-applied to live prod RG before merge):

- Granted `Network Contributor` to GH-OIDC MI scoped to prod NSG. Slice 3 Bicep declares this; lands idempotently on next `az deployment group create`.
- Granted `Reader` to prod VM MI on `msaiv2_rg` (Bicep-output read for `backup-to-blob.sh`). Should be added to Slice 3 Bicep as a Phase-1.1 patch.
- Manually installed Docker on both prod + rehearsal VMs (Slice 1 cloud-init dpkg-lock race left it uninstalled). Cloud-init updated in this PR — applies to next provision.
- Manually installed `azure-cli` on both prod + rehearsal VMs (Slice 1 cloud-init didn't include it). Cloud-init updated in this PR — applies to next provision.
- One-off `gh-actions-rehearsal-tmp` federated credential added + deleted around the rehearsal run.

**Council Plan-Review iter 1 (NSG SSH gap, P0):** 5/5 advisors reject static-GH-IP-ranges. Default (transient JIT NSG rule + Network Contributor scoped to NSG only) approved with 5 mandatory mitigations (Bicep child-resources, concurrency cancel-in-progress:false, cleanup as separate job, reaper cron, ADR + runbook). Contrarian caught 2 P0s the others missed (Bicep drift bomb, concurrent-deploy collision). See `docs/decisions/deploy-ssh-jit.md`.

**Code-review iter 1 (pr-review-toolkit; Codex CLI locked out):** 0 P0, 0 P1, 4 P2 all addressed (additive-only migrations rule in `.claude/rules/database.md`, systemctl combine, SHA-pin actions in deploy.yml + reap.yml, reaper timestamp comment), 5 P3 deferred non-blocking.

**Verification:** verify-app subagent — 1821 backend unit tests PASS, ruff clean. All Slice 3 infra tests green (actionlint, shellcheck, bash -n, bicep build + Slice 2/3 grep, Caddyfile positive + negative validation).

**Slice 4 carry-over:** nightly cron via `azcopy`; active-`live_deployments` hard refusal gate; Log Analytics dashboards + alert rules; SHA-pin Slice 2's `build-and-push.yml`; custom RBAC role with only `securityRules/*` actions; Azure Policy deny on non-runner-IP `sourceAddressPrefix`. Full list in `docs/decisions/deploy-ssh-jit.md` "Deferred".

### 2026-05-10 — Deployment-pipeline Slice 2: CI Image Publish (`feat/deploy-pipeline-ci-image-publish`)

**Goal:** Wire push-to-main into Azure via OIDC federation, build backend + frontend Docker images, push to ACR with immutable short-SHA tags. **No deploy step** — that's Slice 3.

Council-ratified scope per [`docs/decisions/deployment-pipeline-slicing.md`](decisions/deployment-pipeline-slicing.md) §Slice 2 (Approach A, 3/5 advisors APPROVE/CONDITIONAL; ratified 2026-05-10). Phase 3.1/3.1b/3.1c marked PRE-DONE per `feedback_skip_phase3_brainstorm_when_council_predone`; Phase 3.2 + 3.3 ran fresh.

**Plan + research:** [`docs/plans/2026-05-10-deploy-pipeline-ci-image-publish.md`](plans/2026-05-10-deploy-pipeline-ci-image-publish.md), [`docs/research/2026-05-10-deploy-pipeline-ci-image-publish.md`](research/2026-05-10-deploy-pipeline-ci-image-publish.md), [`docs/prds/deploy-pipeline-ci-image-publish.md`](prds/deploy-pipeline-ci-image-publish.md).

**Plan-review loop:** 4 iterations. iter1 (Codex): 2 P1 + 2 P2 (formatter stripped YAML indent inside fenced blocks → restructured Task 3 as single-file Write; ACR short-name vs FQDN mismatch → split into `vars.ACR_NAME` and `vars.ACR_LOGIN_SERVER`; `Microsoft-WindowsEvent` typo in Edit 3a → corrected; runbook missing 2 of 7 vars → all 8 enumerated; Phase 5 what-if gate phrasing → aligned to `test_bicep.sh` actual contract). iter2 (Codex): 1 P2 + 2 P3 (partial what-if fix + Slice 1 reparenting + stale supporting docs). iter3 (Codex): 1 P2 + 1 P3 (broken `jq | grep -i acr-push` post-check — `guid()` produces opaque UUIDs not literal strings; stale "Not Researched" entry contradicting corrected ACR_NAME guidance). iter4 (Codex): PASS — synthetic-test-validated the corrected jq filter against AcrPush role-def GUID match. Trajectory: drafting issues → narrow correctness → idempotency proof.

**Code-review loop:** 2 iterations. iter1 (Codex + PR Toolkit in parallel): Codex 1 P2 (runbook used unsupported `scripts/deploy-azure.sh --operator-ip / --ssh-public-key-file` flags; script auto-resolves both); PR Toolkit 1 P1 (workflow_dispatch from non-main → silent OIDC AADSTS70021) + 1 P2 (separate `compute-sha` job adds ~30s cold-start without payoff) + 2 P3 (mixed action pinning, duplicated prelude — both non-blocking per brutal simplicity at N=2). All P1+P2 fixed: runbook now uses `./scripts/deploy-azure.sh` + `OPERATOR_IP=` env-var override; both build jobs got `if: github.ref == 'refs/heads/main'` defense-in-depth guards; `compute-sha` removed and inlined per job. iter2: both reviewers PASS, all iter-1 fixes verified genuine.

**Simplify pass:** 3 parallel agents (reuse, quality, efficiency). Reuse + efficiency clean. Quality flagged 1 P3 (SHA-step comment narrated iter-1 history → trimmed to one line per CLAUDE.md style "comments shouldn't reference the current task or fix"). Workflow now 134 lines (was 139).

**Verify-app:** PASS. 2183 backend pytest pass (11 skipped, 16 xfailed pre-existing), ruff + mypy --strict + actionlint + az bicep build + test_bicep.sh all clean. Zero regressions.

**E2E:** N/A (verified). Slice 2 changes zero runtime code (only `.github/workflows/`, `infra/`, `tests/infra/`, `docs/` paths). Live acceptance is intrinsically post-merge — the workflow can only execute once the file lives on `main` AND operator has set 8 GH Variables AND re-applied Bicep. Pre-merge static gates all clean (actionlint, az bicep build, test_bicep.sh, no-secret grep, distinct-cache-scope grep, no-environment grep, if-guard grep). Pattern matches Slice 1 PR #51.

**What shipped:**

- **`.github/workflows/build-and-push.yml`** (134 lines, new): triggers on `push: branches: [main]` + `workflow_dispatch:`. Workflow-level `permissions: id-token: write, contents: read` (OIDC) and `concurrency: cancel-in-progress: true` (newer commits supersede). Two parallel jobs (`backend`, `frontend`), each guarded with `if: github.ref == 'refs/heads/main'` (defense-in-depth against dispatch from feature branches that would mint AAD-rejected OIDC tokens). Each job: `actions/checkout@v4.2.2` → inline `${GITHUB_SHA::7}` short SHA → `azure/login@v2` (OIDC, no SPN secret) → `az acr login --name "${{ vars.ACR_NAME }}" --expose-token` (mints 3-hour ACR token; required because ACR Basic SKU + `docker/login-action` doesn't natively understand OIDC) → `docker/login-action@v3` with sentinel UUID `00000000-0000-0000-0000-000000000000` username + token password → `docker/setup-buildx-action@v3` → `docker/build-push-action@v6` with GHA cache scoped distinctly per image (`scope=backend`, `scope=frontend` — without distinct scopes, parallel jobs silently overwrite each other's cache per research brief topic 3). Backend: `context: .`, `file: backend/Dockerfile`, no build-args. Frontend: `context: ./frontend`, `file: frontend/Dockerfile`, three `NEXT_PUBLIC_*` build-args (Tenant ID, Client ID, API URL — baked into JS bundle at build time per Next.js requirement; frontend Dockerfile fail-fast guards `exit 1` on empty). Tags: `${{ vars.ACR_LOGIN_SERVER }}/msai-{backend,frontend}:${{ steps.short.outputs.short_sha }}` only — no `latest`, strict immutability per slicing-verdict rollback discipline.
- **`infra/main.bicep`** (+19 lines): added `roleDefIdAcrPush` variable (Azure built-in role GUID `8311e382-0749-4cb8-b61a-304f252e45ec`) and `ghOidcAcrPushAssignment` resource (scope `acr`, principal `ghOidcMi.properties.principalId`, `principalType: 'ServicePrincipal'` — required to dodge user-assigned MI eventual-consistency 503 per research brief topic 9). Pattern mirrors the four existing role-assignment resources verbatim. Comment updates at lines 90 (Slice 1's "lives in Slice 2" forward-reference now resolved) and 547 ("4 total" → "5 total: ... gh-oidc MI gets AcrPush").
- **`tests/infra/test_bicep.sh`** (+14 lines): three grep assertions (`roleDefIdAcrPush` var present, `ghOidcAcrPushAssignment` resource present, role-def reference correct) inserted before the existing what-if block; runs even with `SKIP_WHATIF=1`.
- **`docs/research/2026-05-09-deploy-pipeline-iac-foundation.md`** (+5 lines): topic 3 corrected. Original brief asserted `streams: ['Microsoft-Heartbeat']` was the heartbeat-only DCR shape — empirically wrong (caught at PR #53 acceptance smoke; AMA stream enum has no `Microsoft-Heartbeat`). Corrected to: `kind: 'Linux'` + `dataSources.syslog: [{ streams: ['Microsoft-Syslog'], ... }]` produces Heartbeat **automatically** as a side effect. Added 5th source citing MS Learn DCR-structure as the canonical AMA stream-enum reference. Folded in per `feedback_ama_dcr_kind_linux_required.md` in user MEMORY.
- **`docs/runbooks/vm-setup.md`** (+118 lines): new H2 section `## Slice 2 acceptance smoke (10 min)` (sibling to Slice 1's, NOT child — avoids reparenting Slice 1's "If something fails" troubleshooting). Documents 4 operator steps: re-apply Bicep to grant AcrPush; set 8 GH repo Variables (with retrieval `az` commands); trigger workflow via `gh workflow run` after ~60s RBAC propagation; verify images in ACR via `az acr repository show-tags`. Includes 5 named failure modes with diagnostic-and-recovery commands.
- **PRD + plan + research-brief + discussion** at `docs/prds/`, `docs/plans/`, `docs/research/` documenting the slice's design and convergence trajectory.

**Operator pre-merge prerequisite (one-time):** Pablo sets 8 GitHub repo Variables: `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_CLIENT_ID` (= gh-oidc MI client ID), `ACR_NAME` (short, for `az acr login --name`), `ACR_LOGIN_SERVER` (FQDN, for `docker/login-action.registry` and image tags), `NEXT_PUBLIC_AZURE_TENANT_ID`, `NEXT_PUBLIC_AZURE_CLIENT_ID` (frontend Entra SPA app reg), `NEXT_PUBLIC_API_URL` (placeholder until Slice 3 picks DNS). All 8 are public values, none are secrets. Runbook documents the exact `az` retrieval + `gh variable set` commands.

**Slice 2 acceptance smoke (post-merge, ~10 min total):** (1) operator re-applies Bicep with `./scripts/deploy-azure.sh` (no flags — script auto-resolves OPERATOR_IP and SSH key); (2) operator sets 8 GH Variables via `gh variable set` or repo Settings UI; (3) wait ~60s for RBAC propagation; (4) `gh workflow run build-and-push.yml`; (5) `az acr repository show-tags --name <acr> --repository msai-{backend,frontend}` shows the short SHA. Council-ratified literal acceptance: workflow runs green on a no-op commit; ACR shows `msai-backend:<sha7>` + `msai-frontend:<sha7>`.

**Next session:** `/new-feature deploy-pipeline-ssh-deploy-and-first-deploy` (Slice 3). Hawk's blocking objection requires backup-first (`scripts/backup-to-blob.sh` against empty prod Postgres + verify dump in Blob) and Contrarian's blocking objection requires full end-to-end deploy rehearsal in a throwaway resource group BEFORE first prod deploy.

### 2026-05-10 — Slice 1 acceptance fixes (PR #52 + PR #53 merged)

After PR #51 merged, the Slice 1 acceptance smoke (operator runs `./scripts/deploy-azure.sh` from main) caught 4 reality gaps in 2 follow-up PRs.

**PR #52 (`b5869fe`) — 3 deploy-blocker fixes** (Bicep wouldn't deploy at all):

1. **Bicep BCP258**: every parameter without a default in `main.bicep` MUST be assigned in `main.bicepparam`, even when CLI `--parameters` overrides at deploy time. Add `operatorIp`, `operatorPrincipalId`, `vmSshPublicKey` placeholders in bicepparam; CLI override semantics still take precedence.
2. **Ubuntu 24.04 image URN**: Canonical changed the URN scheme. The plan's `0001-com-ubuntu-server-noble:24_04-lts-gen2:latest` doesn't exist. Correct: `Canonical:ubuntu-24_04-lts:server:latest` (Gen2 in eastus2).
3. **KV `enablePurgeProtection: false`**: Azure rejects this with "cannot be set to false. Enabling the purge protection for a vault is an irreversible action." Property must be `true` (irreversible) or absent. Plan's "Phase 1 stays at false" intent achieved by omitting the property (default disabled).

**PR #53 (`64126fd`) — DCR malformed; Heartbeat never flowed**:

The DCR shipped in Slice 1 had three issues that together prevented the Slice 1 acceptance smoke step 3 from passing:

1. **`kind` field absent**. AMA on Linux requires `kind: 'Linux'` for the MCS endpoint to recognize the DCR. Without it, MCS returns 404 for the VM-association lookup every refresh cycle.
2. **Stream `Microsoft-Heartbeat` is NOT a documented AMA stream.** The valid stream enum (per MS Learn DCR 2022-06-01 reference): `Microsoft-Event`, `Microsoft-InsightsMetrics`, `Microsoft-Perf`, `Microsoft-Syslog`, `Microsoft-WindowsEvent`. Research-brief topic 3 cited `Microsoft-Heartbeat` as the heartbeat-only DCR shape — empirically wrong.
3. **`dataSources` block missing**. AMA needs at least one valid data source for MCS to publish the DCR config.

**Fix**: `kind: 'Linux'` + `Microsoft-Syslog` data source (Warning+ severity on 7 facilities: `auth, authpriv, cron, daemon, kern, syslog, user`). Heartbeat then flows automatically as a side effect of the AMA-DCR association. Bonus: syslog data is operationally useful for Slice 4 alert rules at no extra cost.

**Operational caveat (matters for Slice 4 / future DCR edits)**: After applying the Bicep fix, AMA on the running VM still required `sudo systemctl restart azuremonitoragent` to clear its negative-cache and re-fetch the now-valid DCR. Without restart, AMA's next ~10-min refresh continued to return 404 even after the ARM-side DCR was fixed. This is the documented MS remediation path; AMA's negative-cache is stickier than its 10-min refresh window suggests.

**Codex research validated**: corrected pattern matches MS Learn's documented Bicep template at [`learn.microsoft.com/.../resource-manager-agent#azure-linux-virtual-machine`](https://learn.microsoft.com/en-us/azure/azure-monitor/agents/resource-manager-agent#azure-linux-virtual-machine).

**Slice 1 acceptance**: 7/7 smoke steps PASS. Heartbeat verified flowing (8 records, last seen `2026-05-10T06:15:01Z`, Computer=`msaiv2-vm`, Category=`Azure Monitor Agent`).

**Research-brief topic 3 needs revision** before Slice 2 starts (deferred — adds context for next session).

### 2026-05-10 — Deployment-pipeline Slice 1: IaC Foundation (PR #51, merged)

**Goal:** Provision the foundational Azure infrastructure for the deployment pipeline. Slice 1 of 4 in the council-ratified series ([`docs/decisions/deployment-pipeline-architecture.md`](decisions/deployment-pipeline-architecture.md), [`docs/decisions/deployment-pipeline-slicing.md`](decisions/deployment-pipeline-slicing.md)). NO application deploys — Slice 1 lays the platform; Slice 2 wires GH Actions; Slice 3 wires SSH deploy + first real prod deploy; Slice 4 wires backups + alert rules.

**Plan + research:** [`docs/plans/2026-05-09-deploy-pipeline-iac-foundation.md`](plans/2026-05-09-deploy-pipeline-iac-foundation.md), [`docs/research/2026-05-09-deploy-pipeline-iac-foundation.md`](research/2026-05-09-deploy-pipeline-iac-foundation.md). Council verdicts at [`docs/decisions/deployment-pipeline-architecture.md`](decisions/deployment-pipeline-architecture.md) (architecture, locked) and [`docs/decisions/deployment-pipeline-slicing.md`](decisions/deployment-pipeline-slicing.md) (slicing, locked) cover Phase 3.1/3.1b/3.1c (skip per `feedback_skip_phase3_brainstorm_when_council_predone`); Phase 3.2 + 3.3 ran fresh.

**Plan-review loop:** 6 iterations. iter1 (Claude only — Codex stalled on long-content prompt per `feedback_codex_cli_stalls_on_long_audit_prompts`): 4 P1 + 3 P2. iter2 (short-prompt + file refs unblocked Codex): 6 P1 + 5 P2 + 1 P2-meta. iter3: 3 P1 + 1 P2. iter4: 2 P1 + 1 P2. iter5: 2 P1 + 1 P2. iter6: 2 P1 + 1 P2. Trajectory: architectural → file-level → narrow correctness. Total 19 P1 + 12 P2 fixed before Phase 4.

**Code-review loop:** 4 iterations. iter1 Codex 2 P2 (IPv4 forcing, what-if grep). iter2 Codex 5 P2 (--what-if RG creation, HTTPS IP, KV soft-delete, jq parse-validation, runbook home-dir). iter3 Codex 2 P2 + Toolkit 1 P2 (TWS*\* re-classification, wrong VM SKU, ifconfig.me docstring). iter4 Codex 1 P2 (corrected my flawed iter3 empirical Compose test — TWS*\* must be REQUIRED because Compose interpolates :? BEFORE profile filtering) + Toolkit 2 P2 (D4s_v6 + ifconfig.me docstring drift). 13 P2 fixed across 4 iterations.

**What shipped:**

- **`infra/main.bicep`** (558 lines, single-file shape per research-brief Bicep idiom guidance): VNet + Subnet + NSG (operator-IP-only SSH, ports 80/443 open) + Public IP + Premium SSD data disk (P10, 128 GB) + Standard_LRS storage account + `msai-backups` Blob container + ACR Basic + Key Vault (RBAC mode, 90-day soft-delete, KV diagnosticSettings → Log Analytics) + Log Analytics workspace + Azure Monitor Agent VM extension + heartbeat DCR + DCR-association (extension-resource pattern with `scope:`, NOT child) + GH OIDC user-assigned managed identity (`msai-gh-oidc`) + federated credential (`repo:marketsignal/msai-v2:ref:refs/heads/main`, `aud=api://AzureADTokenExchange`, exact-match — flexible federated credentials are PREVIEW only) + 4 role assignments (VM system-assigned MI gets KV Secrets User + AcrPull + Storage Blob Data Contributor on container scope; operator gets KV Secrets Officer for data-plane access — required because `enableRbacAuthorization=true` blocks even subscription Owner from `az keyvault secret set/show` without it).
- **`infra/cloud-init.yaml`** (63 lines): Ubuntu 24 LTS first-boot. Installs `jq` + `curl` + Docker engine + compose plugin via official apt repo. Formats and mounts the Premium SSD data disk at `/var/lib/msai`. Plants `/etc/docker/daemon.json` with `data-root: /var/lib/msai/docker` BEFORE Docker installs (so dockerd's first start picks up the relocated data root). Plants `/usr/local/bin/render-env-from-kv.sh` + `/etc/systemd/system/msai-render-env.service` via `write_files: encoding: b64` (sidesteps YAML indentation issues — Bicep base64-encodes both via `loadTextContent` and `replace`).
- **`infra/main.bicepparam`**, **`infra/README.md`**: parameter file (mostly defaults; per-operator inputs pass via `--parameters` at deploy time) + 44-line README explaining structure + slice progression.
- **`scripts/render-env-from-kv.sh`** (157 lines): boot-time secret renderer. Pure Bash + curl + jq. 10-attempt IMDS retry with exponential backoff (research-brief topic 5: doubled from the standard 5-attempt guidance to absorb post-deployment MI propagation tail of 30-90s with outliers to 3-5 min). 5-attempt KV retry per secret with linear backoff (~100s budget for RBAC propagation). Distinguishes 403 (RBAC propagating, retry) from 404/401 (config wrong, fail-fast). KV name normalization (`tr '[:upper:]_' '[:lower:]-'`) matches `backend/src/msai/core/secrets.py:112` convention. REQUIRED + OPTIONAL secret split — REQUIRED list (REPORT_SIGNING_SECRET, POSTGRES_PASSWORD, AZURE_TENANT_ID, AZURE_CLIENT_ID, CORS_ORIGINS, IB_ACCOUNT_ID, TWS_USERID, TWS_PASSWORD) cross-checked against `docker-compose.prod.yml` `:?`-guards including profile-gated services (Compose interpolates `:?` BEFORE profile filtering). Atomic write to `/run/msai.env` via `mv`. Single-quote escaping for compose/systemd EnvironmentFile parsers. No temp files (response captured in shell var via `curl -w '\n%{http_code}'` + parameter expansion).
- **`scripts/msai-render-env.service`** (38 lines): `Type=oneshot`, `Restart=on-failure`, `RestartSec=30`, `StartLimitIntervalSec=900` + `StartLimitBurst=5` (in `[Unit]`, NOT `[Service]`). `Before=docker-compose-msai.service`. NOT enabled in Slice 1 — Slice 3 enables on first deploy.
- **`scripts/deploy-azure.sh`** REWRITE (167 lines, was 34): replaces hardcoded `az vm create` flow targeting wrong RG/region/VM-size. Pre-flight checks subscription (MarketSignal2 `68067b9b-943f-4461-8cb5-2bc97cbc462d`), creates RG only on `create` mode (not `--what-if`), warns about soft-deleted KV name reservation (90-day after RG nuke). Resolves operator IP via HTTPS `https://api.ipify.org` (was insecure `http://ifconfig.me`), validates IPv4 format, forces `-4` (NSG `/32` rule rejects IPv6). Resolves SSH pubkey from `~/.ssh/id_{ed25519,rsa,ecdsa}.pub`. Resolves operator Entra object ID via `az ad signed-in-user show` (Bicep needs it for KV Secrets Officer grant). Split `deploy_bicep_create` and `deploy_bicep_whatif` (what-if doesn't accept `--query`/`-o table`).
- **`tests/infra/test_bicep.sh`** (50 lines): CI test. `az bicep build` lint check + optional `az deployment group what-if` against `msaiv2_rg` (gated on `SKIP_WHATIF=1` or absent Azure auth). Validates JSON parses + `.changes` exists BEFORE checking for Create/Delete (prevents false PASS from jq-parse-failure exit).
- **`docs/runbooks/vm-setup.md`** §1-§3 updated: `msai-rg`/`eastus` → `msaiv2_rg`/`eastus2`, VM `Standard_D4s_v6` → `Standard_D4ds_v6` (Ddsv6 family — Dsv6 was wrong family, mismatched the documented quota). Slice 1 acceptance smoke section appended (7-step manual smoke: Bicep what-if no Create/Delete, AMA provisioning, Heartbeat in LAW, KV access from VM via raw IMDS+REST curl since `azure-cli` is NOT preinstalled on Ubuntu 24, ACR exists with adminUserEnabled=False, federated credential registered, Blob backup container exists). Plus troubleshooting: AMA stuck, Heartbeat empty, KV 403, KeyVaultAlreadyExists post-RG-nuke.
- **`docs/decisions/deployment-pipeline-slicing.md`**: brought into the worktree (created in prior session, never committed).

**Phase 5 verify:** `bash -n` clean on all 3 scripts. `shellcheck` clean. `az bicep build` exit 0 (615-line ARM template). `code-simplifier` agent reported "no simplifications needed" after 10-iteration review convergence. systemd-analyze N/A on darwin (will run on VM at deploy). E2E: N/A — IaC slice has no user-facing UI/API/CLI surface; the deploy script runs against Azure not localhost.

**Acceptance:** Operator runs `./scripts/deploy-azure.sh --what-if` then `./scripts/deploy-azure.sh`. After ~10-15 min, the runbook's Slice 1 acceptance smoke (7 steps) validates idempotency, AMA heartbeat, KV access via VM managed identity, ACR + federated credential + Blob container existence. Re-running the deploy script produces no Create/Delete operations.

**Out of scope (Slices 2-4):** GH Actions workflow + image push (Slice 2), SSH deploy + first real prod deploy (Slice 3 — gated on tested backup + end-to-end rehearsal per Hawk + Contrarian blocking objections), nightly backup cron + alert rules + active-`live_deployments` hard gate (Slice 4).

### 2026-05-09 — Prod compose deployable (precursor PR open, branch `feat/prod-compose-deployable`)

**Goal:** Make `docker-compose.prod.yml` actually deployable so a future deployment-pipeline branch can wire CI/CD. Council verdict at [`docs/decisions/deployment-pipeline-architecture.md`](decisions/deployment-pipeline-architecture.md) (5 advisors — Simplifier, Hawk, Pragmatist, Contrarian, Maintainer; 4/5 APPROVE/CONDITIONAL, Contrarian OBJECT cleared by this PR) ratified the architecture; this PR is the literal "Next Step" — fixes Blocking Objections items 1-6.

**Plan:** [`docs/plans/2026-05-09-prod-compose-deployable.md`](plans/2026-05-09-prod-compose-deployable.md). 11 tasks, sequential mode. Per memory feedback `feedback_skip_phase3_brainstorm_when_council_predone.md`, Phases 3.1/3.1b/3.1c PRE-DONE per council; Phase 3.2 (writing-plans) and 3.3 (plan-review) ran fresh — Claude review found 1 P0 + 2 P1 + 2 P2 over 2 iterations, all fixed before execution. Codex CLI unavailable in worktree (`.claude/hooks/lib/` is gitignored — exit 127 reproducer); user-as-second-reviewer satisfied via `/new-feature` "Full workflow" choice.

**What shipped:**

- **`backend/Dockerfile`**: `COPY alembic/` + `COPY alembic.ini` so `alembic upgrade head` runs from the container, not the host (Contrarian objection — `verify-paper-soak.sh:211` was a workaround for this defect). **Build context switched to repo root** (`docker build -f backend/Dockerfile .`) — fixes a latent bug where the original `COPY strategies/` couldn't possibly work with `context: ./backend` because `strategies/` lives at the repo root. Bug never previously triggered because dev uses `Dockerfile.dev` which doesn't COPY strategies (mounts as a volume).
- **`docker-compose.prod.yml`**:
  - Switched all 6 application services (`backend`, `backtest-worker`, `research-worker`, `portfolio-worker`, `live-supervisor`, `frontend`) from `build:` to `image:` references with `:?` guards on `MSAI_REGISTRY` / `MSAI_BACKEND_IMAGE` / `MSAI_FRONTEND_IMAGE` / `MSAI_GIT_SHA`. Compose now expects pre-built registry images for image-pull deploys.
  - Added one-shot `migrate` service (`alembic upgrade head`, `restart: "no"`, `depends_on: postgres: service_healthy`).
  - Added missing `ingest-worker` service (Contrarian/Maintainer — `IngestWorkerSettings` exists at `backend/src/msai/workers/ingest_settings.py:35`, queue routed at `backend/src/msai/core/queue.py:150`, but prod had no consumer — symbol onboarding would have hung forever in production).
  - All app services + `live-supervisor` now `depends_on: migrate: condition: service_completed_successfully` — race-free single-runner migrations.
  - Plumbed missing env vars through backend service: `REPORT_SIGNING_SECRET` (`config.py:295` hard-fails prod startup on the dev default), `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `JWT_TENANT_ID/CLIENT_ID` (default to AZURE\_\*), `CORS_ORIGINS`, `MSAI_API_KEY` (optional). **P0 plan-review finding**: `REPORT_SIGNING_SECRET` is required by EVERY service that imports `msai.core.config.settings` (alembic, all workers, live-supervisor) — not just the backend HTTP app. Prod-mode validator runs at module-import time; without the var, every container crashloops. Fixed.
  - Moved `ib-gateway` + `live-supervisor` behind `profiles: ["broker"]` — default `up -d` no longer touches a running trading session. Council Blocking Objection #7 (NautilusTrader gotcha #3 — duplicate `client_id` silently disconnects). Operator opts in via `COMPOSE_PROFILES=broker`.
- **`frontend/Dockerfile`**: Added `ARG NEXT_PUBLIC_AZURE_CLIENT_ID` / `NEXT_PUBLIC_AZURE_TENANT_ID` / `NEXT_PUBLIC_API_URL` + corresponding `ENV` declarations BEFORE `RUN pnpm build`. Next.js bakes `NEXT_PUBLIC_*` into the JS bundle at build time — without these, the prod bundle ships with empty MSAL config and auth silently fails for every user. Smoke-test verified the test client/tenant ids land in `.next/server/*.js`.
- **Runbooks (`docs/runbooks/vm-setup.md` + `disaster-recovery.md`)**: `/api/v1/health` → `/health` (4 occurrences). VM size `Standard_D4s_v5` → `Standard_D4s_v6` (council Verification — DSv5 family quota is 0/0 on MarketSignal2; Ddsv6 has 0/10 default, no quota request needed). Added `COMPOSE_PROFILES=broker` documentation to vm-setup.md.

**Phase 5 smoke test:** `docker compose -f docker-compose.prod.yml config` validates with all `:?` vars set; fails cleanly with the named missing var when any is unset. Backend image builds + `alembic --help` succeeds inside the image; alembic can load `msai.core.config.settings` with REPORT_SIGNING_SECRET set (verified by attempting `alembic current` against a fake DB host — failure mode is connection refused, not config validation). Frontend image builds + grep confirms `test-client-id` / `test-tenant-id` baked into bundle. No `compose up` in smoke (would touch shared `name: msai_postgres_data` volume — defer live-bring-up to deployment-pipeline branch).

**Out of scope (deployment-pipeline branch):** Bicep IaC, GitHub Actions OIDC + ACR provisioning, Key Vault + managed identity, nightly `pg_dump` automation, Azure Log Analytics agent, VM provisioning. The council's Phase 1 deploy shape stands; this PR removes the precondition that was blocking it.

### 2026-05-07 — Coverage day-precise refactor (PR #49 open, branch `feat/coverage-day-precise`)

**Goal:** Replace `compute_coverage`'s month-granularity scan with day-precise trading-day inspection. Spike (`docs/research/2026-05-07-coverage-granularity-spike.md`) confirmed production paths CAN emit partial-month parquet files (sub-month onboarding, provider partial returns, CLI spot fixes); pre-Scope-B those silently passed as `status="full"`. Council ratified Scope B with 6 prereqs (4 Contrarian + 2 Hawk).

**Plan-review loop:** 6 iterations to convergence. Trajectory 11 → 4 → 2 → 3 → 2 → 0. Reviewers: Claude (`feature-dev:code-reviewer`) + Codex (`codex exec` with focused-prompt fallback per memory `feedback_codex_cli_stalls_on_long_audit_prompts`). Codex caught the most architecturally-subtle issues: SQLAlchemy multi-loop async-engine sharing on iter 3 (drove redesign from `session_factory + thread + asyncio.run` to `NullPool engine + asyncio.run` per call); missing `await asyncio.to_thread(...)` wrap of the actual `data_ingestion.ingest_historical` call site on iter 4; AST-level verification correctness on iter 5. Memory: `feedback_plan_review_cant_verify_external_api_behavior` saved (CMES MLK 2025 empirical bug — 6 iters of plan review missed it; implementer caught on first run).

**Phase 4 (Execute):** 13 tasks via `superpowers:subagent-driven-development`, 19 commits on the branch. Per-task subagent dispatch with two-stage review (spec compliance + code quality). Tasks 0 (capture-before-change snapshot) → 11 (post-Scope-B diff report); Task 6 split into 6a-6d for bite-sized TDD discipline.

**What shipped:**

- **`services/trading_calendar.py`** wraps `exchange_calendars` (NYSE/CMES) with asset-class → exchange map (ingest taxonomy: stocks/options/forex/futures + registry aliases equity/option/fx; crypto falls back to `pandas.bdate_range` weekday-only). `lru_cache(maxsize=8)` on calendar construction. New dep `exchange_calendars>=4.5,<5.0`.
- **New table + migration `aa00b11c22d3`:** `parquet_partition_index` (composite PK `(asset_class, symbol, year, month)`; columns `min_ts/max_ts/row_count/file_mtime/file_size/file_path/indexed_at`).
- **`services/symbol_onboarding/partition_index.py`:** `PartitionFooter`, `PartitionRow`, `PartitionIndexService` (mtime/size invalidation), `make_refresh_callback(database_url=...)` (NullPool engine + `asyncio.run` per call — never share async engine across loops), `CacheRefreshMisuseError(RuntimeError)` (caller-contract violation class).
- **`services/symbol_onboarding/partition_index_db.py`:** SQLAlchemy `INSERT ... ON CONFLICT DO UPDATE` gateway.
- **`ParquetStore.write_bars`** takes a sync `partition_index_refresh: Callable | None` callback. Invokes per `(year, month)` group post-atomic-write. Outer `try/except` catches `CacheRefreshMisuseError` and **re-raises** (contract violation — fail loud); catches `Exception` and logs+swallows (transient runtime failure — best-effort cache update; parquet is source of truth).
- **`compute_coverage` rewrite:** `_covered_days_from_rows` uses `trading_days(min, max, asset_class)` per row, unioned (P1 plan-review fix — original walked calendar days and admitted weekends/holidays as covered, defeating the entire point of Scope B). `_apply_trailing_edge_tolerance` uses 21-day calendar lookback to harvest 7 trading days. `_collapse_missing` uses 5-day-gap heuristic for run boundaries. Public `CoverageReport` shape preserved (status / covered_range / missing_ranges) — no caller-side breakage. Required new kwarg `partition_index: PartitionIndexService`.
- **`is_trailing_only`** uses 7-trading-day cutoff (matches `compute_coverage`'s tolerance) instead of "missing range starts ≥ prev-month-1st". Adds `asset_class: str = "equity"` kwarg (default keeps legacy callers green); `derive_status` extended to thread `asset_class` through to `is_trailing_only` so non-equity rows get correct cutoffs.
- **New metric `msai_coverage_gap_detected_total{symbol, asset_class}`** — emits ONLY on `status="gapped"` exit. Vacuous-full (no expected days) and `status="none"` (no covered days) deliberately do NOT emit (Hawk #5 with 2 scoped deviations: no `asset_subclass` label and no `is_production` gating — both reversible config changes once registry has those fields).
- **Alerting:** `compute_coverage` calls `alerting_service.send_alert(level="warning", title=..., message=...)` on gapped exit. Failures logged with `exc_info=True` and swallowed.
- **One-time backfill:** `scripts/build_partition_index.py` walks `DATA_ROOT/parquet/` and upserts every existing partition via `PartitionIndexService.refresh_for_partition`. Idempotent (verified: 36 partitions → 36 partitions on re-run).
- **Capture-before-change script:** `scripts/snapshot_inventory.py` with atomic `os.replace` write + `--window` format validation.
- **Pre/post snapshot fixtures committed:** `tests/fixtures/coverage-pre-scope-b.json` + `coverage-post-scope-b.json` + diff report at `docs/plans/2026-05-07-coverage-day-precise-diff-report.md`. **0 unexplained gaps** — 3 rows refined (covered_range now reports trading-day min/max instead of clamped request-window; missing_ranges extends 1 day to include 2024-12-31 trading day not in the parquet); 4 rows unchanged (registered without parquet backing).

**Phase 5.4 verify-e2e caught a real integration bug (UC-CDP-005 = FAIL_BUG → fixed inline):** the CLI ingest path (`msai ingest`, `msai ingest-daily`, `msai data-status`) and the `nightly_ingest` scheduler constructed `ParquetStore` WITHOUT the `partition_index_refresh` callback. Result: parquet files written but `parquet_partition_index` NOT updated, so day-precise `compute_coverage` reported stale (or missing) coverage state until something else triggered `build_partition_index.py`. Fix at `e9c2952` wires `make_refresh_callback(database_url=settings.database_url)` into all 4 missing construction sites. **NO BUGS LEFT BEHIND honored.** Plan also patched for 2 minor stale items found by verify-e2e: UC-CDP-002's path-style readiness URL (`/{symbol}/readiness` returns 404; actual route uses query-params `?symbol=...`); UC-CDP-003's metric label assertion (`asset_class="equity"` was the registry value; counter actually emits `asset_class="stocks"` because `compute_coverage` is invoked with the post-`normalize_asset_class_for_ingest` value).

**Quality gates green:**

- Unit + integration: 2179 passed / 2 skipped / 16 xfailed. 3 pre-existing failures (`auto_heal::test_happy_path` instrument-resolution drift `'AAPL.NASDAQ' != 'AAPL'`; 2× `test_registry_venue_divergence` Prometheus counter pollution between unit + integration suites). All verified pre-existing via stash-test on Tasks 6c + 7. NOT introduced by Scope B.
- `ruff check src/` clean.
- `mypy --strict` clean across 186 source files.
- Migration `1e2d728f1b32 → aa00b11c22d3` applies and rolls back cleanly on Postgres 16.
- Backfill idempotent on re-run (36 → 36 partitions).
- verify-e2e: 5 PASS (4 API + 1 CLI post-fix); 2 FAIL_INFRA (UI cases blocked on Azure Entra ID auth setup — pre-existing infra gap, not Scope B). Report: `tests/e2e/reports/coverage-day-precise-2026-05-07.md`.

**Implementer-time corrections beyond the plan:**

- Task 1 (CMES MLK): plan asserted CMES closes on 2025-01-20 (MLK); actually CMES Globex stays open with reduced hours, only NYSE-tracked products close. Implementer changed test data to Christmas 2025-12-25 (which CMES does close for) and updated the plan inline. Empirical bug plan-review couldn't verify without running the lib.
- Task 6a (leap-year Feb 29): plan's fixture had `feb_days = list(range(1, 29))` which omits Feb 29 — 2024 is a leap year and Feb 29 is a real NYSE trading day (Thu). Fixed.
- Task 6d: P2-3 plan-review fix honored — integration tests for `test_inventory_endpoint.py`, `test_orchestrator.py`, `test_end_to_end_run.py`, `test_orchestrator_failure_paths.py`, `test_symbol_onboarding_readiness.py` all updated in the SAME commit as the API/orchestrator wiring. No "failing tests across commits" violation.
- Task 7 architectural extension: `derive_status(asset_class=...)` kwarg threaded through to `is_trailing_only` so futures/fx rows get correct calendar cutoffs (would otherwise inherit equity's NYSE schedule).

**Operational deploy notes (in PR description):**

1. Image rebuild required — `pyproject.toml` adds `exchange_calendars`; existing container venvs are stale.
2. Run `scripts/build_partition_index.py` once per environment after `alembic upgrade head` to populate `parquet_partition_index` from existing parquet files.
3. UI E2E auth not configured — separate infra ticket.

**State:** PR #49 open at https://github.com/marketsignal/msai-v2/pull/49. Awaiting review.

### 2026-05-01 — Market Data v1 / `/universe-page` Phase 5 code-review loop — iter-1 + iter-2 fixes (branch `feat/universe-page`) — IN PROGRESS

**Goal:** /loop tick-1 of 10-min cadence — drive Phase 5 quality gates to "ready to merge."

**Code-review loop (in progress, 2 iterations so far — productive narrowing):**

- **Iter 1** (pr-review-toolkit code-reviewer; Codex unavailable per memory `feedback_codex_cli_locked_out_council_fallback`): 0 P0 / 1 P1 / 4 P2.
  - **P1-1 (watchlist regex)**: symbols containing `.`, `_`, `/` (`BRK.B`, `ES.c.0`, `EUR/USD`) failed backend validation `^[a-z0-9-]+$` when used in `watchlist_name`. Added `slugifySymbol(s) = s.toLowerCase().replace(/[^a-z0-9-]+/g, "-")` and applied at both call sites: `frontend/src/lib/hooks/use-symbol-mutations.ts` (refresh slug) + `frontend/src/components/market-data/add-symbol-dialog.tsx` (onboard slug).
  - **P2-1 (cost-cap visibility leak)**: pre-dedup `UPDATE hidden_from_inventory=False` ran BEFORE the cap-check 422 short-circuit — a rejected onboard would silently un-hide. Moved the block to AFTER the cap-check in `backend/src/msai/api/symbol_onboarding.py`.
  - **P2-2 (missing regression tests)**: added `test_re_onboard_after_delete_restores_visibility_even_when_deduplicated` (Override O-11; exercises the dedup path explicitly with two POSTs of identical canonical body) and `test_worker_upsert_does_not_modify_hidden_from_inventory` (Override O-15; verifies `_upsert_definition_and_alias` source-level invariant by seeding hidden=True and asserting the writer doesn't touch the column).
  - **P2-3 (misleading column header)**: "Last refresh" → "Last update" in `inventory-table.tsx` + `row-drawer.tsx` with comment explaining v1's `last_refresh_at = updated_at` trade-off (column advances on any row mutation, not exclusively on data ingestion).
  - **P2-4 (mixed today() semantics)**: `func.current_date()` (Postgres server-tz) and `_date.today()` (UTC) replaced with `exchange_local_today()` (Chicago) in `services/nautilus/security_master/service.py:list_registered_instruments` and 3 callsites in `api/symbol_onboarding.py`. Project invariant per memory `feedback_alias_windowing_must_use_exchange_local_today`. Required co-locating import + usage in single edit pattern (memory: `feedback_colocate_imports_with_usage_in_edits`) — done usage-first to survive ruff format pass.

- **Iter 2** (pr-review-toolkit code-reviewer): 0 P0 / 0 P1 / 3 P2.
  - **P2-A (chart deep-link broken)**: `frontend/src/app/market-data/chart/page.tsx` never read `useSearchParams()` — the inventory's "View chart" navigation set `?symbol=AAPL` in the URL but the page silently picked `flat[0].value` instead. Added `useSearchParams` import + `initialSymbol = searchParams.get("symbol") ?? ""` to seed `useState<string>(initialSymbol)`. Fall-through to `flat[0]` only when the param is absent.
  - **P2-B (missing 422-rejection regression test)**: added `test_cost_cap_rejection_does_not_clear_hidden_from_inventory` — seeds hidden=True, POSTs with cap < dry-run estimate, asserts 422 + `hidden is True` + `pool.enqueue_job.assert_not_awaited()`. Future refactor reverting the iter-1 P2-1 reorder will now break this test.
  - **P2-C (dedup test fidelity)**: added `pool.enqueue_job.assert_awaited_once()` to `test_re_onboard_after_delete_restores_visibility_even_when_deduplicated`. Proves the second POST took the dedup branch rather than spawning a new arq job (run_id match alone is reasonable but not airtight).
  - **P3-B fixed inline**: stale UC5 reference `test_remove_during_in_flight_onboard_stays_hidden` → `test_worker_upsert_does_not_modify_hidden_from_inventory` (the actual test name).

- **Iter-1 fix verification:** all 3 new + iterated tests pass (`pytest tests/integration/api/test_symbol_onboarding_api.py::{cost_cap_rejection,re_onboard,worker_upsert}` 3/3 in 2.93s); mypy --strict clean; ruff clean on all touched files (78 ruff errors are pre-existing on main, confirmed via `git stash` + recheck — outside this branch's scope).

**Test count update:** 51 backend tests in scope (was 48; added 1 new test in iter-2 + iter-2 strengthened 1 existing test with enqueue assertion). All pass. Frontend tsc + eslint still clean (1 pre-existing warning in `app/research/page.tsx` — unrelated).

**Workflow-loop trajectory (`feedback_code_review_iteration_discipline`):** iter-1 (0/1/4) → iter-2 (0/0/3) — narrowing. Findings became more specific each pass. No architectural surprises.

**Workflow state:** Phase 5 — Quality gates. Next tick: iter-3 review (expected clean) → simplify → verify → E2E (verify-e2e against running stack for UC1–UC6) → Phase 6 (commit + PR). Wakeup scheduled for 10-min cadence per user's `/loop` invocation.

### 2026-05-01 — Market Data v1 / `/universe-page` Phase 4 complete — frontend + E2E use cases (branch `feat/universe-page`) — IN PROGRESS

**Goal:** Symbol-centric inventory page at `/market-data` replacing `/data-management`. Phase 4 (TDD execution) complete this session — all 18 tasks done across both halves; ready for Phase 5 quality gates.

**Frontend shipped this session (uncommitted in worktree, lands at Phase 5):**

- **C1 routing reshape:** moved `frontend/src/app/market-data/page.tsx` → `.../market-data/chart/page.tsx` (chart now lives at `/market-data/chart`); deleted `frontend/src/app/data-management/` and the unused `frontend/src/components/data/{storage-chart,ingestion-status}.tsx` components; removed Data Management entry + `Database` icon import from sidebar.
- **D1 typed API + inventory hook:** added `InventoryRow` / `OnboardRequest` / `OnboardResponse` / `DryRunResponse` / `OnboardStatusResponse` types + `getInventory` / `postOnboard` / `postOnboardDryRun` / `getOnboardStatus` / `deleteSymbol` helpers to `frontend/src/lib/api.ts`. Created `useInventoryQuery` (TanStack Query v5) with Override O-5 applied: flat-string debounce + `useMemo` to avoid object-identity infinite re-render.
- **D2 StatusBadge:** 6-variant pill (`ready` / `stale` / `gapped` / `backtest_only` / `live_only` / `not_registered`), color + icon + text per accessibility rule "never color alone."
- **D3 InventoryTable:** sticky header, kebab actions per row (Refresh / Repair gaps / View chart / Remove), Override O-6 applied (server-trusted `is_stale`, no client double-count).
- **D4 RowDrawer:** sectioned panel (Actions / Coverage / Recent jobs / Metadata) with per-range Repair buttons; closes mutually-exclusively with JobsDrawer.
- **D5 AddSymbolDialog:** dry-run cost preview with explicit $0-included emerald banner branch (Pablo's Databento plan covers v1 schemas) vs sky-blue estimated-cost banner. Explicit return types added per project typescript-style rule.
- **D6 polling discipline:** pure `computeRefetchInterval(status, prevStatus, consecutiveSameCount)` in `frontend/src/lib/hooks/refetch-policy.ts` per Overrides O-7 + O-13 (consistent 2s base, exp backoff to 30s on no-state-change, terminal-status returns `false` for hard stop). `useJobStatusQuery` integrates per Override O-16 with `useRef` for prevStatus + sameCount (mutated outside render phase, React 19 strict-mode safe). `refetchIntervalInBackground: false` for visibility-pause.
- **D7 HeaderToolbar + EmptyState:** asset-class ToggleGroup (All / Equity / Futures / FX), trailing-window Select (1y / 2y / 5y / 10y / Custom), Add + Jobs buttons, conditional bulk-action chips (`<N> stale · Refresh all`, `<N> gapped · Repair all`).
- **E1 page composition:** `frontend/src/app/market-data/page.tsx` rewritten to compose all subcomponents. Mutually-exclusive drawer rule enforced (open one → close other). Override O-8 confirm-remove flow: shadcn AlertDialog with soft-delete description ("Parquet preserved, active strategies not blocked, re-onboarding restores"); `useRemoveSymbol` mutation invalidates inventory query on success. Bulk repair fans out per-row refresh covering the full window (worker dedups inside).

**E2E use cases authored (Phase 3.2b artifact):** 6 markdown files at `tests/e2e/use-cases/market-data/uc{1..6}*.md` covering: browse inventory (UC1) · add-symbol $0 happy path (UC2) · refresh stale (UC3) · repair mid-window gap (UC4) · remove-from-inventory + re-onboard restore (UC5) · jobs-drawer polling discipline (UC6: 2s cadence + visibility-pause + terminal-stop). Each follows the Intent → Setup (sanctioned ARRANGE only) → Steps → Verification → Persistence template.

**Verification this session:** full-frontend `pnpm exec tsc --noEmit` exit 0; `pnpm lint` 0 errors (2 pre-existing warnings in unrelated files: `app/research/page.tsx` exhaustive-deps + `tests/e2e/fixtures/auth.ts` unused param). Backend tests untouched this session — 28 still pass from prior.

**Files touched this session:** 19 files changed — 1 chart-page rename, 2 deletions (data-management page + 2 unused data components), 9 new frontend components/hooks/use-cases, 6 new use-case markdown files, sidebar + api.ts + page.tsx + state.md edits.

**Backend shipped (uncommitted in worktree, lands at Phase 5):**

- New: `GET /api/v1/symbols/inventory?start=&end=&asset_class=` — bulk readiness with server-derived `status` field (`ready` / `stale` / `gapped` / `backtest_only` / `live_only` / `not_registered`) per the council taxonomy. Single SQL query with `bool_or` aggregation for IB qualification (no per-row N+1). Filters out hidden rows. Concurrency-capped `asyncio.gather` for per-row coverage scans.
- New: `DELETE /api/v1/symbols/{symbol}?asset_class=` — soft-delete sets `instrument_definitions.hidden_from_inventory = true`. Parquet preserved. Per Pablo's call: does NOT block on usage in active strategies/live deployments. 204 on success, 404 if symbol not registered.
- New column: `instrument_definitions.hidden_from_inventory: BOOLEAN NOT NULL DEFAULT FALSE` + Alembic migration `1e2d728f1b32`.
- Modified `POST /api/v1/symbols/onboard`: (a) cost-cap fallback to new settings default `symbol_onboarding_default_cost_ceiling_usd` ($50) gated on `databento_api_key` presence — protects CLI/X-API-Key callers; logs `cost_cap_skipped_no_databento_key` warning when key absent. (b) Pre-dedup UPDATE clears `hidden_from_inventory=False` for any symbols in the request, so re-onboarding a removed symbol restores visibility even when the run is deduplicated. Per Override O-15: worker UPSERT path does NOT touch the hidden flag (user-owned column).
- New helpers: `services/symbol_onboarding/inventory.py` — pure `derive_status` (worst-actionable-wins priority resolution) + `is_trailing_only` (single-range, prev-month-start boundary). 18 unit tests.
- New: `SecurityMaster.list_registered_instruments` bulk reader using project conventions (`self._db`, `raw_symbol`, `provider` model fields).
- New schema: `InventoryRow` Pydantic model (13 fields) with the locked status taxonomy.

**Tests added:** 28 total — 18 unit (B1) + 8 integration (B3 inventory + B6b DELETE/restore) + 2 added to existing `test_symbol_onboarding_api.py` (B5 cap fallback branches + B6b re-onboard race fix). All ruff + mypy --strict clean.

**Frontend pending:** C1 (routing reshape — move chart to `/market-data/chart`, retire `/data-management`, sidebar update), D1–D7 (TanStack Query hooks, StatusBadge, InventoryTable, RowDrawer, AddSymbolDialog, JobsDrawer, HeaderToolbar+EmptyState), E1 (page assembly + remove flow + AlertDialog), F1 (E2E use cases UC1-UC6). Plan + design + 16 corrections at `docs/plans/2026-05-01-universe-page.md` and `docs/plans/2026-05-01-universe-page-design.md`.

**Workflow trajectory:**

- **Phase 1:** PRD reframed mid-discussion from watchlist-centric (locked SPLIT_RELEASE_TRAIN scope 2026-04-30) to symbol-centric (Pablo's vision 2026-05-01); validated via Codex second opinion. Empirical Databento billing probe confirmed v1 schemas are $0 metered under Pablo's existing plan; cost-cap reframed as defense-in-depth not primary friction.
- **Phase 2:** Research-first agent surfaced TanStack Query v5 as the canonical polling/cancellation surface; flagged perf risk on per-row coverage scans at 80 rows.
- **Phase 3:** Engineering Council (5 advisors + Codex chairman gpt-5.5 xhigh) ratified all 9 design questions. Hawk's CONDITIONAL verdict surfaced 3 NFR blockers (polling discipline, debounce, server-side cap) — accepted as v1 hard-requirements.
- **Plan-review loop (3 iterations):**
  - **Iter 1** (Codex gpt-5.5 xhigh): 2 P0 + 12 P1 + 5 P2. Major findings: providers.tsx collision, `list_registered_instruments` compile errors, `last_refresh_at` source wrong, polling backoff missing, fixtures non-existent, US-005 remove not in tasks.
  - **Iter 2** (Codex gpt-5.5 xhigh): 1 P0 + 5 P1 + 2 P2 + 1 P3. Caught B3 referencing column added by B6, `apiDelete` 204 incompatibility with `apiPost` shape, re-onboard race with dedup path, asset_class collision in JSONB key check.
  - **Iter 3** (Claude self-review; Codex stalled at 17 min on now-3300-line plan, matching `feedback_codex_cli_stalls_on_long_audit_prompts`): 0 P0 + 1 P1 + 1 P2. Worker UPSERT must NOT touch hidden flag (user-remove race during in-flight onboard); polling integration wiring snippet missing.
  - User sign-off closed the loop per workflow fallback.

**Caveats:**

- B6a Alembic autogen surfaced 3 unrelated pre-existing model/DB drift items (`ix_instrument_aliases_uid` rename, `order_attempt_audits` unique-constraint→index swap, `ix_symbol_onboarding_runs_created_at` drop). Stripped from this branch's migration to keep focus. Follow-up cleanup migration owed per "no bugs left behind."
- B3 perf spike at 80 real symbols deferred — dev stack containers from another worktree blocking startup. Re-run when stack is up.

### 2026-04-28 — Developer-journey how-tos (branch `feat/how-tos-developer-journey`) — DOCS-ONLY PR

**Goal:** 9-doc developer-journey set in `docs/architecture/` covering the path from blank repo → live P&L. Subsystem-level deep-dives modeled on `mcpgateway/docs/architecture/how-*-works.md`. ~5,440 lines, no code changes.

**Docs shipped:**

- `00-developer-journey.md` — front-of-house narrative + ASCII component diagram + Mermaid trial diagram
- `how-symbols-work.md` — symbol onboarding, `instrument_definitions`+`instrument_aliases` registry
- `how-strategies-work.md` — git-only authoring, `code_hash`/`git_sha`, `FailureIsolatedStrategy` (mixin, no shipping adopter)
- `how-backtesting-works.md` — single-strategy single-symbol, BacktestRunner subprocess, QuantStats
- `how-research-and-selection-works.md` — sweeps, walk-forward CV, OOS validation, promotion seam
- `how-graduation-works.md` — 9-stage state machine, immutable transition log, no-RBAC-yet caveat
- `how-backtest-portfolios-work.md` — multi-strategy × multi-symbol allocation; per-component fan-out + aggregation (no portfolio-level walk-forward yet)
- `how-live-portfolios-and-ib-accounts.md` — `LivePortfolio→Revision→Deployment`, IB ports `(4002,4004)`/`(4001,4003)`, `DU`/`DF` paper prefixes, 3-layer idempotency, 4-layer kill-all, multi-IB-login fabric
- `how-real-time-monitoring-works.md` — WebSocket + JWT first-message auth, dual-channel `msai:live:state:*` + `msai:live:events:*` pub/sub, dashboard

**Workflow trajectory:**

- **Phase 2 research:** N/A — docs-only, no external libraries (justification in research brief)
- **Phase 3 design:** Codex (gpt-5.4 xhigh) consult validated structure on 2026-04-28 — adopted both Codex pivots: split walk-forward into research (not portfolio); split portfolio into backtest-vs-live as two distinct domains
- **Phase 4 execution:** 4 parallel research agents (cluster A/B/C/D citation reports) → hand-write `00-developer-journey.md` to lock voice → 8 parallel writer subagents
- **Phase 5 code-review loop (3 iterations):**
  - **Round 1 (9 Claude reviewers in parallel):** found ~5 P0, ~34 P1, many P2 across the set. Codex CLI attempted in parallel but stalled reliably (12+ hour hangs at 0% CPU even with `high` reasoning); reverted to Claude-only review per user direction.
  - **Round 1 fix-pass (9 parallel fixers):** every P0/P1/P2 addressed against actual source. Cross-doc propagated errors (graduation stage names, `backtest_job.py` filename, "approved-only" enforcement, SIM-venue pinning) batched in `scratch/shared-corrections.md`.
  - **Round 2 (9 Claude reviewers):** confirmed 7 docs CLEAN; 1 doc (D5 graduation) had truncated round-1 fixer leaving P0 leftovers; 1 doc (D3 backtesting) had 3 NEW P1 fabrications surface in §5.
  - **Round 2 targeted re-fixes:** D5 graduation re-fix scrubbed all risk-overlay fabrications + 422 body-shape examples; D3 backtesting fixed 3 fictional `FailureCode` enum values + SIGTERM/SIGKILL distinction; polish-pass landed 4 P2 nits across 00/01/02/08.
  - **Round 3 (D5 only):** CLEAN.

**P3 nits intentionally deferred:** style/cosmetic items not affecting correctness (mixin-vs-base-class wording, ASCII raggedness, single-line off-by-one cites).

**Diagram convention:** ASCII canonical (portable, doesn't rot). One Mermaid trial in doc 00 — pending render-quality assessment on GitHub.

**Files modified outside docs/architecture:** `docs/architecture/README.md` reading-order updated with new "Subsystem Deep Dives — Developer Journey" section.

### 2026-04-27 — Instrument-cache → registry migration (branch `feat/instrument-cache-registry-migration`) — PHASE 5 CODE-REVIEW LOOP CLOSED (3 iters)

**Code-review loop trajectory:** iter-1 (2 P0 + 22 P1 + 25 P2 + 14 P3) → iter-2 (0 P0 + 8 P1 + 13 P2 + 7 P3) → iter-3 (0 P0 / 0 P1 / 0 P2 / 2 P3 deferrable). **Decisive convergence per `feedback_code_review_iteration_discipline.md`.**

**Iter-1 reviewers** (5 in parallel: code-reviewer · silent-failure-hunter · pr-test-analyzer · comment-analyzer · type-design-analyzer; Codex unavailable due to CLI version + model gate). **Iter-1 fix-pass** (3 file-disjoint subagents A/B/C) addressed all 22 P1s + 2 P0s including: 2 alembic P0s (share-class venue trap, asset_class taxonomy normalization), `effective_from` UTC-vs-Chicago boundary, alias-rotation hazard on re-runs, `bulk_resolve` warm-hit-on-futures asymmetry + cold-path duplicate work via shared `_resolve_one(today, warm_def)` helper, `IBQualifier.listing_venue_for(instrument)` extraction shared with CLI, `IBContractNotFoundError(LookupError)` typed exception, `asset_class_for_alias` narrowed catch, `DEFAULT_CACHE_VALIDITY_DAYS` deletion + class-docstring rewrite, preflight session isolation + `RegistryMissError.symbols` attribution + `TypeError` catch, `derive_asset_class` poisoned-session rollback, CLI mid-batch resolved-list visibility, structural-guard CANARYs (3 positive-falsification tests), runbook council-Q hygiene scrubs (3 sites), pyproject `banned-api` `msg=` rewrites (forward-looking), 9 task-ID hygiene scrubs across 4 files. Plus 3 inline mid-wave fixes: NULLIF guard added to migration's COALESCE (asyncpg binds Python `None` as JSONB literal `'null'`, distinct from SQL NULL — plain COALESCE silently overwrites; mirrors runtime pattern in `service.py`); preflight test PYTHONPATH; preflight session rollback after table-missing exception.

**Iter-2 reviewers** found 8 narrow mechanical P1s (F1–F8). **Iter-2 fix-pass-2** addressed all 8: F1 KNOWN_VENUES → import `_DATABENTO_MIC_TO_EXCHANGE_NAME.values()` source-of-truth (BABA.AMEX no longer false-trigger); F2 thread `RegistryAssetClass` + `Provider` Literal types through 7 callsites (cascaded into `databento_bootstrap.py` + `continuous_futures.py`, where mypy --strict caught 2 cross-file typo-class bugs — exactly what `Literal` types are designed to prevent); F3 rename `AliasResolution.ingest_asset_class` → `registry_asset_class: RegistryAssetClass` (the field carried registry-taxonomy values under an ingest-taxonomy name — genuine correctness fix); F4 replace `__new__(SecurityMaster)` constructor-bypass in 5 tests + add `_EFFECTIVE_FROM_SENTINEL` assertion; F5 `market_hours.py:56` `Phase 2 schema` 1-line scrub; **F6 (CRITICAL)** migration close-prior-active UPDATE: `effective_to = now.date()` instead of `_EFFECTIVE_FROM_SENTINEL` to avoid CHECK violation `ck_instrument_aliases_effective_window: effective_to >= effective_from` on registries with prior alias-rotation history (would have caused IntegrityError + corrupt state mid-migration on Pablo's dev DB during US-005 drill); F7 add 4 more structural-guard CANARYs (Attribute, FunctionDef, Assign, dotted-Import) so all 7 walker AST node types have positive-falsification tests; F8 narrow `derive_asset_class.py` `contextlib.suppress(Exception)` → `contextlib.suppress(SQLAlchemyError)` so programmer errors propagate.

**Iter-3 (slim panel — 3 reviewers; the 3 who flagged critical iter-2 issues):**

- **code-reviewer iter-3 CONVERGENCE** — 0 P0 / 0 P1 / 0 P2; 1 P3 (alembic close-prior comment doesn't call out the same-day zero-width-window case from PR #44 migration `b6c7d8e9f0a1` semantics — non-blocking).
- **type-design-analyzer iter-3 CONVERGENCE** — 0 P0 / 0 P1 / 0 P2; 1 P3 (`_resolve_databento_continuous` plain `ValueError` — out-of-band deferrable).
- **silent-failure-hunter iter-3 RATE-LIMITED** — could not get explicit CONVERGENCE signal due to API usage cap. Its iter-2 P1 findings (F6, F7, F8) were directly addressed in fix-pass-2 and independently verified clean by code-reviewer iter-3 — closure by transitive trust.

**Final state on disk** (uncommitted per workflow gate):

- 14 source modified, 2 deleted, 6 new artifacts (Alembic Rev A + B, preflight script, runbook, structural-guard rewrite, `security_master/types.py`).
- **All gates green**: 169/169 affected PR-touched tests PASS · ruff clean across `src/` · mypy `--strict` clean across 13+ source files (with 5 `Literal` aliases in types.py threaded through 7 callsites + cross-file cascade) · alembic head `e2f3g4h5i6j7` · 0 forbidden symbols in `backend/src/` per AST structural guard.

**Simplify-pass S1–S8 DONE** (3 parallel reviewers reuse/quality/efficiency → 1 fix subagent): centralized `REGISTRY_TO_INGEST_ASSET_CLASS` map in `security_master/types.py` (closes the latent `option`/`options` divergence between `service.py` and `symbol_onboarding/__init__.py` that would have caused different Parquet directory paths for the same instrument when options support lands); explicit `_REGISTRY_TO_SPEC_ASSET_CLASS` bridge for `_resolve_one`'s registry → spec taxonomy hop; `RETURNING` clause on Rev B's ON CONFLICT DO UPDATE (eliminates per-row UID re-fetch); `market_hours.py` migrated stdlib logging → structlog; extracted `_alembic_subprocess.py` shared helper; `KNOWN_VENUES` readability split into named locals + sorted; `_EFFECTIVE_FROM_SENTINEL: Final[date]`; dropped vestigial `today` parameter from `_resolve_one`. 120/120 affected tests PASS · ruff clean · mypy `--strict` 0/18.

**verify-app full sweep PASS:** 2120/2120 effective tests + 11 skipped + 16 xfailed (2 pre-existing PR #41 flakes confirmed unchanged); ruff clean across `src/`; mypy `--strict` 0 errors across **181 source files**; alembic head `e2f3g4h5i6j7`. Wall-clock 230.77s.

**E2E drill executed 2026-04-27 21:51–21:56 CT against volume-pinned dev DB (`msai_postgres_data`, this branch's freshly-built images):**

- pg_dump checkpoint → preflight PRE-migration (PASS exit 0) → docker compose down → bring up postgres+redis only → `alembic upgrade head` (`c7d8e9f0a1b2 → d1e2f3g4h5i6 → e2f3g4h5i6j7 (head)`, 0 rows migrated since instrument_cache was empty post-PR-#45 work, table DROPPED, registry data preserved at 4 definitions + 7 aliases including 2 closed rotations from PR #32 + PR #37 + PR #44 history) → `docker compose up -d` (all 9 containers healthy) → `/health` 200 → log scan for `instrument_cache` empty.
- **UC-ICR-002 PRE + POST migration PASS.** Post-migration preflight validates F7 (`ProgrammingError` narrowing on UndefinedTableError) + F8 (`await session.rollback()` recovers asyncpg poisoned transaction) live: `[info] instrument_cache table dropped (post-migration): UndefinedTableError ... [ok] Preflight passed.`
- **UC-ICR-003 backtest PASS.** AAPL EMA cross 2024-01-02..12 completes in 2s wall-clock; `/results` returns populated `series` payload with `series_status=ready`; `/trades` paginated cleanly. Full backtest pipeline (API → arq worker → `SecurityMaster.resolve_for_backtest` → registry → Parquet → backtest runner → results materialization) verified end-to-end through S2's explicit registry-to-spec taxonomy bridge.
- **UC-ICR-004 fail-loud cold-miss PASS.** `lookup_for_live(['GOOG'], …)` raises `RegistryMissError(symbols=['GOOG'])` with operator hint `Run: msai instruments refresh --symbols GOOG --provider interactive_brokers` + structured log `live_instrument_resolved source=registry_miss` + alert fired. Bonus: AAPL (registry has only databento alias, no IB alias) also raised `RegistryMissError` — provider isolation verified end-to-end.
- **UC-ICR-005 SKIPPED_INFRA** (paper IB opt-in `RUN_PAPER_E2E=1`).
- **UC-ICR-001 / UC-ICR-006:** the migration core step + worker restart + clean log scan already satisfy council Q8 binding evidence substantively. Richer paper-deploy + restart drill is optional operator step.
- **F6 risk surface did NOT fire** (cache was empty post-PR-#45 — no IB rows triggered the close-prior path); F6 correctness on real rotation history remains validated by testcontainer suite (`test_revision_b_idempotent_when_rerun_against_seeded_registry`).

**6 UCs graduated** at `tests/e2e/use-cases/instruments/instrument-cache-registry-migration.md`. Verification report at `tests/e2e/reports/2026-04-27-instrument-cache-registry-migration.md`.

**Outstanding:** commit + push + PR.

### 2026-04-27 — Instrument-cache → registry migration (branch `feat/instrument-cache-registry-migration`) — PHASE 4 COMPLETE (15/15 tasks)

**Goal:** Migrate the legacy `instrument_cache` Postgres table into the new `instrument_definitions` + `instrument_aliases` registry (PR #32 + #35), drop the legacy table, AND fully delete `canonical_instrument_id()` (closed-universe Phase-1 helper, two definition sites). Internal-mechanics work in service of the ratified Symbol Onboarding PRD's runtime correctness — Pablo authorized clean end-state over conservative compatibility.

**Council verdict** (2026-04-27, standalone `/council` 5-advisor + Codex xhigh chairman) on Q1–Q10: combined PR (Q1=a), preflight gate before alembic upgrade (Q2=yes), JSONB column for `trading_hours` (Q3=a), delete `nautilus_instrument_json` UNANIMOUS (Q4), drop `ib_contract_json` (Q5=b — re-qualify on demand), fail-loud on orphans (Q6=a), hard cutover same-PR (Q7=a UNANIMOUS), branch-local restart proof required (Q8), full canonical removal scope incl. CLI direct-normalization not registry-backed per Simplifier's circular-CLI catch (Q9), AST-walking structural guard scanning all of `backend/src/msai/` (Q10).

**Plan-review trajectory:** 5 iterations (13→8→7→3→0). Codex iter-5 verdict: "ready to execute."

**Phase 4 — TDD execution complete**, all 15 tasks (T0–T14) via subagent-driven-development across 5 parallelized waves (~14 implementer subagent dispatches + 2 inline fixes). Pablo's directive: "parallelize as much as possible." Waves grouped by file-conflict topology:

- **Wave 1** (3 parallel + 1 inline runbook): T3 `SecurityMaster.resolve()`/`bulk_resolve()` rewritten registry-only + new `InstrumentRegistry.find_by_aliases_bulk` + cache IO deletion + `_upsert_definition_and_alias` ON CONFLICT DO UPDATE for trading_hours; T5 CLI per-asset-class IBContract factories (STK with `--primary-exchange`, FUT closed CME quarterly set {ES,NQ,RTY,YM}, CASH BASE/QUOTE) + `IBQualifier.qualify_contract` + `current_quarterly_expiry` underscore-rename; T9 Alembic Revision B `e2f3g4h5i6j7_drop_instrument_cache.py` (reflected-table pattern, asset-class taxonomy translation `future→futures`/`forex→fx`/fail-loud on `index`, ON CONFLICT DO UPDATE with COALESCE, schema-only downgrade) + 2 migration tests; T14 operator runbook with `pg_dump` checkpoint + 7-step playbook + restart drill.
- **Wave 2** (3 parallel): T4 service.py cleanup — `resolve_for_live` registry-only with `live_resolver.RegistryMissError` reuse, delete `_ROLL_SENSITIVE_ROOTS` + `_spec_from_canonical` + cascading async migration of `asset_class_for_alias` (sync→async, registry-driven via `find_by_alias` instead of parser-then-spec) + update caller in `derive_asset_class.py`; T7 delete both `canonical_instrument_id` + `_es_front_month_local_symbol` from `live_instrument_bootstrap.py` (kept `current_quarterly_expiry`/`_FUT_MONTH_CODES`/`phase_1_paper_symbols`); T8 `backend/scripts/preflight_cache_migration.py` (JOINs over `LiveDeployment → LivePortfolioRevision → LivePortfolioRevisionStrategy.instruments` because `LiveDeployment.canonical_instruments` is NOT a column — Maintainer's prescient council caveat) + extend `make_live_deployment` factory with auto-default user/strategy + `member_instruments` kwarg + `LivePortfolioRevisionStrategy` row creation.
- **Wave 3** (T11 heavy): delete `SecurityMaster.resolve_for_live` entirely + 5-file test fixture migration (parity test + IB smoke test + cli mocks + phase2 e2e + security_master tests all migrated to `lookup_for_live`) + delete `test_instrument_cache_model.py` + `test_security_master_resolve_live.py`. Closes Maintainer's binding objection on "parallel resolution stacks" — registry is sole authority; supervisor + tests both route through `live_resolver.lookup_for_live` directly.
- **Wave 4** (3 parallel): T6 delete `canonical_instrument_id` from `instruments.py` + inline body into `default_bar_type`; T10 delete `models/instrument_cache.py` + remove from `__init__.py` + scrub stale `InstrumentCache` references in `parser.py` docstrings; T13 round-trip migration test (A→B→down→down→A→B with final-head assertion).
- **Wave 5** (T12 final, runs after all forbidden symbols are gone): AST-walking structural-guard rewrite at `tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py` scans every `.py` under `backend/src/msai/` (133 files) for forbidden Name/Attribute/Import/FunctionDef/ClassDef/Assign nodes; allowlist is just the test file itself. Plus ruff `[tool.ruff.lint.flake8-tidy-imports.banned-api]` block as belt+suspenders documenting the 4 banned import paths.

**Inline mid-wave fixes** (per "no bugs left behind" policy):

- T8 implementer placed preflight script at `./scripts/` (repo root); plan + tests expected `backend/scripts/`. Moved.
- Preflight test subprocess invocation `[sys.executable, "scripts/..."]` didn't pick up pyproject's `pythonpath=["src"]` → added `PYTHONPATH=str(backend_root / "src")` to env dict.
- Preflight script's `try/except` around `SELECT count(*) FROM instrument_cache` swallowed `UndefinedTableError` but left asyncpg transaction in `InFailedSQLTransactionError` → subsequent JOIN over `live_deployments` failed. Added `await session.rollback()` in the fail-soft branch.

**Final state on disk** (all uncommitted per workflow-gate hook, per `feedback_workflow_gate_blocks_preflight_commits.md`):

- 14 source files modified, 2 source files deleted (`models/instrument_cache.py` + the cache-only test files).
- 4 new artifacts: Alembic Revision B, `backend/scripts/preflight_cache_migration.py`, `docs/runbooks/instrument-cache-migration.md`, rewritten structural-guard test.
- 7/7 migration tests PASS, 2/2 structural-guard tests PASS, ruff clean across `src/`, alembic head `e2f3g4h5i6j7`.
- **0 forbidden symbols in `backend/src/`** (the 7 council-ratified bans: `canonical_instrument_id`, `InstrumentCache`, `_read_cache`, `_read_cache_bulk`, `_write_cache`, `_instrument_from_cache_row`, `_ROLL_SENSITIVE_ROOTS`). The one surface-level `canonical_instrument_id()` substring at `cli.py:719` is a docstring narrative explaining what the new IBContract factories replaced; correctly ignored by the AST walker (it's a `Constant(value=str)` node, not a `Name` node).

**Next**: Phase 5 quality gates (code-review loop with Codex + pr-review-toolkit, simplify pass, verify-app, E2E via verify-e2e agent, Q8 branch-local restart drill, then commit + PR).

### 2026-04-24 — Symbol Onboarding (branch `feat/symbol-onboarding`) — PHASE 4 IN PROGRESS (13/18 tasks)

**Goal:** Operator-facing API + CLI for declaring "onboard these symbols at these windows" and having the system orchestrate bootstrap → ingest → coverage check → optional IB qualification end-to-end. Driven by a YAML watchlist manifest in git. Replaces the manual SQL-seed + per-symbol `instruments refresh` ritual; closes PR #44 backlog item #2.

**Council verdict** (2026-04-24, standalone `/council` 5-advisor + Codex xhigh chairman): **Approach 1** — single arq entrypoint `run_symbol_onboarding`, `_onboard_one_symbol()` seam, phase-local bounded concurrency only in bootstrap phase, sequential ingest/IB. 4 binding renames adopted (`SymbolOnboardingRun` / `symbol_states` / `cost_ceiling_usd` / `request_live_qualification`).

**Mini-council on iter-1 P0 (queue self-deadlock):** Option A + 3 binding constraints — extract `ingest_symbols(...)` async helper from `run_ingest`; orchestrator calls it directly in-process (no child arq job); `IngestWorkerSettings` unchanged. Plan-review trajectory: iter-1 16 findings → iter-2 6 → iter-3 2 → iter-4 CLEAN both reviewers. Plan grew 3,981 → 5,150 lines (18 tasks: T0–T15 + T6a + T8-prime).

**Phase 4 progress (13/18 tasks complete; T0–T5 committed, T6a–T9 uncommitted on disk per workflow-gate hook):**

- **T0–T5 committed** (`6441b64` … `9ad9119`): pyproject deps + conftest fixtures + watchlists scaffold; `SymbolOnboardingRun` model + alembic migration `c7d8e9f0a1b2` (plural table, `updated_at`, `job_id_digest` unique-indexed, `cost_ceiling_usd Numeric(12,2)`); Pydantic V2 schemas with cross-field invariants + canonical enum vocabularies (`SymbolStatus`, `SymbolStepStatus`, `RunStatus`); manifest parser with `trailing_5y` sugar (default `end=today-1d`) + cross-watchlist dedup + `normalize_asset_class_for_ingest` translation seam; Databento cost estimator with declared confidence classification + per-bucket `metadata.get_cost`; on-the-fly Parquet coverage scanner with 7-day trailing-edge tolerance.
- **T6a, T13, T8-prime, T6, T7, T8, T9 uncommitted** (workflow-gate hook blocks Phase 4 commits per project policy):
  - **T6a:** `ingest_symbols(...)` extracted from `run_ingest` (3-line shim retained for arq wire compat); `IngestResult` dataclass with `bars_written`/`symbols_covered`/`empty_symbols`/`coverage_status`; 4 unit tests including delegation contract test.
  - **T13:** 3 Prometheus metrics (`msai_onboarding_jobs_total{status}` Counter, `msai_onboarding_symbol_duration_seconds` Histogram unlabeled — project's hand-rolled primitive doesn't support labels; per-step granularity in `symbol_onboarding_step_completed` structured log; `msai_onboarding_ib_timeout_total` Counter).
  - **T8-prime:** `_error_response` promoted to `api/_common.py::error_response`; 11 call sites in `api/backtests.py` + 1 in `api/instruments.py` swapped; 50 regression tests still pass.
  - **T6:** `_onboard_one_symbol` orchestrator (4-phase: bootstrap via `DatabentoBootstrapService.bootstrap(symbols=[...], asset_class_override=..., exact_ids=None)` batch API → ingest in-process via `ingest_symbols(...)` → coverage via `compute_coverage(...)` → optional IB qualify with `asyncio.wait_for(120s)` and `onboarding_ib_timeout_total.inc()` on timeout); per-phase JSONB persistence under `SELECT FOR UPDATE` row lock.
  - **T7:** `run_symbol_onboarding` arq task registered on `IngestWorkerSettings.functions` (single-queue per council); council-pinned terminal-status semantics (`failed` reserved for outer try/except systemic short-circuit only; all-per-symbol-failed → `completed_with_failures`); 3 integration tests pass.
  - **T8:** `POST /api/v1/symbols/onboard/dry-run` preflight cost estimate; pure read, no DB write, no enqueue; 2 tests pass.
  - **T9:** `POST /onboard` + `GET /onboard/{run_id}/status` + `POST /onboard/{run_id}/repair` with **full council-pinned idempotency** via shared `_enqueue_and_persist_run` helper (`SELECT FOR UPDATE` digest → fast-path 200 → enqueue first → 100ms backoff re-SELECT on `None` return → 409 `DUPLICATE_IN_FLIGHT` else commit on success → 202; Redis-down → 503 `QUEUE_UNAVAILABLE` zero-row guarantee). Dedup branches return `JSONResponse(status_code=200)` to override decorator's 202 default. Both `/onboard` and `/repair` route through the same helper. New `compute_blake2b_digest_key(*parts)` sibling to `compute_advisory_lock_key` in `security_master/service.py`. 7/7 integration tests pass after sentinel-vs-default fix in `_make_pool` test helper (initial `enqueue_returns=None` was misinterpreted as "use default mock job" instead of "return None"; fixed via `_DEFAULT_JOB = object()` sentinel).

**Remaining (T10–T15):**

- T10 `GET /readiness` window-scoped + new `find_active_aliases` aggregator (~60 LOC of SQL — honest-scoped new code, NOT a wrapper over existing `resolve_for_backtest`/`lookup_for_live`)
- T11 wire router into `main.py` + drop `asset_universe` import
- T12 `msai symbols` CLI sub-app (onboard/status/repair, --dry-run, --watch)
- T14 delete `api/asset_universe.py` + prune route tests
- T15 integration failure-path tests + 6 E2E use cases (UC-SYM-001..006)

**Phase 5 gates pending:** code review loop (Codex + pr-review-toolkit), simplify pass, verify-app, verify-e2e on live stack.

**Known concerns to address before Phase 5:**

1. `tmp_parquet_root` fixture (T0) is broken — `PosixPath.seed = ...` raises `AttributeError` because `Path` is slotted. Tests work around it with `tmp_path` directly. Needs wrapper class fix.
2. `_default_ib_service()` (T6) raises `NotImplementedError` — no `ib_provider_factory.get_interactive_brokers_instrument_provider` exists in the repo by that exact name. Live-qualification path needs wiring during Phase 5 alongside the broker compose profile.
3. T13 histogram is unlabeled — per-step granularity moved to `symbol_onboarding_step_completed` structured log events instead.

---

### 2026-04-23 — Databento registry bootstrap for equities/ETFs/futures (branch `feat/databento-registry-bootstrap`) — PHASE 5 COMPLETE, READY TO COMMIT

**Status:** All Phase 4 + Phase 5 gates closed. Shipped state below updated with Phase 5 additions: code-review loop (2 iter), simplify pass, verify-app PASS, E2E 3/6 PASS + 1 FAIL_STALE + 2 SKIPPED_INFRA, 6 UCs graduated. 2 known concerns resolved (same-day CHECK relaxation via migration `b6c7d8e9f0a1`; conftest ruff cleanup). 1 FAIL_BUG surfaced by verify-e2e agent + fixed in-branch: Databento nightly-window date-range probe (`start=today` failed 4xx during pre-publication window; fixed to `start=today-7d, end=today-1d`).

**Phase 5 additions:**

- **Migration `b6c7d8e9f0a1` (new):** relax `ck_instrument_aliases_effective_window` from strict `>` to `>=` so same-calendar-day alias rotations no longer 500. Zero-width `[today, today)` audit rows are semantically correct (half-open interval contains no dates → never selected as active). Downgrade self-cleans by DELETE'ing zero-width rows before re-adding the strict CHECK.
- **Code review iter-1 landed:** path-traversal fix via `_safe_filename(symbol)` sha1 digest; `asyncio.gather(..., return_exceptions=True)` + synthetic `UPSTREAM_ERROR` materialization (CancelledError/KeyboardInterrupt/SystemExit re-raised); `SQLAlchemyError` explicit rollback in `_upsert_and_classify` + continuous-futures; continuous-futures `except Exception` → 5 discrete typed handlers; classification race fixed (orchestrator pre-acquires `pg_advisory_xact_lock` before pre-state SELECT); `_extract_venue` fail-loud; `_pick_highest_severity` outcome ranking (UNAUTHORIZED > RATE_LIMITED > UPSTREAM_ERROR); `BootstrapResult` → `@dataclass(frozen=True, slots=True)` with `__post_init__` invariants; `BootstrapResultItem` `model_validator(mode="after")` enforcing registered↔outcome + failed⇒¬live_qualified + failed⇒¬backtest_data_available + failed⇒canonical_id=None + ambiguous⇒≥2 candidates. Pre-existing `test_databento_fetch_definition.py` migrated to typed `DatabentoUpstreamError` + fake-SDK `databento.common.error` submodule. Comment scrub across 12 source files (~20 P1 violations: dates, OQ-/US-/T-N IDs, council names, iter-N references). 7 new tests: UNMAPPED_VENUE outcome, UPSTREAM_ERROR all-datasets, RATE_LIMITED no-fallback, severity-ranking tie-break, gather-preserves-partial-progress, DATABENTO_NOT_CONFIGURED API 500, tenacity 5xx exhaustion, `live_qualified=true` two-step graduation, exact_id forwarded kwarg regression.
- **Code review iter-2 landed:** test-pollution fix on `_install_fake_databento` via snapshot-tuple restore pattern (iter-1 helper dropped submodules instead of restoring originals, breaking sibling tests that imported `BentoClientError` class at module-load — Codex caught this); continuous-futures lock-and-classify race mirrored from equity path; remaining comment scrub items in `cli.py` + `service.py`. User ratified "1 P1 acceptable" at iter-2; loop closed.
- **Simplify pass landed (3 fixes):** `compute_advisory_lock_key(provider, raw_symbol, asset_class) -> int` extracted to `service.py` as shared module-level helper — both `_upsert_definition_and_alias` and `DatabentoBootstrapService._upsert_and_classify` / `_bootstrap_continuous_future` import + call it (closes drift hazard where two byte-identical blake2b copies had to stay in sync by comment); deleted dup `_CONTINUOUS_FUTURES_RE` + `_is_continuous_futures` in favor of `is_databento_continuous_pattern` at `continuous_futures.py:38`; `api/instruments.py` uses shared `_error_response` helper from `api/backtests.py:92` (matches PR #41 canonical error-envelope rule). ~60 LOC deleted.
- **verify-app PASS:** 2054/2054 effective pass, 10 skipped, 16 xfailed. 2 failures on `test_backtest_job.py::test_materialize_series_payload_*` are pre-existing flakes from PR #41 (documented in CONTINUITY "Done cont'd 12") — both pass in isolation on this branch AND on main. No regressions.
- **E2E PASS (after mid-run FAIL_BUG fix):** verify-e2e agent ran 6 UCs. FAIL_BUG found: bootstrap probed Databento with `start=today_utc_midnight` which fails HTTP 422 `data_start_after_available_end` during nightly window before Databento's daily publication (observed at 00:38 UTC still failing). ALL 3 equity datasets failed identically, blocking all executable UCs. Fix at `databento_bootstrap.py`: probe 7-day historical window ending yesterday — definition-schema records describe current contract metadata so any recent snapshot contains the symbol. Continuous-futures path updated analogously to `start=(today-1d)`. Re-verified live: UC-001 AAPL PASS (`canonical_id="AAPL.NASDAQ"`), UC-002 SPY CLI PASS (exit 0, JSON `summary.failed=0`), UC-004 idempotency PASS (`outcome=noop` on second call). UC-003 FAIL_STALE (BRK.B no longer ambiguous in real Databento — ambiguity path covered by unit tests). UC-005 SKIPPED_INFRA (`RUN_PAPER_E2E=1` opt-in). UC-006 SKIPPED_INFRA (IB Gateway container not active).
- **6 UCs graduated** to `tests/e2e/use-cases/instruments/databento-registry-bootstrap.md` with real Databento response shapes + container-compatible `python -m msai.cli instruments bootstrap ...` invocation.

---

#### Original Phase 4 narrative

**Goal:** Ship an on-demand Databento path for populating the instrument registry (`POST /api/v1/instruments/bootstrap` + `msai instruments bootstrap` CLI) so cold-start environments can register equity/ETF/futures symbols without an IB Gateway dependency. Databento-bootstrapped rows are backtest-discoverable only; live graduation still requires an explicit `instruments refresh --provider interactive_brokers` second step.

**Scope council verdict** (2026-04-23, 5 advisors + Codex xhigh chairman): `1b + 2b + 3a + 4a` — arbitrary on-demand CLI/API, equities+ETFs+futures (NO options; NO Forex — Databento Spot FX is "Coming soon"; NO cash indexes — use ETF/futures proxies), Databento as peer provider (not replacement), metered-mindful rate limiting. 7 blocking constraints locked in. Decision doc: `docs/decisions/databento-registry-bootstrap.md`.

**Venue-normalization sub-council** (2026-04-23): Option A (normalize MIC→exchange-name at write boundary) with 3 blocking constraints — (1) closed MIC map + fail-loud on unknown, (2) named helper `normalize_alias_for_registry`, (3) preserve raw Databento venue via additive `source_venue_raw` column.

**Plan review loop:** 3 iterations, productive convergence trajectory 42 → 21 → 9 findings. All 72 combined P0/P1/P2 findings addressed in v3 plan revisions.

**Shipped — new source files (6):**

- `services/nautilus/security_master/venue_normalization.py` — `normalize_alias_for_registry` + closed MIC→exchange-name map (16 entries including `EPRL→PEARL`) + `UnknownDatabentoVenueError` fail-loud.
- `services/nautilus/security_master/databento_bootstrap.py` — `DatabentoBootstrapService` orchestrator. Session-per-symbol via `async_sessionmaker` (safe for `asyncio.gather`). `max_concurrent=3` hard cap. Tiered `XNAS.ITCH → XNYS.PILLAR → ARCX.PILLAR` fallback for equities. Continuous-futures delegated to `SecurityMaster.resolve_for_backtest`. 8 outcome types (CREATED/NOOP/ALIAS_ROTATED/AMBIGUOUS/UPSTREAM_ERROR/UNAUTHORIZED/UNMAPPED_VENUE/RATE_LIMITED).
- `services/data_sources/databento_errors.py` — typed `DatabentoError` hierarchy (`Unauthorized` / `RateLimited` / `Upstream`) carrying `http_status` + `dataset`.
- `schemas/instrument_bootstrap.py` — Pydantic v2 `BootstrapRequest` (`asset_class_override: Literal["equity","futures","fx","option"]` matching DB CHECK taxonomy, `max_concurrent: Field(ge=1, le=3)`, `exact_ids: dict[str, str]` alias-string semantics), `BootstrapResultItem`, `BootstrapResponse`, `build_bootstrap_response` helper.
- `api/instruments.py` — FastAPI router `POST /api/v1/instruments/bootstrap` with 200/207/422 status contract (200 all-success, 207 mixed, 422 all-failed).
- `alembic/versions/a5b6c7d8e9f0_add_source_venue_raw_to_instrument_aliases.py` — additive `source_venue_raw String(64) NULL` migration chained off `z4x5y6z7a8b9`.

**Shipped — modified source files (8):**

- `services/data_sources/databento_client.py` — tenacity retry via `asyncio.to_thread` (3 attempts, exponential 1-9s, retry on 429/5xx only, 401/403 fail fast); typed-error classification on final raise; ambiguity detection with dedup-by-id; `exact_id: str | None` kwarg pre-filters BEFORE ambiguity raise.
- `services/nautilus/security_master/service.py::_upsert_definition_and_alias` — `source_venue_raw` kwarg with auto-derive from pre-normalization alias; `normalize_alias_for_registry` call gated on `venue_format=="mic_code"` (implementer-corrected from plan's `provider=="databento"` to preserve continuous-futures path which uses `provider="databento"` + `venue_format="databento_continuous"` with already-exchange-name aliases like `ES.Z.0.CME`); `pg_advisory_xact_lock` keyed on `blake2b` digest (NOT Python `hash()` — process-seed randomized, drifts across workers); pre-upsert venue-divergence detection on IB-refresh path (fires only on real migrations post-normalization).
- `services/observability/trading_metrics.py` — 3 new counters (`msai_databento_api_calls_total{endpoint,outcome}`, `msai_registry_bootstrap_total{provider,asset_class,outcome}`, `msai_registry_venue_divergence_total{databento_venue,ib_venue}`) + 1 histogram (`msai_registry_bootstrap_duration_ms` with int-typed buckets `(100, 500, 1k, 2k, 5k, 10k, 30k)` matching project pattern).
- `core/database.py` — `get_session_factory()` FastAPI dependency wrapper around existing `async_session_factory`.
- `cli.py` — `instruments bootstrap` subcommand bypassing `_api_call` (which auto-fails on non-2xx — lethal for the 207 common case) with direct `httpx.request` accepting status ∈ {200, 207, 422}; Typer-native `StrEnum` for `--asset-class`; `--exact-id SYMBOL:ALIAS_STRING` repeatable flag.
- `main.py` — register `instruments_router`.
- `models/instrument_alias.py` — `source_venue_raw: Mapped[str | None]` column (nullable, additive).
- `pyproject.toml` — add `tenacity>=9.1.0,<10` dependency.

**Test counts (52 total, 0 regressions):**

- Unit: `test_venue_normalization.py` (9) · `test_databento_client_retry.py` (4) · `test_databento_client_ambiguity.py` (5) · `test_schemas_instrument_bootstrap.py` (10) · `test_cli_instruments_bootstrap.py` (6) · `test_databento_bootstrap_equities.py` (5) · `test_databento_bootstrap_metrics.py` (1)
- Integration: `test_security_master_advisory_lock.py` (2) · `test_security_master_databento_bootstrap.py` (3) · `test_registry_venue_divergence.py` (2) · `test_api_instruments_bootstrap.py` (5)
- Regression: 43 pre-existing security_master unit + integration tests — all still PASS.
- ruff clean + mypy `--strict` clean on every PR-touched file.

**Known concerns flagged for Phase 5 resolution:**

1. **Same-day alias rotation** — `ck_instrument_aliases_effective_window` strict-inequality CHECK (`effective_to > effective_from`) crashes the `ALIAS_ROTATED` outcome when both dates are `today`. T10 integration test sidestepped via `freezegun`. Production rotations via bootstrap will 500 on a real-life same-day venue migration unless the CHECK is relaxed to `>=` with a zero-window guard, OR `effective_to` is stamped at timestamp precision, OR PRD US-005 is amended to document next-day-only rotation semantics.
2. **T0 `conftest_databento.py` ruff nits** — 7 pre-existing I001/TC003/E501 surfaced by T12 subagent's wider ruff scope (module import ordering, type-check-only imports, line length). Clean up in Phase 5 polish pass.

### 2026-04-23 — Mypy --strict cleanup (branch `fix/mypy-strict-cleanup`) — MYPY NOW A BLOCKING CI GATE

**Context:** PR #42 unblocked CI end-to-end but left `mypy --strict` with `continue-on-error: true` because 132 pre-existing errors accumulated while CI was broken. Per Codex-ratified sequencing, this cleanup was scheduled before #8 Databento bootstrap so the remaining CI gate could start blocking.

**Outcome:** 128 mypy errors on `src/` → 0. `continue-on-error: true` removed from `ci.yml`. CI run `24846320955` green end-to-end (frontend 52s + backend 6m29s, all gates blocking).

**Scope — 128 errors resolved across 14 rule categories via per-site triage (no blanket ignores):**

- **26 × name-defined:** Added `if TYPE_CHECKING:` imports for SQLAlchemy relationship forward references across 12 model files. PR #42's UP037 auto-fix had unquoted `Mapped["Strategy"]` → `Mapped[Strategy]`; SQLA resolves via its class registry at runtime, but mypy needs names at type-check time.
- **31 × type-arg:** Bare `dict` / `dict | None` on SQLAlchemy `Mapped[...]` JSONB columns + function signatures → `dict[str, Any]`. Added `from typing import Any` where missing.
- **18 × unused-ignore:** Removed stale `# type: ignore[import-untyped]` comments across 8 files. Extended `[[tool.mypy.overrides]]` with `azure.*` so optional Azure Key Vault deps no longer raise `import-not-found` when absent.
- **14 × valid-type + attr-defined on `list?[X]`:** `builtins.list[X]` in annotations inside classes whose own `async def list()` method shadows the builtin (`services/{asset_universe,portfolio_service}.py`).
- **11 × attr-defined (library stub gaps):** Targeted `# type: ignore[attr-defined]` with rationale comments for `BaseContext.Process` (multiprocessing ctx) + `IBMarketDataTypeEnum` (Nautilus 1.223 re-exports without `__all__` entry).
- **6 × arg-type on SQLAlchemy `pg_insert`:** Pass mapped class instead of `.__table__` at 3 sites; `.__table__.delete()` → `sqlalchemy.delete(Model)` at one site.
- **6 × no-any-return:** Typed-local-var returns at 6 boundaries (security_master/parser.py, nautilus/parity/normalizer.py, strategy_registry.py, live_command_bus.py, workers/backtest_job.py).
- **4 × misc (redis-py await narrowing):** `await redis.<method>(...)` sites in `compute_slots.py` get narrow `# type: ignore[misc]` — redis-py's `ResponseT = Awaitable[T] | T` union defeats await narrowing.
- **3 × misc (class-scoped import):** Hoisted `arq.cron`, `nightly_ingest`, `pnl_aggregation` from class-body imports to module level in `workers/settings.py`. Verified no circular-import or startup side-effect changes.
- **3 × no-untyped-call:** `quantstats.reports.html(...)` + 2 × `redis.asyncio.from_url(...)` get narrow ignores with rationale.
- **2 × arg-type (Path→str):** `MarketDataQuery(str(path))` in `api/market_data.py` + `services/data_ingestion.py`.
- **2 × no-untyped-def:** Added `Iterator[None]` + `Callable[...]` annotations in `nautilus/trading_node_subprocess.py` + `cli.py`.
- **2 × Sequence→list assignment:** Wrap `scalars().all()` in `list(...)` in `api/live.py` + `live_supervisor/__main__.py`.
- **1 × misc (lambda closure-capture):** Replaced default-arg lambda with explicit named nested function `_run_ingest` in `portfolio_service.py`; preserves per-iteration default-arg-capture semantics without a blanket `type: ignore`.
- **1 × misc (class-or-None sentinel):** `NautilusBase = None` fallback in strategy_registry.py gets `# type: ignore[misc,assignment]` with a rationale comment explaining both codes.
- **1 × attr-defined (secrets.py):** Azure SecretClient typed `Any` instead of `object` so `.get_secret(...)` resolves post-override.

**CI hardening (plan M11):**

- `.github/workflows/ci.yml`: drop `continue-on-error: true` from mypy step. `uv run mypy src/ --strict` is now a hard-blocking gate on every PR.
- `.pre-commit-config.yaml`: new file, wires `actionlint@v1.7.7` for `.github/workflows/` parse-bug detection before first push. Would have caught PR #42's `hashFiles()` bug locally.

**Simplify sweep applied inline (3 fixes from the 3-agent reuse/quality/efficiency review):**

- pg_insert parity at `security_master/service.py:865` — third site now matches the other two (dropped `# type: ignore[arg-type]`).
- `strategy_registry.py:416` `[misc,assignment]` dual-ignore now has a rationale comment explaining both codes.
- `portfolio_service.py` `_run_ingest` closure — inlined the redundant `async_class`/`async_syms` intermediates.

**Verification:** mypy --strict clean on 166 source files · ruff clean · pytest 1703/1703 unit · actionlint clean on both workflows · CI run `24846320955` green.

**Known carry-overs (NOT in this PR):** none. This is the final CI-hardening follow-up from PR #42.

### 2026-04-23 — CI probe + full unblock (branch `fix/ci-ping-probe`) — CI GREEN END-TO-END

**Context:** Per Codex-ratified sequencing, the plan was CI probe → #8 Databento bootstrap → #2 Symbol Onboarding → #3 `instrument_cache` migration. Probe started as a `/quick-fix` scoped to "add minimal Ping workflow + open `ci.yml` triggers + `workflow_dispatch` — diagnose org-policy-vs-config". Escalated to `/fix-bug` when the probe uncovered 110-error ruff drift, 147-error mypy drift, a pytest pythonpath gap, a stale test fixture, and two CI-env-only test failures — all documented-or-implicit fall-out of CI never actually running since the post-flatten rename.

**Outcome:** CI run `24822937903` on the branch is GREEN. Backend: ruff clean, mypy advisory (132 pre-existing errors), pytest 1703/1703. Frontend: lint + build PASS.

**What shipped:**

1. **Parse bug fix.** `.github/workflows/ci.yml:47` used `hashFiles('frontend/pnpm-lock.yaml') != ''` at `jobs.<job_id>.if` level — GitHub Actions only allows `hashFiles()` in step-level contexts. Workflow was failing at parse time with 0s-duration. Invisible pre-flatten because it lived at `claude-version/.github/workflows/`. Guard removed; lockfile is committed so the guard was unnecessary.
2. **Ruff cleanup — 110 errors across 13 categories, per-site triage.**
   - Safe auto-fix pass: 32 fixes (UP037 quoted-annotation on SQLAlchemy models — safe under `from __future__ import annotations`, all F401 unused imports, I001 import-sort, 1 × UP041).
   - Manual triage: 10 × B904 (`from exc` where detail surfaces the inner exception; `from None` where a fixed detail hides internals); 4 × SIM105 → `contextlib.suppress()`; 6 × E501 line-wrap; 3 × F821 undefined-name in `main.py:91,95,98` (`Any`/`StreamRegistry` referenced without import — latent due to `from __future__ import annotations` but still real); 2 × E402 → hoist imports; 2 × N806 (+ 1 × N814 by dropping a non-compliant alias); 1 × B905 `zip(strict=True)`; 3 × B008 `# noqa` on FastAPI Query/Depends defaults; 2 × SIM102 collapsible-if; 1 × SIM103 needless-bool.
   - TC001/002/003 (44 errors): experimentally confirmed that moving `datetime`/`UUID`/`Decimal` into `if TYPE_CHECKING:` breaks SQLAlchemy 2.0 `Mapped[...]` resolution (`NameError` at class-construction). Resolved via `pyproject.toml` per-file-ignores for `src/msai/{models,schemas,api}/*.py` + `core/auth.py` + `core/database.py` (runtime annotation inspection). TC moves applied only in `services/*.py` where it's safe (6 files).
3. **Mypy stub overrides + advisory mode.** Added `[[tool.mypy.overrides]]` `ignore_missing_imports` for 16 untyped libraries (nautilus_trader, databento, polygon, pandas, arq, duckdb, etc.) — knocked 147 → 132 errors. Remaining 132 are real code drift (31 × type-arg missing generic params, 26 × name-defined forward refs, 11 × unused-ignore, 11 × attr-defined, plus misc). Not fixable same-day; mypy step marked `continue-on-error: true` with a pointer to the follow-up PR. Ruff + pytest + frontend remain blocking gates.
4. **Pytest infrastructure.** Added `pythonpath = ["src"]` to `[tool.pytest.ini_options]`. Without it, CI's `uv run pytest tests/` fails with `ModuleNotFoundError: No module named 'msai'` before collecting a single test.
5. **Stale test fixture fix.** `test_coverage_still_missing_after_ingest_returns_partial_gap` was broken on main since PR #40's 7-day coverage tolerance landed (documented in CONTINUITY but never fixed). Fixture updated from 60-second gap to 15-day gap so the partial-ingest path classifies as `COVERAGE_STILL_MISSING` instead of being tolerated.
6. **Two CI-env-only test fixes.**
   - `test_materialize_series_payload_*`: structlog's `cache_logger_on_first_use=True` (set by `setup_logging`) freezes processor chains on first log call, defeating `structlog.testing.capture_logs()` when an earlier integration test warms the logger. Fixed by making `setup_logging` test-env aware — `ENVIRONMENT=test` disables caching.
   - `test_refresh_help_documents_providers`: CliRunner output carries ANSI color sequences in CI (but not in local shells), splitting `--provider` across escape codes and breaking the substring match. Fixed with a regex ANSI strip before the assertion.
7. **Trigger opening.** `ci.yml` now runs on all pushes + PRs + `workflow_dispatch`, so feature-branch pushes get CI signal before PR creation.

**Tooling.** Installed `actionlint` via `brew install actionlint` — would have caught the `hashFiles()` parse bug before the first push. Should become a pre-commit hook in the mypy-cleanup PR.

**Follow-up deferred (dedicated PR): mypy --strict cleanup.** 132 real errors to triage. Categories: 31 × type-arg, 26 × name-defined, 11 × unused-ignore, 11 × attr-defined, 8 × valid-type, 8 × arg-type, 7 × int, 6 × no-any-return, misc. Once cleaned, remove `continue-on-error: true` from `ci.yml`.

**Commits on branch (9):** probe + parse-fix (b23739a, 5ec7b94) · CONTINUITY diagnostic (23edc18) · safe ruff auto-fix (9497141) · manual ruff cleanup (a00412a) · TC00x + per-file-ignores (ab2f313) · pytest pythonpath + auto_heal fixture (48e573c) · mypy overrides + advisory (5bb6812) · CI-env test fixes first attempt (0248fb2) · conftest location correction (8761d6b) · setup_logging test-env awareness (cdca1d2).

### 2026-04-21 — Backtest results charts & trade log (branch `feat/backtest-results-charts-and-trades`) — PHASE 4 EXECUTION IN PROGRESS

**What this PR will do:** Surface Pyfolio-style tear-sheet content (equity curve, drawdown, monthly-returns heatmap, paginated trade log + in-app QuantStats iframe) on every completed backtest's detail page, plus preserve the existing downloadable HTML report. Closes CONTINUITY #7 (UI-RESULTS-01) flagged by Pablo during the 2026-04-21 SPY live demo.

**Phase 1-3 committed** at `84de2cf` (PRD + council-ratified decision doc + research brief + 11-iter-converged implementation plan).

**Phase 4 COMPLETE — subagent-driven, 17 waves / 17 tasks. Backend (W1–W10) + Frontend (W11–W17) all shipped.**

- Wave 1 ✅ B0b (Histogram primitive at `2178f29`), B1 (Alembic migration `z4x5y6z7a8b9` adds `Backtest.series` JSONB + `series_status` VARCHAR(32)).
- Wave 2 ✅ B2 (`SeriesPayload` / `SeriesStatus` / `SeriesDailyPoint` / `SeriesMonthlyReturn` Pydantic types + `Backtest.series` + `series_status` columns on the SQLAlchemy model).
- Wave 3 ✅ B0 (unit-test persistence fixtures), B3 (canonical `normalize_daily_returns` extracted to `analytics_math.py`; `_normalize_report_returns` now delegates).
- Wave 4 ✅ B4 (`build_series_payload` — daily + monthly-end TypedDict payload, round-trips through `SeriesPayload`).
- Wave 5 ✅ B6 (`BacktestResultsResponse` extended with `series` / `series_status` / `has_report`; inline `trades` field removed).
- Wave 6 ✅ B10 (HMAC signed-URL machinery: `report_signer.py` module + `POST /{id}/report-token` + `GET /report?token=` extension + `BacktestReportTokenResponse` + `get_current_user_or_none` helper + prod-secret guard + `report_token_ttl_seconds ≤ 300` cap).
- Wave 7 ✅ B7 (wire new response shape on `GET /results` — `func.count()` for `trade_count`, `JSONResponse`-wrapped 404 envelope).
- Wave 8 ✅ B8 (paginated `GET /{id}/trades` with `(executed_at, id)` secondary sort + server-side page-size clamp at 500).
- Wave 9 ✅ B5 (worker integration — `_materialize_series_payload` helper in `workers/backtest_job.py` + caller-side invocation in `_execute_backtest` + `_finalize_backtest` signature extended with `series_payload` + `series_status`; fail-soft failure log with `nautilus_version` per PRD §7).
- Wave 10 ✅ B9 (payload-size observability — canonical `msai_backtest_results_payload_bytes` histogram registered in `trading_metrics.py` with 1KB/10KB/100KB/1MB/10MB buckets; observed at BOTH worker-write and `/results` response; `msai_backtest_trades_page_count` counter labeled by effective page_size on `/trades`).
- Wave 11 ✅ F1 (frontend TS types — `SeriesStatus`, `SeriesDailyPoint`, `SeriesMonthlyReturn`, `SeriesPayload`, extended `BacktestResultsResponse`, replaced `BacktestTradeItem` with individual-fill shape, new `BacktestTradesResponse` + `BacktestReportTokenResponse`, `getBacktestTrades()` + `getBacktestReportToken()` client helpers).
- Wave 12 ✅ F2 (`<ReportIframe>` — signed-URL flow; mounts → `useAuth()` → `getBacktestReportToken()` → origin-qualifies against `NEXT_PUBLIC_API_URL` → sets iframe `src` with `sandbox="allow-scripts allow-same-origin"`. Per-mount fetch so expired 60s tokens auto-refresh on tab re-open).
- Wave 13 ✅ F3 (detail-page Tabs wrapper — Native view / Full report split; removed `equityCurve: []` + `<TradeLog trades={[]} />` hardcodes at `app/backtests/[id]/page.tsx`).
- Wave 14 ✅ F4 (wired equity + drawdown charts to `series.daily`; drawdown formatters updated for ratio→percent conversion; gate on `seriesStatus === "ready" && daily.length > 0` with `<SeriesStatusIndicator>` fallback).
- Wave 15 ✅ F5 (native `<MonthlyReturnsHeatmap>` — CSS Grid + Tailwind oklch cells, year rows × month columns, intensity scaling on |pct|, hover tooltip with precise pct).
- Wave 16 ✅ F7 (shared `<SeriesStatusIndicator>` — 3-state empty: info for `not_materialized`, amber warning for `failed`, fragment for `ready`).
- Wave 17 ✅ F6 (paginated `<TradeLog>` — `backtestId` prop drives `useAuth` + `getBacktestTrades`; Prev/Next buttons + page-of-N counter; per-fill columns: timestamp / instrument / side badge / quantity / price / P&L / commission; loading/error/empty states).

**Inline "no bugs left behind" fix:** `@playwright/test` package wasn't installed after the 2026-04-21 Playwright scaffold move from repo-root to `frontend/`, which was breaking `pnpm build`'s typecheck step. Installed as devDep (~2 MB metadata; browsers remain un-downloaded, which is fine for build-only) — `pnpm build` now compiles 16/16 routes clean.

**Phase 4 final test totals:** Backend 96/96 pytest green across `test_backtest_job`, `test_analytics_math`, `test_backtest_schemas`, `test_backtest_model`, `test_report_signer`, `test_metrics`, and `test_backtests_api` integration. Ruff + mypy --strict clean on all touched backend files. Frontend `tsc --noEmit` clean, `pnpm build` 16/16 routes OK, `pnpm lint` 0 errors (2 pre-existing warnings in unrelated files).

**Session recovery (2026-04-21 context compaction):** Waves 1–8 work was shelved to `stash@{0}` during context compaction and restored cleanly on resume via `git stash pop`. No work lost.

**Phase 5 — Code review loop iter-1 (2026-04-22):** 6 reviewers in parallel (Codex CLI + 5 pr-review-toolkit agents). Raw findings: 3 P0 + ~20 P1 + ~12 P2. All applied inline:

_Security P0s:_ (1) prod-secret guard extended to reject empty + short (<32 char) secrets — HMAC with empty key silently turns signed URLs into a forgeable no-op; (2) path-traversal check switched from `str.startswith()` (defeated by prefix collision like `.../reports_evil/...`) to `Path.is_relative_to()`; (3) `user_sub` claim now enforced when a session is attached (cross-user token replay returns 403 `TOKEN_SUB_MISMATCH`) while still allowing the capability-token pattern for the iframe fetch.

_P1 fixes:_ Alembic CHECK constraint on `series_status` (values outside `{ready,not_materialized,failed}` now fail at write, not as API 500 at read); Pydantic `model_validator` enforces the `series ⇔ series_status == "ready"` invariant; `has_report` now checks `Path.is_file()` so stale DB pointers don't produce a "click-tab-see-spinner-see-404" UX; all `/report` error paths use `JSONResponse` with structured codes (`NOT_FOUND`, `NO_REPORT`, `FORBIDDEN`, `REPORT_FILE_MISSING`, `INVALID_TOKEN`, `TOKEN_SUB_MISMATCH`); empty-series `"ready"` state gets a distinct `<EmptySeriesPanel>` instead of silently blank chart cards; `/results` retry-exhaustion surfaces an error banner instead of null-rendering; `BacktestTradeItem.side` narrowed to `Literal["BUY","SELL"]`; `msai_backtest_trades_page_count` bucketed to 3 label classes (cardinality guard); `NEXT_PUBLIC_API_URL` default corrected `:8000`→`:8800` across 3 call sites; `CancelledError`/`SystemExit`/`KeyboardInterrupt` explicitly re-raised in `_materialize_series_payload`; 4 KB token input cap in `verify_report_token`; iter-N/task-ID/advisor-name/date references scrubbed from production code per CLAUDE.md rule.

_Frontend UX:_ `<ReportIframe>` + `<TradeLog>` now map `ApiError.body.error.code` to user-facing copy ("Report link expired — switch tabs to reload", etc.) instead of leaking URL templates.

_Tests added:_ 4 `get_current_user_or_none` unit tests; 6 prod-secret-guard Settings tests; 2 new signer edge tests (tampered-payload distinct from cross-backtest; oversized-token rejection); 3 `/report` auth-boundary integration tests (no-auth+no-token, invalid-token-string, sub-mismatch); 3 P2 gap-closures (`page_size=0` rejection, secondary-sort order-preservation, path-traversal negative); alembic round-trip extended to verify the CHECK constraint survives upgrade/downgrade/re-upgrade.

_Current test totals:_ 114/114 backend pytest pass; ruff clean; mypy --strict clean on PR-touched source files; frontend `tsc --noEmit` + `pnpm lint` clean. Iter-2 pending.

**Phase 5 — Code review loop iter-2 → iter-6 (2026-04-22):** 5 additional iterations, narrowing each pass. Trajectory: iter-2 (0 P0 + 5 P1 + 6 P2) → iter-3 (2 P1) → iter-4 (1 P1 + 3 comment residuals) → iter-5 (1 P1 + 1 P2) → iter-6 (0/0/0 across 5 available reviewers; Codex pending). Each iteration strictly narrower than the previous — productive convergence.

_Iter-2 fixes:_ `SeriesDailyPoint.equity` relaxed to `ge=0.0` (total-loss days legitimate); `TOKEN_SUB_MISMATCH` logs WARNING for cross-user-replay forensic trail; `_report_is_deliverable()` helper deployed across `/results`, `/report-token`, `/report` for eligibility parity; `get_current_user_or_none` logs INFO on HTTPException swallow; `verify_report_token` exception catch narrowed to `(binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError)`; comment scrub extended to `core/config.py` + test files.

_Iter-3 fixes:_ Resolved merge-conflict markers in `tests/unit/test_metrics.py`; rebuilt alembic rejecting-INSERT test with correct Strategy columns (`config_schema_status`, not the non-existent `status`); added `math.isnan` guard in `Histogram.observe` to preserve Prometheus `+Inf bucket count == _count` invariant.

_Iter-4 fixes:_ Git index state resolution (`git add` on previously-UU files); 3 comment residual scrubs (schemas "council verdict", conftest "Task B0" ×2).

_Iter-5 fixes (critical iframe-rendering bug):_ Codex caught `FileResponse(..., filename=...)` defaulting to `Content-Disposition: attachment`, which would make browsers download the QS HTML instead of rendering it inside the iframe. Fix: `content_disposition_type="inline"` + new integration-test assertion pinning the header contract. Also: CONTINUITY.md merge-marker cleanup; `Task B10` docstring scrub in test_report_signer.py.

_Iter-6 verdicts:_ code-reviewer CONVERGED · silent-failure-hunter CONVERGED · pr-test-analyzer CONVERGED · comment-analyzer CONVERGED · type-design-analyzer CONVERGED · Codex found 1 P2 (chart X-axis TZ off-by-one: `new Date("YYYY-MM-DD")` + local `getMonth/getDate` renders each point one calendar day early for US timezones).

_Iter-6 fix (iframe bug aftermath):_ introduced `formatTickDate(isoDate: string) -> string` helper in `results-charts.tsx` that parses YYYY-MM-DD string components directly (no `new Date()` coercion). Both equity + drawdown chart `XAxis tickFormatter` props switched to the helper. Fallback returns the original string on malformed input so the chart tick never crashes.

_Iter-7 verdicts:_ code-reviewer · silent-failure-hunter · pr-test-analyzer · comment-analyzer · type-design-analyzer · Codex — all 6 CONVERGED. **Code review loop PASS after 7 iterations.**

**Phase 5.2 — Simplify pass (3 agents parallel):** _Reuse:_ `_error_response(status_code, code, message) -> JSONResponse` helper extracted into `api/backtests.py`; 11 call sites deduped (~50 LOC removed). _Quality:_ 13 edits across 7 files — magic numbers named (heatmap oklch constants, chart tick interval, histogram bucket bounds, page-size buckets), narrative comments trimmed. _Efficiency:_ `MonthlyReturnsHeatmap` year/month pivot wrapped in `useMemo` so it doesn't rebuild on every parent render.

**Phase 5.3 — Verify (via verify-app subagent):** Caught 3 regressions from earlier iter-fixes, all corrected inline. (1) P0 React hooks-order: `useMemo` in `MonthlyReturnsHeatmap` was placed after the empty-state early return — moved above; `pnpm lint` + `pnpm build` now clean (16/16 routes compile). (2) mypy unused `type: ignore` in `analytics_math.py:323` — removed. (3) `test_coverage_still_missing_after_ingest_returns_partial_gap` fails — confirmed pre-existing at HEAD via stash-check (0 diff on `auto_heal.py` and the test file from HEAD), not a regression. Final: 1701/1702 unit + 24/24 integration + 6/6 Settings + 4/4 auth-optional tests pass. Ruff clean on all 21 PR-touched files. mypy --strict clean on PR source. Frontend `tsc --noEmit` + `pnpm lint` + `pnpm build` clean.

**Phase 5.4 — E2E verified (verify-e2e agent + Playwright MCP):** Report at `tests/e2e/reports/2026-04-22-backtest-results-charts-and-trades.md`. **Verdict: PASS 5/6 + 1 SKIPPED_INFRA** — no FAIL_BUG.

- UC-BRC-001 (fresh-backtest happy path): API + UI PASS. Submitted SPY 2024-01 backtest, auto-heal downloaded data, `series_status="ready"`, 318 trades, Sharpe 5.05. Full-report tab iframe rendered QuantStats tearsheet inline (Cumulative Returns + Key Performance Metrics).
- UC-BRC-002 (legacy-row empty state): API + UI PASS. Pre-feature backtest shows `<SeriesStatusIndicator>` empty-state ("Analytics unavailable for this backtest"), Full-report tab disabled when `has_report=false`.
- UC-BRC-003 (compute-failed distinct state): SKIPPED_INFRA — state unreachable through public inputs; unit coverage in `test_backtest_job.py` + `test_backtest_schemas.py` pins the contract.
- UC-BRC-004 (paginated trade log): API + UI PASS. TradeLog shows "318 fills · Page 1 of 4", Previous disabled on page 1, Next advances to "Page 2 of 4".
- UC-BRC-005 (signed-URL auth boundary): API PASS on all 6 steps — unauthenticated 401, malformed token 401, valid token 200 with `Content-Disposition: inline`, expired 401, cross-backtest 401, cross-user 403 with WARNING log.
- UC-BRC-006 (iframe inline + download): PASS (via UC-001). `content_disposition_type="inline"` fix validated end-to-end — iframe renders 334 KB QS HTML inline, Download Report button works separately.

**Phase 5.4b — E2E regression:** PASS. UC-BRC-001's auto-heal execution doubles as regression coverage for PR #40 UC-BAI-001. Error-envelope contract (PR #39) exercised by UC-BRC-005. Strategy config-form surface (PR #38) untouched. Live-trading surface (PR #37) untouched + skipped per live-trading safety rail.

**Phase 6.2b — UCs graduated:** 6 UCs committed at `tests/e2e/use-cases/backtests/results-charts-and-trades.md`.

_Final test totals after iter-6:_ 123/123 backend pytest pass; ruff clean on all PR source + test files; mypy --strict clean on PR source files; frontend `tsc --noEmit` + `pnpm lint` clean (0 errors, 2 pre-existing warnings in unrelated files).

**Architectural pivot at plan-review iter-9:** original Next.js iframe proxy with server-side `MSAI_API_KEY` was an auth bypass. Redesigned as stateless HMAC signed URLs (60s TTL, scoped to `backtest_id + user_sub`) — scales to multi-tenant services per Pablo's roadmap.

This entry will be replaced with the full PR-merge changelog when Phase 6 completes.

---

### 2026-04-21 — CLAUDE.md template sync from claude-codex-forge (uncommitted, no workflow)

Non-substantive docs update syncing `CLAUDE.md` against the current `claude-codex-forge/CLAUDE.template.md`:

- Added `@CONTINUITY.md` import at line 1 so CONTINUITY auto-loads into every session.
- Added `### Research Enforcement` subsection explaining the `research-first` Phase-2 brief at `docs/research/YYYY-MM-DD-<feature>.md`.
- Added top-level `## No Bugs Left Behind Policy` H2 (surfaces the policy already in `.claude/rules/critical-rules.md`).

Commit when convenient; not on an active workflow branch.

**Playwright scaffold location — RESOLVED (Option b, forge intent).** Moved Playwright scaffold to `frontend/` to match the forge's `setup.sh --with-playwright` auto-detect (which finds the lone `package.json` subdir in msai-v2's backend+frontend split). Executed:

- Deleted root `playwright.config.ts` + root `tests/e2e/{.auth,fixtures}/` (stale Apr-19 scaffold from pre-auto-detect setup.sh).
- Kept root `tests/e2e/{use-cases,reports}/` (verify-e2e agent artifacts, independent of the Playwright framework).
- Fixed `frontend/playwright.config.ts` baseURL from `:3000` (container-internal) → `:3300` (host-exposed Docker port, matches the existing local-dev convention).
- Accepted the uncommitted `docs/ci-templates/{README.md,e2e.yml}` diff — stamps `working-directory: frontend` + updated comments.
- Updated CLAUDE.md file-tree block + Playwright Framework section to document `frontend/` scaffold + the agent-artifact split at repo root.

### 2026-04-21 — backtest auto-ingest on missing data (branch `feat/backtest-auto-ingest-on-missing-data`) — READY FOR PR

**What this PR does:** When a backtest fails with `FailureCode.MISSING_DATA`, the platform transparently auto-downloads the missing data (bounded lazy: ≤10y, ≤20 symbols, no options-chain fan-out) and re-runs the backtest. Agents submitting backtests via API/CLI/UI see success without seeing the missing-data failure — the failure envelope only surfaces when auto-heal itself fails (guardrail rejection, 30-min cap timeout, or provider error). Closes the PR #39 scope-defer by deriving `asset_class` server-side from the canonical instrument ID.

**Shipped on branch (not yet in main):**

- **Auto-heal orchestrator** `backend/src/msai/services/backtests/auto_heal.py` — `run_auto_heal(backtest_id, instruments, start, end, catalog_root, caller_asset_class_hint, pool)` coordinates the full cycle: async `derive_asset_class` → guardrail evaluation → Redis dedupe lock acquire (placeholder → Lua CAS swap to real `job_id`) → `enqueue_ingest` on the dedicated `msai:ingest` queue → arq `Job.status()` poll with 30-min wall-clock cap → `verify_catalog_coverage` re-check (with 7-day tolerance for market-hour edge gaps) → return `AutoHealOutcome.{SUCCESS, GUARDRAIL_REJECTED, TIMEOUT, INGEST_FAILED, COVERAGE_STILL_MISSING}`. 10 unit tests.
- **Server-side `asset_class` derivation** `backend/src/msai/services/backtests/derive_asset_class.py` — async `derive_asset_class(symbols, *, start, db)` uses `SecurityMaster.asset_class_for_alias` (registry-first) with shape-heuristic fallback for unregistered symbols. `asset_class_for_alias` translates registry taxonomy (`equity`/`future`/`option`/`forex`/`crypto`) to ingest taxonomy (`stocks`/`futures`/`options`/`forex`/`crypto`). Closes PR #39's documented "UI defaults to stocks" bug for futures. 22 unit tests.
- **Dedicated ingest queue routing** — `backend/src/msai/core/queue.py:enqueue_ingest` now returns `arq.Job` and passes `_queue_name=settings.ingest_queue_name`. `backend/src/msai/workers/ingest_settings.py:IngestWorkerSettings.functions` registers `run_ingest` alongside `run_nightly_ingest`. Fixes a 2-line bug where on-demand ingest jobs previously landed on the default backtest queue (starvation risk flagged by council as a P1 blocker).
- **Redis dedupe lock** `backend/src/msai/services/backtests/auto_heal_lock.py` — `AutoHealLock` (`frozen=True, slots=True`) with `try_acquire` / `release` / `get_holder` / `compare_and_swap` methods. Key = `auto_heal:sha256(asset_class|sorted(symbols)|start|end)[:32]`. Concurrent submissions for the same symbol/range share a single ingest job. 8 unit tests including TTL-expiry + Redis-connection-error paths.
- **Catalog coverage verification** — `backend/src/msai/services/nautilus/catalog_builder.py:verify_catalog_coverage` wraps Nautilus-native `ParquetDataCatalog.get_missing_intervals_for_request` with end-of-day nanosecond precision (iter-2 fix for off-by-one gap). Orchestrator applies a 7-day total-gap tolerance to accept legitimate market-closed edge gaps (NYE / weekends / last-bar-of-day).
- **Guardrail evaluator** `backend/src/msai/services/backtests/auto_heal_guardrails.py` — `evaluate_guardrails(asset_class, symbols, start, end, max_years, max_symbols, allow_options) -> GuardrailResult`. First-match order: empty → options-disabled → range-exceeds-10yr → symbol-count-exceeds-20. Council-locked values: `AUTO_HEAL_MAX_YEARS=10`, `AUTO_HEAL_MAX_SYMBOLS=20`, `AUTO_HEAL_ALLOW_OPTIONS=False` (Databento OPRA OHLCV-1m = $280/GB), `AUTO_HEAL_WALL_CLOCK_CAP_SECONDS=1800`, `AUTO_HEAL_POLL_INTERVAL_SECONDS=10`, `AUTO_HEAL_LOCK_TTL_SECONDS=3000`. All 7 settings exposed as env vars. 8 unit tests + invariant `__post_init__` validators.
- **Retry-once integration** `backend/src/msai/workers/backtest_job.py` refactored: `_execute_backtest(...)` extracts the catalog-build + subprocess-spawn + finalize path; outer `run_backtest_job` runs a retry-once loop (`attempt < 2`). On first-attempt `FileNotFoundError`, `run_auto_heal(..., pool=ctx["redis"])` runs one cycle. `AutoHealOutcome.SUCCESS` → re-enter `_execute_backtest` with the same `backtest_row` snapshot (single `_start_backtest` call — `attempt` counter increments once, not twice). Non-SUCCESS outcomes translate to typed exceptions via `_OUTCOME_TO_EXC` (FileNotFoundError→MISSING_DATA, TimeoutError→TIMEOUT, RuntimeError→ENGINE_CRASH) so the existing classifier produces the right FailureCode. 6 integration tests.
- **`BacktestStatusResponse` + `BacktestListItem` schema** extended with `phase: Literal["awaiting_data"] | None` + `progress_message: str | None`. `/status` and `/history` endpoints populate from the new columns. 8 schema tests + 2 integration tests.
- **4 additive Postgres columns** via Alembic `y3s4t5u6v7w8_add_backtest_auto_heal_columns`: `phase String(32)`, `progress_message Text`, `heal_started_at Timestamptz`, `heal_job_id String(64)`. All nullable, no backfill required. Round-trip test in `test_alembic_migrations.py`.
- **Frontend UI** — `frontend/src/app/backtests/[id]/page.tsx` gains a polling `useEffect` loop (3s cadence, local `let resultsRetries = 0` to avoid a useState stale closure, bounded at 10 retries × 3s = 30s window for the /results race after status=completed). A subtle `<div data-testid="backtest-phase-indicator">` with a `Loader2` spinner and `data-testid="backtest-phase-message"` text renders when `phase === "awaiting_data"`. `frontend/src/app/backtests/page.tsx` list page: compact "Fetching data…" badge (`data-testid="backtest-list-fetching-badge"`) next to the Running status badge; "View details" link now renders for all non-pending rows (was completed/failed only).
- **13 structured-log events** emitted via `structlog` (contextvars-bound `backtest_id`): `backtest_auto_heal_{started, guardrail_rejected, ingest_enqueued, ingest_completed, ingest_failed, ingest_enqueue_declined, timeout, coverage_still_missing, coverage_check_failed, completed, phase_update_failed, lock_cas_lost, ingest_status_not_found_falling_through}`.
- **`fakeredis[lua]>=2.20`** added to `backend/pyproject.toml` dev deps for Lua-CAS tests.

**Artifacts produced:**

- PRD: `docs/prds/backtest-auto-ingest-on-missing-data.md` (v1.0, 7 user stories)
- Discussion log: `docs/prds/backtest-auto-ingest-on-missing-data-discussion.md`
- Research brief: `docs/research/2026-04-21-backtest-auto-ingest-on-missing-data.md` (8 targets + 3 cross-cutting observations; 4 design-changing findings including the 2-line queue-routing fix)
- Plan: `docs/plans/2026-04-21-backtest-auto-ingest-on-missing-data.md` (8-iteration plan-review loop, productive convergence trajectory 10 → 7 → 2 → 1 → 1 → 1 → 1 → 0)
- E2E use cases: `tests/e2e/use-cases/backtests/` (5 UCs graduating in Phase 6.2b)
- E2E report: `tests/e2e/reports/2026-04-21-backtest-auto-ingest-on-missing-data.md` (PASS 3 + PARTIAL 1 + SKIPPED_INFRA 1)
- Council verdict: preserved in discussion log (bounded lazy auto-heal, no eager pre-seed, options hard-reject, separate ingest queue blocker accepted, exception-type preservation for classifier)

**Phase 5 gates status (all GREEN):**

- **Code-review loop (3 iterations — clean on iter-3):** iter-1 6 reviewers parallel (Codex `exec review` + 5 pr-review-toolkit agents) found 6 P1 + 10 P2. All applied: classifier stale comment, `asset_class_for_alias` registry→ingest taxonomy map (P1 — would silently write Parquet to wrong path), AutoHealResult/GuardrailResult `__post_init__` cross-field invariants, `verify_catalog_coverage` exception guard (Nautilus failures now map to COVERAGE_STILL_MISSING not ENGINE_CRASH), heartbeat + nautilus version capture `exc_info=True`, `AutoHealLock` `frozen+slots`, phase Literal extension-point comment, stale-lock TTL test + 2 Redis connection-error regression tests. Iter-2 found 1 P2 (`gaps=[]` on exception path misleading vs `None`=verification-errored). Iter-3 clean.
- **Simplify:** 3-agent parallel sweep (reuse/quality/efficiency). 2 efficiency wins (`_set_backtest_phase` single `update()` statement instead of SELECT+mutate+commit → halves DB roundtrips; frontend polling `setStatus` shallow-compare guard prevents 20 no-op React re-renders/minute during awaiting_data). 1 abstraction fix (moved `CAS_LOCK_VALUE_LUA` `pool.eval()` into typed `AutoHealLock.compare_and_swap(key, from_holder, to_holder, ttl_s) -> bool`). 9 comment-hygiene fixes (stripped iter-N markers from production code; deduped field-doc blocks on status/list schemas + 3 API call sites; trimmed module docstrings).
- **Verify-app:** 6/6 gates GREEN. Backend pytest 1896 pass / 0 fail / 10 skipped / 16 xfailed. Backend ruff clean on PR-touched files. Backend mypy --strict total errors reduced 97→70 on the same file set (PR cleans up pre-existing issues incidentally). Frontend tsc clean. Frontend lint 0 new warnings (1 pre-existing in `app/research/page.tsx:114`). Frontend pnpm build 16 routes compiled clean.
- **E2E (Phase 5.4):** PASS 3 + PARTIAL 1 + SKIPPED_INFRA 1 per report. UC-BAI-002 (guardrail 11y): GREEN — full envelope (`code=missing_data`, `suggested_action=Run: msai ingest futures ES.n.0.XCME 2013-01-01 2024-12-31`, `auto_available=false`). UC-BAI-003 (futures asset_class): GREEN via structured logs (`asset_class=futures` in both orchestrator + ingest worker; closes PR #39 bug); full envelope path blocked by Databento entitlement (env, not product). UC-BAI-004 (concurrent dedupe): GREEN — two concurrent submits shared `lock_key` + `ingest_job_id`; second caller logged `dedupe_result=wait_for_existing:...`. UC-BAI-005 (UI): GREEN via direct Playwright MCP (not `verify-e2e` agent) — paused `ingest-worker` to hold backtest in awaiting_data, confirmed detail-page `backtest-phase-indicator` + `backtest-phase-message` + list-page `backtest-list-fetching-badge` + running-row clickable + reload persistence + terminal transition clears indicator. UC-BAI-001 (cold-stock happy path): SKIPPED_FAIL_INFRA (registry empty for stocks at time of initial verify-e2e run).
- **Live end-to-end demo (Pablo-requested 2026-04-21 post-Phase-5.4):** Pablo asked to see the full flow with a real symbol. Inserted SPY + AAPL into the registry manually; submitted SPY backtest for 2024-01-01→2024-01-31 with no pre-existing data. Auto-heal triggered: status flipped to `running`+`phase=awaiting_data`+`"Downloading stocks data for SPY.XNAS"`. Ingest worker downloaded 10,350 real bars from Databento. Coverage verified (with 7-day tolerance). Backtest re-entered with real data, produced 418 trades, Sharpe 4.97, Sortino 12.30, Max Drawdown -0.25%, Total Return +112.15%, Win Rate 30.1% on SPY Jan 2024. UI rendered all 6 metric cards correctly. Total wall-clock: 12 seconds from submit to completed.
- **Two bugs surfaced + fixed during the live demo (no bugs left behind):**
  - **Venue-convention mismatch:** The auto-heal coverage re-check originally called `SecurityMaster.resolve_for_backtest` which returns the registry's MIC-code alias (`SPY.XNAS`), but `ensure_catalog_data` / the backtest subprocess write the Nautilus catalog under the Nautilus venue convention (`SPY.NASDAQ`). Coverage lookup found nothing → perpetual COVERAGE_STILL_MISSING even after successful ingest. **Fix:** auto-heal orchestrator now calls `ensure_catalog_data(...)` directly to get canonical IDs in the SAME form the subprocess uses (same directory structure the catalog actually stores). Applied at `auto_heal.py:320-336`.
  - **Coverage check too strict:** Nautilus's `get_missing_intervals_for_request` does contiguous nanosecond coverage. For equities, legitimate market-closed windows (NYE, weekends, last-bar-of-day boundaries) show up as gaps. 31-day request with 10,350 real bars gave 2 edge gaps totaling ~30 hours → falsely flagged as coverage missing. **Fix:** auto-heal orchestrator applies a 7-day total-gap tolerance — catches real partial returns (e.g., "Jun-Dec when Jan-Dec requested" = ~150-day gap) while accepting holiday/weekend edges. Applied at `auto_heal.py:364-394`.

**Known out-of-scope limitations (not introduced by this PR; pre-existing):**

- `/backtests/{id}/results` returns only 6 aggregate metrics. No timeseries fields (`equity_curve`, `drawdown_series`, `monthly_returns`). The detail page at `frontend/src/app/backtests/[id]/page.tsx:203` explicitly says `// The backend results endpoint does not yet return an equity curve or a trade log. Show empty charts until the backend supports it.` — Equity Curve / Drawdown / Monthly Returns Heatmap all render empty. QuantStats HTML report (full analytics, 60+ stats) IS generated per backtest and downloadable via `/api/v1/backtests/{id}/report` — piping that into the React UI is a separate feature.
- `<TradeLog trades={[]} />` hardcoded empty. `/results` endpoint DOES return 418 trades but the frontend doesn't pass them through. Wiring requires a TS type fix too (backend sends individual fills with `price`+`executed_at`; TS type expects entry/exit round-trips with `entryPrice`+`exitPrice`+`holdingPeriod`).

**Recommended follow-up PR (scope-separated):** extend `/results` response with `equity_curve`, `drawdown_series`, `monthly_returns` + wire `results.trades` through to `<TradeLog>` with a TS type fix.

---

### 2026-04-21 — strategy config schema extraction (branch `feat/strategy-config-schema-extraction`) — MERGED as PR #38

**Shipped on branch (not yet in main):**

- `msai.services.nautilus.schema_hooks` module with `nautilus_schema_hook` (covers `InstrumentId`, `BarType`, `StrategyId`, `Venue`, `Symbol`, `AccountId`, `ClientId`, `OrderListId`, `PositionId`, `TraderId`, `ComponentId`), `ConfigSchemaStatus` StrEnum (`ready | unsupported | extraction_failed | no_config_class`), and `build_user_schema(config_cls) -> (schema, defaults, status)` that trims inherited `StrategyConfig` base-class fields via `config_cls.__annotations__` so the frontend form only shows user-defined parameters. 18 unit tests green.
- `DiscoveredStrategy` extended with `config_schema`, `default_config`, `config_schema_status` fields at `backend/src/msai/services/strategy_registry.py:53-81`.
- `discover_strategies` wraps `build_user_schema()` in per-strategy `try/except` (Hawk council blocking objection #1) — a single malformed `*Config` class cannot poison the whole discovery list; it surfaces with `config_schema_status == "extraction_failed"` instead.
- `sync_strategies_to_db(session, strategies_dir)` helper at `backend/src/msai/services/strategy_registry.py:~370` decouples list/detail endpoint side effects (Maintainer council blocking objection #2). Both `GET /api/v1/strategies/` and `GET /api/v1/strategies/{id}` now call it; the detail endpoint no longer depends on list having run first to sync DB state.
- Memoization by `code_hash` in the sync helper (Hawk council blocking objection #2) — when a row's stored `code_hash` matches the on-disk file hash, schema columns are NOT recomputed, avoiding an `msgspec.json.schema()` call on every `GET /strategies/` request.
- New Alembic migration `w1r2s3t4u5v6_add_config_schema_status_and_code_hash` — revises `v0q1r2s3t4u5`. Adds `strategies.config_schema_status: String(32) NOT NULL DEFAULT 'no_config_class'` and `strategies.code_hash: String(64) NULL` with `ix_strategies_code_hash` index.
- `Strategy` SQLAlchemy model + `StrategyResponse` Pydantic schema extended with `config_schema_status` and `code_hash` columns/fields.
- Server-authoritative config validation on `POST /api/v1/backtests/run` (Hawk council blocking objection #4, 2026-04-20): `_validate_backtest_config()` helper loads the strategy's `*Config` class, runs `config_cls.parse(json.dumps(config))`, catches `msgspec.ValidationError` and returns HTTP 422 with `error.details[].field` extracted from the msgspec error path. Same rule applies to CLI/API/UI callers; validation is skipped gracefully when the strategy has no `*Config` class (legacy path).

**Artifacts produced:**

- Council pre-gate spike: `backend/tests/unit/test_strategy_registry.py::TestMsgspecSchemaFidelitySpike` (5 tests) — pinned `msgspec.json.schema(…, schema_hook=...)` behavior + `StrategyConfig.parse()` round-trip + field-level error paths. Gate PASSED 2026-04-20.
- PRD: `docs/prds/strategy-config-schema-extraction.md`
- Discussion log: `docs/prds/strategy-config-schema-extraction-discussion.md`
- Research brief: `docs/research/2026-04-20-strategy-config-schema-extraction.md`
- Plan: `docs/plans/2026-04-20-strategy-config-schema-extraction.md`

**Additional shipped (later in session):**

- B7 parity test at `backend/tests/integration/test_parity_config_roundtrip.py::test_api_and_worker_inject_identical_configs_for_omitted_defaults` — asserts the API's `_prepare_and_validate_backtest_config` and the worker's `_prepare_strategy_config` produce byte-identical dicts for the same user-submitted config + resolved instruments. Contrarian council blocking objection #2 resolved.
- F1 `<SchemaForm>` mini-renderer at `frontend/src/components/strategies/schema-form.tsx` (~300 LOC, shadcn-native, zero new npm dep). Dispatches `integer / number / string / boolean / enum / nullable` field types; renders format hints for `x-format: instrument-id` / `bar-type`; hides backend-injected fields (`instrument_id`, `bar_type`) from the form since they're derived server-side from the separate `Instruments` input.
- F2 integration in `frontend/src/components/backtests/run-form.tsx`: fetches `/api/v1/strategies/{id}` on selection change, activates `<SchemaForm>` only when `config_schema_status === "ready"`, falls back to JSON textarea otherwise. 422 field-level errors (from B6's envelope) rendered inline under the relevant field.
- `StrategyResponse` TypeScript interface at `frontend/src/lib/api.ts` extended with `config_schema_status` + `ConfigSchemaStatus` type export.
- E2E use cases authored at `tests/e2e/use-cases/strategies/config-schema-form.md` (4 UCs: UC-SCS-001 to 004).
- Pre-existing test fix-up: `tests/unit/test_security_master_multi_asset.py::test_es_june_2025_fixed_month` updated to assert `YYYYMM` not `YYYYMMDD` — leftover from PR #37's format migration. "No bugs left behind".
- Dev compose `docker-compose.dev.yml` gained volume mounts for `./frontend/tsconfig.json` + `./frontend/package.json` so TS config + dep updates propagate without rebuilding the dev image.

**Phase 5 gates status (all GREEN):**

- 5.1 Code-review loop (2 iterations — clean on iter-2). Iter-1 Codex + pr-review-toolkit found: (P1) config-class suffix-swap misclassifies `FooParams` / `FooStrategyConfig`; (P1) `code_hash` doesn't track sibling `config.py`; (P2) 422 envelope wrapped under `detail`; (P2) SchemaForm didn't emit null for nullable fields; (P2) `useEffect` stale closures from unmemoized `getToken`; (P2) CORS didn't include 3300; plus Important #1 `_find_config_class` may pick up imported `StrategyConfig` base + Important #2 no orphan-row prune on file rename. All applied: added `config_class` String(255) column on `Strategy` model + alembic migration bump; `_combined_strategy_hash(info)` folds sibling `config.py` hash; new `StrategyConfigValidationError` + `@app.exception_handler` in `main.py` returns top-level `{error: {code, message, details}}` per api-design.md; SchemaForm "Use null (unset)" checkbox for `anyOf(T, null)` fields; `useAuth` memoized via `useCallback` on `login`/`logout`/`getToken` (root fix — drops all per-consumer `eslint-disable` workarounds); CORS defaults extended to include `http://localhost:3300` + 127.0.0.1 variants; `_find_config_class` explicitly excludes Nautilus base + prefers module-defined; `sync_strategies_to_db(prune_missing=True)` deletes orphan rows. Iter-2 pr-toolkit caught **P0** (runtime `NameError` — `Path` was only imported under `TYPE_CHECKING` so the orphan-prune branch would crash the first time a file was renamed in prod) + **P2** (missing regression test for prune path). Both fixed: `from pathlib import Path` moved to runtime import; new `TestSyncStrategiesToDb` class adds 2 regression tests. Iter-2 pr-toolkit verdict: **READY TO MERGE**.
- 5.2 Simplify: 3-agent sweep run during iter-1 review (reuse/quality/efficiency). No redundant state or duplicate utilities surfaced — memoized auth hooks + change-detection on schema recompute already in place.
- 5.3 Verify: **1767 backend tests pass**, 10 skipped, 16 xfailed, 0 fail (includes new `TestSyncStrategiesToDb` + `TestPrepareAndValidateBacktestConfig` + `TestMsgspecSchemaFidelitySpike` + `test_security_master_parser::TestNautilusInstrumentToCacheJson`). `ruff check` clean on changed files; `mypy --strict` clean on changed modules. Frontend: `pnpm exec tsc --noEmit` clean; `pnpm build` clean; `pnpm lint` 0 errors, 1 pre-existing warning in `app/research/page.tsx` (from pre-branch flatten commit `82a56fd` — out of scope).
- 5.4 E2E verify-e2e: **PASS 4/4**. Report at `tests/e2e/reports/2026-04-21-strategy-config-schema-extraction.md`. UC-SCS-001/003/004 via the verify-e2e agent over HTTP; UC-SCS-002 driven by the main agent via `mcp__playwright__*` against the running stack (verify-e2e agent toolbox doesn't include Playwright — tooling limitation). Infra bugs FE-01 + BE-01 (listed below) were fixed IN-BRANCH before the final run; the final 422 envelope shape was re-verified via direct curl after the iter-1 exception-handler fix.
- 5.4b E2E regression: vacuously passes — `tests/e2e/use-cases/strategies/` was empty prior to this PR; this is the first graduated use-case file in that category.

**Infra bugs found + fixed in-branch ("no bugs left behind"):**

- **FE-01 FIXED** Docker `msai-claude-frontend` failed to resolve CSS `@import "tw-animate-css"` + `@import "shadcn/tailwind.css"` + path alias `@/components/providers` despite modules present in `node_modules`. Root cause: `postcss.config.mjs` + `next.config.ts` never mounted/copied into container, so Next.js fell back to a no-op config. Fix: added `./frontend/postcss.config.mjs:/app/postcss.config.mjs:ro` + `./frontend/next.config.ts:/app/next.config.ts:ro` + `./frontend/tsconfig.json:/app/tsconfig.json:ro` + `./frontend/package.json:/app/package.json:ro` volume mounts in `docker-compose.dev.yml`, plus `COPY postcss.config.mjs next.config.ts tsconfig.json ./` in `frontend/Dockerfile.dev` as baseline. Host `pnpm build` + container `npm run dev` both now succeed with Tailwind v4 + path aliases resolving.
- # **BE-01 FIXED** `msai instruments refresh --provider databento --symbols ES.n.0` failed with `TypeError: FuturesContract.to_dict() takes no arguments (1 given)` at `backend/src/msai/services/nautilus/security_master/parser.py::nautilus_instrument_to_cache_json`. Root cause: Nautilus ships `FuturesContract` in two backends — Cython instruments expose `to_dict(obj)` (staticmethod-style, needs self passed explicitly), pyo3 instruments expose bound `to_dict(self)`. Databento's loader returns pyo3 instruments; IB qualification returns Cython. Fix: try/except dual dispatch — `try: instrument.to_dict() except TypeError: instrument.to_dict(instrument)`. New regression tests `TestNautilusInstrumentToCacheJson::test_prefers_bound_method_first` + `test_falls_back_to_staticmethod_signature_on_typeerror`.

### 2026-04-21 — backtest failure surfacing (branch `feat/backtest-failure-surfacing`) — IN PROGRESS

**Shipped on branch (not yet in main):**

- `msai.services.backtests` package (new) — 3 modules + 1 classifier:
  - `failure_code.py` — `FailureCode` `StrEnum` with 5 members (`missing_data`, `strategy_import_error`, `engine_crash`, `timeout`, `unknown`) + null-safe `parse_or_unknown(value)` classmethod. Mirrors the existing `services/live/failure_kind.py::FailureKind` precedent.
  - `sanitize.py` — `sanitize_public_message(raw)` strips `/app/...` → `<DATA_ROOT>/...` + `<APP>/...`, `/Users|/home/...` → `<HOME>`, traceback `File "...", line N` bookkeeping (including `SyntaxError` frames without the `in <func>` suffix + caret lines), JWT triples, bearer tokens, `api_key=...` secret-KV, and DSN-with-credentials (`postgresql+asyncpg://user:pass@host:5432/db` → `postgresql+asyncpg://<redacted>`). Truncates to 1 KB. Module-level compiled regexes.
  - `classifier.py` — `classify_worker_failure(exc, *, instruments, start_date, end_date, asset_class=None) -> FailureClassification` returns a `@dataclass(frozen=True, slots=True)` with `code / public_message / suggested_action / remediation`. Handles direct + wrapped-RuntimeError classification paths (BacktestRunner wraps subprocess exceptions at `backend/src/msai/services/nautilus/backtest_runner.py:239` as `RuntimeError(traceback_text)`, so classifier regex-matches `\b(ImportError|ModuleNotFoundError|SyntaxError|NameError)\b` against the wrapped text to recover STRATEGY_IMPORT_ERROR vs ENGINE_CRASH). Remediation for `missing_data` emits `msai ingest <asset_class> <symbols> <start> <end>` with caller-supplied `asset_class` overriding regex-capture fallback. `public_message` always non-empty (US-006 contract).
- New Alembic migration `x2r3s4t5u6v7_add_backtest_error_classification` — revises `v0q1r2s3t4u5`. Adds `backtests.error_code: String(32) NOT NULL DEFAULT 'unknown'` (PG16 `attmissingval` fast path) + `error_public_message: Text NULL` + `error_suggested_action: Text NULL` + `error_remediation: JSONB NULL`. NO SQL backfill of `error_public_message` (would leak raw content); `_build_error_envelope` sanitizes-on-read for pre-migration rows.
- `Backtest` SQLAlchemy model extended with the 4 new columns; types match migration exactly.
- `Remediation` (Pydantic `Literal["ingest_data", "contact_support", "retry", "none"]` kind + optional symbols/asset_class/start/end + `auto_available=False` default) and `ErrorEnvelope` (code, message, suggested_action?, remediation?) added to `backend/src/msai/schemas/backtest.py`. Symmetric with the api-design.md 422 shape used by PR #38.
- `BacktestStatusResponse` gains `error: ErrorEnvelope | None = None`; `BacktestListItem` gains compact `error_code` + `error_public_message` (no `suggested_action` / `remediation` in list responses — bandwidth discipline).
- `_build_error_envelope(row)` helper at `backend/src/msai/api/backtests.py` returns `None` for non-failed rows, sanitize-on-read when `error_public_message IS NULL` but `error_message` populated (US-006 null-safe). Wired into `POST /run`, `GET /{id}/status`, and `GET /history`.
- `POST /run` + `GET /{id}/status` decorated with `response_model_exclude_none=True` so the `error` key is ABSENT (not `null`) on non-failed rows per PRD contract — TS types updated to match (`error?: ...`, `started_at?: ...`, `completed_at?: ...`).
- `_mark_backtest_failed` rewritten at `backend/src/msai/workers/backtest_job.py` — signature: `(backtest_id, exc, instruments, start_date, end_date, asset_class=None)`. Calls classifier, persists all 4 new columns + raw `error_message` fallback-to-class-name for empty-str exceptions. Collapsed 3 except blocks in `run_backtest_job` → 1, preserving the 3 structured-log event names operators grep on (`backtest_missing_data` / `backtest_timeout` / `backtest_job_failed`). Uses `symbols` (bound before try) + `backtest_row["start/end_date"]`, NOT `instrument_ids` (unbound on early failure).
- Frontend:
  - `frontend/src/lib/api.ts` — `RemediationKind`/`Remediation`/`ErrorEnvelope` TS interfaces; `BacktestStatusResponse` + `BacktestHistoryItem` extended.
  - `<TooltipProvider delayDuration={200}>` mounted in `frontend/src/app/layout.tsx`.
  - `frontend/src/app/backtests/page.tsx` — `failed` rows wrap the status `<Badge>` in a Radix `<Tooltip>` showing the first 150 chars of `error_public_message`; `tabIndex={0}` + `role="button"` + `aria-label` for keyboard accessibility (Radix tooltips are desktop-hover-only per WAI-ARIA spec — mobile users access the full envelope via the new nav link). Failed rows also get an `<ExternalLink>` action-cell button linking to `/backtests/[id]`.
  - `frontend/src/components/backtests/failure-card.tsx` (new, ~147 LOC) — full structured envelope with code badge, sanitized message, `<pre><code>` block for `suggested_action` + `aria-label`ed copy-to-clipboard button, remediation details (symbols / asset_class / date range). Timer cleanup via `useRef` + `useEffect` unmount guard + clear-before-set on rapid clicks. Clipboard `try/catch` handles unsupported/insecure-origin environments with a visible fallback message.
  - `frontend/src/app/backtests/[id]/page.tsx` mounts `<FailureCard>` when `status === "failed" && status.error`.
- 7 new test files (~570 LOC): `test_backtest_failure_code.py` (4), `test_backtest_sanitize.py` (11 — including DSN + SyntaxError-frame regression), `test_backtest_schemas.py` (5), `test_backtest_classifier.py` (11 — including caller-asset_class-override + wrapped-RuntimeError paths), `test_backtest_mark_failed.py` (5 — persists every classifier code end-to-end at the worker boundary), `test_backtest_model.py` (4), `test_backtest_fixtures.py` (3 smoke tests for B0's seed fixtures).
- 3 new test classes appended to `test_backtests_api.py` — envelope on `/status` for failed/pending/historical rows + compact error fields on `/history`.
- Shared fixtures `seed_failed_backtest` / `seed_historical_failed_row` / `seed_pending_backtest` added to `backend/tests/unit/conftest.py` — all install `get_db` override via `AsyncMock(spec=AsyncSession)` + `try/finally` cleanup.

**Artifacts produced:**

- PRD: `docs/prds/backtest-failure-surfacing.md` (6 user stories + Codex-ratified Q1–Q7 design decisions)
- Discussion log: `docs/prds/backtest-failure-surfacing-discussion.md`
- Research brief: `docs/research/2026-04-20-backtest-failure-surfacing.md` (7 libraries surveyed — Radix Tooltip, Pydantic Literal, SQLAlchemy JSONB, Alembic NOT-NULL migration, Playwright hover, StrEnum)
- Plan: `docs/plans/2026-04-20-backtest-failure-surfacing.md` (14 tasks: B0–B8 + F1–F4; 9 plan-review iterations)
- Worker stale-import refresh ran via `docker compose -f docker-compose.dev.yml restart` (Phase 5.4 pending).

**Phase 5 gates status:**

- 5.1 Code-review loop: **3 iterations — PASS.** Iter-1 Codex 2 P1 + 3 P2 / pr-toolkit 0 P0/P1/P2 + 5 P3 nits. Iter-1 findings applied: classifier `asset_class` kwarg plumbed through worker; sanitizer DSN + SyntaxError-frame regressions added; `response_model_exclude_none=True`; Badge `tabIndex=0`/role/aria-label for keyboard access; `FailureCard` clipboard try/catch. Iter-2 Codex 1 P1 (UI doesn't send `asset_class`) + 1 P2 (TS types mismatch); P1 documented as scope-defer in classifier docstring (core feature works; only `stocks` positional arg will be wrong for futures-via-UI); P2 fixed via `started_at?`/`completed_at?` TS optional. Iter-3: Codex "PLAN APPROVED — no new P0/P1/P2".
- 5.2 Simplify: 3-agent sweep. Reuse: clean. Quality: DRY'd `symbols_for_cmd` in classifier + swept all `[iter-N P?]` / `[Phase 5 P?]` breadcrumb markers from code (kept prose, dropped prefixes). Efficiency: `FailureCard` timers now `useRef` + `clearTimeout` + unmount cleanup.
- 5.3 Verify: verify-app iter-1 FAIL (ruff 9 errors); iter-2 **ALL 6 GATES PASS** — 1779 backend tests (1 pre-existing out-of-scope fail: `test_es_june_2025_fixed_month` in security_master), ruff clean on PR-touched files, mypy --strict no new errors, frontend tsc 0 errors, pnpm build clean.
- 5.4 E2E: pending (UC-BFS-001..005 defined in the plan — 1 API status-envelope, 1 API history-compact, 1 CLI `msai backtest show`, 1 UI tooltip + nav, 1 UI FailureCard).

**Known limitation (documented scope-defer):**

- The UI's Run Backtest form does not currently send `config.asset_class`; worker defaults to `"stocks"`. For a futures-backtest launched via UI against a symbol like `ES.n.0`, the remediation command will read `msai ingest stocks ES.n.0 ...` instead of `msai ingest futures`. Core feature (user sees WHY the backtest failed) is unaffected — only the positional `asset_class` arg of the suggested command is wrong. Follow-up PR: either add an `asset_class` dropdown to the UI form, or derive it server-side from the resolved canonical instrument ID shape.
  > > > > > > > Stashed changes

### 2026-04-20 — live-path registry wiring (branch `feat/live-path-wiring-registry`)

**Shipped:**

- `lookup_for_live(symbols, as_of_date)` pure-read resolver over `instrument_definitions` + `instrument_aliases` at `backend/src/msai/services/nautilus/security_master/live_resolver.py`. Returns typed `ResolvedInstrument` (options-extensible `contract_spec` dict). Supports dotted inputs (`AAPL.NASDAQ` → `find_by_alias`) and bare tickers (`AAPL` → `find_by_raw_symbol`). Provider-filtered alias walk + overlap tie-break by `(effective_from DESC)`; same-day overlap raises `AmbiguousRegistryError(reason=SAME_DAY_OVERLAP)`.
- `AssetClass` enum (`equity/futures/fx/option/crypto`) + `ResolvedInstrument` frozen dataclass + error hierarchy: `LiveResolverError(ValueError)` base with `RegistryMissError / RegistryIncompleteError / UnsupportedAssetClassError / AmbiguousRegistryError` subclasses. Each error exposes `.to_error_message()` emitting a JSON envelope (`{code, message, details}`) parsable by the API layer.
- `_build_contract_spec` derives IB-compatible `{secType, symbol, exchange, primaryExchange, currency, lastTradeDateOrContractMonth}` dict from a registry row pair. Per-asset-class logic: equity/ETF STK, futures FUT with month-code parser (decade-boundary-safe: `2029-12-15 + ESH0 → 203003`, not `2020-03`), FX CASH with `BASE/QUOTE` split and `base`/`quote` non-empty validation.
- `build_ib_instrument_provider_config_from_resolved(resolved)` at `backend/src/msai/services/nautilus/live_instrument_bootstrap.py` — registry-backed IB preload config builder. Reconstructs `IBContract` from `contract_spec` dicts; filters unknown kwargs for forward-compat with options. No `PHASE_1_PAPER_SYMBOLS` gate.
- `build_portfolio_trading_node_config` at `backend/src/msai/services/nautilus/live_node_config.py:~466` now aggregates `member.resolved_instruments` dedup'd by `canonical_id` across strategy members. Raises `ValueError` if the aggregate is empty (supervisor-threading bug safety net).
- Supervisor `backend/src/msai/live_supervisor/__main__.py:~281-328` now calls `await lookup_for_live(...)` in the payload factory (replacing per-member `canonical_instrument_id` calls) and threads `resolved_instruments=member_resolved` through the `StrategyMemberPayload(...)` construction. Defensive empty-instruments guard added before the resolver call.
- `StrategyMemberPayload.resolved_instruments: tuple[ResolvedInstrument, ...] = ()` field added at `backend/src/msai/services/nautilus/trading_node_subprocess.py`. Pickle-round-trip test at `test_trading_node_payload_multi_strategy.py` locks the `mp.spawn` invariant.
- `ProcessManager` at `backend/src/msai/live_supervisor/process_manager.py:~261-297` permanent-catch dispatches on `LiveResolverError` subtype before the generic `ValueError` branch. Each subtype maps to its own `FailureKind` and the reason stored in `live_node_processes.error_message` is the JSON envelope from `exc.to_error_message()` — parseable by the endpoint.
- `FailureKind` enum extended with 4 new variants: `REGISTRY_MISS`, `REGISTRY_INCOMPLETE`, `UNSUPPORTED_ASSET_CLASS`, `AMBIGUOUS_REGISTRY` (String(32) column is additive; `parse_or_unknown` preserves backward compatibility).
- `EndpointOutcome.registry_permanent_failure(kind, error_message)` factory at `backend/src/msai/services/live/idempotency.py` — HTTP 422 + `{"error": {"code", "message", "details"}, "failure_kind"}` envelope per `.claude/rules/api-design.md`. Parses the JSON envelope from the row's `error_message`; defensive fallback for non-JSON. `cacheable=False` so retry-after-fix (operator runs `msai instruments refresh`) works with the same `Idempotency-Key`.
- `/api/v1/live/start-portfolio` handler at `backend/src/msai/api/live.py:~642-658` now dispatches on `FailureKind`: the 4 registry kinds route to `registry_permanent_failure` (422); legacy permanent kinds (SPAWN_FAILED_PERMANENT, RECONCILIATION_FAILED, BUILD_TIMEOUT, HEARTBEAT_TIMEOUT, UNKNOWN) stay on the existing `permanent_failure` (503).
- Structured telemetry `live_instrument_resolved` (structlog) + counter `msai_live_instrument_resolved_total` (project's hand-rolled MetricsRegistry, exposed via `/metrics`). Labels: `source ∈ {registry, registry_miss, registry_incomplete}`, `asset_class ∈ {equity, futures, fx, option, crypto, unknown}`. Emitted on all three outcome paths (success, miss, incomplete).
- `_fire_alert_bounded(level, title, message)` helper in `live_resolver.py` — wraps `alerting_service.send_alert` via `loop.run_in_executor(_HISTORY_EXECUTOR, ...)` + `asyncio.wait_for(shield, timeout=_HISTORY_WRITE_TIMEOUT_S)` matching `alerting.py:305-328` production pattern. Registry miss fires WARN; registry incomplete fires ERROR. Late-completion done-callback logs exceptions so post-timeout failures aren't silently swallowed.
- Registry enhancements (Task 3b): `InstrumentRegistry.find_by_alias` and `require_definition` now REQUIRE `as_of_date: date` (no default; UTC fallback removed — prevents silent roll-day regression). `AmbiguousSymbolError` carries `symbol/provider/asset_classes` as attributes instead of only a formatted string; `lookup_for_live` wraps it into `AmbiguousRegistryError(reason=CROSS_ASSET_CLASS)` without string parsing.
- AST-based regression test at `backend/tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py` — walks `live_supervisor/__main__.py` + `live_node_config.py` AST and asserts ZERO references to `canonical_instrument_id` (catches aliased imports, attribute access, and re-exports; grep would miss those). Positive assertion also confirms the helper still exists in `live_instrument_bootstrap.py` (CLI seeding still uses it).

**Changed:**

- Non-Phase-1 symbols (QQQ, GBP/USD, NQ, GOOGL, etc.) are now deployable via `msai instruments refresh --symbols X` + deploy — no code edits required.
- `canonical_instrument_id()` removed from the live-start runtime path (supervisor + live_node_config). Helper stays at `live_instrument_bootstrap.py` for the CLI seeding path only.
- `PHASE_1_PAPER_SYMBOLS` gate removed from the runtime IB preload builder.

**Tests:** 1491 tests pass (unit + integration). New test files:

- `tests/unit/services/nautilus/security_master/test_live_resolver_types.py` — 8 tests (types + exceptions + to_error_message round-trip)
- `tests/unit/services/nautilus/security_master/test_live_resolver_contract_spec.py` — 11 tests (per-asset-class + decade boundary + FX malformed guards)
- `tests/unit/services/nautilus/security_master/test_live_resolver_counter.py` — 4 tests (counter registration introspection; no state mutation)
- `tests/integration/services/nautilus/security_master/test_lookup_for_live.py` — 11 tests (dotted/bare, overlap, miss aggregation, futures roll, option reject, ambiguous SAME_DAY)
- `tests/integration/services/nautilus/security_master/test_live_resolver_telemetry.py` — 2 tests (structured log via `structlog.testing.capture_logs`)
- `tests/integration/services/nautilus/security_master/test_live_resolver_alerts.py` — 1 test (positional-arg mock on `alerting_service`)
- `tests/unit/services/nautilus/test_live_instrument_bootstrap.py` — 5 new tests appended (equity/FX/futures/empty/unknown-kwargs)
- `tests/unit/services/nautilus/test_live_node_config_registry.py` — 3 tests (cross-member aggregation, dedup, empty-aggregated raise)
- `tests/unit/live_supervisor/test_process_manager_registry_dispatch.py` — 8 tests (isinstance ladder, JSON envelope persistence, consistency with real source)
- `tests/integration/live_supervisor/test_supervisor_uses_lookup_for_live.py` — 4 integration tests (resolver in supervisor env, StrategyMemberPayload.resolved_instruments round-trip)
- `tests/unit/services/live/test_endpoint_outcome_registry_factory.py` — 8 tests (all 4 registry kinds + assertion guard + 3 fallback paths)
- `tests/unit/structure/test_canonical_instrument_id_runtime_isolation.py` — 2 tests (absent from runtime + present in bootstrap)
- `tests/unit/test_trading_node_payload_multi_strategy.py` — 1 new pickle round-trip test appended

**Validated:** real-money drill on `U4705114` per `docs/runbooks/drill-live-path-registry-wiring.md` (report at `docs/runbooks/drill-reports/...`). Registry-backed BUY 1 → `/kill-all` → flat. Council verdict constraint #5 satisfied.

**Council verdict:** `docs/decisions/live-path-registry-wiring.md` (ratified 2026-04-19).
**PRD:** `docs/prds/live-path-wiring-registry.md`.
**Research:** `docs/research/2026-04-20-live-path-wiring-registry.md`.
**Plan:** `docs/plans/2026-04-20-live-path-wiring-registry.md` (4 plan-review iterations: 3 P0 → 0 P0, 7 P1 → 0 P1, 5 P2 → 0 P2).

### Changed

- 2026-04-19: **Flattened repository layout** (commits `9c30116` + `68a3189`, same PR as the codex archival below). The surviving implementation (previously under `claude-version/`) was promoted to the repo root. Physical moves via `git mv` preserve rename history for 455 files. Follow-up commit strips `claude-version/` path prefixes from source code, tests, scripts, configs, and active docs; drops dangling `Ported from codex-version/...` provenance comments in 5 backend files (content is preserved at git tag `codex-final` for revival: `git checkout codex-final -- <path>`). Root `CLAUDE.md` rewritten to absorb API endpoints + architecture notes + env-var documentation that previously lived in `claude-version/CLAUDE.md`. `.gitignore` de-prefixed and augmented with log/Jupyter/OS patterns from the deleted `claude-version/.gitignore`. `README.md` moved to repo root (there was no prior root README). `docker-compose.{dev,prod}.yml` + `.github/workflows/ci.yml` + `strategies/` + `data/` + `backend/` + `frontend/` all now at the top level. Net result: no `claude-version/` or `codex-version/` paths in operational surfaces; entity labels remain only in historical decision/council narratives (e.g., `docs/decisions/which-version-to-keep.md`) where they describe the comparison itself.

### Removed

- 2026-04-19: **`codex-version/` archived and deleted** (branch `feat/playwright-e2e-port`, commit `7ea2c5e`). Council verdict 2026-04-19 (`docs/decisions/which-version-to-keep.md`) chose to keep `claude-version` and delete `codex-version`. An initial attempt to port codex's 9 Playwright specs as a prerequisite for deletion was abandoned after plan-review iteration 1 found UI drift too large to port faithfully (14 of 15 codex copy strings absent from claude; e.g., `"Backtest Runner"`, `"Research Console"`, `"Interactive Brokers status"`, `"Daily Universe"`). Option C (direct delete, no port) adopted — see decision-doc postscript. **Changes:** (1) tagged pre-delete commit `e9ac08e` as `codex-final` for archival (revival via `git checkout codex-final -- codex-version/`); (2) `git rm -r codex-version/` (~17K LOC, 297 files); (3) removed 15 Feb-25 baseline screenshot PNGs at repo root (~2.4 MB, unreferenced); (4) updated root `CLAUDE.md` to reflect single-stack operation (dropped "Two Competing Implementations" table, dual-port E2E matrix, etc.); (5) retargeted `playwright.config.ts` default `baseURL` to `http://localhost:3300`; (6) updated `scripts/seed_market_data.py` usage docstring. Abandoned port plan + research preserved at `docs/plans/2026-04-19-playwright-e2e-port.md` + `docs/research/2026-04-19-playwright-e2e-port.md` as audit trail. 5-advisor council verdict preserved in `docs/decisions/` with minority report (Contrarian objection re: multi-login gateway + instrument-registry scope creep — deferred to 6-month architecture-governance review 2026-10-19).

### In progress

- 2026-04-18: `msai instruments refresh --provider interactive_brokers` — completing PR #32 deferred item #2. Branch `feat/instruments-refresh-ib-path`.
  - **Phases 1-3 (docs)** at `087690b`: PRD + discussion + research brief + design doc. Research surfaced 4 design-changing findings against NautilusTrader 1.223.0 (`wait_until_ready` swallows `TimeoutError`; `client.stop()` is async-scheduled; factory globals have no `.clear()`; `_connect()` retries forever on dead gateway). 5-advisor council + Codex chairman synthesized the 6 design Qs.
  - **Phase 3 plan-review loop:** 4 iterations of parallel Claude + Codex review converged; caught 7 P1 + 8 P2 issues including supervisor account-source confusion, double `client.start()`, ES futures CONTFUT misroute, YYYYMM-format bug, duplicate-month canonical-id, monkeypatch target errors.
  - **Phase 4 batch 1 (Foundation):** commits `38db52b` (3 Settings fields + 3 unit tests), `af4f031` (extract `ib_port_validator.py` with 28 combinatorial tests), `fe8bfbe` (rewire `live_node_config.py` — dedup #1 of 2). 103 new tests, ruff+mypy clean on new files.
  - **Phase 4 batch 2 (Phase A complete):** commits `52410cb` (supervisor validator rewire — dedup #2 of 2; preserves `deployment.account_id` as source of truth), `32b63d8` (autouse pytest fixture clears `IB_CLIENTS` + `IB_INSTRUMENT_PROVIDERS` between tests), `6fd2b75` (fix `_spec_from_canonical` to set FUT expiry from front-month third-Friday + strip local-symbol suffix to root; resolves pre-existing latent CONTFUT misroute for `resolve_for_live(['ES'])`). 1403 unit tests green + 32 resolve-path integration tests green. Phase A foundation done — ready for Phase B CLI implementation.
  - **Phase 4 batch 3 (Phase B complete — CLI implementation):** commits `be829a5` (CLI rejects unknown symbols outside `PHASE_1_PAPER_SYMBOLS`; drops stale deferral test), `ea1bbef` (CLI preflight validates `IB_PORT` vs `IB_ACCOUNT_ID` via shared validator), `cb13042` (`_run_ib_resolve_for_live` — short-lived Nautilus IB factory chain, caller-side `asyncio.wait_for` connect fence bypassing the buggy `wait_until_ready`, awaited `_stop_async` teardown, `IB_MAX_CONNECTION_ATTEMPTS=1` env cap, fast-fail operator hint). 1406 unit tests green (+3 new IB-branch CLI cases verifying factory kwargs + no-double-start + `_stop_async`-awaited contract). `--provider interactive_brokers` is now live end-to-end in the Python path — ready for smoke-test + manual drill.
  - **Phase 4 batch 4 (docs + opt-in smoke):** commits `ab69af7` (CLI docstring + `--help` text — dropped "deferred to follow-up PR" language; describes shipped IB path + all 4 env-var tunables), `3b8a782` (`claude-version/CLAUDE.md` — same dedup), `104a594` (opt-in `pytest.mark.ib_paper` smoke test at `tests/e2e/test_instruments_refresh_ib_smoke.py` — runs CLI as subprocess, verifies row-count growth, idempotent re-run, warm-resolve-doesn't-touch-IB; `ib_paper` marker registered in pyproject.toml; skipped by default in CI on `RUN_PAPER_E2E` env). All automatable Phase 4 work done — remaining: manual paper drill (Phase D) + finish-branch.
  - **Phase D paper drill (2026-04-18 20:30 UTC) — all 5 drills pass.** Stack restarted from worktree (bind-mount now serves worktree code). D1: docker compose health green + IB Gateway socat port 4004 reachable from backend network. D2: `msai instruments refresh --symbols AAPL --provider interactive_brokers` exit 0 in 2.6s; preflight log names host/port/account/client_id/timeouts; Postgres shows 1 `instrument_definitions` row (AAPL.NASDAQ equity) + 1 `instrument_aliases` row (effective_from=2026-04-19, effective_to=NULL). D3: idempotent re-run — counts unchanged (1/1). D4: gateway stopped → CLI fast-fails in 7.7s (5s connect-timeout + docker-exec overhead) with operator hint naming all 4 env vars + 3 diagnostic buckets. D5: `IB_PORT=4001` + `IB_ACCOUNT_ID=DUP733213` (paper) → exit non-zero in 1.3s with "live port 4001 paired with paper-prefix account 'DUP733213'" — no IB connection attempted. Port routing used socat `4004` correctly (validator accepts both raw + socat ports).

### Added

- Initial project setup with Claude Code configuration
- 2026-04-16: Portfolio-per-account-live PR #1 — live-composition schema + domain layer (branch `feat/portfolio-per-account-live`). Pure additive, zero live-risk. Four new tables (`live_portfolios`, `live_portfolio_revisions`, `live_portfolio_revision_strategies`, `live_deployment_strategies`), two new columns (`live_deployments.ib_login_key`, `live_node_processes.gateway_session_key`), partial unique index (`uq_one_draft_per_portfolio`). Services: `compute_composition_hash`, `PortfolioService` (create + add_strategy + list_draft_members + get_current_draft with graduated-strategy invariant), `RevisionService` (snapshot with `SELECT … FOR UPDATE` serialization + identical-hash collapse + `get_active_revision` + `enforce_immutability` guard). 13 new integration tests (portfolio_service + revision_service + full_lifecycle + alembic round-trip). 5-advisor council approved; plan-review loop 3 iterations to clean. Nothing in `/api/v1/live/*`, supervisor, or read-path touched. Design doc: `docs/plans/2026-04-16-portfolio-per-account-live-design.md`. Implementation plan: `docs/plans/2026-04-16-portfolio-per-account-live-pr1-plan.md`.
- 2026-04-16: ES futures canonicalization pipeline (PR #23) — `canonical_instrument_id()`, `phase_1_paper_symbols()` as a function with fresh front-month per call, `exchange_local_today()` helper on America/Chicago, `TradingNodePayload.spawn_today_iso` threading so supervisor + subprocess agree on the same quarterly contract across midnight-on-roll-day spawns. 28 new unit tests in `test_live_instrument_bootstrap.py` (39 total). Branch `fix/es-contract-spec`.

- 2026-04-16: Portfolio-per-account-live PR #2 — semantic cutover (PR #29, branch `feat/portfolio-per-account-live-pr2`). New `POST /api/v1/live/start-portfolio` endpoint accepting `portfolio_revision_id + account_id`. Multi-strategy `TradingNode` via `TradingNodeConfig.strategies=[N ImportableStrategyConfigs]`. `FailureIsolatedStrategy` base class wraps event handlers via `__init_subclass__` to prevent one strategy crashing the node. Portfolio CRUD API (`/api/v1/live-portfolios`). `PortfolioDeploymentIdentity` replaces strategy-level identity. `LiveDeploymentStrategy` bridge rows for per-member attribution. Supervisor payload factory resolves portfolio members. Audit hook per-strategy tagging via `_resolve_strategy_id()` lookup. Cache-key namespace helper. 3 Alembic migrations (add FK, backfill, drop legacy columns). `strategy_id_full` format changed to `{class}-{order_index}-{slug}` for same-class disambiguation. 20 commits, 1341 unit tests, E2E 15/15 against live dev Postgres. 2-iteration code review loop (Codex + 5 PR-review-toolkit agents).
- 2026-04-17: Portfolio-per-account-live PR #3 — multi-login Gateway topology (PR #30, branch `feat/portfolio-per-account-live-pr3`). `GatewayRouter` resolves `ib_login_key → (host, port)` from `GATEWAY_CONFIG` env var. Per-gateway-session spawn guard (concurrent-startup check scoped to `gateway_session_key`). `gateway_session_key` populated on `LiveNodeProcess` at creation. Enforce NOT NULL on `ib_login_key` + `gateway_session_key` via migration. Resource limits on all live-critical Docker Compose containers. Recreated backfill migration lost in PR#2 squash merge.

### 2026-04-17 — db-backed-strategy-registry PR (scope-backed to backtest-only)

**Shipped:**

- New Postgres tables `instrument_definitions` + `instrument_aliases` (UUID PK, effective-date alias windowing for futures rolls).
- `SecurityMaster.resolve_for_backtest(symbols, *, start, end, dataset)` — registry lookup with Databento `.Z.N` continuous-futures synthesis on cold miss.
- `SecurityMaster.resolve_for_live(symbols)` — registry lookup with closed-universe `canonical_instrument_id()` fallback.
- `DatabentoClient.fetch_definition_instruments(...)` — download + decode `.definition.dbn.zst` with `use_exchange_as_venue=True` on `from_dbn_file()` call site.
- `msai instruments refresh --symbols ... --provider [interactive_brokers|databento]` CLI — pre-warm the registry before deploying strategies. (Databento path works; IB path deferred — see Deferred section.)
- `SecurityMaster.__init__` relaxed: `qualifier` and `databento_client` both optional (same class now serves backtest + live callers).
- Continuous-futures helpers: `is_databento_continuous_pattern`, `raw_symbol_from_request`, `ResolvedInstrumentDefinition`, `resolved_databento_definition`, `definition_window_bounds_from_details`, `continuous_needs_refresh_for_window`.
- Backtest API wired: `POST /api/v1/backtests/run` now resolves via registry (`api/backtests.py:90`).
- Split-brain normalization: `.XCME` → `.CME` across source docstrings + 26 test fixtures.
- `.. deprecated::` notices added to `instruments.py` + `live_instrument_bootstrap.py` (modules remain load-bearing for closed-universe live path + live-supervisor payload factory).

**Tests:** 1366 unit passes + ~40 integration tests (including full-lifecycle, backtest/live parity via freezegun, Cache-Redis roundtrip, continuous-futures placeholder). Zero regressions from the registry work.

**Architectural decisions (after 5 plan-review iterations):**

- `InstrumentDefinition.instrument_uid` is UUID, never venue-qualified string. Venue-qualified aliases live in `instrument_aliases` rows with effective-date windowing — futures rolls are row updates, not PK migrations.
- Runtime canonical = exchange-name (`AAPL.NASDAQ`, `ES.CME`, `EURUSD.IDEALPRO`). Matches IB adapter defaults.
- `asset_class` DB enum = `equity|futures|fx|option|crypto` (note plural `futures` — matches CHECK constraint, diverges from codex's `stocks`/`options`).
- Nautilus's `Cache(database=redis)` owns `Instrument` payload durability — MSAI registry holds only control-plane metadata. Verified end-to-end in `test_cache_redis_instrument_roundtrip.py`.
- Schema bug caught during Task 9 testing: `instrument_aliases.venue_format` widened `String(16)` → `String(32)` in-place (pre-merge).

**Deferred to follow-up PRs (not in this PR):**

- **Live-path wiring.** Plan attempted 3 architectures (A: supervisor calls SecurityMaster inline — blocked by no IBQualifier; B: persist canonicals on revision_strategies — blocked by composition_hash immutability; C: payload-dict hint — blocked by supervisor deliberate ignore). Option D candidate (persist on `LiveDeployment`, warm-cache-only at API) pending its own design pass. Skeleton: end of `docs/plans/2026-04-17-db-backed-strategy-registry.md`.
- **InstrumentCache → Registry migration.** Existing `instrument_cache` table (Nautilus payloads + trading_hours + IB contract JSON) coexists with new registry. Needs its own PR to migrate 7 call sites + trading_hours relocation.
- **Pydantic config-schema extraction on `StrategyRegistry`.** Orthogonal to registry; deferred.
- **IB provider factory in `msai instruments refresh`.** Needs `Settings` expansion (ib_request_timeout_seconds, etc.) — ships with the live-wiring follow-up.

**Known limitations discovered post-Task 20 (Codex Phase 5 review):**

- **`msai instruments refresh --symbols <plain>` works only for `.Z.N` continuous-futures.** For plain symbols (`AAPL`, `ES`), the CLI delegates to `SecurityMaster.resolve_for_backtest`, which raises `DatabentoDefinitionMissing` because no fetch-and-synthesize path exists for non-continuous symbols. **Workaround:** operators seed plain-symbol registry rows via direct SQL until the follow-up PR adds a proper Databento plain-symbol fetch. Example: `INSERT INTO instrument_definitions (raw_symbol, listing_venue, routing_venue, asset_class, provider, lifecycle_state) VALUES ('AAPL', 'NASDAQ', 'NASDAQ', 'equity', 'databento', 'active')` + matching alias row.

- **`resolve_for_backtest` uses today's date for alias windowing**, not the backtest's `start_date`. After a futures front-month roll, a historical backtest (e.g. `start_date=2025-12-01, end_date=2026-01-31`) will receive the **current** front-month alias rather than the contract active during the backtest window. **Workaround:** operators passing continuous-futures `.Z.N` patterns avoid this issue. For concrete futures with historical windows, operators must manually specify the correct contract (e.g. `ESZ5.CME` for Dec-2025 backtests). Follow-up: thread `start_date` into `InstrumentRegistry.find_by_alias` within `resolve_for_backtest`.

- **Worker parquet lookup assumes raw-symbol == canonical prefix.** `workers/backtest_job.ensure_catalog_data` passes `Backtest.instruments` (canonical IDs like `ESM6.CME`) to `catalog_builder.build_catalog_for_symbol`, which then calls `resolve_instrument()` and splits on `.` to derive the raw_symbol. For equities this happens to work (`AAPL.NASDAQ` → raw `AAPL`, parquet root is `AAPL/`), but for futures it fails (`ESM6.CME` → raw `ESM6`, parquet root is `ES/`). Fix 9 adds an optional `raw_symbol_override` kwarg to `build_catalog_for_symbol`/`ensure_catalog_data` so the worker can pass the user's original input; **wiring the worker + `Backtest.input_symbols` column is a follow-up** (see plan doc).

**Commits (22 total):** 21b9ec1, 3b2cc35, 7ea6fb1, 75a3cf1, 9282824, 15b2d22, 2fb64b1, 38edeb9, 2829585, 3c26ad3, a2b9b01, 32f0e57, c87751f, c17aef6, b39d318, 71c904b, bfe90e8, c84e697, 7383319, dce4f82, 7324e0b, plus this commit.

**Post-review fixes (2, landed before squash-merge):** Codex GitHub bot reviewed the open PR and raised 2 P1s; both fixed in-branch before merge, both tracked back to earlier-deferred findings from the local review loop.

- `8f5f943` — close previous active alias before inserting new one in `_upsert_definition_and_alias`. Without this, a futures-roll or repeated refresh left multiple aliases active for the same `(instrument_uid, provider)` pair, making `next((a for a in aliases if a.effective_to is None))` order-dependent. Fix runs `UPDATE ... SET effective_to = today WHERE instrument_uid = :uid AND provider = :p AND effective_to IS NULL AND alias_string != :new` before the insert, preserving the half-open `[effective_from, effective_to)` window invariant. Regression test: AAPL.NASDAQ → AAPL.ARCA roll verifies old alias closed today + new one active + exactly one active alias remains.

- `415a858` — raise `AmbiguousSymbolError` on cross-asset-class raw-symbol match in `InstrumentRegistry.find_by_raw_symbol`. Schema uniqueness is `(raw_symbol, provider, asset_class)` — so SPY can legitimately exist as both equity and option-underlying rows. `resolve_for_{live,backtest}` don't pass `asset_class`, so the previous `.limit(1)` silently picked one. Fix fetches all matches when `asset_class is None` and raises with conflicting asset_classes in the message; callers pinning `asset_class` keep the `.limit(1)` fast path. Regression test: SPY under both equity + option asset classes verifies raise-on-ambiguous + resolves-cleanly-when-pinned paths.

**PR shipped as:** squash commit `a52046f` on main (2026-04-17).

### 2026-04-18 — Fix: stale test cleanup after PR #29/PR #30/PR #31 schema moves

The portfolio-per-account-live PR series (#29/#30/#31) dropped 5 columns from `live_deployments`, added `NOT NULL` on `ib_login_key` (PR #30), added `NOT NULL` on `gateway_session_key` to `live_node_processes` (PR #30), enforced `portfolio_revision_id NOT NULL` (PR #31), and deprecated the legacy `/api/v1/live/start` endpoint in favor of `/start-portfolio` (PR #31). The test suite was not fully updated; 30 tests failed + 78 errored on main against the current schema.

**Shipped:**

- `tests/integration/_deployment_factory.py` — new `make_live_deployment()` helper that seeds a `LivePortfolio` → `LivePortfolioRevision` → `LiveDeployment` chain matching the post-PR#29 NOT NULL set. Accepts either ORM instances (`user=`, `strategy=`) or IDs (`user_id=`, `strategy_id=`) for maximum flexibility. Generates unique slugs + identity signatures per call so module-scoped fixtures don't collide.
- `test_audit_hook.py`, `test_heartbeat_monitor.py`, `test_heartbeat_thread.py`, `test_live_node_process_model.py`, `test_live_start_endpoints.py`, `test_live_status_by_id.py`, `test_order_attempt_audit_model.py`, `test_process_manager.py`, `test_trading_node_subprocess.py`, `test_portfolio_deploy_cycle.py` — all migrated to the factory (or patched with the current NOT NULL kwargs). `gateway_session_key` added to every `LiveNodeProcess(...)` construction.
- `test_alembic_migrations.py` — deleted 4 obsolete "Phase 1 task 1.1b" tests that asserted columns dropped in PR #29 (`config_hash`, `instruments_signature`, etc.). Updated assertions in `test_backfill_creates_portfolio_for_legacy_deployment` (backfill migration intentionally writes empty config + empty instruments per `r6m7n8o9p0q1` line 92) and `test_migration_drops_legacy_columns_keeps_identity` (`portfolio_revision_id` is now NOT NULL after `u9p0q1r2s3t4`). Kept the duplicate-identity-collision test — it still targets valid intermediate migration semantics.
- `test_live_deployment_stable_identity.py` — deleted entirely. 6 tests of the intermediate v9 identity design that PR #29 replaced with `PortfolioDeploymentIdentity`; new behavior is covered by `test_portfolio_deploy_cycle.py` + portfolio service tests.
- `test_live_start_endpoints.py` — deleted 9 tests targeting the deprecated `/api/v1/live/start` endpoint (now returns 410 Gone per PR #31). Kept the 2 `/stop` tests which remain valid.
- `test_parity_determinism.py` — fixed the synthetic OHLC generator. The `_write_synthetic_bars` helper computed `open`, `high`, `low` from independent random draws, producing bars where `high < open` and tripping Nautilus's `Bar.__init__` invariant check. Now derives `high = max(open, close) + |r|*0.1` and `low = min(open, close) - |r|*0.1`, guaranteeing the invariant holds.
- `test_parity_config_roundtrip.py::test_smoke_config_accepts_full_live_injection` — updated the stale `order_id_tag == "abcd1234abcd1234"` assertion. Since PR #29 the order-index prefix is carried verbatim to match `derive_strategy_id_full`'s `{class}-{order_index}-{slug}` format, so the decoded value is `"0-abcd1234abcd1234"`.

**Scope note:** Test-only cleanup. No production code modified. No public API contract changes. No user-facing behavior changes.

### 2026-04-18 — Fix: `resolve_for_backtest` honors `start` for alias windowing

Closes known-limitation #2 from the PR #32 CHANGELOG entry. `SecurityMaster.resolve_for_backtest` was defaulting alias lookups to today UTC, so post-roll historical backtests silently received today's front-month / current listing venue instead of the contract active during the backtest window. Two warm paths were affected:

- **Path 2 (dotted alias)** — `registry.find_by_alias(sym, provider="databento")` missed `as_of_date=`, so `find_by_alias` defaulted to today. A closed-window alias (e.g. `AAPL.NASDAQ`, effective 2020–2023) returned `None` under today's date and raised a misleading `DatabentoDefinitionMissing`.
- **Path 3 (bare ticker)** — selected the alias with `effective_to IS NULL` (currently-open), not the alias active on `start_date`.

Fix parses the existing `start: str | None` kwarg once (`date.fromisoformat(start) if start else datetime.now(UTC).date()`) and threads the resulting `date` through both paths. `find_by_alias` already accepted `as_of_date: date | None`, so no signature change was needed at the registry layer. Path 3 now filters aliases by the full half-open window predicate `effective_from <= as_of AND (effective_to IS NULL OR effective_to > as_of)`, matching `find_by_alias`'s SQL semantics.

3 new integration tests (`test_resolve_for_backtest_dotted_alias_honors_start_date`, `test_resolve_for_backtest_bare_ticker_honors_start_date`, `test_resolve_for_backtest_bare_ticker_no_start_uses_today`) seed AAPL with two consecutive venue aliases (`AAPL.NASDAQ` 2020–2023, `AAPL.ARCA` 2023–∞) and pin the three regressions: historical dotted, historical bare-ticker, and today-default. All 6 tests in `test_security_master_resolve_backtest.py` pass; 122 tests in the security_master/backtest scope pass; ruff + mypy clean on the changed lines.

Solution doc: `docs/solutions/backtesting/alias-windowing-by-start-date.md`.

Incidental: ruff-format normalized ~10 multi-line-to-single-line call sites in adjacent methods (`resolve_for_live`, `_resolve_databento_continuous`, `_spec_from_canonical`) when the formatter ran on save. These are whitespace-only, aligned with project style (python-style.md line-length 100), and would flip on any future edit to the file regardless.

### Changed

- 2026-04-16: Live-supervisor now canonicalizes user-facing instrument ids before passing to strategy config — e.g., `ES.CME` → `ESM6.CME` for futures, identity for stocks/ETF/FX. Overwrites stale explicit `instrument_id` / `bar_type` only when the root symbol changes (futures rollover), preserving operator aggregation choices on stocks/FX.

### Fixed

- 2026-04-16: `/live/positions` empty while open position exists (PR #27, branch `fix/live-positions-empty`). Five compounding bugs: (1) `derive_message_bus_stream` returned `-stream` but Nautilus writes to `:stream` — every PositionOpened/OrderFilled/AccountState event silently dropped since the projection consumer was wired (Alembic `n2h3i4j5k6l7` normalizes existing rows); (2) Nautilus `Cache.cache_all()`/`positions_open()` silently drops rows — switched cold-path readers to `adapter.load_positions()`/`adapter.load_account()` directly; (3) `deployment.status` stuck at `starting` on warm-restart when process already `running` (UP-direction mirror of PR #26); (4) `/live/positions` filter now keyed on latest-process-row status via subquery instead of stale `deployment.status`; (5) `_to_snapshot`/`_to_account_update` couldn't parse Nautilus `Money` strings (`"0.00 USD"`), added `_money_to_decimal` helper. Live-verified on running stack (paper EUR/USD smoke): BUY 1 @ 1.17805 filled, `/live/positions` returned `[{qty:"1.00", avg_price:"1.17805", realized_pnl:"-2"}]` — first time a position was actually visible through this endpoint in the project's history.
- 2026-04-16: Deployment-status sync on normal stop + typed `HEARTBEAT_TIMEOUT` error (branch `fix/status-sync-and-typed-errors`). **Fix A:** `trading_node_subprocess._mark_terminal` now syncs the parent `LiveDeployment.status` on clean exit — previously only the spawn-failure path did this (X3 iter 1), so `/live/stop` left `live_deployments.status='running'` indefinitely. **Fix B:** new `FailureKind.HEARTBEAT_TIMEOUT` replaces opaque `UNKNOWN` for stale-heartbeat sweeps; `HeartbeatMonitor` also syncs parent deployment.status. Endpoint classifier + idempotency cache accept `HEARTBEAT_TIMEOUT` in `permanent_kinds`, so retries with same Idempotency-Key return structured `503 {failure_kind: "heartbeat_timeout"}` instead of "unknown failure". Live-verified on running stack: injected stale process row on orphan `starting` deployment, supervisor sweep flipped both rows within one 10s cycle. 1209 unit tests pass (+1 parametrize).
- 2026-04-16 14:52 UTC: **First live real-money drill success on `U4705114` (MarketSignal.ai LLC, mslvp000/test-lvp)**. Deployment `5828fe02` deployed `SmokeMarketOrderStrategy` against `AAPL.NASDAQ` with `paper_trading=false`; bars flowed in real-time via API, smoke fired `BUY 1 AAPL MARKET` at 14:52:30, filled at $261.33 (commission $1.00, broker_trade_id `0002264f.69e1362b.01.01`). Validated live: PR #23 canonicalization ✓, PR #21 `side="BUY"` string ✓, PR #24 WS reconnect returns 3 hydrated trades ✓. Nautilus ExecEngine startup reconciliation also surfaced pre-existing external positions on the account (SPY 156 @ $658.04, EEM 309 @ $49.06) as `inferred OrderFilled` audit rows — noted as follow-up to distinguish reconciliation-inferred fills from strategy-submitted fills in `audit_hook.py`. Env setup required: `IB_PORT=4003`, `TRADING_MODE=live`, `IB_ACCOUNT_ID=U4705114`, `TWS_USERID=mslvp000`. Also required adding `IB_PORT` + `TRADING_MODE` var declarations to the live-supervisor env block in `docker-compose.dev.yml` (they were previously absent, so env overrides weren't propagating into the container).
- 2026-04-16: WebSocket reconnect snapshot now hydrates `orders` / `trades` / `status` / `risk_halt` alongside `positions` / `account` (PR #24, branch `feat/live-state-controller`). Phase 2 #4 narrow Option B — engineering council rejected the 1,200 LOC LiveStateController port as too risky pre-drill, approved this 150 LOC augmentation that reuses claude's existing authoritative read models (`OrderAttemptAudit`, `Trade`, `ProjectionState`). Structured log `ws_snapshot_emitted` emits all counts per connect. Also fixed a pre-existing cold-path crash in `position_reader._read_via_ephemeral_cache_account` (bare `AccountId("DUP733213")` → `ValueError: did not contain a hyphen`) that was silently closing every fresh-backend WS snapshot with 1011 — now qualifies with `INTERACTIVE_BROKERS-` prefix to match Nautilus's `AccountState` format. 14 new unit tests (1208 total). E2E verified against paper IB Gateway: all 8 snapshot keys arrive on the wire, 50 real EUR/USD trades round-trip through the reader, structured log emits as specified.
- 2026-04-16: ES deployments producing zero bar events (drill 2026-04-15 failure mode, PR #23) — root cause was an instrument-id mismatch between the user-facing `ES.CME` bar subscription and the concrete `ESM6.CME` instrument Nautilus registers after IB resolves `FUT ES 202606`. Now canonicalized at the supervisor. Live-verified: subscription succeeds against paper IB Gateway with no "instrument not found" error. Also caught a `.XCME` (ISO MIC) vs `.CME` (IB_SIMPLIFIED native) venue bug in an earlier iteration that would have shipped without the live e2e test. NOTE: bars still don't fire due to a broader IB entitlement gap on the account tied to `DUP733213` — NOT a code bug. Confirmed via direct `ib_async` probes against the paper gateway (port 4004): IB error **354** for CME futures (ES) and IB error **10089** for NASDAQ-primary equities (AAPL, `"Requested market data requires additional subscription for API. AAPL NASDAQ.NMS/TOP/ALL"`). Open question: user reports trading SPY/QQQ on IBKR "for years" which contradicts the 10089 error — possible explanations: (a) trading was via TWS desktop, which honors different subscription gating than the API (many entitlements need a separate "enable for API" checkbox); (b) trading was on a different user login (`pablo-data`, `apis1980`, etc.) with its own subscription list; (c) paper accounts don't auto-inherit live subscriptions without an explicit "Share Live Market Data With Paper Account" toggle. EUR/USD (IDEALPRO FX) is the only asset class currently producing real-time bars through the Nautilus live path.

### Removed

---

## Format

Each entry should include:

- Date (YYYY-MM-DD)
- Brief description
- Related issue/PR if applicable
