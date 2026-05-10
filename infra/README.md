# MSAI v2 — Azure IaC

Bicep templates declaring the Azure resources MSAI v2 needs in production. Slice 1 of a 4-slice deployment-pipeline series.

## Files

- **`main.bicep`** — single-file template (~600 lines). Section-ordered: params → vars → identity → networking → storage/data → registry/secrets → vm+extensions → role assignments → outputs. Symbolic-name implicit dependencies, no `dependsOn:` arrays.
- **`main.bicepparam`** — parameter file. Mostly defaults; override per-environment if needed.
- **`cloud-init.yaml`** — VM first-boot configuration. Bicep `loadTextContent`s the boot-time secret renderer (`scripts/render-env-from-kv.sh`) and systemd unit (`scripts/msai-render-env.service`) and base64-encodes them into the YAML's `write_files` slots, sidestepping YAML indentation issues. The script + unit are planted but the unit is NOT enabled — Slice 3 enables on first deploy.

## Deploy

```bash
# Pre-flight (operator)
az login --tenant 2237d332-fc65-4994-b676-61edad7be319         # MarketSignal tenant
az account set --subscription MarketSignal2

# Dry-run (recommended before first apply)
./scripts/deploy-azure.sh --what-if

# Apply
./scripts/deploy-azure.sh
```

`scripts/deploy-azure.sh` handles operator-IP resolution (HTTPS via `curl -4 -fsS https://api.ipify.org`), SSH public key (from `~/.ssh/id_ed25519.pub` etc.), and Entra object ID lookup (`az ad signed-in-user show`).

## Slice progression

| Slice | What it adds                                                                                                                                                         | Status                                 |
| ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| **1** | This template — VM + NSG + Premium SSD + Storage + Blob backup container + ACR + Key Vault + Log Analytics + AMA + GH OIDC federated credential + 4 role assignments | **In progress (this PR)**              |
| 2     | GH Actions workflow + image build/push to ACR + AcrPush role assignment on `msai-gh-oidc` MI                                                                         | Pending                                |
| 3     | SSH deploy + first real production deploy + enable `msai-render-env.service`                                                                                         | Pending (gated on backup verification) |
| 4     | Nightly backup cron + Log Analytics alert rules + active-`live_deployments` hard gate                                                                                | Pending                                |

## Council verdicts (locked architectural decisions)

- [`docs/decisions/deployment-pipeline-architecture.md`](../docs/decisions/deployment-pipeline-architecture.md)
- [`docs/decisions/deployment-pipeline-slicing.md`](../docs/decisions/deployment-pipeline-slicing.md)

## Plan + research

- [`docs/plans/2026-05-09-deploy-pipeline-iac-foundation.md`](../docs/plans/2026-05-09-deploy-pipeline-iac-foundation.md) — 12-task plan with 6 plan-review iterations
- [`docs/research/2026-05-09-deploy-pipeline-iac-foundation.md`](../docs/research/2026-05-09-deploy-pipeline-iac-foundation.md) — Azure/Bicep/AMA/OIDC research brief
