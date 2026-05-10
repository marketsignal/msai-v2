# Discussion: Deployment Pipeline Slice 1 — IaC Foundation

**Status:** Complete
**Date:** 2026-05-09

---

## Context

This is **Slice 1 of 4** in the deployment-pipeline series. The architectural decisions and slicing have already been ratified by Engineering Council in two prior verdicts:

- **Architecture (locked):** [`docs/decisions/deployment-pipeline-architecture.md`](../decisions/deployment-pipeline-architecture.md) — 5 advisors, 4/5 APPROVE/CONDITIONAL. Provisioning via Bicep checked-in + hand-applied for week 1; deploy target = single Azure VM running docker compose; secrets via Azure Key Vault + VM system-assigned managed identity; observability via Azure Log Analytics agent.
- **Slicing (locked):** [`docs/decisions/deployment-pipeline-slicing.md`](../decisions/deployment-pipeline-slicing.md) — 5 advisors, 3/5 APPROVE/CONDITIONAL on Approach A (4 incremental PRs). Hawk's re-ordering applied: observability + backup target ship in Slice 1.

**This discussion is therefore an alignment doc, not a fresh deliberation.** The council outputs ARE the user stories, constraints, and non-goals; the PRD operationalizes them.

---

## What Slice 1 Must Deliver (per the council verdict)

Provisioning the foundational Azure resources for `msaiv2_rg` (eastus2, MarketSignal2 sub) so future slices can build on top without re-doing IaC.

**Must include (per Hawk's Blocking Objection #1 — observability + backup target ship in Slice 1, not deferred):**

1. Bicep IaC declaring:
   - **Compute:** D4s_v6 VM (Ddsv6 family, 0/10 quota; DSv5 was 0/0)
   - **Network:** NSG (SSH from operator IP only, ports 80/443 inbound, no other inbound)
   - **Storage (data plane):** Premium SSD managed data disk for DATA_ROOT (separate from OS disk)
   - **Storage (backups):** Standard_LRS storage account + `msai-backups` Blob container (target lives here in Slice 1; the cron that writes to it ships in Slice 4)
   - **Container Registry:** Azure Container Registry, Basic SKU
   - **Secrets:** Azure Key Vault
   - **Observability:** Log Analytics workspace + Azure Monitor Agent (AMA) on the VM via Bicep extension
   - **Identity:** GH OIDC federated credential + VM system-assigned managed identity with three RBAC role assignments (Key Vault Secrets User, AcrPull, Storage Blob Data Contributor on the backup container)
2. Extension to `scripts/deploy-azure.sh` to call `az deployment group create -f infra/main.bicep`
3. New `scripts/render-env-from-kv.sh` + systemd unit (~30 lines): runs at boot, fetches every required secret via managed identity, writes `/run/msai.env` chmod 600

**Must NOT include (out of scope per slicing verdict):**

- GitHub Actions workflow + image build/push (Slice 2)
- SSH deploy + first real production deploy (Slice 3)
- Nightly backup cron + alert rules + active-`live_deployments` hard gate (Slice 4)
- Any application deploys

---

## Personas

- **Pablo (developer/operator):** Owner on MarketSignal2 sub. Runs `./scripts/deploy-azure.sh` from his Mac to provision/update infrastructure. Verifies acceptance via `az` CLI smoke checks.
- **GitHub Actions runner (future, Slice 2):** Federated identity will exchange OIDC token for an Azure access token. Slice 1 declares the federated-credential resource so Slice 2 can wire push-to-main → ACR push.
- **systemd on VM (future, Slice 3):** Boots and runs `render-env-from-kv.sh` to materialize `/run/msai.env` before docker compose comes up. Slice 1 ships the script + unit; Slice 3 enables it as part of the first real deploy.

---

## Constraints

- **Azure quota reality:** D4s_v5 has 0/0 quota in MarketSignal2; D4s_v6 has 0/10. Quota request avoidance was already baked into the architecture verdict.
- **Subscription:** MarketSignal2 (`68067b9b-943f-4461-8cb5-2bc97cbc462d`); tenant `2237d332-fc65-4994-b676-61edad7be319`. Pablo has Owner role.
- **Resource group:** `msaiv2_rg` (eastus2), confirmed empty.
- **Resource isolation (Contrarian Blocking Objection #5):** No reuse of the existing Azure ML workspace's ACR/KV/log resources at `msaimls2_rg`. Slice 1 must create dedicated msai resources.
- **Secret never on GH runner (Hawk + Maintainer architecture verdict):** IB credentials must reside in Key Vault from day one — not on the runner, not in repo, not in plain `.env`. The render-env-from-kv.sh script + systemd unit deliver this on the VM side.
- **Idempotency:** Bicep + `az deployment group create` must converge on subsequent runs (no orphan resources, no version drift between Bicep and Azure-side state).

---

## Non-Goals (explicit)

- ❌ No Helm charts, no Kubernetes anything (council verdict explicitly chose docker-compose-on-VM for Phase 1)
- ❌ No GH Actions workflow (Slice 2)
- ❌ No SSH from runner to VM (Slice 3)
- ❌ No backup cron (Slice 4)
- ❌ No alert rules (Slice 4)
- ❌ No application deployment of any kind
- ❌ No Azure Postgres Flexible Server (Phase 2 work; Phase 1 uses containerized Postgres)
- ❌ No `.env` files anywhere except `/run/msai.env` rendered at boot

---

## Acceptance Criteria (per slicing verdict §Slices, row 1)

- `az deployment group what-if -g msaiv2_rg -f infra/main.bicep` validates clean against the existing empty `msaiv2_rg` (Council "Missing Evidence" item 1)
- `az keyvault secret show` works from the VM via managed identity in a 15-minute manual smoke test (after seeding ≥1 dummy secret in KV and SSHing into the VM)
- Log Analytics agent reports VM heartbeat in the workspace (visible in Azure portal → Log Analytics → Heartbeat)

---

## Council-Mandated Phase 2 Research (research-first agent)

The slicing verdict's "Missing Evidence" section enumerates what Phase 2 must answer before design lands:

1. Bicep current idioms (2026-era — modules vs single file, params, output references)
2. GitHub OIDC federated credential subject-claim format (`repo:org/repo:ref:refs/heads/main` vs alternatives)
3. Azure Monitor Agent (AMA) vs legacy Linux Monitoring Agent (MMA) — confirm AMA is current best practice
4. AMA installation via Bicep VM extension
5. VM system-assigned managed identity propagation latency (document wait + retry pattern for `render-env-from-kv.sh`)

---

## Open Questions

- [ ] Should `infra/main.bicep` be one file or split into modules? (research-first will confirm 2026 idiom)
- [ ] Storage account naming: globally unique requirement — pick `msaiv2backupsXXXX` with a deterministic suffix (subscription ID hash) or random?
- [ ] Should the federated credential be created in Slice 1 even though it's only consumed in Slice 2? (Yes per slicing verdict — declared in Slice 1, used in Slice 2)
- [ ] AMA data collection rule (DCR) scope: sys log + heartbeat for Slice 1, leave perf counters for Slice 4? (Decision deferred to plan)

---

## References

- **Architecture verdict:** `docs/decisions/deployment-pipeline-architecture.md`
- **Slicing verdict:** `docs/decisions/deployment-pipeline-slicing.md`
- **Precursor PR (already merged):** `6900210` — `feat/prod-compose-deployable` (PR #50)
- **State at session start:** `.claude/local/state.md` § Next (lines 39–40 of pre-update state)
