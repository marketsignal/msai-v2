# Decision: Deployment-Pipeline Branch Slicing (4-PR series, Approach A)

**Date:** 2026-05-10
**Status:** **FINAL — council-ratified** (3 of 5 advisors APPROVE/CONDITIONAL on A; 1 each on B and C; no OBJECTs)
**Decided by:** Engineering Council (`/council`) — 5 advisors (3 Claude, 2 Codex), Claude-as-chairman with engine-diversity caveat (Codex CLI long-prompt stall)
**Builds on:** [`docs/decisions/deployment-pipeline-architecture.md`](deployment-pipeline-architecture.md) (the locked architectural verdict); precursor PR #50 already merged (`6900210`)
**Next session entry point:** `/new-feature deploy-pipeline-iac-foundation` (Slice 1 of this slicing)

---

## TL;DR

The architectural verdict is RATIFIED. The deployment-pipeline branch ships as **4 incremental PRs**, not one mega-PR and not an MVP-first that contradicts the KV verdict. Hawk's re-ordering applies: observability + backup target provisioning ships with the foundational IaC in Slice 1, not deferred to the tail. Slice 3's first real deploy is gated on a tested backup (Hawk) and a full end-to-end rehearsal (Contrarian).

---

## Slices

| Slice | Branch name                                        | Scope                                                                                                                                                                                                                                                                                                                                      | Acceptance                                                                                                                                                                                                                                                                                  |
| ----- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1** | `feat/deploy-pipeline-iac-foundation`              | Bicep IaC + ACR + Key Vault + Blob backup target + Log Analytics workspace + agent on VM + VM system-assigned managed identity + GitHub OIDC federated credential. No app deploys. Bicep checked-in, hand-provisioned via extended `scripts/deploy-azure.sh`.                                                                              | `az deployment group what-if` validates parity in CI; `az keyvault secret show` works from VM via managed identity (15-min manual smoke); Log Analytics agent reports VM heartbeat.                                                                                                         |
| **2** | `feat/deploy-pipeline-ci-image-publish`            | GH Actions workflow on push-to-main + Azure OIDC federation + `docker build -f backend/Dockerfile .` and `docker build ./frontend` with `NEXT_PUBLIC_*` build-args + push to ACR with `${git-sha}` immutable tags. No deploy step.                                                                                                         | Workflow runs green on a no-op commit; ACR shows `msai-backend:abc1234` and `msai-frontend:abc1234`.                                                                                                                                                                                        |
| **3** | `feat/deploy-pipeline-ssh-deploy-and-first-deploy` | `scripts/render-env-from-kv.sh` (systemd unit, ~30 lines); workflow deploy job: SSH to VM, `docker compose pull && up -d --wait migrate backend …`; deploy success signal (`/health` + `/ready` + `XINFO GROUPS msai:live:commands`); image rollback on failure (last-good SHA tag retained). **First real production deploy lives here.** | **Hawk's gate:** before first `up -d --wait`, `scripts/backup-to-blob.sh` against empty prod Postgres + verify dump in Blob — no backup, no first deploy. **Contrarian's gate:** full VM deploy path rehearsed end-to-end in a smoke run (against a throwaway resource group) before merge. |
| **4** | `feat/deploy-pipeline-ops-backup-observability`    | Nightly cron on VM running `scripts/backup-to-blob.sh` (Postgres + DATA_ROOT azcopy); Log Analytics dashboards + alert rules; active-`live_deployments` hard gate in the deploy workflow (refuse deploy if any active deployment exists; operator must `msai live stop --all` first).                                                      | Nightly backup verified by restoring to throwaway Postgres + `\dt` matching prod schema; alert rules tested by breaking `/health`; hard gate tested by leaving a stale `live_deployments` row and confirming workflow refuses.                                                              |

---

## Consensus Points

- **5/5 reject Approach C** ("MVP-first, defer KV") as written. Simplifier (its proponent) made approval contingent on a written verdict-doc addendum, which would mean reopening the architecture verdict — worse than just shipping the ~30 lines of KV-render shell.
- **5/5 reject "deploy what's there now."** PR #50 cleared the precondition; the question is the next step.
- **3/5 prefer A.** 1/5 (Contrarian) prefers B with strict commit hygiene; 1/5 (Simplifier) prefers C with addendum.
- **4/5 want Slice 1 to include observability + backup-target provisioning** (Hawk explicit; the others implicitly).
- **5/5 want first deploy gated on a tested backup**.

---

## Blocking Objections (must be honored)

