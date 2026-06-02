# Research: BrokerAccount First-Class Entity (Multi-Account Broker Fleet PR 3)

**Date:** 2026-06-01
**Feature:** Operator-managed CRUD for IB broker accounts; credentials written server-side to Azure Key Vault (Option B'), DB stores only a pinned secret reference + version.
**Researcher:** research-first agent

## Libraries Touched

| Library                | Our Version     | Latest Stable | Breaking Changes vs ours | Source                                                                                                                                          |
| ---------------------- | --------------- | ------------- | ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| azure-keyvault-secrets | 4.10.0          | 4.10.0        | None (current)           | [SecretClient API](https://learn.microsoft.com/en-us/python/api/azure-keyvault-secrets/azure.keyvault.secrets.secretclient) (2026-06-01)        |
| azure-identity         | 1.25.2          | ~1.25.x       | None relevant            | [identity troubleshooting](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/identity/azure-identity/TROUBLESHOOTING.md) (2026-06-01) |
| SQLAlchemy / Alembic   | 2.0.36+ / 1.14+ | (in use)      | None — established       | In-repo pattern (`alembic/versions/r6m7n8o9p0q1_*.py`)                                                                                          |
| Typer                  | 0.15.0+         | (in use)      | None — established       | In-repo pattern (`backend/src/msai/cli.py`)                                                                                                     |
| Next.js 15 + shadcn/ui | 15 (in use)     | (in use)      | None — established       | In-repo pattern (`frontend/src/lib/api/live-portfolios.ts`)                                                                                     |

> NOTE: `azure-identity` and `azure-keyvault-secrets` are ALREADY backend dependencies, pinned in `backend/pyproject.toml` under the `[project.optional-dependencies] azure` extra (`azure-identity>=1.19.0`, `azure-keyvault-secrets>=4.9.0`) and resolved in `uv.lock` to **1.25.2** and **4.10.0** respectively. No new dependency is required — the write path uses methods on the same `SecretClient` the existing read-only `AzureKeyVaultProvider` already constructs.

---

## Per-Library Analysis

### azure-keyvault-secrets (PRIORITY TARGET — writable KV)

**Versions:** ours=4.10.0 (resolved in `uv.lock`), latest=4.10.0. Synchronous client lives at `azure.keyvault.secrets.SecretClient`; an async twin exists at `azure.keyvault.secrets.aio.SecretClient` (same method names; LRO methods return `AsyncLROPoller`). The existing `AzureKeyVaultProvider` in `core/secrets.py:96` uses the **sync** client.

**Breaking changes since ours:** None — we are on the latest stable.

**Deprecations:** None relevant to this feature.

#### 1. `set_secret()` — create/rotate + capture the version GUID for DB pinning

Signature (verified against the 4.x reference):

```python
set_secret(name: str, value: str, *, enabled: bool | None = None,
           tags: dict[str, str] | None = None, content_type: str | None = None,
           not_before: datetime | None = None, expires_on: datetime | None = None,
           **kwargs) -> KeyVaultSecret
```

- "If _name_ is in use, create a NEW version of the secret. If not, create a new secret." This is exactly the rotation semantics US-003 needs — a rotate is just another `set_secret(name, new_value)`; the old version stays addressable.
- Requires the `secrets/set` permission.
- **Capturing the version GUID for `credentials_secret_version`:** the return is a `KeyVaultSecret` which has `.properties` (a `SecretProperties`) and `.value`/`.name`/`.id`. The version GUID is `result.properties.version`. (`KeyVaultSecret(properties: SecretProperties, value)`; `SecretProperties` carries `.version`, `.name`, `.id`.) **Pin `result.properties.version` onto the DB row** — do not parse it out of the `.id` URL.
- Raises `HttpResponseError` on failure (and `ResourceExistsError` in the soft-delete collision case — see #3).

#### 2. `get_secret(name, version)` — reading a pinned version

Signature:

```python
get_secret(name: str, version: str | None = None, *, out_content_type=None, **kwargs) -> KeyVaultSecret
```

- **Omitting `version` returns the LATEST version.** Confirmed in docs: "(optional) Version of the secret to get. If unspecified, gets the latest version."
- **Version pinning is exactly what the council mandate requires:** pass the stored `credentials_secret_version` as the second positional arg so a background rotation cannot silently swap credentials mid-flight (Security Outcome "fail safe — pinned version"). The spawn path MUST call `get_secret(name, pinned_version)`, never the bare form.
- Raises `ResourceNotFoundError` if the secret name OR the specific pinned version does not exist (maps to `kv_not_found` → US-006 "pinned version no longer exists" edge case).

#### 3. Soft-delete + purge — the critical "re-add archived account" gotcha (US-004)

- `begin_delete_secret(name) -> LROPoller[DeletedSecret]` — deletes ALL versions of a secret. With soft-delete enabled (the Azure default since 2020, non-disableable on new vaults), the secret enters a _deleted-but-recoverable_ state for the vault's retention window (default 90 days, configurable 7–90). Requires `secrets/delete`. The poller's `result()` returns immediately; call `.wait()` (needs `secrets/get`) only if you must block until deletion completes.
- `begin_recover_deleted_secret(name) -> LROPoller[SecretProperties]` — restores a soft-deleted secret. Requires `secrets/recover`.
- `purge_deleted_secret(name) -> None` — irreversible immediate purge; requires `secrets/purge` AND the vault's `recovery_level` must include `Purgeable`. Many hardened vaults have **purge protection** ON, in which case purge is _impossible_ until the retention window elapses.
- **GOTCHA that directly drives the US-004 design (edge case "Re-add a previously-archived IB account-id"):** if a secret NAME is in the soft-deleted state, calling `set_secret(name, ...)` on that same name **fails** with `ResourceExistsError` / Azure error `ObjectIsDeletedButRecoverable` — you cannot create a fresh secret under a name that is soft-deleted until it is either recovered or purged. (Confirmed via Azure SDK issue #9743 and external-secrets #3519.)
  - The PRD US-004 edge case says re-adding an archived account-id should be "a brand-new row + new secret (not a revival of the old row)." Two viable designs, **decide in design phase:**
    1. **Per-account-instance unique secret names** (e.g., include the new row's UUID or a monotonic suffix in the secret name) so a re-add never collides with a soft-deleted name. Simplest; avoids the recover/purge dance entirely. **Recommended default.**
    2. Recover-then-overwrite the old name — but this _revives_ the old secret history, contradicting "brand-new row," and needs `secrets/recover`. Not recommended.

#### 4. Rotation (US-003)

- Covered by #1: `set_secret(name, new_value)` creates a new version; the prior version remains retrievable via `get_secret(name, old_version)`. The DB UPDATE swaps `credentials_secret_version` to `new_result.properties.version`. The council's "prior version retained for recovery" is satisfied for free by KV versioning — no extra retention logic in our code.
- **Half-rotation safety (US-003 edge case "rotate but secret-store write fails"):** the row must keep pointing at the prior valid version. Design implication: write to KV FIRST, capture the new version, and only then UPDATE the DB row in the same request handler. If `set_secret` raises, the DB row is never touched — atomicity is achieved by ordering, not by a distributed transaction.

#### 5. Auth — managed-identity writes vs reads (RBAC)

- The existing provider builds the client with `DefaultAzureCredential()` (`core/secrets.py:95`). On the prod VM that resolves to the VM's managed identity (`ManagedIdentityCredential` inside the chain). No code change to credential construction is needed for writes.
- **RBAC role delta — this is a deploy/IaC task, not a code task:** the read-only provider only needs `secrets/get`. The new write path needs `secrets/set` (create/rotate) and `secrets/delete` (archive), plus optionally `secrets/recover` if design option 3.2 is chosen. With Azure RBAC data-plane auth, the **"Key Vault Secrets Officer"** role covers get/set/delete/recover/purge; **"Key Vault Secrets User"** covers get only. The prod managed identity currently almost certainly has only the User role — **flag: the VM's managed identity must be granted Secrets Officer (or a custom role with set+delete) before the write path works in prod.** Open Risk below.

#### 6. Failure-mode → `SPAWN_FAILED_PERMANENT` reason mapping

The decision doc requires every KV failure to map to a named reason (`kv_unauthorized | kv_not_found | kv_throttled | kv_unreachable | decrypt_failed`). Exception types to classify on (all from `azure.core.exceptions` except the credential one):

| Reason            | Exception to catch                                                                                              | How to detect                                                                                                                                            |
| ----------------- | --------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kv_unauthorized` | `azure.core.exceptions.HttpResponseError` (status 401/403) OR `azure.core.exceptions.ClientAuthenticationError` | `ClientAuthenticationError` for token-acquisition failures; `HttpResponseError.status_code in (401, 403)` for RBAC-denied data-plane calls               |
| `kv_not_found`    | `azure.core.exceptions.ResourceNotFoundError`                                                                   | Subclass of `HttpResponseError` (404). Existing provider already special-cases this at `secrets.py:118`. Covers missing name AND missing pinned version. |
| `kv_throttled`    | `azure.core.exceptions.HttpResponseError`                                                                       | `status_code == 429`. Retry-After honored by SDK's built-in retry policy, but a persistent 429 surfaces as `HttpResponseError`.                          |
| `kv_unreachable`  | `azure.core.exceptions.ServiceRequestError` (and `ServiceResponseError`)                                        | Network-level — DNS/connection failures, no HTTP status. These are the "vault unreachable at boot" → fail-closed case (US-006).                          |
| `decrypt_failed`  | n/a for KV path                                                                                                 | Reserved for the Contrarian's envelope-encryption fallback ("Fourth option"); not produced by the KV path.                                               |

Catch order matters: `ResourceNotFoundError` and `ClientAuthenticationError` are subclasses/siblings — catch the specific ones before the broad `HttpResponseError`, and `ServiceRequestError` separately (it is NOT an `HttpResponseError`).

**Sources:**

1. [SecretClient class reference (azure-python)](https://learn.microsoft.com/en-us/python/api/azure-keyvault-secrets/azure.keyvault.secrets.secretclient) — accessed 2026-06-01
2. [KeyVaultSecret class reference](https://learn.microsoft.com/en-us/python/api/azure-keyvault-secrets/azure.keyvault.secrets.keyvaultsecret) — accessed 2026-06-01
3. [Azure SDK issue #9743 — error when setting a secret after delete](https://github.com/Azure/azure-sdk-for-python/issues/9743) — accessed 2026-06-01
4. [external-secrets #3519 — ObjectIsDeletedButRecoverable on push to soft-deleted](https://github.com/external-secrets/external-secrets/issues/3519) — accessed 2026-06-01
5. [azure-identity TROUBLESHOOTING.md](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/identity/azure-identity/TROUBLESHOOTING.md) — accessed 2026-06-01

**Design impact:**

- Add a dedicated `BrokerCredentialsStore` interface with `put / get(version) / rotate / delete` semantics (council blocking objection #4); do NOT bolt writes onto the read-only `SecretsProvider` Protocol at `core/secrets.py:16`. The prod adapter wraps the SAME `SecretClient`; the dev adapter is env-var based (no KV emulator — council #2).
- Pin `set_secret(...).properties.version` onto the DB `credentials_secret_version` column.
- Spawn-time read MUST be `get_secret(name, pinned_version)` (never bare) — fail-closed if version missing.
- Rotation: write KV first, capture new version, then UPDATE the row — ordering gives half-rotation safety.
- Archive: use unique-per-instance secret names (option 3.1) so re-adding an archived account-id never hits the soft-deleted-name `ResourceExistsError`. Settle PRD Open Question on naming convention here.
- Boot-time KV reachability probe must catch `ServiceRequestError` → fail-closed when an active deployment exists (council #6).

**Test implication:**

- Unit-test the exception→reason classifier with FAKES raising each of `ClientAuthenticationError`, `HttpResponseError(status_code=403/429)`, `ResourceNotFoundError`, `ServiceRequestError` — assert each maps to the correct `SPAWN_FAILED_PERMANENT` reason. (Mock the SDK — never hit live KV in unit tests; matches `testing.md` "Mock external APIs.")
- The dev `EnvSecretsProvider`-backed `BrokerCredentialsStore` is the path exercised by the API/CLI/UI E2E use cases; assert no cleartext leaks in any response.
- Round-trip test for the "re-add archived account-id" path against the dev store to lock in the unique-name behavior.
- **LIVE writable-KV spike is NOT runnable in this dev worktree** — see Open Risks.

---

### azure-identity

**Versions:** ours=1.25.2, latest≈1.25.x. **Breaking changes:** None relevant. **Deprecations:** None relevant.

**Recommended pattern:** keep `DefaultAzureCredential()` as the existing provider does. In prod it resolves the VM managed identity. `ClientAuthenticationError` (token acquisition failed) and `CredentialUnavailableError` (no credential configured — dev) are the two identity-layer exceptions; the former maps to `kv_unauthorized`, the latter should never fire in prod (managed identity present) but in dev the BrokerCredentialsStore uses the env adapter and never constructs a credential.

**Sources:**

1. [azure-identity TROUBLESHOOTING.md](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/identity/azure-identity/TROUBLESHOOTING.md) — accessed 2026-06-01
2. [SecretClient constructor (credential param)](https://learn.microsoft.com/en-us/python/api/azure-keyvault-secrets/azure.keyvault.secrets.secretclient) — accessed 2026-06-01

**Design impact:** No new credential wiring; reuse the existing `DefaultAzureCredential` construction.
**Test implication:** Standard coverage — covered by the classifier tests above (`ClientAuthenticationError` → `kv_unauthorized`).

---

### SQLAlchemy 2.0 model + Alembic data backfill (US-005 LVP/HVP migration)

**Triage:** Lightly researched — established in-repo. The idiomatic data-backfill pattern is proven at
`backend/alembic/versions/r6m7n8o9p0q1_backfill_legacy_deployments_as_portfolios.py`:

- `conn = op.get_bind()`; raw `sa.text(...)` parameterized SELECT/INSERT/UPDATE; **idempotent** by skipping rows that already satisfy the post-condition (`WHERE portfolio_revision_id IS NULL` filter, returns early on empty). Mirror this for US-005's "migration run twice → no duplicate rows."
- It even imports an app service inside `upgrade()` (`from msai.services.live.portfolio_composition import compute_composition_hash`) — so importing a helper to derive secret names/refs during the backfill is an accepted pattern here, but keep it minimal.
- **Additive-only discipline (`database.md`):** `broker_accounts` is a NEW table → safe. The data-backfill MUST NOT rewrite/move existing LVP/HVP secret material (PRD AC: "does not rewrite or move existing credential material") — it only INSERTs rows that POINT at the existing secret refs. If the existing secret is not found at the expected ref, surface an actionable error rather than creating a broken row (PRD edge case).
- **Open Question to settle in design:** the migrated rows reference the EXISTING `TWS-USERID-<suffix>` / `TWS-PASSWORD-<suffix>` secret material (per PRD §7). Confirm the exact suffix convention against the real prod `.env` / compose during plan phase — the backfill encodes those names.

**Design impact:** Write the US-005 backfill as a separate idempotent Alembic data-migration revision following the `r6m7n8o9p0q1` shape; do not autogenerate it.
**Test implication:** Integration test the migration twice on a clean test DB; assert exactly 2 rows (LVP, HVP) and no duplicates on re-run. Assert the "secret material missing" path raises rather than inserting.

---

### Typer sub-app registration (US-001/002/003/004 CLI surface — PR 3b)

**Triage:** Lightly researched — established in-repo. `backend/src/msai/cli.py:90-120` shows the exact pattern: construct a `typer.Typer(help=...)` per area, then `app.add_typer(<sub_app>, name="<name>")`. There are already 13 registered sub-apps (the CLAUDE.md "9 sub-apps" count is stale — strategy, backtest, research, live, graduation, portfolio, account, system, instruments, alerts, auth, market-data). A new `broker` (or `accounts`) sub-app is wired identically: define `broker_app = typer.Typer(help="Broker account management")` and `app.add_typer(broker_app, name="broker")`. CLI commands are thin `httpx` wrappers around `/api/v1/...` sending `X-API-Key` from `$MSAI_API_KEY` (cli.py docstring + `:49`).

**Design impact:** New `broker` sub-app added to `cli.py`; commands proxy the new REST endpoints (do NOT talk to KV or DB directly from the CLI — keep server as single source of truth, matching the existing thin-wrapper convention). Credentials passed on `add`/`rotate` go in the POST body to the backend, never logged.
**Test implication:** CLI E2E UC (PR 3b) — `broker add` then `broker list` in a separate invocation shows the row WITHOUT any credential field in stdout.

---

### Next.js 15 + shadcn/ui multi-step wizard (US-001/003 UI surface — PR 3b)

**Triage:** Lightly researched — established in-repo. The typed API-client pattern is `frontend/src/lib/api/live-portfolios.ts` (thin `apiGet`/`apiPost` wrappers from `@/lib/api`, Bearer-token-first then `NEXT_PUBLIC_MSAI_API_KEY` fallback, typed request/response interfaces, `encodeURIComponent` on path params). A `broker-accounts.ts` client mirrors this. Wizard form should be a client component (multi-step state, credential inputs) calling the typed client; follow `frontend-design.md` (semantic HTML, labels not placeholders, focus styles) and `testing.md` (`getByTestId`/role selectors for the verify-e2e + Playwright spec).

**Design impact:** Add `frontend/src/lib/api/broker-accounts.ts` typed client mirroring `live-portfolios.ts`. Credential fields are write-only inputs — never round-tripped into the form on edit (the GET never returns them, so the edit form shows metadata only + a "rotate credentials" sub-step).
**Test implication:** UI E2E UC (PR 3b) — operator completes the wizard, the new account appears in the list, reload confirms persistence, and the detail view shows credential METADATA only (no secret). Standard shadcn/Playwright coverage otherwise.

---

## Not Researched (with justification)

- **PostgreSQL / asyncpg** — no version-sensitive surface; only a new additive table. Established in-repo (`database.md`).
- **FastAPI / Pydantic** — schema-separation pattern (Create/Update/Response) is established in `api-design.md` and used across existing routers; no version delta relevant to this feature.
- **NautilusTrader / IB Gateway spawn** — explicitly OUT of scope per PRD Non-Goals ("not re-implementing gateway process spawn/lifecycle"); the supervisor (PR 2, #85) owns the process. This feature only persists the entity + allocates a static-pool slot and materializes env at spawn.
- **Prometheus client (metrics)** — the council-mandated `broker_account_spawn_failed` / `kv_secret_age_seconds` counters use the project's existing metrics surface (`core/metrics`); no new library, no version research needed.

---

## Open Risks

1. **LIVE writable-KV spike cannot run in this dev worktree.** Dev has no Key Vault and uses `EnvSecretsProvider`; `set_secret`/`begin_delete_secret` against real Azure KV requires the prod VM's managed identity. The council's pre-merge "writable KV integration spike" (decision doc §"Missing evidence") is a **plan task gated on prod KV access** — it proves `set_secret` + version pinning + read-back end-to-end. Document SDK behavior from docs (this brief) now; schedule the live spike as an operator-run step before merge. If the spike surfaces a blocker, the documented fallback is the Contrarian's **envelope-encryption "Fourth option"** (decision doc §277).
2. **Prod managed-identity RBAC must be widened before the write path works.** The read-only provider only needs `secrets/get`; the write path needs `secrets/set` + `secrets/delete` (+ maybe `secrets/recover`). The VM identity likely holds only "Key Vault Secrets User" today. Granting "Key Vault Secrets Officer" (or a custom set+delete role) is an IaC/deploy task that must land WITH this PR per "No Bugs Left Behind" — not a follow-up. Verify the current role assignment during the spike.
3. **Soft-delete + purge-protection retention.** If the prod vault has purge protection ON, a deleted secret name is unrecoverable-into-reuse for the full retention window (up to 90d). This makes the unique-per-instance secret-name design (analysis §3 option 1) effectively mandatory for the "re-add archived account-id" path, not just preferred. Confirm the vault's `recovery_level` / purge-protection setting during the spike.
4. **Secret-name suffix convention for migrated accounts is unconfirmed** (PRD Open Question §7). The US-005 backfill hard-codes the existing `TWS-USERID-<suffix>` / `TWS-PASSWORD-<suffix>` names; if those don't match the real prod `.env`/compose, the migration creates broken refs. Confirm against the real secrets before writing the migration.
5. **CLAUDE.md sub-app count is stale** (says "9 sub-apps"; cli.py actually registers 13). Minor, but the plan should reference the real `cli.py` registrations, not the doc count.
