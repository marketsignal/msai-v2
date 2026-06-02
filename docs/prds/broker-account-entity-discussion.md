# PRD Discussion: BrokerAccount First-Class Entity (Multi-Account Broker Fleet PR 3)

**Status:** Complete
**Started:** 2026-06-01
**Participants:** Pablo, Claude

## Original User Stories

Derived from the ratified roadmap in `docs/decisions/multi-account-broker-fleet.md`
§"Addendum 2026-05-30 — Forward plan after PR 1 ship + PR 3 credentials council":

- As an **operator**, I want to **add a new IB account** (login + the symbols/risk it
  trades) through the UI/CLI/API, so I never have to hand-edit env vars + restart
  containers to bring an account online.
- As an **operator**, I want to **edit an existing account** (rotate credentials,
  adjust config) without downtime for the rest of the fleet.
- As an **operator**, I want to **archive/remove an account** I no longer trade, with
  its secret material cleaned up safely.
- As an **operator**, I want **adding/removing an account to be safe at runtime** —
  one bad credential set must not crash the rest of the fleet (this is why PR 2's
  per-account supervisor ownership landed first).

## Scope (confirmed with Pablo, 2026-06-01)

**This branch = PR 3a + PR 3b together** (one branch, can ship as one or two PRs):

- **3a:** `broker_accounts` table + Alembic migration + CRUD API +
  `BrokerCredentialsStore` (`EnvSecretsProvider` dev + `AzureKeyVaultProvider` prod) +
  API-level E2E UC.
- **3b:** `msai` CLI sub-app + Settings UI wizard + CLI & UI E2E UCs.

All three surfaces (API / CLI / UI) are in E2E scope.

## Already-Settled (council-ratified — NOT re-litigated here)

These are FIXED by the 2026-05-30 council. Listed so the discussion stays on genuinely
open product questions:

- **Credential storage = Option B'.** Backend writes credentials to AKV server-side
  (managed identity, prod) or `.env` via `EnvSecretsProvider` (dev) from the POST
  payload. Row stores ONLY `credentials_secret_ref` + `credentials_secret_version`
  (pinned KV version GUID) + `credentials_updated_at` + `credentials_updated_by`.
  GET never returns cleartext. IB Gateway reads `TWS_USERID`/`TWS_PASSWORD` from its
  own process env, materialized at spawn from a fresh KV fetch.
- **Dedicated `BrokerCredentialsStore` interface** with `put/get/rotate/delete` — NOT
  bolted onto the read-only `SecretsProvider`.
- **No local KV emulator in dev** — `EnvSecretsProvider` reading `TWS_USERID_<suffix>`.
- **Static-pool provisioning** — N predefined `ib-gateway-{1..N}` compose services
  (proposed N=10). Provisioning = allocate a free slot + restart with KV-loaded env.
  NOT docker-py spawn, NOT k8s.
- **Mandatory alerts in same PR** — `broker_account_spawn_failed{account_id,reason}`,
  `kv_secret_age_seconds{account_id}`, `credentials_last_accessed`, boot-time KV
  reachability probe.
- **Fail-LOUD** — credential resolution errors → `SPAWN_FAILED_PERMANENT` naming the
  account + secret ref.
- **Pre-merge spike** — prove writable-KV (`SecretClient.set_secret` + version pin +
  read-back) works with the existing managed-identity setup (<30 min). Runs in research.

## Discussion Log

### Round 1 (2026-06-01) — open product decisions the council deferred