1. **[Hawk]** Slice 1 includes Bicep + ACR + KV AND Blob container for backups + Log Analytics workspace + agent on the VM. Infrastructure, not application — belongs with provisioning.
2. **[Hawk]** Slice 3 deploy gate: run `scripts/backup-to-blob.sh` against empty prod Postgres and verify the dump exists in Blob, BEFORE the first `compose up -d --wait`.
3. **[Hawk]** Slice 4 cannot be optional — DR automation + Log Analytics dashboards + alert rules ship as a tracked PR with a date.
4. **[Contrarian]** No merge of Slice 3 until full VM deploy path rehearsed end-to-end. KV + managed identity stays in scope.
5. **[Contrarian]** PRs must avoid reusing the existing Azure ML workspace's ACR/KV/log resources at `msaimls2_rg` (per architecture verdict §Verification).
6. **[Simplifier — parked unless KV is reopened]** If the KV-from-day-one decision is ever revisited, the addendum must define a concrete migration trigger (e.g., "migrate to KV before `IB_ACCOUNT_ID` starts with `U`") rather than vague TODO.
7. **[Maintainer]** Slice 1 must include Key Vault + managed identity as real checked-in IaC intent, not deferred prose.

---

## Minority Report

### The Contrarian (Codex) — VERDICT: CONDITIONAL on B (preserved, partially folded)

> "The fatal flaw in A is horizontal slicing. Bicep, ACR, OIDC, SSH, Key Vault env rendering, migrations, smoke checks, rollback, and DR are not independently valuable. The risk lives in their interaction."

**Why partially folded, not overruled:** Real concern. **Folded into A as Blocking Objection #4** — Slice 3 cannot merge until full VM deploy path is rehearsed end-to-end. Slices 1+2 stand up infrastructure but don't deploy production; Slice 3 exercises everything in one transaction. **Re-trigger condition:** if Slice 3 ships without an end-to-end rehearsal, the Contrarian's position becomes active again.

### The Simplifier (Claude) — VERDICT: CONDITIONAL on C (preserved, not adopted)

> "The KV justification doesn't hold for paper-only. A `.env chmod 600` on the VM, populated once via SSH, never touches the GH runner either."

**Why not adopted:** The Simplifier himself required a written verdict-doc addendum to escape the bypass-precedent concern. With Pablo's Owner-on-sub-scope perm confirmed (the original triggering condition for `.env` fallback), the architectural blocker is gone — KV is unblocked. Doing addendum-and-revise gymnastics to save 30 lines of shell is worse than just shipping. **Re-trigger condition:** if Slice 1's KV-render-env script proves materially harder than ~30 lines (e.g., managed-identity propagation has multi-day Azure-side delay), reopen with a verdict-doc addendum.

---

## Missing Evidence

Resolve before/during Slice 1:

1. **Bicep idempotency on a hand-provisioned VM.** Spike `az deployment group what-if` against the existing empty `msaiv2_rg` on day one.
2. **Linux Monitoring Agent vs Azure Monitor Agent (AMA).** Confirm AMA is current best practice; whether it installs via Bicep VM extension.
3. **GitHub OIDC federated credential subject claim.** Verify GH's actual OIDC token claims for push-to-main triggers.
4. **VM system-assigned managed identity propagation latency.** Document wait + retry pattern.
5. **`docker context` over public internet vs SSH** — verdict chose SSH; revisit if Slice 3 SSH proves painful (escape hatch).
6. **Codex CLI long-prompt stall** (operational, not architectural). Both this and the architecture council saw the chairman silent-stall on long prompts; fall back to Claude-as-chairman or `codex exec review --uncommitted` for long synthesis.

---

## Next Step

**Next session: invoke `/new-feature deploy-pipeline-iac-foundation`.**

**Scope brief (paste verbatim into the `/new-feature` invocation):**

> Provision the foundational Azure infrastructure for the deployment pipeline per the council verdicts at `docs/decisions/deployment-pipeline-architecture.md` (architecture, ratified) and `docs/decisions/deployment-pipeline-slicing.md` (this slicing decision). NO application deploys; this is Slice 1 of 4. Deliverables: `infra/main.bicep` declaring `msaiv2_rg` resources (D4s_v6 VM in eastus2, NSG, Premium SSD data disk, Standard_LRS storage + `msai-backups` Blob container, ACR Basic, Key Vault, Log Analytics workspace, GH OIDC federated credential, VM system-assigned managed identity with KV Secret Get + ACR Pull + Blob Contributor RBAC); extension to `scripts/deploy-azure.sh` to call `az deployment group create -f infra/main.bicep`; new `scripts/render-env-from-kv.sh` (systemd unit, runs at boot, fetches every required secret via managed identity, writes `/run/msai.env` chmod 600). Acceptance: `az deployment group what-if` validates clean; `az keyvault secret show` works from the VM via managed identity (15-min manual smoke); Log Analytics agent reports VM heartbeat. Council-mandated Phase 2 research (research-first agent): Bicep current idioms, GH OIDC subject-claim format, Azure Monitor Agent vs legacy MMA, Linux Monitoring Agent VM extension. Out of scope: GH Actions workflow, image push, deploys, scheduled backups, alert rules.

Estimated Slice 1 effort: 4-6 hours (1-2 sessions).
