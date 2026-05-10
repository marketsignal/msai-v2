# Decision: Just-In-Time SSH from GitHub Actions to the Prod VM

**Date:** 2026-05-10
**Status:** **FINAL — council-ratified** (5/5 advisors APPROVE/CONDITIONAL on Default; 0 OBJECT; Contrarian found 2 P0s the others missed)
**Decided by:** Engineering Council (`/council`) during Plan-Review Iter 1 of `feat/deploy-pipeline-ssh-deploy-and-first-deploy`. 5 advisors (3 Claude + 2 Codex-personas-via-Claude under engine-diversity caveat per `feedback_codex_cli_locked_out_council_fallback.md`).
**Builds on:** [`deployment-pipeline-architecture.md`](deployment-pipeline-architecture.md) (locked) + [`deployment-pipeline-slicing.md`](deployment-pipeline-slicing.md) (Slice 3 of 4).

---

## TL;DR

Slice 1's NSG only allows SSH inbound from `operatorIp/32`. The Slice 3 deploy workflow on a GH-hosted runner cannot SSH to the VM with that NSG. Self-hosted runner is REJECTED architecturally (lateral-movement risk). Azure Bastion is $$ + complexity. Pull-based deploy is out of scope for Phase 1.

**Decision:** GH-OIDC MI gets `Network Contributor` scoped to the NSG only. `deploy.yml` opens a transient `gha-transient-${run_id}-${run_attempt}` allow rule for the runner's `/32` public IP at priority 200 (operator's `priority 100` rule always wins), runs the deploy, then a separate `cleanup` job (`needs:[deploy] if:always()`) deletes the rule. A scheduled `reap-orphan-nsg-rules.yml` reaps any rule older than 30 min that escaped cleanup. Bicep NSG `securityRules:` are refactored from inline-property to child-resource so the transient rule survives concurrent Bicep reapplies.

## Why not the alternatives?

