# PRD: BrokerAccount First-Class Entity (Multi-Account Broker Fleet PR 3)

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-06-01
**Last Updated:** 2026-06-01

---

## 1. Overview

Today, bringing an Interactive Brokers account online means hand-editing environment
variables and restarting containers. This feature makes a broker account a **first-class,
operator-managed entity**: the operator adds, edits, rotates credentials for, and archives
IB accounts through the UI, the `msai` CLI, or the REST API — no env-var surgery, no
manual restarts. It is PR 3 of the multi-account broker fleet, landing on the per-account
supervisor isolation shipped in PR 2 (#85) so runtime account changes can't take down the
rest of the fleet. Credential handling follows the council-ratified **Option B'**: the
backend writes secrets to Azure Key Vault server-side and the database stores only a
pinned secret reference — cleartext credentials are never persisted in the DB nor returned
by any read.

## 2. Goals & Success Metrics

### Goals

- Operators can add / edit / rotate-credentials-for / archive an IB account through any of
  the three product surfaces (UI, CLI, API) without touching env vars or restarting the
  fleet by hand.
- Credentials are captured once in the operator's submission and stored securely
  server-side; they are never readable back through the product.
- Adding or removing an account at runtime is safe — a bad credential set fails LOUD for
  that account only and never crashes the rest of the fleet.
- The two existing accounts (LVP, HVP) become rows in the new system of record without
  disrupting the currently-running live path.

### Success Metrics

| Metric                                                                      | Target               | How Measured                                                                                                             |
| --------------------------------------------------------------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Operator can add a new tradeable account end-to-end with zero env-var edits | 100% of new accounts | Operator adds an account via UI/CLI/API; it appears in the fleet list and is eligible to be brought live                 |
| Cleartext credential exposure through the product                           | 0                    | No GET/list/CLI/UI path ever returns `tws_userid`/`tws_password`; verified by E2E + code review                          |
| Blast radius of a bad credential set                                        | 1 account            | A deliberately-bad credential set fails that account's spawn LOUD; other accounts keep trading (rests on PR 2 isolation) |
| Existing accounts migrated without live-path disruption                     | 2/2 (LVP, HVP)       | Migration backfills both rows; the running env-var live path is unaffected                                               |
| Credential-resolution failure is actionable                                 | 100%                 | Every KV failure mode maps to a `SPAWN_FAILED_PERMANENT` reason naming the account + secret ref                          |

### Non-Goals (Explicitly Out of Scope)

- ❌ Dashboard account selector / fleet view filters — **PR 4**.
- ❌ Per-account hard risk caps + fleet-aggregate ledger — **PR 5**.
- ❌ Re-implementing gateway process spawn/lifecycle — this feature persists the entity
  and allocates a static pool slot; the existing supervisor owns the process lifecycle.
- ❌ Runtime-dynamic container creation (docker-py / Kubernetes) — rejected by council;
  static pool of `ib-gateway-{1..N}` only.
- ❌ Local Key Vault emulator in dev — `EnvSecretsProvider` is the dev path.
- ❌ Multi-user RBAC roles / read-only personas — single-operator product today.
- ❌ Synchronous IB-login validation during a CRUD call — credentials are validated at
  spawn (fail-loud), not on create.

## 3. User Personas

### Operator

- **Role:** The human who decides which IB accounts the fleet trades (Pablo today;
  potentially multiple operators later). Works from a laptop via the Settings UI wizard,
  the `msai` CLI on the box, or the REST API for scripting.
- **Permissions:** Full CRUD on broker accounts. Any authenticated principal has operator
  rights — consistent with the rest of `/api/v1/*` (no separate role tier in this phase).
- **Goals:** Bring a new account online quickly and safely; rotate a leaked/expired login
  without downtime for other accounts; retire an account cleanly; trust that credentials
  are stored safely and never leak back.

## 4. User Stories

### US-001: Add a new IB account

**As an** operator
**I want** to add a new IB account (its identifier, credentials, and the config it trades)
through the UI/CLI/API
**So that** I can bring an account online without editing env vars or restarting containers

**Scenario:**

```gherkin
Given I am authenticated and a free gateway slot exists in the static pool
When I submit a new account with its IB account-id, login credentials, and trading config
Then the backend stores the credentials securely server-side (never in the DB as cleartext)
And a broker account record is created referencing only the secret location + version
And the account appears in my account list as "inactive" (persisted, not yet spawned)
And the response never echoes the credentials back
```

**Acceptance Criteria:**

- [ ] A new account submitted via API/CLI/UI is persisted and listed afterward.
- [ ] Credentials are stored server-side; no surface returns them.
- [ ] The stored record exposes only credential metadata (secret reference, pinned
      version, who/when updated, last-accessed) — never the secret.
- [ ] A free static-pool slot is allocated to the account at creation.
- [ ] The created account is created in a non-trading ("inactive") state.

**Edge Cases:**
| Condition | Expected Behavior |
| --------- | ----------------- |
| No free gateway slot in the pool | Create is rejected with a clear "no free slot" conflict the operator can act on |
| Duplicate IB account-id (active row already exists) | Rejected with a clear conflict naming the existing account |
| Malformed account-id / missing required field | Rejected with a field-level validation error before any secret is written |
| Secret-store write fails | Create fails LOUD with an actionable error; no orphan row left behind |

**Priority:** Must Have

---

### US-002: List and inspect accounts (no secret leakage)

**As an** operator
**I want** to list my accounts and inspect one
**So that** I can see fleet state and credential metadata without ever seeing the secret

**Scenario:**

```gherkin
Given one or more accounts exist
When I list accounts and then open one
Then I see each account's id, status, slot, and trading config
And I see credential metadata (secret reference, pinned version, updated-at/by, last-accessed)
And I never see the credential cleartext through any field
```

**Acceptance Criteria:**

- [ ] List returns all non-deleted accounts with status + slot + config.
- [ ] Get returns one account with full credential METADATA but no secret.
- [ ] Archived accounts are distinguishable from active ones in the listing.

**Edge Cases:**
| Condition | Expected Behavior |
| --------- | ----------------- |
| No accounts yet | Empty list, not an error |
| Account id not found | Clear not-found response |

**Priority:** Must Have

---

### US-003: Edit config and rotate credentials

**As an** operator
**I want** to edit an account's trading config and rotate its credentials
**So that** I can respond to a leaked/expired login or a config change without downtime for other accounts

**Scenario:**

```gherkin
Given an existing account
When I update its trading config and/or submit new credentials
Then the config change is persisted
And new credentials produce a new pinned secret version (prior version retained for recovery)
And the audit metadata (updated-at / updated-by) reflects the change
And the IB account identifier cannot be changed
```

**Acceptance Criteria:**

- [ ] Editing trading config persists and is visible on re-read.
- [ ] Rotating credentials creates a new pinned version; the row's version reference updates.
- [ ] Audit columns update on every mutation.
- [ ] Attempting to change the IB account-id is rejected.

**Edge Cases:**
| Condition | Expected Behavior |
| --------- | ----------------- |
| Rotate credentials but secret-store write fails | Edit fails LOUD; the row keeps pointing at the prior valid version (no half-rotation) |
| Edit an archived account | Rejected — archived accounts are not editable |

**Priority:** Must Have

---

### US-004: Archive an account

**As an** operator
**I want** to archive an account I no longer trade
**So that** its slot is freed and its secret material is cleaned up, while the audit record survives

**Scenario:**

```gherkin
Given an existing active account
When I archive it
Then its status becomes "archived" and its gateway slot is freed
And its stored secret is deleted (subject to the secret store's soft-delete retention window)
And the account record and its history remain for audit
And the archived account can no longer trade
```

**Acceptance Criteria:**

- [ ] Archiving sets status=archived and frees the slot.
- [ ] The secret is deleted from the store on archive.
- [ ] The row + audit history persist after archive.
- [ ] An archived account is excluded from "active/tradeable" listings but visible when explicitly requested.

**Edge Cases:**
| Condition | Expected Behavior |
| --------- | ----------------- |
| Archive an account that currently has a live deployment | Blocked or requires the deployment to be stopped first (must not silently orphan a running node) |
| Re-add a previously-archived IB account-id | Allowed as a brand-new row + new secret (not a revival of the old row) |

**Priority:** Must Have

---

### US-005: Migrate existing env-var accounts into the system of record

**As an** operator
**I want** the two existing accounts (LVP, HVP) represented as rows in the new system
**So that** the broker-accounts table is the single source of truth without breaking the running live path

**Scenario:**

```gherkin
Given LVP and HVP run today via environment variables
When the migration runs
Then a broker account row exists for each, referencing its existing secret material and slot
And the currently-running env-var-driven live path continues to work unchanged
```

**Acceptance Criteria:**

- [ ] After migration, both LVP and HVP appear as accounts via the API/CLI/UI.
- [ ] The migration does not rewrite or move existing credential material.
- [ ] The running live path is not disrupted by the migration.

**Edge Cases:**
| Condition | Expected Behavior |
| --------- | ----------------- |
| Migration run twice | Idempotent — no duplicate rows |
| Existing secret material not found at expected location | Migration surfaces a clear, actionable error rather than creating a broken row |

**Priority:** Must Have

---

### US-006: Runtime account changes are safe and fail loud

**As an** operator
**I want** adding/removing accounts at runtime to be isolated and fail loudly on bad credentials
**So that** one bad account never silently degrades or crashes the rest of the fleet

**Scenario:**

```gherkin
Given a multi-account fleet is running
When an account is added with an invalid/unreachable credential set
Then only that account fails to come live, with a loud actionable failure naming the account
And every other account keeps trading unaffected
```

**Acceptance Criteria:**

- [ ] A bad/unreachable credential set fails that account's spawn as a permanent, named failure.
- [ ] Other accounts are unaffected (rests on PR 2 per-account supervisor isolation).
- [ ] Each credential-resolution failure mode is observable (alert/metric) and attributable to an account.

**Edge Cases:**
| Condition | Expected Behavior |
| --------- | ----------------- |
| Secret store unreachable at boot while an active deployment exists | Fail closed — do not start in an unknown credential state |
| Secret version pinned on the row no longer exists in the store | Loud permanent failure naming the account + missing version |

**Priority:** Must Have

## 5. Constraints & Policies

### Business / Compliance Constraints

- This is a real-money hedge-fund platform. The real **fund** account is NOT touched by
  this work — only the LVP (local) and HVP (prod) test accounts. (Project rule.)
- IB permits only ONE live session per login — a CRUD operation must never open a
  competing IB session for validation (drives the store-and-defer decision).

### Platform / Operational Constraints

- Single-VM Docker Compose deployment today. The gateway pool is a **static set of N
  predefined `ib-gateway-{1..N}` services** (proposed N=10); the pool size is changed by
  editing compose, not at runtime.
- Routine push-to-main deploys exclude the `broker` profile and refuse while any live
  deployment is active (the PR 2 binding deploy contract) — account provisioning must
  respect that contract.
- Dev environments have no Key Vault — the dev secret path is environment-variable based.

### Dependencies & Required Integrations

- **Requires:** PR 2 per-account supervisor ownership (shipped, #85) — the isolation that
  makes runtime add/remove safe.
- **Named integrations (scope, not mechanism):**
  - **Azure Key Vault** — credentials are stored in and retrieved from Key Vault in
    production; secret versions are pinned and rotatable.
  - **Interactive Brokers Gateway** — accounts ultimately drive IB Gateway containers that
    read their login from process env materialized from the stored secret at spawn.

## 6. Security Outcomes Required

- **Who can access what:** Any authenticated principal can manage broker accounts
  (single-operator product). Unauthenticated requests are rejected as everywhere else.
- **What must never leak:** IB login credentials (`tws_userid` / `tws_password`) must
  never be retrievable through any product surface — not via API responses, list, CLI
  output, UI, or logs. They are write-only from the product's perspective.
- **What must be auditable:** Every credential write/rotation is attributable to an actor
  and a time (`credentials_updated_at` / `credentials_updated_by`), and last-access is
  tracked. Account lifecycle transitions (add / edit / rotate / archive) are traceable.
- **What must fail safe:** Credential-resolution and secret-store failures must fail LOUD
  and fail CLOSED (no trading in an unknown-credential state); a pinned secret version
  must be used so a background rotation can't silently swap credentials mid-flight.

## 7. Open Questions

- [ ] Exact secret naming convention for migrated existing accounts vs the
      `TWS-USERID-<suffix>` / `TWS-PASSWORD-<suffix>` convention — confirm against the
      real `secrets.py` + compose during the research/plan phase.
- [ ] Whether the pre-merge writable-KV spike surfaces a blocker that would trigger the
      documented envelope-encryption fallback (decision-doc "Fourth option").
- [ ] Whether archiving an account with a live deployment should hard-block or auto-stop
      the deployment first (US-004 edge case) — settle in design.

## 8. References

- **Discussion Log:** `docs/prds/broker-account-entity-discussion.md`
- **Authoritative decision doc:** `docs/decisions/multi-account-broker-fleet.md`
  §"Addendum 2026-05-30 — Forward plan after PR 1 ship + PR 3 credentials council"
- **Related PRDs:** `docs/prds/multi-account-broker-fleet.md`,
  `docs/prds/pr-2-per-account-supervisors.md`
- **Prior PRs:** #84 (PR 1 control plane), #85 (PR 2 per-account supervisors)

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes     |
| ------- | ---------- | -------------- | ----------- |
| 1.0     | 2026-06-01 | Claude + Pablo | Initial PRD |

## Appendix B: Approval

- [ ] Product Owner approval
- [ ] Technical Lead approval
- [ ] Ready for technical design