**Q1. Account-removal semantics (council blocking #8).**
**A: Soft-archive + secret delete.** Removing an account sets `status=archived` (row +
audit history retained, gateway slot freed) AND deletes the KV secret (Azure soft-delete
retention applies as a recovery window). An archived account cannot trade; re-adding the
same IB account is a NEW row + NEW secret. → satisfies the council's audit-continuity
preference while keeping credential hygiene tight.

**Q2. Existing env-var accounts (LVP/HVP).**
**A: Migrate into the table.** A backfill migration creates `broker_accounts` rows for
the 2 existing accounts (LVP=U4705114, HVP=U4715997), referencing their existing
secret material + assigned gateway slots, so the table is the single source of truth.
The env-var path stays as a legacy fallback but the table is authoritative.
⚠ Constraint: the migration is data-only and MUST NOT disrupt the currently-running
env-var-driven live path (no credential re-write, just row backfill pointing at the
existing secret ref/slot).

**Q3. Create-time credential validation.**
**A: Store-and-defer to spawn.** POST validates payload SHAPE (account-id format, slot
availability, required fields), writes credentials to KV, and creates the row. REAL IB
login validation happens at gateway spawn and is fail-LOUD (`SPAWN_FAILED_PERMANENT`,
already in scope). Rationale: IB allows one session per login (gotcha #3) — a synchronous
login inside a CRUD call would be slow, flaky, and could collide with a live session.

**Q4. Edit scope.**
**A: Config + credential rotation mutable; IB account-id immutable.** Editable: traded
symbols/config + risk fields + credential rotation (re-POST creds → new pinned KV
version, old version retained for recovery). The IB account identifier (U.../DU...) is
immutable — changing it is semantically a different account (archive + add new). The
`credentials_secret_ref` is keyed to the account, so an immutable id keeps the binding
and audit trail clean.

## Refined Understanding

### Personas

- **Operator** — the single human (Pablo today; multi-user later) who manages which IB
  accounts the fleet trades. Reaches the capability via UI wizard, `msai` CLI, or REST
  API. No separate read-only persona in scope (single-operator product today); RBAC =
  any authenticated principal, consistent with the rest of `/api/v1/*`.

### User Stories (Refined)

- **US-1 (Add):** Operator adds a new IB account (account-id + credentials + traded
  config) via UI/CLI/API; backend writes creds to KV server-side, allocates a free
  gateway slot, and persists a `broker_accounts` row. Operator sees the account listed
  as `inactive` (not yet spawned), with NO cleartext credentials ever returned.
- **US-2 (List/Get):** Operator lists accounts and inspects one; response shows
  account-id, status, slot, config, and credential METADATA (`credentials_secret_ref`,
  `credentials_secret_version`, `credentials_updated_at/by`, `credentials_last_accessed`)
  — never the secret.
- **US-3 (Edit/Rotate):** Operator edits config or rotates credentials; rotation creates
  a new pinned KV version and updates audit columns. IB account-id is immutable.
- **US-4 (Archive):** Operator archives an account → `status=archived`, slot freed, KV
  secret deleted (soft-delete retention). Audit row survives.
- **US-5 (Migrate):** The 2 existing env-var accounts are backfilled into the table on
  migration without disrupting the running live path.
- **US-6 (Safety):** Adding/removing accounts at runtime cannot crash the rest of the
  fleet (rests on PR 2's per-account supervisor ownership) and credential-resolution
  failures fail LOUD as `SPAWN_FAILED_PERMANENT`.

### Non-Goals

- **Actually spawning/starting a gateway from the CRUD path** — provisioning a slot's
  process lifecycle (restart with KV-loaded env) is wired to the existing supervisor;
  this PR persists the entity + slot allocation, it does not re-implement spawn.
- **Dashboard account selector / fleet filters** — that's PR 4.
- **Per-account hard caps + fleet-aggregate ledger** — that's PR 5.
- **Runtime-dynamic container creation** (docker-py / k8s) — rejected; static pool only.
- **Local KV emulator in dev** — `EnvSecretsProvider` only.
- **Multi-user RBAC roles** — single-operator product; any authenticated principal.

### Key Decisions

- Credential storage Option B' (council-ratified) — see Already-Settled block above.
- Soft-archive + KV secret delete on removal.
- Backfill-migrate LVP/HVP into the table (data-only, non-disruptive).
- Store-and-defer credential validation (real login validated fail-loud at spawn).
- IB account-id immutable; config + credential rotation are the mutable surface.
- Slot-pool exhaustion → explicit error on create (409, no free slot) — defaulted, not
  asked; surfaces clearly to the operator.

### Open Questions (Remaining)

- [ ] Exact KV secret naming convention for the migrated existing accounts vs the
      `TWS-USERID-<suffix>`/`TWS-PASSWORD-<suffix>` convention in the decision doc —
      resolve in the research/plan phase against the real `secrets.py` + compose.
- [ ] Whether the writable-KV spike (research phase) surfaces a blocker that triggers
      the Contrarian's envelope-encryption fallback (documented in the decision doc).

**Status:** Complete — ready for `/prd:create broker-account-entity`.