| Option                                                    | Why rejected                                                                                                                                                                                                                                                                                 |
| --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Self-hosted runner                                        | Architecture verdict §3: lateral-movement risk. Locked.                                                                                                                                                                                                                                      |
| Azure Bastion                                             | $$ + complexity. Phase 1 single-VM personal stack doesn't justify it.                                                                                                                                                                                                                        |
| Pull-based deploy (VM polls a queue)                      | Significant scope creep into Slice 5+. Defers Slice 3 indefinitely.                                                                                                                                                                                                                          |
| Static rules covering all GH Actions egress (~3000 CIDRs) | 5/5 council reject. Always-open SSH to all of GitHub egress = wider blast radius than 5 min × runner-IP. `api.github.com/meta` changes weekly; static rules drift; AND GH 2025-introduced "larger runners" use NAT-gateway egress IPs not in `/meta`, invalidating the list as a closed set. |
| ACI-in-VNet jump (Contrarian's 3rd option)                | Genuinely strong technically (cost ~$0.003/deploy, removes 4/5 failure modes). DEFERRED as escape hatch — adopt if Slice 4 ops surfaces orphan-rule patterns or if a real-money posture later justifies the operational uplift.                                                              |

## Council-mandated mitigations

All 5 advisors plus the Plan-Review iter-1 self-review agreed on these:

1. **Bicep NSG `securityRules:` → child resources** (`infra/main.bicep` T13). Inline property = ARM full-reconcile on apply = silent deletion of transient rules mid-deploy. Critical fix; without it the entire pattern is unsafe.
2. **`Network Contributor` scoped to `nsg.id` only**, NOT subscription / not RG (T14). Built-in role; tight blast radius. Custom role with only `securityRules/{read,write,delete}` is a Phase 2 hardening (see "Deferred" below).
3. **Rule name format `gha-transient-${run_id}-${run_attempt}`** — uniqueness across re-runs avoids `priority 200` 409 conflicts; greppable via prefix match for orphan diagnosis.
4. **`concurrency: group: deploy-msai, cancel-in-progress: false`** on `deploy.yml`. Queue, never cancel — cancellation mid-deploy = orphaned rule + stale Caddy state.
5. **Cleanup as a separate job** with `needs:[deploy] if:always()`. Survives runner-VM kill on the deploy job (the `if: always()` step on the same job does NOT survive runner host preemption).
6. **Reaper workflow** (`reap-orphan-nsg-rules.yml`) on a 15-min cron. Belt-and-braces for cleanup-job failures.
7. **Contrarian's <30 min spike**: in the rehearsal RG, simulate concurrent rule creates + a Bicep reapply mid-flight. Confirms the child-resource refactor (mitigation #1) actually works as theorized. T13 acceptance gate.

## What we accepted (risk register)

| Risk                                                                            | Likelihood | Impact  | Mitigation                                                                                                                                                                                            |
| ------------------------------------------------------------------------------- | ---------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cleanup job fails AND reaper fails, leaving rule open >30 min                   | L          | M       | Reaper wakes every 15 min; even one missed run = ~30 min total exposure window for a runner-IP/32 (now belonging to a different GH tenant). Tightens to the runner-IP, not all of GitHub.             |
| Compromised job mints `az network nsg rule create` for `0.0.0.0/0:22`           | L          | H       | Network Contributor on NSG = capability exists. **Phase 2 hardening:** Azure Policy `deny` on `sourceAddressPrefix != <runner-cidr>` + custom RBAC role with only securityRules CRUD on this NSG.     |
| Two concurrent deploys race on `priority 200` rule                              | L          | L       | Concurrency group serializes. Re-run uses `${run_id}-${run_attempt}` so re-run-of-run_X attempts don't collide with run_X attempt 1's leftover rule (cleanup job runs once per attempt regardless).   |
| Bicep reapply (Slice 4) deletes the transient rule mid-SSH                      | M (was)    | H (was) | **Mitigated** by mandatory mitigation #1 (child-resource refactor). T13 spike proves it.                                                                                                              |
| Runner public IP recycled to another tenant's runner shortly after rule cleanup | L          | M       | The window of concern is the gap between rule cleanup and Azure GH-runner-pool IP recycling (~hours per Contrarian). The rule's `/32` minimizes scope; SSH still requires the VM private key (gated). |

## Deferred (Phase 2 hardening — open ticket when triggered)

- **Custom RBAC role** with only `Microsoft.Network/networkSecurityGroups/securityRules/{read,write,delete}` actions, replacing built-in Network Contributor. Trigger: when real-money posture is reached (Phase 2 2-VM split).
- **Azure Policy `deny`** on any NSG rule mutation where `sourceAddressPrefix != <approved-CIDR-set>` and `destinationPortRange != 22`. Trigger: same as above.
- **Azure Monitor alert** on orphan rule age > 30 min (i.e. reaper failed too). Slice 4 ops scope.
- **ACI-in-VNet jump** as the SSH primitive instead of NSG mutation. Trigger: Slice 4 surfaces operational pain (>1 orphan-rule incident / month, or cleanup-job reliability issues).

## Operator runbook — orphan rule diagnosis

If `az network nsg rule list -g $RG --nsg-name $NSG --query "[?starts_with(name, 'gha-transient-')]"` returns rules >30 min old:

1. **Check the reaper:** `gh run list --workflow=reap-orphan-nsg-rules.yml --limit 5` — has it been firing?
2. **Map rule name → workflow run:** rule name format is `gha-transient-${run_id}-${run_attempt}`. `gh run view <run_id>` shows the deploy run. If conclusion is failed/cancelled and cleanup job didn't fire, that's the orphan source.
3. **Manual reap:** `az network nsg rule delete -g $RG --nsg-name $NSG --name <rule-name> --output none`.
4. **If rules accumulate (>1 incident / month):** trigger the deferred Phase 2 hardening or switch to ACI-in-VNet jump.

## References

- Council advisor outputs (this session, in plan-review iter 1):
  - Simplifier: APPROVE Default
  - Pragmatist: APPROVE Default
  - Hawk: CONDITIONAL — janitor cron + unique rule names + alert on orphans + cleanup as separate job
  - Maintainer: CONDITIONAL — ADR + runbook + comment blocks in Bicep + workflow YAML
  - Contrarian: CONDITIONAL — 5 mandatory fixes including the Bicep child-resource P0 the others missed, plus ACI-in-VNet as the strongly-recommended Phase 2 escape hatch
- Implementation: `infra/main.bicep` (T13/T14), `.github/workflows/deploy.yml` (T06d), `.github/workflows/reap-orphan-nsg-rules.yml` (T15), `scripts/deploy-on-vm.sh` (T05a — env file pattern avoiding sudo env-strip)
