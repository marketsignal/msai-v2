# How IBKR account secrets work (dev file store ↔ prod Azure Key Vault)

> **Audience:** operators + engineers working on the multi-account broker fleet
> (`BrokerAccount` entity, PR #86 onward). This is the authoritative "how the
> credential storage actually works, and how it's set up in prod" reference.
>
> **TL;DR:** When an operator adds/rotates an IB broker account, the **TWS
> userid + password are written server-side into a secrets backend**, and the
> database row stores **only a pointer** (`credentials_secret_ref` + pinned
> `credentials_secret_version`) — never the password. In **dev** the backend is a
> local file (`EnvFileBrokerCredentialsStore`); in **prod** it is **Azure Key
> Vault** (`AzureKvBrokerCredentialsStore`). The prod VM's managed identity needs
> **Key Vault Secrets Officer** (read **+ write + delete**) to do this — that grant
> is one-time, owner-applied, and codified in `infra/main.bicep`.

---

## 1. Why this design (council "Option B'")

Council ratified **Option B'** (2026-05-30, `docs/decisions/multi-account-broker-fleet.md`):

- The **backend writes** the credentials to the secrets backend on `POST`/rotate.
- The DB stores **only** `credentials_secret_ref`, pinned `credentials_secret_version`,
  `credentials_backend`, and audit columns. **No plaintext credential is ever in the
  DB, an API response, the CLI output, the UI, or a 422 validation echo.**
- At trade time, the live-supervisor resolves the credential back out of the backend via
  `resolve_for_spawn` (control-plane → data-plane seam).

Operator UX is identical in dev and prod — add an account through the UI/CLI/API; the
operator never opens an Azure portal.

## 2. The two backends

|                             | **dev**                                                      | **prod**                                     |
| --------------------------- | ------------------------------------------------------------ | -------------------------------------------- |
| Class                       | `EnvFileBrokerCredentialsStore`                              | `AzureKvBrokerCredentialsStore`              |
| Selected when               | `settings.environment != "production"`                       | `settings.environment == "production"`       |
| Storage                     | a local JSON file, mode `0o600`                              | Azure Key Vault secrets                      |
| `credentials_backend` value | `env`                                                        | `azure_kv`                                   |
| Auth                        | none (local file)                                            | VM **system-assigned managed identity** → KV |
| Source                      | `backend/src/msai/services/live/broker_credentials_store.py` | same file                                    |

Both implement the same `BrokerCredentialsStore` protocol (`put` / `get(pinned version)` /
`delete`), so the service logic (`BrokerAccountService`) is backend-agnostic. The factory
`get_broker_credentials_store()` picks the backend from config.

**Why a file store in dev (not a local KV emulator):** the council explicitly chose
NO local KV emulator — dev uses a fail-closed, owner-only file. This means the **Azure
write path cannot be exercised in dev** (see §5, the spike).

## 3. Prod components (resource group `msaiv2_rg`, sub `MarketSignal2`)

| Component       | Name                                        | Role                                                                        |
| --------------- | ------------------------------------------- | --------------------------------------------------------------------------- |
| Key Vault       | `msai-kv-4cd6d2obcxqaa`                     | stores every runtime secret incl. broker credentials                        |
| VM              | `msai-vm` (system-assigned MI `b1a25f12-…`) | runs the backend; reads + writes KV via its MI                              |
| GH Actions OIDC | `msai-gh-oidc` (`9fefa89d-…`)               | builds/pushes images + SSH-deploys; **no RBAC-write privilege — by design** |
| Operator        | `pablo@marketsignal.ai` (`da595508-…`)      | Owner; applies IaC + RBAC; seeds bootstrap secrets                          |

The vault URI the backend talks to is `https://msai-kv-4cd6d2obcxqaa.vault.azure.net/`
(`AZURE_KEYVAULT_URI`). See §6 for how it's rendered.

## 4. RBAC — the critical bit

Key Vault data-plane access is RBAC. Two relevant built-in roles:

| Role                          | GUID                                   | Capability                                       |
| ----------------------------- | -------------------------------------- | ------------------------------------------------ |
| **Key Vault Secrets User**    | `4633458b-17de-408a-b874-0445c86b69e6` | **read** secrets only                            |
| **Key Vault Secrets Officer** | `b86a8fe4-44ce-4948-aee5-eccb2c155cd7` | read **+ set + delete** secrets (supersets User) |

Before PR #86, the VM MI had **only Secrets User (read-only)** — fine for the boot-time
secret renderer, but it would **403 on every `set_secret`/`begin_delete_secret`** the
moment an operator added or rotated an account. PR #86 therefore grants the VM MI
**Secrets Officer**.

### Who applies the grant — and why NOT the CI pipeline

Creating a role assignment requires `Microsoft.Authorization/roleAssignments/write`
(Owner or User Access Administrator). The GH Actions OIDC identity deliberately does
**not** have that (it has only AcrPush + the SSH-deploy path) — giving CI the ability to
mint RBAC would be a privilege-escalation foothold. So the grant is a **one-time,
owner-authenticated action, codified in IaC** (`infra/main.bicep` →
`vmKvSecretsOfficerAssignment`, line ~670). It is **not** created by the routine deploy
workflow. The deploy workflow's job is to **verify** the write path, not grant it (§7).

The grant is additive and idempotent. It was applied to prod on **2026-06-02** as the
role assignment `3d129dfe-cc74-5233-8fa3-c0925abb00de` (= `guid(vm.id, kv.id,
'kv-secrets-officer')`, the exact name `main.bicep` uses, so a future full IaC re-apply
is a no-op).

### How to (re)apply the grant — fresh VM, rehearsal RG, or disaster recovery

The grant lives in `infra/main.bicep`, so the normal path is the full IaC apply:

```bash
az login --tenant 2237d332-fc65-4994-b676-61edad7be319        # MarketSignal tenant
az account set --subscription 68067b9b-943f-4461-8cb5-2bc97cbc462d   # MarketSignal2
./scripts/deploy-azure.sh --what-if    # dry-run; confirm the diff
./scripts/deploy-azure.sh              # apply (also reconciles NSG operator-IP, VM, etc.)
```

⚠️ A **full** `deploy-azure.sh` apply rewrites the NSG SSH allow rule to the running
machine's public IP (`nsgRuleSshFromOperator` uses `${operatorIp}/32`). If you only want
the KV grant **without** that blast radius (as was done on 2026-06-02), apply a **scoped**
template that creates only the role assignment with the same `guid()` name — see
`docs/runbooks/iac-parity-reapply.md` and the one-off template pattern in the PR #86
session notes. The surgical apply touched **1 resource (the role assignment), 22 ignored**.

## 5. The writable-KV integration spike (council "missing evidence")

Because dev uses the file store, the **Azure write path was never exercised** until prod.
The council made a writable-KV spike **required before relying on operator-added accounts
in prod**. It was run on **2026-06-02** on `msai-vm` via the system-assigned MI and
**PASSED**:

| Step                          | Result                                              |
| ----------------------------- | --------------------------------------------------- |
| `set_secret` (put)            | ok — version `v1` captured                          |
| `get(name, v1)` pinned read   | returns `spike-v1` ✓                                |
| rotate (`set_secret` again)   | new version `v2` ≠ `v1` ✓                           |
| `get(name, v1)` after rotate  | still `spike-v1` (old version retained) ✓           |
| not-found probe               | `SecretNotFound` (→ store maps to `kv_not_found`) ✓ |
| `begin_delete_secret` + purge | soft-deleted + purged (cleanup) ✓                   |

RBAC propagated immediately (no retry needed). The other failure modes
(`kv_unauthorized` / `kv_unreachable`) are covered by the store's unit tests
(`classify_kv_exception`) and are not force-tested in prod (would require breaking RBAC).

To re-run the spike (e.g., on a rehearsal RG), run on the VM via its managed identity —
no checked-in script needed, it's a short round-trip:

```bash
az vm run-command invoke -g <rg> --name <vm> --command-id RunShellScript --scripts '
  set -euo pipefail; KV=<kv-name>; N="spike-test-$(date +%s)"   # -e: any failed step fails the spike
  az login --identity --output none
  v1=$(az keyvault secret set --vault-name "$KV" --name "$N" --value v1 --query id -o tsv); v1=${v1##*/}
  az keyvault secret show --vault-name "$KV" --name "$N" --version "$v1" --query value -o tsv   # -> v1 (pinned)
  v2=$(az keyvault secret set --vault-name "$KV" --name "$N" --value v2 --query id -o tsv); v2=${v2##*/}  # rotate
  az keyvault secret show --vault-name "$KV" --name "$N" --version "$v1" --query value -o tsv   # -> v1 (old retained)
  az keyvault secret delete --vault-name "$KV" --name "$N" --output none                        # soft-delete
  az keyvault secret purge  --vault-name "$KV" --name "$N" --output none || true                # cleanup
'
```

A fresh role assignment can take a few minutes to propagate — if `set` 403s, wait and retry.

### Soft-delete naming gotcha

A soft-deleted KV secret **name** can't be re-`set_secret`'d until recovered/purged
(`ResourceExistsError`). The code sidesteps this with **unique-per-row secret names**
(`broker-cred-<row-uuid>`), so re-adding an archived `ib_account_id` is always a fresh row

- fresh name — never a collision.

## 6. `AZURE_KEYVAULT_URI` — derived, not fetched

The backend needs the vault's URL. It's **derived from `KV_NAME`** by the boot-time
renderer rather than fetched as a KV secret (which would be circular — storing the vault's
own URI inside itself):

- `scripts/render-env-from-kv.sh` emits
  `AZURE_KEYVAULT_URI="https://${KV_NAME}.vault.azure.net/"` into `/run/msai.env`.
- `KV_NAME` reaches the renderer via the `kv-name.conf` systemd drop-in written by
  `scripts/deploy-on-vm.sh` Phase 5.
- The prod compose backend guard `${AZURE_KEYVAULT_URI:?}` then fails-loud if it's ever
  missing.

**Frozen-unit caveat (important):** the render unit + script are baked once by cloud-init
at VM provision time. `deploy-on-vm.sh` Phase 5 **re-installs** them from
`/opt/msai/scripts/` on every deploy (and `deploy.yml` stages them), so render-logic
changes reach existing VMs instead of being frozen. Don't remove that re-sync.

`AZURE_KV_MI_CLIENT_ID` is optional (blank = use the VM's **system-assigned** MI, which is
what we use). **Never** set it to `AZURE_CLIENT_ID` (that's the JWT audience, a different
identity).

## 7. CI/CD — what's automated vs one-time

| Concern                                    | Owner                                                              | Automated?                                                                                     |
| ------------------------------------------ | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| Create the Officer RBAC grant              | Owner via IaC (`deploy-azure.sh`)                                  | One-time / on IaC re-apply. **Not** the CI pipeline (OIDC SP has no RBAC-write — intentional). |
| Render `AZURE_KEYVAULT_URI` onto the VM    | `render-env-from-kv.sh` (boot)                                     | ✅ every deploy (derived from `KV_NAME`)                                                       |
| Re-sync render unit/script to existing VMs | `deploy-on-vm.sh` Phase 5                                          | ✅ every deploy                                                                                |
| **Verify the KV write path is healthy**    | `scripts/deploy-smoke.sh` (VM-side, Phase 12 of `deploy-on-vm.sh`) | ✅ every deploy — see below                                                                    |

### KV write-path smoke (automated guardrail)

`deploy-smoke.sh` (Step 0a) runs a **put → get(pinned) → delete round-trip through the
real `AzureKvBrokerCredentialsStore`, executed INSIDE the backend container**
(`docker compose exec backend python -c …`). Running it in the container — not on the VM
host — means it proves the exact code path, credential env (`AZURE_KEYVAULT_URI` /
`AZURE_KV_MI_CLIENT_ID` → `ManagedIdentityCredential(client_id=…)`), and IMDS/KV network
that operator add/rotate actually uses; a host-only quirk can't false-pass while the
container's store still fails. All three of put/get(pinned)/delete are exercised because
the store needs all three (delete backs the archive path's `begin_delete_secret`).

It runs **before** the live-deployment skip gate on purpose: the round-trip only touches a
throwaway `deploy-smoke-kv-<ts>` secret (never Parquet/DuckDB, never real `broker-cred-*`),
so it is safe even during live trading — and a silent KV regression must not slip through
on exactly those deploys.

A **deadline-bounded retry (~5 min budget)** absorbs Azure RBAC data-plane **propagation
lag** — a just-created Secrets Officer assignment can 401/403 for a few minutes on a
fresh/rehearsal deploy (the renderer retries 403 for the same reason), so the smoke must
not false-fail a correctly-configured new environment. The common steady-state case (grant
long-propagated) succeeds on the first attempt with no delay; the budget only bites on
fresh/rehearsal/broken deploys. Each attempt is **hard-bounded by a 30s `timeout`** so a
blackholed KV/IMDS can't hang the deploy — a timed-out attempt (exit 124) is treated as
transient. Classification:

- **Round-trip succeeds** → pass.
- **Any 401/403 observed, still failing after the budget** → `FAIL_SMOKE_KV` →
  `deploy-on-vm.sh` **rolls back**. A _sticky_ auth flag means even if the FINAL attempt
  only times out/throttles, an earlier 401/403 still forces the fail — a transient tail can
  never mask a genuinely missing grant. This is the core regression catch.
- **Not-found / malformed-payload / import error / SIGKILL(137)** → `FAIL_SMOKE_KV` (store
  contract broken / self-check crashed — fail-closed).
- **Purely transient** (throttle / unreachable / 124 timeout, _no_ auth failure ever
  observed) after the budget → logged `WARN_SMOKE_KV`; the deploy proceeds (no rollback) —
  avoids false rollbacks on transient infra.

So after the one-time grant, **no manual KV step is ever needed**: the renderer supplies
the URI, the deploy re-syncs the render unit, and the smoke proves writes work or rolls
back. A fresh/rehearsal RG gets the grant from the IaC apply.

## 8. Quick reference

```
Vault:        msai-kv-4cd6d2obcxqaa   (https://msai-kv-4cd6d2obcxqaa.vault.azure.net/)
RG / sub:     msaiv2_rg / MarketSignal2 (68067b9b-943f-4461-8cb5-2bc97cbc462d)
VM MI:        b1a25f12-c430-4010-bc6a-141e99d665c8  (roles: Secrets User + Secrets Officer)
Grant name:   3d129dfe-cc74-5233-8fa3-c0925abb00de  (= guid(vm.id, kv.id,'kv-secrets-officer'))
Secret names: broker-cred-<broker_account_row_uuid>   (unique per row; pinned version stored on the row)
```

**Inspect live RBAC:**

```bash
KVID=$(az keyvault show -n msai-kv-4cd6d2obcxqaa -g msaiv2_rg --query id -o tsv)
az role assignment list --scope "$KVID" \
  --assignee b1a25f12-c430-4010-bc6a-141e99d665c8 --query "[].roleDefinitionName" -o tsv
# expect: Key Vault Secrets Officer  +  Key Vault Secrets User
```

**Related docs:** `docs/decisions/multi-account-broker-fleet.md` (Addendum 2026-06-01),
`docs/plans/2026-06-01-broker-account-entity.md`, `docs/runbooks/iac-parity-reapply.md`.
