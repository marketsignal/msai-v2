# BrokerAccount First-Class Entity (Multi-Account Broker Fleet PR 3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an IB broker account a first-class, operator-managed entity (`broker_accounts` table) with CRUD over UI/CLI/API, credentials stored server-side per the council-ratified Option B' (Azure Key Vault in prod, file-backed dev store; DB holds only a pinned secret reference + version), so operators add/rotate/archive accounts without env-var edits or manual restarts.

**Architecture:** A new `BrokerAccount` SQLAlchemy entity is the system of record for account identity + gateway-slot binding + credential metadata. A dedicated, **writable** `BrokerCredentialsStore` (separate from the read-only `SecretsProvider`) owns `put/get(version)/rotate/delete`, with a prod Azure-KV adapter and a dev file-backed adapter; the DB row stores only `credentials_secret_ref` + pinned `credentials_secret_version` + audit columns — never cleartext. This PR owns the **control plane** (entity, CRUD, credential storage, slot allocation, audit, alerts). It does NOT re-implement gateway process spawn NOR wire credentials into the running gateway (PRD Non-Goal). **Verified reality (Codex plan-review iter-1):** nothing in `src/` reads `TWS_USERID`/`TWS_PASSWORD` today — the IB-Gateway container gets them from compose env (`docker-compose.dev.yml:259/322`), and the supervisor (`live_supervisor/__main__.py`) builds the TradingNode payload with host/port/account only, NO credentials. So this PR ships `resolve_for_spawn(account_id)` as a **tested, ready, but NOT-yet-wired** control-plane helper; the data-plane wiring (a future supervisor change that calls it and injects creds into the gateway container env at spawn) is explicitly DEFERRED and called out as such everywhere below. See the Approach Comparison (council-ratified) below.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 · Alembic · `azure-keyvault-secrets` 4.10.0 + `azure-identity` 1.25.2 (already pinned) · Typer (CLI) · Next.js 15 + shadcn/ui + react-hook-form (UI) · pytest + testcontainers.

---

## Approach Comparison

> **Status: PRE-DONE — ratified by Engineering Council 2026-05-30** (standalone `/council`, 4-1 majority). Persisted in `docs/decisions/multi-account-broker-fleet.md` §"Addendum 2026-05-30 — PR 3 credentials handling". This plan does NOT re-litigate the storage decision; it implements the ratified verdict + its 8 blocking conditions. Reproduced here per Phase 3.2.

### Chosen Default

**Option B'** — the single UI/CLI/API operator flow accepts credentials in the POST payload. Backend writes them to Azure Key Vault server-side (managed identity, prod) or a file-backed dev store (dev). The `broker_accounts` row stores ONLY `credentials_secret_ref` + `credentials_secret_version` (pinned version GUID) + `credentials_updated_at` + `credentials_updated_by` + `credentials_last_accessed`. GET never returns cleartext. The IB Gateway container reads `TWS_USERID`/`TWS_PASSWORD` from its own process env, materialized at spawn from a fresh `get(ref, version)`.

### Best Credible Alternative

**Option A** — DB-encrypted-at-rest with a `MASTER_ENCRYPTION_KEY` (Pragmatist minority report). Faster to ship (~2-3d vs ~3-4d), but master-key loss is unrecoverable, audit/rotation must be re-built later, and it fails QUIET (DB blip + bad key → garbage plaintext → opaque IB session-down). Overruled. (Fourth option — Contrarian's envelope-encryption — documented as the fallback if the writable-KV spike finds a blocker.)

### Scoring (fixed axes)

| Axis                  | Default (B')                                    | Alternative (A)                   |
| --------------------- | ----------------------------------------------- | --------------------------------- |
| Complexity            | M                                               | L                                 |
| Blast Radius          | L                                               | M                                 |
| Reversibility         | H (KV version pinning preserves prior versions) | L (master-key loss unrecoverable) |
| Time to Validate      | M (live KV spike, gated on prod access)         | L                                 |
| User/Correctness Risk | L (fail-LOUD at spawn)                          | M (fails QUIET)                   |

### Cheapest Falsifying Test

The council-flagged **writable-KV integration spike** (`SecretClient.set_secret` + version pin + read-back against real Azure KV with the prod managed identity). Estimated < 30 min, but **gated on prod KV access** — NOT runnable in this dev worktree (dev uses the file-backed store). Scheduled as an operator-run pre-merge step (Task 17). If it surfaces a blocker, fall back to the documented envelope-encryption Fourth Option.

## Contrarian Verdict

**COUNCIL (pre-done).** The Phase 3.1c gate is satisfied by the standalone 2026-05-30 council that produced Option B' with 8 blocking conditions and a preserved Pragmatist minority report. No re-run — the council IS the contrarian gate for this architecture-level decision (per the `skip-phase3-brainstorm-when-council-predone` discipline). File-level design below is fresh and goes through the Phase 3.3 plan-review loop.

---

## Developer Briefing (Gate 1)

**What I'll build.** A new "Broker Accounts" capability: an operator can add an Interactive Brokers account (its id, login, trading config, and credentials) through the web Settings UI, the `msai broker` CLI, or the REST API — and later edit its config, rotate its credentials, or archive it. The account's login credentials are written straight into secure storage server-side and are never shown back anywhere. The two accounts that run today via environment variables (LVP, HVP) get backfilled into the new table so it becomes the single source of truth.

**How it'll fit.**

```mermaid
flowchart LR
  OP["Operator"] -->|UI / CLI / API| API["broker-accounts router [planned]"]
  API --> SVC["BrokerAccountService [planned]"]
  SVC -->|put/get/rotate/delete| STORE["BrokerCredentialsStore [planned]"]
  SVC -->|row: ref + version + audit| DB[("broker_accounts table [planned]")]
  STORE -->|prod| KV["Azure Key Vault"]
  STORE -->|dev| FILE["file-backed dev store"]
  SUP["live supervisor (existing, PR 2)"] -.->|at spawn: get(ref,version)| STORE
  SUP -.->|TWS_USERID/PASSWORD env| GW["ib-gateway container"]
```

**Planned file-map** (see File Structure for the full list): new `models/broker_account.py`, `services/live/broker_credentials_store.py`, `services/live/broker_account_service.py`, `schemas/broker_account.py`, `api/broker_accounts.py`, `services/observability/broker_account_metrics.py`, two Alembic revisions, a `broker` CLI sub-app, and the frontend client + Settings pages/wizard.

**Key decisions.**

- `[verified]` Control-plane vs data-plane boundary: this PR stores + allocates + ships a tested `resolve_for_spawn` helper. It does NOT wire that helper into the supervisor — today nothing in `src/` reads `TWS_USERID`/`TWS_PASSWORD` (verified) and the supervisor builds payloads without credentials; the gateway gets creds from compose env. Data-plane wiring is a deferred follow-up (the dotted edge in the diagram), not part of this PR.
- `[planned]` Unique-per-instance secret names (`broker-cred-<row-uuid>`) so re-adding an archived account-id never collides with a soft-deleted KV name (research finding #1).
- `[planned]` Write-credential-store-first → then persist/UPDATE the row, so a store failure never leaves a row pointing at a non-existent version (half-rotation safety, research finding #2).
- `[planned]` Migrated LVP/HVP rows use a `legacy_env` credential backend pointing at the existing env keys with NULL version — satisfies "don't move existing credential material."

---

## File Structure

**Backend — new files:**

- `backend/src/msai/models/broker_account.py` — `BrokerAccount` model + `BrokerAccountStatus` + `CredentialsBackend` enums.
- `backend/src/msai/services/live/broker_credentials_store.py` — `BrokerCredentialsStore` Protocol, `Credentials`/`CredentialWriteResult` dataclasses, `CredentialResolutionError`, `classify_kv_exception`, the dev + prod adapters, and the `get_broker_credentials_store` factory.
- `backend/src/msai/services/live/broker_account_service.py` — `BrokerAccountService` (create/list/get/update/rotate/archive + slot allocation).
- `backend/src/msai/schemas/broker_account.py` — request/response Pydantic schemas.
- `backend/src/msai/api/broker_accounts.py` — `/api/v1/broker-accounts` router.
- `backend/src/msai/services/observability/broker_account_metrics.py` — counters/gauges.
- `backend/alembic/versions/<rev1>_create_broker_accounts.py` — CREATE TABLE (down_revision `e5f6a7b8c9d0`).
- `backend/alembic/versions/<rev2>_backfill_broker_accounts_lvp_hvp.py` — idempotent data backfill (down_revision `<rev1>`).

**Backend — modified files:**

- `backend/src/msai/main.py` — register the new router (~line 304-316 block); construct the credentials store + boot KV reachability probe in `lifespan` (near the `gateway_router` wiring at line 243).
- `backend/src/msai/cli.py` — add the `broker` Typer sub-app (mirror `account_app`, lines 90-120 + `_api_call`).
- `backend/src/msai/core/config.py` — add `broker_gateway_slots` setting (env `BROKER_GATEWAY_SLOTS`, default `["ib-gateway", "ib-gateway-hvp"]`).

**Frontend — new files:**

- `frontend/src/lib/api/broker-accounts.ts` — typed client (mirror `live-portfolios.ts`).
- `frontend/src/app/settings/broker-accounts/page.tsx` — list + entry to wizard/detail.
- `frontend/src/components/broker-accounts/broker-accounts-table.tsx` — list table.
- `frontend/src/components/broker-accounts/broker-account-wizard.tsx` — multi-step add/edit dialog (mirror `portfolio-start-dialog.tsx`).
- `frontend/src/components/broker-accounts/broker-account-detail.tsx` — detail (metadata only) + archive/rotate actions.

**Tests — new files:**

- `backend/tests/unit/test_broker_credentials_store.py`
- `backend/tests/unit/test_broker_account_metrics.py`
- `backend/tests/integration/api/test_broker_accounts_api.py`
- `backend/tests/integration/test_broker_accounts_migration.py`
- `frontend/tests/e2e/specs/broker-accounts.spec.ts` (Phase 6.2c, after verify-e2e records selectors)

**Considered & deferred:**

- ~~Enforcing `ib_login_key`/`gateway_session_key` NOT NULL~~ — **ALREADY DONE upstream** (Codex iter-1 P2#8 correction): migrations `t8o9p0q1r2s3_enforce_login_key_not_null.py` + `u9p0q1r2s3t4_*` already enforce these non-null and the models already declare `nullable=False`. Nothing for this PR — removed from scope.
- Building out compose `ib-gateway-3..N` services. **Deferred**: slot _allocation_ is in scope (against the configured pool, default = the 2 existing slots `ib-gateway` + `ib-gateway-hvp`); compose pool _expansion_ is operational plumbing tied to actual scale-up (decision doc line 213). The slot pool is config-driven so expansion is a compose edit, no code change.
- **Data-plane credential wiring** (supervisor calls `resolve_for_spawn` + injects `TWS_USERID`/`TWS_PASSWORD` into the gateway container env at spawn). **Deferred** (Codex iter-1 P1): today the gateway reads creds from compose `.env`; rewiring it to pull from `BrokerCredentialsStore` at spawn is a supervisor change with its own real-money drill. This PR ships the ready-and-tested `resolve_for_spawn` seam; the wiring + the live KV spike (Task 17) gate the eventual cutover.

---

## E2E Use Cases (Phase 3.2b)

**Surface coverage decision** (project exposes API + CLI + UI — see `CLAUDE.md`/the reconciled `surfaces:` list; the council scoped this feature to all three):

- **API — Covered** (UC-BA-API-1).
- **CLI — Covered** (UC-BA-CLI-1).
- **UI — Covered** (UC-BA-UI-1).

Live-trading safety: NONE of these UCs submit orders or touch a live IB session — they exercise the control-plane CRUD only, against the dev stack with the file-backed credentials store. No paper/live account is contacted. (The real credential→spawn path is proven separately by the operator KV spike + market-hours drill, Task 17.)

### UC-BA-API-1 — Integrator provisions an account and confirms it without ever seeing the secret

```
Actor:        API integrator scripting fleet setup against the broker-accounts service
Scenario:     They are onboarding a new IB account for the fleet and need to register it
              programmatically, then confirm it persisted so the next provisioning step
              can bind a portfolio to it — and they must be sure the login secret never
              comes back in any response.
Interface:    API
Intent:       The integrator registers a broker account on behalf of the operator and
              retrieves it back from the account list, confirming credentials are stored
              but never readable.
Setup:        Obtain an API key via the documented X-API-Key dev path (MSAI_API_KEY).
              (Do NOT pre-create the account — that's the action under test.)
Steps:        POST /api/v1/broker-accounts {ib_account_id, ib_login_key, trading_mode,
              gateway_slot?, tws_userid, tws_password}  →  GET /api/v1/broker-accounts
              → GET /api/v1/broker-accounts/{id}
Verification: Receives 201 + a Location header; following that link returns the new
              account with status "inactive"/"active-not-spawned", the same ib_account_id,
              and credential METADATA (credentials_secret_ref, credentials_secret_version,
              credentials_updated_at/by) — and NO tws_userid/tws_password field anywhere
              in the body. The list response includes the new account by id.
Persistence:  Re-request GET /api/v1/broker-accounts/{id} after a short delay; the account
              is still listed with the same id + metadata and still no secret.
```

### UC-BA-API-2 (error/edge) — Rotating credentials never half-writes on store failure

```
Actor:        API integrator rotating a compromised login
Scenario:     A login was leaked; the integrator rotates credentials via the API. They
              must be able to confirm the rotation produced a new pinned version, and that
              a rotation is safe (the row never ends up pointing at a version that doesn't
              exist).
Interface:    API
Intent:       The integrator rotates an account's credentials and confirms the row advances
              to a new pinned version without exposing the secret.
Setup:        Create an account via POST (sanctioned) to rotate.
Steps:        GET the account (note credentials_secret_version v1) → POST
              /api/v1/broker-accounts/{id}/rotate-credentials {tws_userid, tws_password}
              → GET the account again
Verification: Rotate returns 200; the follow-up GET shows credentials_secret_version
              CHANGED (v2 ≠ v1) and credentials_updated_at advanced, with NO secret in the
              body. (Half-rotation safety is unit-tested separately by forcing a store
              error and asserting the row still points at v1.)
Persistence:  Re-request GET after a delay; the account still shows v2 (the rotation stuck).
```

### UC-BA-CLI-1 — Operator adds an account from the shell and lists it back

```
Actor:        Operator on the prod box bootstrapping a new account from the CLI
Scenario:     They prefer the shell for ops work and want to add an account and confirm it
              landed in the fleet registry without opening the UI.
Interface:    CLI
Intent:       The operator adds a broker account and lists it back in a separate
              invocation to confirm it persists, with no secret echoed to the terminal.
Setup:        MSAI_API_KEY set in the environment; backend reachable (dev stack).
              Export MSAI_BROKER_TWS_PASSWORD=p (password never goes on argv — Codex iter-1 P0#2).
              (Do NOT pre-create the account.)
Steps:        Run `MSAI_BROKER_TWS_PASSWORD=p msai broker add --ib-account-id DU111
              --ib-login-key testlogin --trading-mode paper --tws-userid u`
              → run `msai broker list`
Verification: `add` stdout shows a success line naming the created account id (e.g.
              "Created broker account DU111 (id: <uuid>) — status inactive") with exit 0
              and NO credential values in stdout; the next invocation `msai broker list`
              returns a table whose rows include DU111 with its status + slot, no secret.
Persistence:  Run `msai broker list` again in a fresh invocation → DU111 still listed with
              the same id.
```

### UC-BA-UI-1 — Operator adds an account through the Settings wizard and it survives reload

```
Actor:        Signed-in operator on the Broker Accounts settings page
Scenario:     They just got a new IB login and want to add it through the UI wizard,
              confirm it appears in the fleet list, and trust it's still there tomorrow —
              and never see the password echoed back.
Interface:    UI
Intent:       The operator completes the add-account wizard and sees the new account in the
              list with credential metadata only, surviving a reload.
Setup:        Authenticate via the documented dev login/bypass; navigate to
              /settings/broker-accounts. (Do NOT pre-create the account.)
Steps:        Click "Add account" → step 1: enter ib_account_id + ib_login_key + trading
              mode → step 2: enter credentials (masked inputs) → step 3: review → Click
              "Create"
Verification: The operator sees a success toast naming the account, and the new row appears
              in the accounts table with status + slot; opening its detail shows credential
              METADATA only (secret ref + version + updated-at) with the password field
              masked/absent — never the cleartext.
Persistence:  Reload /settings/broker-accounts → the new account is still in the table at
              its position; the detail still shows metadata only.
```

---

## Tasks

> **Alembic head is `e5f6a7b8c9d0` (verified via `alembic heads`).** New revisions chain from it. Generate revision files with `uv run alembic revision -m "..."` (auto-sets `down_revision` to current head) — do NOT hand-pick revision ids.
> **Run all backend commands from `backend/` with `uv run`.** Tests use testcontainers Postgres (see `backend/tests/integration/conftest_portfolio_backtest.py`).

### Task 1: `BrokerAccount` model + enums

**Files:**

- Create: `backend/src/msai/models/broker_account.py`
- Test: `backend/tests/unit/test_broker_account_model.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_broker_account_model.py
from msai.models.broker_account import BrokerAccount, BrokerAccountStatus, CredentialsBackend


def test_broker_account_status_values():
    assert BrokerAccountStatus.ACTIVE == "active"
    assert BrokerAccountStatus.ARCHIVED == "archived"


def test_credentials_backend_values():
    assert CredentialsBackend.AZURE_KV == "azure_kv"
    assert CredentialsBackend.ENV == "env"
    assert CredentialsBackend.LEGACY_ENV == "legacy_env"


def test_broker_account_tablename_and_columns():
    assert BrokerAccount.__tablename__ == "broker_accounts"
    cols = BrokerAccount.__table__.columns.keys()
    for required in (
        "id", "ib_account_id", "ib_login_key", "label", "status",
        "gateway_slot", "trading_mode", "credentials_backend",
        "credentials_secret_ref", "credentials_secret_version",
        "credentials_updated_at", "credentials_updated_by",
        "credentials_last_accessed", "created_by", "created_at", "updated_at",
    ):
        assert required in cols, f"missing column {required}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_broker_account_model.py -v`
Expected: FAIL — `ModuleNotFoundError: msai.models.broker_account`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/msai/models/broker_account.py
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base, TimestampMixin
from msai.models.user import User


class BrokerAccountStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class CredentialsBackend(StrEnum):
    AZURE_KV = "azure_kv"        # prod: secret written to Azure Key Vault
    ENV = "env"                  # dev: file-backed writable store
    LEGACY_ENV = "legacy_env"    # migrated LVP/HVP: points at existing env keys, NULL version


class BrokerAccount(TimestampMixin, Base):
    """Operator-managed IB broker account. System of record for account identity,
    gateway-slot binding, and credential METADATA (never the secret itself)."""

    __tablename__ = "broker_accounts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # IB account identifier (U.../DU...). IMMUTABLE after create.
    ib_account_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ib_login_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[BrokerAccountStatus] = mapped_column(
        String(32), nullable=False, default=BrokerAccountStatus.ACTIVE
    )
    gateway_slot: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="paper")

    credentials_backend: Mapped[CredentialsBackend] = mapped_column(String(32), nullable=False)
    credentials_secret_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    credentials_secret_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # tz-aware to match the migration's DateTime(timezone=True) + the service's aware-UTC writes (Codex iter-7 P2)
    credentials_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    credentials_updated_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    credentials_last_accessed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    creator: Mapped[User | None] = relationship(lazy="selectin")
```

Also register the model so Alembic autogenerate + metadata see it: add `from msai.models.broker_account import BrokerAccount  # noqa: F401` to `backend/src/msai/models/__init__.py` (follow the existing import-aggregation pattern there).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_broker_account_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/msai/models/broker_account.py backend/src/msai/models/__init__.py backend/tests/unit/test_broker_account_model.py
git commit -m "feat(broker-accounts): add BrokerAccount model + status/backend enums"
```

### Task 2: Alembic CREATE TABLE migration

**Files:**

- Create: `backend/alembic/versions/<rev1>_create_broker_accounts.py` (via `alembic revision`)
- Test: `backend/tests/integration/test_broker_accounts_migration.py`

- [ ] **Step 1: Write the failing test** (schema assertions; mirror `test_alembic_migrations.py`)

```python
# backend/tests/integration/test_broker_accounts_migration.py
import sqlalchemy as sa
from sqlalchemy import inspect


def test_broker_accounts_table_created(migrated_engine):  # fixture runs `alembic upgrade head`
    insp = inspect(migrated_engine)
    assert "broker_accounts" in insp.get_table_names()
    cols = {c["name"]: c for c in insp.get_columns("broker_accounts")}
    assert "ib_account_id" in cols and not cols["ib_account_id"]["nullable"]
    assert "credentials_secret_ref" in cols and not cols["credentials_secret_ref"]["nullable"]
    assert "credentials_secret_version" in cols and cols["credentials_secret_version"]["nullable"]
    # partial-unique on active ib_account_id
    idx = {i["name"]: i for i in insp.get_indexes("broker_accounts")}
    assert any("ib_account_id" in i["column_names"] for i in idx.values())
```

Reuse/extend the migration-engine fixture from `backend/tests/integration/test_alembic_migrations.py` (PostgresContainer + `run_alembic(url, "upgrade", "head")`). If a shared fixture exists, import it; otherwise add a module-scoped one mirroring that file.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_broker_accounts_migration.py -v`
Expected: FAIL — table absent.

- [ ] **Step 3: Generate + write the migration**

```bash
cd backend && uv run alembic revision -m "create broker_accounts"
```

Then author `upgrade()` (mirror the CREATE-TABLE pattern in `v0q1r2s3t4u5_instrument_registry.py`):

```python
def upgrade() -> None:
    op.create_table(
        "broker_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("ib_account_id", sa.String(32), nullable=False),
        sa.Column("ib_login_key", sa.String(64), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("gateway_slot", sa.String(64), nullable=False),
        sa.Column("trading_mode", sa.String(16), nullable=False, server_default="paper"),
        sa.Column("credentials_backend", sa.String(32), nullable=False),
        sa.Column("credentials_secret_ref", sa.String(256), nullable=False),
        sa.Column("credentials_secret_version", sa.String(128), nullable=True),
        sa.Column("credentials_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credentials_updated_by", sa.String(256), nullable=True),
        sa.Column("credentials_last_accessed", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_broker_accounts_login", "broker_accounts", ["ib_login_key"])
    op.create_index("ix_broker_accounts_created_by", "broker_accounts", ["created_by"])
    # one ACTIVE row per ib_account_id; archived rows don't block re-add
    op.create_index(
        "uq_broker_accounts_active_ib_account_id", "broker_accounts", ["ib_account_id"],
        unique=True, postgresql_where=sa.text("status <> 'archived'"),
    )
    # one ACTIVE row per gateway_slot (slot is exclusively allocated)
    op.create_index(
        "uq_broker_accounts_active_gateway_slot", "broker_accounts", ["gateway_slot"],
        unique=True, postgresql_where=sa.text("status <> 'archived'"),
    )


def downgrade() -> None:
    op.drop_table("broker_accounts")
```

Confirm the generated file's `down_revision = "e5f6a7b8c9d0"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_broker_accounts_migration.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/*create_broker_accounts*.py backend/tests/integration/test_broker_accounts_migration.py
git commit -m "feat(broker-accounts): create broker_accounts table migration"
```

### Task 3: `BrokerCredentialsStore` interface + exception classifier

**Files:**

- Modify: `backend/pyproject.toml` (move azure deps from the optional `[azure]` extra into base `dependencies`)
- Modify: `backend/uv.lock` (regenerated by `uv sync` in Step 0 — Docker uses `uv sync --frozen` so the lock MUST reflect azure as a base dep; Codex iter-4 P1)
- Create: `backend/src/msai/services/live/broker_credentials_store.py`
- Test: `backend/tests/unit/test_broker_credentials_store.py`

- [ ] **Step 0: Move azure deps to base (Codex iter-3 P1).** `azure-identity` + `azure-keyvault-secrets` currently live in `[project.optional-dependencies] azure` (`pyproject.toml:54-55`), so they are NOT installed by the prod image (`Dockerfile:15` runs `uv sync --frozen --no-install-project --no-dev`, no `--extra azure`) NOR by a plain dev `uv sync` (verified: `import azure` → `ModuleNotFoundError` in the current venv). The classifier imports `azure.core.exceptions` at module top AND the classifier unit tests construct azure exception types — so azure must be present in dev, CI, AND prod. **Fix:** move both lines into the base `dependencies` array in `pyproject.toml` (delete the now-empty/azure-only optional group if it has nothing else), then run `cd backend && uv sync` so this worktree's venv actually has azure. The existing `AzureKeyVaultProvider` lazy-import + ImportError helper in `core/secrets.py` becomes dead-but-harmless. Prod's existing `uv sync --no-dev` then installs azure (base deps are included under `--no-dev`); no Dockerfile change required.

  Run after editing: `cd backend && uv sync && uv run python -c "import azure.keyvault.secrets, azure.identity; print('ok')"` → prints `ok`.

- [ ] **Step 1: Write the failing test** (the classifier — the highest-risk logic, per research finding #3)

```python
# backend/tests/unit/test_broker_credentials_store.py
import pytest
from azure.core.exceptions import (
    ClientAuthenticationError, HttpResponseError, ResourceNotFoundError, ServiceRequestError,
)
from msai.services.live.broker_credentials_store import classify_kv_exception, KvFailureReason


def _http(status):
    e = HttpResponseError(message="x")
    e.status_code = status
    return e


@pytest.mark.parametrize("exc,expected", [
    (ClientAuthenticationError("no token"), KvFailureReason.UNAUTHORIZED),
    (_http(401), KvFailureReason.UNAUTHORIZED),
    (_http(403), KvFailureReason.UNAUTHORIZED),
    (ResourceNotFoundError("missing"), KvFailureReason.NOT_FOUND),
    (_http(429), KvFailureReason.THROTTLED),
    (ServiceRequestError("dns"), KvFailureReason.UNREACHABLE),
    (_http(500), KvFailureReason.UNREACHABLE),  # unknown http → unreachable (fail-safe)
])
def test_classify_kv_exception(exc, expected):
    assert classify_kv_exception(exc) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_broker_credentials_store.py -v`
Expected: FAIL — module/symbol missing.

- [ ] **Step 3: Write minimal implementation** (interface + classifier; adapters in Tasks 4-5)

```python
# backend/src/msai/services/live/broker_credentials_store.py
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from azure.core.exceptions import (
    ClientAuthenticationError, HttpResponseError, ResourceNotFoundError, ServiceRequestError,
)


class KvFailureReason(StrEnum):
    UNAUTHORIZED = "kv_unauthorized"
    NOT_FOUND = "kv_not_found"
    THROTTLED = "kv_throttled"
    UNREACHABLE = "kv_unreachable"
    DECRYPT_FAILED = "decrypt_failed"  # malformed/undecodable secret payload or missing pinned version
                                       # (council reason list — decision doc line 257)


class CredentialResolutionError(RuntimeError):
    """Raised when reading/writing credentials fails. `reason` maps to SPAWN_FAILED_PERMANENT."""

    def __init__(self, reason: KvFailureReason, account_ref: str, message: str) -> None:
        self.reason = reason
        self.account_ref = account_ref
        super().__init__(f"[{reason}] {account_ref}: {message}")


@dataclass(frozen=True, slots=True)
class Credentials:
    tws_userid: str
    tws_password: str


@dataclass(frozen=True, slots=True)
class CredentialWriteResult:
    secret_ref: str
    version: str | None


def classify_kv_exception(exc: Exception) -> KvFailureReason:
    # Order matters: specific subclasses before broad HttpResponseError; ServiceRequestError
    # is NOT an HttpResponseError (research finding #3).
    if isinstance(exc, ResourceNotFoundError):
        return KvFailureReason.NOT_FOUND
    if isinstance(exc, ClientAuthenticationError):
        return KvFailureReason.UNAUTHORIZED
    if isinstance(exc, ServiceRequestError):
        return KvFailureReason.UNREACHABLE
    if isinstance(exc, HttpResponseError):
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return KvFailureReason.UNAUTHORIZED
        if status == 429:
            return KvFailureReason.THROTTLED
        return KvFailureReason.UNREACHABLE  # fail-safe for unknown HTTP errors
    return KvFailureReason.UNREACHABLE


@runtime_checkable
class BrokerCredentialsStore(Protocol):
    def put(self, account_ref: str, creds: Credentials, *, actor: str) -> CredentialWriteResult: ...
    def get(self, secret_ref: str, version: str | None) -> Credentials: ...
    def rotate(self, account_ref: str, creds: Credentials, *, actor: str) -> CredentialWriteResult: ...
    def delete(self, secret_ref: str) -> None: ...
    def ping(self) -> bool: ...  # boot reachability probe
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_broker_credentials_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/src/msai/services/live/broker_credentials_store.py backend/tests/unit/test_broker_credentials_store.py
git commit -m "feat(broker-accounts): BrokerCredentialsStore interface + KV exception classifier; move azure deps to base"
```

### Task 4: Dev file-backed adapter (`EnvFileBrokerCredentialsStore`)

**Files:**

- Modify: `.gitignore` (add `data/broker_credentials/` — FIRST step, Codex iter-3 P2; without it the dev cleartext store is committable)
- Modify: `backend/src/msai/services/live/broker_credentials_store.py`
- Test: `backend/tests/unit/test_broker_credentials_store.py`

- [ ] **Step 1: Write the failing test** (put → get(version) → rotate → get both versions → delete)

```python
def test_envfile_store_put_get_rotate_delete(tmp_path):
    from msai.services.live.broker_credentials_store import (
        EnvFileBrokerCredentialsStore, Credentials, CredentialResolutionError,
    )
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "creds.json")
    r1 = store.put("broker-cred-abc", Credentials("u1", "p1"), actor="op@x")
    assert r1.secret_ref == "broker-cred-abc" and r1.version is not None
    assert store.get(r1.secret_ref, r1.version) == Credentials("u1", "p1")
    r2 = store.rotate("broker-cred-abc", Credentials("u2", "p2"), actor="op@x")
    assert r2.version != r1.version
    assert store.get(r2.secret_ref, r2.version) == Credentials("u2", "p2")
    assert store.get(r1.secret_ref, r1.version) == Credentials("u1", "p1")  # old version retained
    store.delete(r1.secret_ref)
    with pytest.raises(CredentialResolutionError):
        store.get(r1.secret_ref, r2.version)
    assert store.ping() is True
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/unit/test_broker_credentials_store.py -k envfile -v` → FAIL.

- [ ] **Step 3: Write the adapter** (append to the module)

```python
import json
import uuid
from pathlib import Path
from threading import Lock


class EnvFileBrokerCredentialsStore:
    """Dev/test writable store. JSON file under DATA_ROOT (gitignored). Per-ref version
    list mirrors KV versioning so dev exercises the same get(ref, version) contract."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("{}")

    def _read(self) -> dict:
        return json.loads(self._path.read_text() or "{}")

    def _write(self, data: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self._path)  # atomic

    def put(self, account_ref, creds, *, actor):
        return self.rotate(account_ref, creds, actor=actor)

    def rotate(self, account_ref, creds, *, actor):
        version = uuid.uuid4().hex
        with self._lock:
            data = self._read()
            entry = data.setdefault(account_ref, {"versions": {}})
            entry["versions"][version] = {"u": creds.tws_userid, "p": creds.tws_password}
            entry["latest"] = version
            self._write(data)
        return CredentialWriteResult(secret_ref=account_ref, version=version)

    def get(self, secret_ref, version):
        if not version:  # mirror Azure pinned-version semantics (Codex iter-2 P2) — never "latest"
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED, secret_ref, "missing pinned secret version")
        data = self._read()
        entry = data.get(secret_ref)
        if entry is None:
            raise CredentialResolutionError(KvFailureReason.NOT_FOUND, secret_ref, "no such secret")
        rec = entry["versions"].get(version)
        if rec is None:
            raise CredentialResolutionError(KvFailureReason.NOT_FOUND, secret_ref, f"version {version} missing")
        return Credentials(rec["u"], rec["p"])

    def delete(self, secret_ref):
        with self._lock:
            data = self._read()
            data.pop(secret_ref, None)
            self._write(data)

    def ping(self):
        return self._path.parent.exists()
```

> **P0 (Codex iter-1 P0#1) — `.gitignore` MUST be updated in THIS task.** `data/` is NOT broadly ignored — `.gitignore` only lists selected subdirs (`data/parquet/`, `data/reports/`, `data/ib-gateway/`, …), so a cleartext `data/dev_broker_credentials.json` would be committable. The dev store path defaults under a dedicated, ignored subdir: `{DATA_ROOT}/broker_credentials/dev_broker_credentials.json`. Add to `.gitignore` as the FIRST step of this task (commit it):
>
> ```gitignore
> # dev broker credentials — cleartext IB logins; NEVER commit
> data/broker_credentials/
> ```
>
> And add a unit assertion / pre-commit-friendly check is out of scope, but the test in Step 1 must point the store at `tmp_path` so tests never touch the real ignored dir. The factory (Task 6) builds the path `Path(data_root) / "broker_credentials" / "dev_broker_credentials.json"`.

- [ ] **Step 4: Run test** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(broker-accounts): dev file-backed credentials store adapter"`.

### Task 5: Prod Azure KV adapter (`AzureKvBrokerCredentialsStore`)

**Files:**

- Modify: `backend/src/msai/services/live/broker_credentials_store.py`
- Test: `backend/tests/unit/test_broker_credentials_store.py`

- [ ] **Step 1: Write the failing test** (mock `SecretClient` — never hit live KV, per `testing.md`)

```python
def test_azure_kv_store_put_pins_returned_version_and_maps_errors(monkeypatch):
    from msai.services.live import broker_credentials_store as mod
    from msai.services.live.broker_credentials_store import (
        AzureKvBrokerCredentialsStore, Credentials, CredentialResolutionError, KvFailureReason,
    )
    from azure.core.exceptions import ResourceNotFoundError

    class FakeProps:
        version = "ver-xyz"

    class FakeSecret:
        def __init__(self): self.properties = FakeProps(); self.value = "u\x1fp"

    class FakeClient:
        def set_secret(self, name, value, **kw): return FakeSecret()
        def get_secret(self, name, version=None, **kw):
            if version == "missing": raise ResourceNotFoundError("nope")
            return FakeSecret()
        def begin_delete_secret(self, name):
            class P:
                def wait(self): pass
            return P()

    store = AzureKvBrokerCredentialsStore(client=FakeClient())
    res = store.put("broker-cred-1", Credentials("u", "p"), actor="op@x")
    assert res.version == "ver-xyz"  # pinned from properties.version, not parsed from .id
    assert store.get("broker-cred-1", "ver-xyz") == Credentials("u", "p")
    with pytest.raises(CredentialResolutionError) as ei:
        store.get("broker-cred-1", "missing")
    assert ei.value.reason == KvFailureReason.NOT_FOUND


def test_azure_kv_store_rejects_null_version_and_malformed_payload():
    from msai.services.live.broker_credentials_store import (
        AzureKvBrokerCredentialsStore, CredentialResolutionError, KvFailureReason,
    )

    class FakeProps:
        version = "v"

    class FakeMalformed:
        def __init__(self, value): self.properties = FakeProps(); self.value = value

    class FakeClient:
        def __init__(self, value): self._value = value
        def get_secret(self, name, version=None, **kw): return FakeMalformed(self._value)

    # null version → DECRYPT_FAILED (never silently read "latest")
    store_ok = AzureKvBrokerCredentialsStore(client=FakeClient("u\x1fp"))
    with pytest.raises(CredentialResolutionError) as e1:
        store_ok.get("broker-cred-1", None)
    assert e1.value.reason == KvFailureReason.DECRYPT_FAILED
    # malformed payload (no separator) → DECRYPT_FAILED, NOT an empty password
    store_bad = AzureKvBrokerCredentialsStore(client=FakeClient("no-separator"))
    with pytest.raises(CredentialResolutionError) as e2:
        store_bad.get("broker-cred-1", "v")
    assert e2.value.reason == KvFailureReason.DECRYPT_FAILED
```

- [ ] **Step 2: Run test to verify it fails** → FAIL.

- [ ] **Step 3: Write the adapter** (encode userid/password into the single secret value with a `\x1f` separator; wrap every SDK call in try/except → `CredentialResolutionError(classify_kv_exception(exc), ...)`).

```python
_SEP = "\x1f"  # unit separator — not valid in IB usernames/passwords


class AzureKvBrokerCredentialsStore:
    def __init__(self, client) -> None:  # client: azure.keyvault.secrets.SecretClient
        self._client = client

    def put(self, account_ref, creds, *, actor):
        return self.rotate(account_ref, creds, actor=actor)

    def rotate(self, account_ref, creds, *, actor):
        value = f"{creds.tws_userid}{_SEP}{creds.tws_password}"
        try:
            result = self._client.set_secret(account_ref, value, tags={"updated_by": actor})
        except Exception as exc:  # noqa: BLE001 — classified + re-raised
            raise CredentialResolutionError(classify_kv_exception(exc), account_ref, str(exc)) from exc
        return CredentialWriteResult(secret_ref=account_ref, version=result.properties.version)

    def get(self, secret_ref, version):
        if not version:  # pinned-version semantics: never silently read "latest"
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED, secret_ref, "missing pinned secret version")
        try:
            secret = self._client.get_secret(secret_ref, version)
        except Exception as exc:  # noqa: BLE001
            raise CredentialResolutionError(classify_kv_exception(exc), secret_ref, str(exc)) from exc
        raw = secret.value or ""
        if _SEP not in raw:  # malformed payload — do NOT return an empty password (Codex iter-1 P1#4)
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED, secret_ref, "secret payload missing separator")
        userid, _, password = raw.partition(_SEP)
        if not userid or not password:
            raise CredentialResolutionError(
                KvFailureReason.DECRYPT_FAILED, secret_ref, "secret payload has empty userid/password")
        return Credentials(userid, password)

    def delete(self, secret_ref):
        try:
            self._client.begin_delete_secret(secret_ref).wait()
        except Exception as exc:  # noqa: BLE001
            raise CredentialResolutionError(classify_kv_exception(exc), secret_ref, str(exc)) from exc

    def ping(self):
        # cheap reachability probe; list one secret property page
        try:
            iterator = self._client.list_properties_of_secrets()
            next(iter(iterator), None)
            return True
        except Exception:  # noqa: BLE001
            return False
```

- [ ] **Step 4: Run test** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(broker-accounts): Azure Key Vault credentials store adapter (version-pinned)"`.

### Task 6: Store factory + config setting + lifespan wiring (boot KV probe)

**Files:**

- Modify: `backend/src/msai/services/live/broker_credentials_store.py` (factory)
- Modify: `backend/src/msai/core/config.py` (add `broker_gateway_slots`)
- Modify: `backend/src/msai/main.py` (construct store + boot probe in `lifespan`)
- Modify: `docker-compose.prod.yml` (pass `AZURE_KEYVAULT_URI` into the backend + worker + live-supervisor env — Codex iter-2 P1)
- Modify: `scripts/msai-render-env.service` (add `AZURE_KEYVAULT_URI` to the rendered env so prod actually provides it)
- Modify: `.env.example` (document `AZURE_KEYVAULT_URI=`)
- Modify: `infra/main.bicep` (grant the VM managed identity **Key Vault Secrets Officer** — write — not just Secrets User; Codex iter-5 P1)
- Test: `backend/tests/unit/test_broker_credentials_store.py`

- [ ] **Step 1: Write the failing test** (factory selects dev adapter when ENVIRONMENT != production)

```python
def test_factory_selects_envfile_in_dev(monkeypatch, tmp_path):
    from msai.services.live.broker_credentials_store import (
        get_broker_credentials_store, EnvFileBrokerCredentialsStore,
    )
    store = get_broker_credentials_store(environment="development", data_root=tmp_path)
    assert isinstance(store, EnvFileBrokerCredentialsStore)
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement factory + config + wiring.**

Factory (append to module):

```python
def get_broker_credentials_store(*, environment: str, data_root, kv_uri: str | None = None,
                                 mi_client_id: str | None = None):
    if environment == "production":
        from azure.identity import ManagedIdentityCredential
        from azure.keyvault.secrets import SecretClient
        if not kv_uri:
            raise RuntimeError("AZURE_KEYVAULT_URI required in production for broker credentials")
        # NOTE: because this fails LOUD in prod, the deploy plumbing MUST provide the var —
        # see the prod-plumbing step below (Codex iter-2 P1). Backend would crashloop otherwise.
        # Codex iter-3 P1: use an EXPLICIT ManagedIdentityCredential — NOT DefaultAzureCredential.
        # The container already exports AZURE_CLIENT_ID as the Entra JWT audience; DefaultAzure-
        # Credential would consume that env var (managed_identity_client_id / EnvironmentCredential)
        # and misroute to the WRONG identity. ManagedIdentityCredential(client_id=None) = the VM's
        # system-assigned MI; pass an explicit user-assigned id ONLY via the dedicated
        # AZURE_KV_MI_CLIENT_ID env (NEVER the JWT AZURE_CLIENT_ID). The live KV spike (T17)
        # confirms which MI the prod vault grants.
        credential = ManagedIdentityCredential(client_id=mi_client_id) if mi_client_id \
            else ManagedIdentityCredential()
        client = SecretClient(vault_url=kv_uri, credential=credential)
        return AzureKvBrokerCredentialsStore(client=client)
    from pathlib import Path
    # gitignored subdir (Codex iter-1 P0#1) — see Task 4 .gitignore step
    return EnvFileBrokerCredentialsStore(
        path=Path(data_root) / "broker_credentials" / "dev_broker_credentials.json")
```

`config.py` — add near the other IB settings (~line 89-101):

**(Codex iter-5 P2 + iter-6 P2)** A bare `list[str]` pydantic-settings field JSON-decodes the env var **in the settings source, BEFORE any validator runs** — so a comma-separated `BROKER_GATEWAY_SLOTS=ib-gateway,ib-gateway-hvp` raises `SettingsError` before a `mode="before"` validator could split it (verified behavior on the installed `pydantic-settings 2.13.1`). Disable the pre-decode with `Annotated[list[str], NoDecode]`, THEN the validator handles both comma-strings and JSON-list strings:

```python
from typing import Annotated
from pydantic_settings import NoDecode

broker_gateway_slots: Annotated[list[str], NoDecode] = Field(
    # Default to the ONLY universally-present slot (prod compose defines just `ib-gateway`;
    # `ib-gateway-hvp` is dev-only — Codex iter-4 P1). Each env overrides via BROKER_GATEWAY_SLOTS:
    # dev Shape-B → "ib-gateway,ib-gateway-hvp"; prod → "ib-gateway".
    default=["ib-gateway"],
    validation_alias=AliasChoices("BROKER_GATEWAY_SLOTS"),
    description="Static pool of compose gateway service slots a broker account can occupy.",
)
```

```python
@field_validator("broker_gateway_slots", mode="before")
@classmethod
def _split_slots(cls, v: object) -> object:
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if s.startswith("["):  # JSON list form
            import json
            return json.loads(s)
        return [part.strip() for part in s.split(",") if part.strip()]
    return v
```

`main.py` `lifespan` — after the `gateway_router` line (243), add:

```python
app.state.broker_credentials_store = get_broker_credentials_store(
    environment=settings.environment,
    data_root=settings.data_root,
    kv_uri=os.environ.get("AZURE_KEYVAULT_URI"),
    mi_client_id=os.environ.get("AZURE_KV_MI_CLIENT_ID"),  # dedicated; NOT the JWT AZURE_CLIENT_ID
)
# Boot KV reachability probe (council blocking #6): fail-closed if KV unreachable AND any
# active live deployment exists.
if settings.environment == "production" and not app.state.broker_credentials_store.ping():
    if await _has_active_live_deployments(app):  # reuse existing active-deploy check
        raise RuntimeError("Broker credentials store unreachable at boot with active deployments")
    logger.warning("broker_credentials_store_unreachable_at_boot_no_active_deploys")
```

**(Codex iter-1 P1#5)** "Active" MUST match the real lifecycle set already used at `main.py:172`: `LiveDeployment.status.in_(("running", "ready", "starting", "building"))` — NOT just `"running"`. Define one module constant `ACTIVE_DEPLOYMENT_STATUSES = ("running", "ready", "starting", "building")` (or reuse the existing tuple in `main.py`) and use it for BOTH the boot probe here AND the archive-block in Task 9. A `starting`/`ready` deployment is in-flight and counts as active.

- [ ] **Step 3d: Prod KV-URI plumbing (Codex iter-2 P1 — required, else prod crashloops).** Because the factory fails LOUD when `AZURE_KEYVAULT_URI` is absent in production, the deploy config MUST supply it:
  - `docker-compose.prod.yml`: add `AZURE_KEYVAULT_URI: ${AZURE_KEYVAULT_URI:?Set AZURE_KEYVAULT_URI in .env}` to the backend service env (and to any worker / live-supervisor service that constructs the store), alongside the existing `AZURE_TENANT_ID`/`AZURE_CLIENT_ID` block (~line 142).
  - `scripts/msai-render-env.service`: append `AZURE_KEYVAULT_URI` to the `REQUIRED_SECRETS` list (line 30) so the env-render step renders it on the VM.
  - `.env.example`: add `AZURE_KEYVAULT_URI=` near the `AZURE_*` block (line 110) with a comment `# Key Vault URI for broker-account credentials (prod only)`, AND `AZURE_KV_MI_CLIENT_ID=` with `# OPTIONAL: user-assigned MI client id for KV; leave blank for system-assigned MI. NEVER reuse AZURE_CLIENT_ID (that's the JWT audience) — Codex iter-3 P1`.
  - `docker-compose.prod.yml`: also pass `AZURE_KV_MI_CLIENT_ID: ${AZURE_KV_MI_CLIENT_ID:-}` (optional/no `:?`) to the same services. Do NOT alias it to `AZURE_CLIENT_ID`.
- [ ] **Step 3e: Durable IaC write grant (Codex iter-5 P1 + iter-6 P1).** Under Option B' the BACKEND (VM managed identity) writes secrets, but `infra/main.bicep:654` currently grants the VM MI only **Key Vault Secrets User** (read). Fresh/rehearsal prod deploys would 403 on `set_secret`/`begin_delete_secret`. **Add a NEW, ADDITIONAL role assignment** `vmKvSecretsOfficerAssignment` granting the VM MI `roleDefIdKvSecretsOfficer` (already declared at `main.bicep:87`), with a NEW GUID seed `guid(vm.id, keyVault.id, 'kv-secrets-officer')`. **Do NOT mutate the existing `vmKvSecretsUserAssignment` in place** (Codex iter-6 P1) — Azure role assignments are effectively immutable by `name`; changing the `roleDefinitionId` on the same GUID-seeded assignment fails on reapply/disaster-recovery. Leave the existing Secrets User assignment as-is (harmless; Officer supersets it) and add the Officer assignment alongside it. Update the bicep comment block (lines 644-645) to note the VM MI now also holds Officer for server-side secret writes. (Least-privilege custom get+set+delete-no-purge role is a possible later refinement; Officer is acceptable since the design soft-deletes, never purges.)
- [ ] **Step 4: Run** → PASS; also `uv run pytest tests/unit/test_broker_credentials_store.py -v` all green.
- [ ] **Step 5: Commit** — `git commit -m "feat(broker-accounts): credentials-store factory + config slots + boot KV probe + prod AZURE_KEYVAULT_URI plumbing"`.

### Task 7: Metrics (spawn-failed counter + secret-age gauge)

**Files:**

- Create: `backend/src/msai/services/observability/broker_account_metrics.py`
- Test: `backend/tests/unit/test_broker_account_metrics.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_broker_account_metrics.py
def test_broker_account_metrics_registered_and_increment():
    from msai.services.observability import get_registry
    from msai.services.observability.broker_account_metrics import (
        SPAWN_FAILED, KV_SECRET_AGE,
    )
    SPAWN_FAILED.inc(account_id="DU1", reason="kv_unauthorized")
    KV_SECRET_AGE.set(123.0, account_id="DU1")
    rendered = get_registry().render()
    assert "msai_broker_account_spawn_failed_total" in rendered
    assert "msai_kv_secret_age_seconds" in rendered
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (mirror `trading_metrics.py` module-level pattern):

```python
# backend/src/msai/services/observability/broker_account_metrics.py
from msai.services.observability import get_registry

_r = get_registry()
SPAWN_FAILED = _r.counter(
    "msai_broker_account_spawn_failed_total",
    "Broker account spawn failures by account and KV failure reason",
)
KV_SECRET_AGE = _r.gauge(
    "msai_kv_secret_age_seconds",
    "Age in seconds of a broker account's stored credential secret (rotation enforcement)",
)
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(broker-accounts): spawn-failed counter + kv-secret-age gauge"`.

### Task 8: Pydantic schemas (no-secret-in-response)

**Files:**

- Create: `backend/src/msai/schemas/broker_account.py`
- Test: `backend/tests/unit/test_broker_account_schemas.py`

- [ ] **Step 1: Write the failing test** (the security-critical invariant: Response has no secret fields)

```python
# backend/tests/unit/test_broker_account_schemas.py
def test_response_schema_has_no_secret_fields():
    from msai.schemas.broker_account import BrokerAccountResponse
    fields = set(BrokerAccountResponse.model_fields)
    assert "tws_userid" not in fields and "tws_password" not in fields
    for meta in ("credentials_secret_ref", "credentials_secret_version", "credentials_updated_at"):
        assert meta in fields


def test_create_request_requires_credentials():
    from msai.schemas.broker_account import BrokerAccountCreateRequest
    f = BrokerAccountCreateRequest.model_fields
    assert "tws_userid" in f and "tws_password" in f and "ib_account_id" in f
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (mirror `schemas/live_portfolio.py` Create/Update/Response separation):

```python
# backend/src/msai/schemas/broker_account.py
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class BrokerAccountCreateRequest(BaseModel):
    ib_account_id: str = Field(max_length=32, pattern=r"^[A-Za-z0-9]+$")
    ib_login_key: str = Field(max_length=64)
    label: str | None = None
    trading_mode: str = Field(default="paper", pattern=r"^(paper|live)$")
    gateway_slot: str | None = None  # None → auto-allocate a free slot
    tws_userid: str = Field(min_length=1, max_length=256)
    tws_password: str = Field(min_length=1, max_length=512)


class BrokerAccountUpdateRequest(BaseModel):
    label: str | None = None
    trading_mode: str | None = Field(default=None, pattern=r"^(paper|live)$")
    # ib_account_id is IMMUTABLE — intentionally absent.


class BrokerAccountRotateCredentialsRequest(BaseModel):
    tws_userid: str = Field(min_length=1, max_length=256)
    tws_password: str = Field(min_length=1, max_length=512)


class BrokerAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    ib_account_id: str
    ib_login_key: str
    label: str | None
    status: str
    gateway_slot: str
    trading_mode: str
    credentials_backend: str
    credentials_secret_ref: str
    credentials_secret_version: str | None
    credentials_updated_at: datetime | None
    credentials_updated_by: str | None
    credentials_last_accessed: datetime | None
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(broker-accounts): request/response schemas (no secret in response)"`.

### Task 9: `BrokerAccountService` (create/list/get/update/rotate/archive + slot allocation)

**Files:**

- Create: `backend/src/msai/services/live/broker_account_service.py`
- Test: `backend/tests/integration/api/test_broker_accounts_api.py` (service exercised via API in Task 10; add a focused service-level integration test here too)

- [ ] **Step 1: Write the failing test** (write-store-first ordering + slot allocation + archive deletes secret)

```python
# backend/tests/integration/test_broker_account_service.py
import pytest
from msai.services.live.broker_account_service import BrokerAccountService, NoFreeSlotError
from msai.services.live.broker_credentials_store import EnvFileBrokerCredentialsStore, Credentials


@pytest.mark.asyncio
async def test_create_allocates_slot_writes_store_then_row(broker_db_session, tmp_path):
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a", "slot-b"])
    acct = await svc.create(
        ib_account_id="DU1", ib_login_key="L1", trading_mode="paper", gateway_slot=None,
        creds=Credentials("u", "p"), actor="op@x",
    )
    assert acct.gateway_slot in ("slot-a", "slot-b")
    assert acct.credentials_secret_ref == f"broker-cred-{acct.id}"
    assert acct.credentials_secret_version is not None
    assert store.get(acct.credentials_secret_ref, acct.credentials_secret_version) == Credentials("u", "p")


@pytest.mark.asyncio
async def test_create_raises_when_pool_exhausted(broker_db_session, tmp_path):
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["only-one"])
    await svc.create(ib_account_id="DU1", ib_login_key="L1", trading_mode="paper",
                     gateway_slot=None, creds=Credentials("u", "p"), actor="op@x")
    with pytest.raises(NoFreeSlotError):
        await svc.create(ib_account_id="DU2", ib_login_key="L2", trading_mode="paper",
                         gateway_slot=None, creds=Credentials("u", "p"), actor="op@x")


@pytest.mark.asyncio
async def test_archive_frees_slot_and_deletes_secret(broker_db_session, tmp_path):
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(ib_account_id="DU1", ib_login_key="L1", trading_mode="paper",
                            gateway_slot=None, creds=Credentials("u", "p"), actor="op@x")
    ref, ver = acct.credentials_secret_ref, acct.credentials_secret_version
    await svc.archive(acct.id, actor="op@x")
    # slot-a is now free → a new create succeeds
    acct2 = await svc.create(ib_account_id="DU2", ib_login_key="L2", trading_mode="paper",
                             gateway_slot=None, creds=Credentials("u2", "p2"), actor="op@x")
    assert acct2.gateway_slot == "slot-a"
    # archived account's secret is gone
    from msai.services.live.broker_credentials_store import CredentialResolutionError
    with pytest.raises(CredentialResolutionError):
        store.get(ref, ver)


@pytest.mark.asyncio
@pytest.mark.parametrize("dep_status", ["starting", "building", "ready", "running"])
async def test_archive_blocked_while_deployment_active(broker_db_session, tmp_path, dep_status):
    # Codex iter-4 P2: archive must 409 for ANY active lifecycle state, and NOT delete the secret.
    from msai.models.live_deployment import LiveDeployment
    from msai.services.live.broker_account_service import BrokerAccountService, AccountInUseError
    from msai.services.live.broker_credentials_store import EnvFileBrokerCredentialsStore, Credentials
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(ib_account_id="DU1", ib_login_key="L1", trading_mode="paper",
                            gateway_slot=None, creds=Credentials("u", "p"), actor="op@x")
    # ARRANGE a matching active deployment (direct insert — this is service-level setup, not the action under test)
    broker_db_session.add(LiveDeployment(
        account_id="DU1", ib_login_key="L1", status=dep_status,
        # …other NOT NULL LiveDeployment columns per the model (deployment_slug, etc.)
    ))
    await broker_db_session.commit()
    with pytest.raises(AccountInUseError):
        await svc.archive(acct.id, actor="op@x")
    # secret NOT deleted — archive aborted before the store.delete
    assert store.get(acct.credentials_secret_ref, acct.credentials_secret_version) == Credentials("u", "p")
    # and the account is still active
    refreshed = await svc.get(acct.id)
    assert refreshed.status == "active"
```

Add a `broker_db_session` fixture to `backend/tests/integration/conftest.py` (reuse the portfolio Postgres stack from `conftest_portfolio_backtest.py`; run `alembic upgrade head` so `broker_accounts` exists).

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement the service.** Key invariants:
  - `create`: pre-check duplicate active `ib_account_id` → derive `secret_ref = f"broker-cred-{new_uuid}"` → **write store first** (`store.put`) → then INSERT the row inside a **bounded slot-allocation retry loop** (Codex iter-1 P1#6 — the read-then-insert is racy): pick a free slot (in `self.slots`, not held by an ACTIVE row), attempt the INSERT, and on `IntegrityError` from the partial-unique slot index, `rollback` + re-read free slots + retry (≤ `len(self.slots)` attempts). If the conflict is on `ib_account_id` → `DuplicateAccountError`. If no free slot remains → `NoFreeSlotError`. **Only after a slot conflict exhausts all slots** do we `store.delete(secret_ref)` (no orphan secret); a transient row-INSERT failure also triggers best-effort `store.delete`. Mirror the `IntegrityError` retry/re-read pattern at `fleet_router.py:1968`.
  - `rotate`: `store.rotate` first → capture new version → UPDATE row's `credentials_secret_version` + `credentials_updated_at`/`by`. If store raises, row is untouched (half-rotation safety).
  - `update`: mutate `label`/`trading_mode` only; never `ib_account_id`.
  - `archive`: block FIRST if a deployment bound to this account is in any ACTIVE lifecycle state — query `live_deployments` by `account_id`/`ib_login_key` with `status.in_(ACTIVE_DEPLOYMENT_STATUSES)` (`("running","ready","starting","building")`, NOT just `running` — Codex iter-1 P1#5); raise `AccountInUseError` → 409. Then set `status=ARCHIVED` → `store.delete(secret_ref)` (skip the store delete for `legacy_env` rows — their material is compose env, not ours to delete) → frees the slot (the partial-unique index now ignores the archived row).
  - Duplicate active `ib_account_id` → pre-check AND catch the unique-violation `IntegrityError` → `DuplicateAccountError` (409).

```python
# backend/src/msai/services/live/broker_account_service.py  (skeleton — full bodies per invariants above)
from __future__ import annotations
from datetime import datetime, timezone
from uuid import UUID, uuid4
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from msai.models.broker_account import BrokerAccount, BrokerAccountStatus, CredentialsBackend
from msai.models.live_deployment import LiveDeployment
from msai.services.live.broker_credentials_store import BrokerCredentialsStore, Credentials


class BrokerAccountError(RuntimeError): ...
class NoFreeSlotError(BrokerAccountError): ...
class DuplicateAccountError(BrokerAccountError): ...
class AccountInUseError(BrokerAccountError): ...
class ImmutableFieldError(BrokerAccountError): ...
class AccountNotFoundError(BrokerAccountError): ...


class BrokerAccountService:
    def __init__(self, db: AsyncSession, store: BrokerCredentialsStore, slots: list[str],
                 backend: CredentialsBackend = CredentialsBackend.ENV) -> None:
        self._db, self._store, self._slots, self._backend = db, store, slots, backend

    async def _free_slot(self) -> str:
        rows = await self._db.execute(
            select(BrokerAccount.gateway_slot).where(BrokerAccount.status != BrokerAccountStatus.ARCHIVED)
        )
        taken = {r[0] for r in rows}
        for s in self._slots:
            if s not in taken:
                return s
        raise NoFreeSlotError(f"no free gateway slot in pool {self._slots}")

    async def create(self, *, ib_account_id, ib_login_key, trading_mode, gateway_slot,
                     creds: Credentials, actor: str) -> BrokerAccount:
        # duplicate active check
        existing = await self._db.execute(
            select(BrokerAccount).where(
                BrokerAccount.ib_account_id == ib_account_id,
                BrokerAccount.status != BrokerAccountStatus.ARCHIVED,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise DuplicateAccountError(f"active account {ib_account_id} already exists")
        if gateway_slot and gateway_slot not in self._slots:
            raise BrokerAccountError(f"unknown gateway slot {gateway_slot}")
        new_id = uuid4()
        secret_ref = f"broker-cred-{new_id}"
        write = self._store.put(secret_ref, creds, actor=actor)  # STORE FIRST
        # Bounded slot-allocation retry (Codex iter-1 P1#6): read-then-insert is racy, so
        # let the partial-unique index arbitrate and retry on conflict.
        try:
            for _attempt in range(len(self._slots) + 1):
                slot = gateway_slot or await self._free_slot()  # raises NoFreeSlotError if none
                acct = BrokerAccount(
                    id=new_id, ib_account_id=ib_account_id, ib_login_key=ib_login_key,
                    status=BrokerAccountStatus.ACTIVE, gateway_slot=slot, trading_mode=trading_mode,
                    credentials_backend=self._backend, credentials_secret_ref=write.secret_ref,
                    credentials_secret_version=write.version,
                    credentials_updated_at=datetime.now(timezone.utc), credentials_updated_by=actor,
                )
                self._db.add(acct)
                try:
                    await self._db.commit()
                except IntegrityError as exc:
                    await self._db.rollback()
                    if _is_account_id_conflict(exc):          # ib_account_id partial-unique
                        raise DuplicateAccountError(f"active account {ib_account_id} already exists") from exc
                    if gateway_slot is not None:              # caller pinned a slot that's now taken
                        raise BrokerAccountError(f"gateway slot {gateway_slot} is in use") from exc
                    continue                                  # slot race — re-read free slot, retry
                await self._db.refresh(acct)
                return acct
            raise NoFreeSlotError(f"no free gateway slot after retries in pool {self._slots}")
        except (DuplicateAccountError, NoFreeSlotError, BrokerAccountError):
            self._store.delete(secret_ref)  # no orphan secret on any terminal allocation failure
            raise
        except Exception:
            await self._db.rollback()
            self._store.delete(secret_ref)  # best-effort: no orphan secret
            raise

    # list / get / update / rotate / archive per invariants above …
    # _is_account_id_conflict(exc): inspect the IntegrityError's constraint name for
    # "uq_broker_accounts_active_ib_account_id" (mirror the constraint-name inspection at
    # fleet_router.py:1968) to distinguish ib_account_id vs gateway_slot conflicts.
```

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(broker-accounts): BrokerAccountService (slot alloc, store-first, archive)"`.

### Task 10: API router `/api/v1/broker-accounts`

**Files:**

- Create: `backend/src/msai/api/broker_accounts.py`
- Modify: `backend/src/msai/main.py` (register router)
- Test: `backend/tests/integration/api/test_broker_accounts_api.py`

- [ ] **Step 1: Write the failing test** (mirror UC-BA-API-1 + no-secret-leak + 201+Location + rotate)

```python
# backend/tests/integration/api/test_broker_accounts_api.py
import pytest


@pytest.mark.asyncio
async def test_create_list_get_no_secret_leak(api_client_authed):
    body = {"ib_account_id": "DU111", "ib_login_key": "L1", "trading_mode": "paper",
            "tws_userid": "u", "tws_password": "p"}
    r = await api_client_authed.post("/api/v1/broker-accounts", json=body)
    assert r.status_code == 201
    assert "Location" in r.headers
    created = r.json()
    assert created["ib_account_id"] == "DU111"
    assert "tws_userid" not in created and "tws_password" not in created
    assert created["credentials_secret_ref"] and created["credentials_secret_version"]
    acct_id = created["id"]
    # follow Location
    got = await api_client_authed.get(r.headers["Location"])
    assert got.status_code == 200 and got.json()["id"] == acct_id
    assert "tws_password" not in got.text
    lst = await api_client_authed.get("/api/v1/broker-accounts")
    assert any(a["id"] == acct_id for a in lst.json())  # bare list, matches portfolios.py:97


@pytest.mark.asyncio
async def test_rotate_advances_version(api_client_authed):
    body = {"ib_account_id": "DU222", "ib_login_key": "L2", "trading_mode": "paper",
            "tws_userid": "u", "tws_password": "p"}
    created = (await api_client_authed.post("/api/v1/broker-accounts", json=body)).json()
    v1 = created["credentials_secret_version"]
    rot = await api_client_authed.post(
        f"/api/v1/broker-accounts/{created['id']}/rotate-credentials",
        json={"tws_userid": "u2", "tws_password": "p2"})
    assert rot.status_code == 200
    v2 = (await api_client_authed.get(f"/api/v1/broker-accounts/{created['id']}")).json()["credentials_secret_version"]
    assert v2 and v2 != v1


@pytest.mark.asyncio
async def test_duplicate_active_account_conflicts(api_client_authed):
    body = {"ib_account_id": "DU333", "ib_login_key": "L3", "trading_mode": "paper",
            "tws_userid": "u", "tws_password": "p"}
    assert (await api_client_authed.post("/api/v1/broker-accounts", json=body)).status_code == 201
    assert (await api_client_authed.post("/api/v1/broker-accounts", json=body)).status_code == 409
```

(Wire `api_client_authed` so `app.state.broker_credentials_store` is the file-backed dev store + `broker_gateway_slots` has ≥2 slots — set in the test app factory/fixture.)

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement the router** (mirror `api/portfolios.py`: `get_current_user` + `get_db` deps, `resolve_user_id(db, claims)` for `created_by` and the claims email/sub for `credentials_updated_by` — Claude review C-P3-1; 201+Location; **list returns a bare `list[BrokerAccountResponse]`** to match the closest analog `portfolios.py:97` (NOT a `{items,…}` envelope — that shape isn't used by the sibling routers, Claude review C-P2-1); map service errors → HTTPException, **checking specific subclasses BEFORE the base** (they all subclass `BrokerAccountError`): `NoFreeSlotError`→409, `DuplicateAccountError`→409, `AccountInUseError`→409, `AccountNotFoundError`→404, then the BASE `BrokerAccountError`→422 (catch-all for unknown/occupied pinned `gateway_slot` — without this those raises become 500s, Codex iter-7 P2), and `CredentialResolutionError`→502 with the reason in the error body but NEVER the secret). Build the service with `app.state.broker_credentials_store`, `settings.broker_gateway_slots`, and the env/prod backend. Register in `main.py` alongside the other routers (~line 304-316).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(broker-accounts): /api/v1/broker-accounts CRUD + rotate + archive router"`.

### Task 11: Data-backfill migration (env-driven, idempotent)

**Files:**

- Create: `backend/alembic/versions/<rev2>_backfill_broker_accounts_legacy.py`
- Modify: `.env.example` + `docker-compose.dev.yml` + `docker-compose.prod.yml` (incl. the `migrate` service env) + `scripts/msai-render-env.service` (route `BROKER_ACCOUNT_BACKFILL` per environment)
- Test: extend `backend/tests/integration/test_broker_accounts_migration.py`

> **Why env-driven (Codex iter-4 P1):** the existing accounts + their gateway slots DIFFER per environment — LVP (`U4705114`) runs on the dev `ib-gateway` slot; HVP (`U4715997`) runs in PROD on the single `ib-gateway` slot (`docker-compose.prod.yml:501`). **`ib-gateway-hvp` exists ONLY in dev compose** (`docker-compose.dev.yml:308`), so a hardcoded `HVP→ib-gateway-hvp` backfill would seed a non-existent slot in prod. Per CLAUDE.md "the same login cannot run on local and prod at the same time" — these are different accounts on different machines. So the backfill is driven by a per-environment `BROKER_ACCOUNT_BACKFILL` env var; **empty default = no rows inserted** (safe). Each environment sets its own real account→slot mapping. This mirrors the existing env-driven `GATEWAY_CONFIG` convention.

- [ ] **Step 1: Write the failing test** (env-driven + idempotency + legacy_env backend + empty-default no-op)

```python
def test_backfill_is_noop_when_env_unset(migrated_engine):
    # default migrated_engine fixture runs `alembic upgrade head` with BROKER_ACCOUNT_BACKFILL unset
    import sqlalchemy as sa
    with migrated_engine.connect() as conn:
        n = conn.execute(sa.text("SELECT count(*) FROM broker_accounts")).scalar_one()
    assert n == 0  # empty default → safe no-op


def test_backfill_seeds_from_env_idempotently(migrated_engine_with_backfill_env):
    # this fixture sets BROKER_ACCOUNT_BACKFILL in the alembic subprocess env, e.g.
    #   "U4705114:lvp:ib-gateway:live:TWS_USERID|TWS_PASSWORD"  (U-prefix = LIVE; IB paper = DU/DF)
    # and runs `upgrade head` TWICE (asserting idempotency)
    import sqlalchemy as sa
    with migrated_engine_with_backfill_env.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT ib_account_id, gateway_slot, credentials_backend, credentials_secret_ref, "
            "credentials_secret_version FROM broker_accounts ORDER BY ib_account_id")).fetchall()
    assert [r[0] for r in rows] == ["U4705114"]          # exactly one, no duplicate on re-run
    assert rows[0][1] == "ib-gateway"                    # slot from the env entry (exists in this env)
    assert rows[0][2] == "legacy_env"
    assert rows[0][3] == "env:TWS_USERID|TWS_PASSWORD"   # paired ref
    assert rows[0][4] is None                            # legacy → no pinned version
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Generate + write the backfill** (`uv run alembic revision -m "backfill broker_accounts legacy"`; mirror `r6m7n8o9p0q1` idempotency). It parses `BROKER_ACCOUNT_BACKFILL` (comma-separated entries `ib_account_id:ib_login_key:gateway_slot:trading_mode:USERID_KEY|PASSWORD_KEY`); for each entry, INSERT a `legacy_env` row with `credentials_secret_ref="env:<USERID_KEY>|<PASSWORD_KEY>"` (paired — Codex iter-1 P1#7), `credentials_secret_version=NULL`. **Idempotent guard:** skip any `ib_account_id` that already has a non-archived row. Empty/unset env → no-op. The migration writes ROWS only — never reads/moves secret material (PRD AC).

```python
import os

def upgrade() -> None:
    conn = op.get_bind()
    raw = os.environ.get("BROKER_ACCOUNT_BACKFILL", "").strip()
    if not raw:
        return  # safe no-op default — each environment opts in via its own env var
    existing = {r[0] for r in conn.execute(sa.text(
        "SELECT ib_account_id FROM broker_accounts WHERE status <> 'archived'"))}
    for entry in (e.strip() for e in raw.split(",") if e.strip()):
        parts = entry.split(":")
        if len(parts) != 5:
            raise ValueError(f"BROKER_ACCOUNT_BACKFILL entry malformed: {entry!r}")
        ib_account_id, login, slot, mode, key_pair = parts
        if ib_account_id in existing:
            continue
        conn.execute(sa.text(
            "INSERT INTO broker_accounts "
            "(id, ib_account_id, ib_login_key, status, gateway_slot, trading_mode, "
            " credentials_backend, credentials_secret_ref, credentials_secret_version) "
            "VALUES (gen_random_uuid(), :a, :l, 'active', :s, :m, 'legacy_env', :ref, NULL)"),
            {"a": ib_account_id, "l": login, "s": slot, "m": mode, "ref": f"env:{key_pair}"})


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM broker_accounts WHERE credentials_backend = 'legacy_env'"))
```

- [ ] **Step 3b: Set the env var per environment AND route it to the prod migrate service.**
  - `.env.example`: add `BROKER_ACCOUNT_BACKFILL=` with a comment showing the format `ib_account_id:ib_login_key:gateway_slot:trading_mode:USERID_KEY|PASSWORD_KEY,...` + that empty = no backfill.
  - Dev `.env`/compose sets `U4705114:lvp:ib-gateway:live:TWS_USERID|TWS_PASSWORD` (add HVP only if the dev Shape-B `ib-gateway-hvp` slot is in use). Prod `.env` sets `U4715997:hvp:ib-gateway:live:TWS_USERID|TWS_PASSWORD` (HVP runs on the prod `ib-gateway` slot — NOT `ib-gateway-hvp`). Both U-prefix accounts are low-value LIVE test accounts (IB paper accounts are DU/DF), so `trading_mode=live` for both — the migration guard rejects a U-prefix + `paper` mismatch (nautilus gotcha #6).
  - **CRITICAL (Codex iter-5 P1):** prod runs migrations in the **one-shot `migrate` service** (`docker-compose.prod.yml:98`, `command: ["alembic","upgrade","head"]`) which has its OWN `environment:` block. It does NOT currently receive `BROKER_ACCOUNT_BACKFILL`, so the backfill would silently no-op in prod. Add `BROKER_ACCOUNT_BACKFILL: ${BROKER_ACCOUNT_BACKFILL:-}` to the `migrate` service's `environment:` in `docker-compose.prod.yml` (and dev compose's migration path if applicable), AND append `BROKER_ACCOUNT_BACKFILL` to `scripts/msai-render-env.service` `REQUIRED_SECRETS`/rendered env so the VM actually provides it. Verify the var is present in the migrate container env before relying on the seed.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(broker-accounts): env-driven idempotent legacy-account backfill migration"`.

### Task 12: `BrokerCredentialsStore.get` audit — stamp `credentials_last_accessed` + secret-age gauge

**Files:**

- Modify: `backend/src/msai/services/live/broker_account_service.py` (add `resolve_for_spawn`)
- Test: `backend/tests/integration/test_broker_account_service.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_resolve_for_spawn_stamps_last_accessed_and_returns_creds(broker_db_session, tmp_path):
    from msai.services.live.broker_credentials_store import EnvFileBrokerCredentialsStore, Credentials
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["slot-a"])
    acct = await svc.create(ib_account_id="DU1", ib_login_key="L1", trading_mode="paper",
                            gateway_slot=None, creds=Credentials("u", "p"), actor="op@x")
    creds = await svc.resolve_for_spawn(acct.id)
    assert creds == Credentials("u", "p")
    refreshed = await svc.get(acct.id)
    assert refreshed.credentials_last_accessed is not None


async def _insert_legacy_row(session, ref: str):
    """Insert a legacy_env BrokerAccount row directly (ARRANGE) and return its id (UUID)."""
    from msai.models.broker_account import BrokerAccount, BrokerAccountStatus, CredentialsBackend
    acct = BrokerAccount(
        ib_account_id="U4705114", ib_login_key="lvp", status=BrokerAccountStatus.ACTIVE,
        gateway_slot="ib-gateway", trading_mode="paper",
        credentials_backend=CredentialsBackend.LEGACY_ENV, credentials_secret_ref=ref,
        credentials_secret_version=None,
    )
    session.add(acct)
    await session.commit()
    await session.refresh(acct)
    return acct.id


@pytest.mark.asyncio
async def test_resolve_for_spawn_legacy_env_reads_paired_keys(broker_db_session, monkeypatch, tmp_path):
    # legacy_env row resolves BOTH paired env keys (Codex iter-1 P1#7)
    monkeypatch.setenv("TWS_USERID", "legacyuser")
    monkeypatch.setenv("TWS_PASSWORD", "legacypass")
    from msai.services.live.broker_credentials_store import EnvFileBrokerCredentialsStore, Credentials
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["ib-gateway"])
    account_id = await _insert_legacy_row(broker_db_session, ref="env:TWS_USERID|TWS_PASSWORD")
    creds = await svc.resolve_for_spawn(account_id)
    assert creds == Credentials("legacyuser", "legacypass")


@pytest.mark.asyncio
async def test_resolve_for_spawn_legacy_env_missing_material_fails_loud(broker_db_session, monkeypatch, tmp_path):
    monkeypatch.delenv("TWS_PASSWORD", raising=False)
    monkeypatch.setenv("TWS_USERID", "legacyuser")
    from msai.services.live.broker_credentials_store import (
        EnvFileBrokerCredentialsStore, CredentialResolutionError, KvFailureReason,
    )
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["ib-gateway"])
    account_id = await _insert_legacy_row(broker_db_session, ref="env:TWS_USERID|TWS_PASSWORD")
    with pytest.raises(CredentialResolutionError) as ei:
        await svc.resolve_for_spawn(account_id)
    assert ei.value.reason == KvFailureReason.NOT_FOUND


@pytest.mark.asyncio
async def test_resolve_for_spawn_rejects_null_version_for_non_legacy(broker_db_session, tmp_path):
    # an env-backed row with NULL version must fail closed (Codex iter-1 P1#4 / iter-2 P2)
    from msai.models.broker_account import BrokerAccount, BrokerAccountStatus, CredentialsBackend
    from msai.services.live.broker_credentials_store import (
        EnvFileBrokerCredentialsStore, CredentialResolutionError, KvFailureReason,
    )
    store = EnvFileBrokerCredentialsStore(path=tmp_path / "c.json")
    svc = BrokerAccountService(db=broker_db_session, store=store, slots=["ib-gateway"])
    acct = BrokerAccount(
        ib_account_id="DU9", ib_login_key="L9", status=BrokerAccountStatus.ACTIVE,
        gateway_slot="ib-gateway", trading_mode="paper",
        credentials_backend=CredentialsBackend.ENV, credentials_secret_ref="broker-cred-x",
        credentials_secret_version=None,  # invalid for a non-legacy row
    )
    broker_db_session.add(acct)
    await broker_db_session.commit()
    await broker_db_session.refresh(acct)
    with pytest.raises(CredentialResolutionError) as ei:
        await svc.resolve_for_spawn(acct.id)
    assert ei.value.reason == KvFailureReason.DECRYPT_FAILED
```

(The `_service_with_legacy_row` / `_service_with_kv_row` helpers insert a row directly via the
session + construct the service; keep them local to this test module.)

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `resolve_for_spawn(account_id)`**: load row, then branch on `credentials_backend`:
  - **`LEGACY_ENV`** (Codex iter-1 P1#7): parse the paired ref `env:<USERID_KEY>|<PASSWORD_KEY>` (strip the `env:` prefix, split on `|`) → read BOTH keys via `EnvSecretsProvider`; if EITHER is missing, `SPAWN_FAILED.inc(...reason="kv_not_found")` and raise `CredentialResolutionError(NOT_FOUND, ...)` naming the missing env key (PRD AC: clear failure when expected material is missing). `credentials_secret_version` is NULL here and that's expected.
  - **`AZURE_KV` / `ENV`**: if `credentials_secret_version is None`, fail-closed immediately — `CredentialResolutionError(DECRYPT_FAILED, ...)` (a non-legacy row MUST have a pinned version; Codex iter-1 P1#4). Otherwise `store.get(ref, version)` (pinned).
  - On any `CredentialResolutionError`: `SPAWN_FAILED.inc(account_id=..., reason=exc.reason)` and re-raise.
  - On success: UPDATE `credentials_last_accessed=now()` and set `KV_SECRET_AGE` gauge from `now - credentials_updated_at` (skip the gauge for `legacy_env`, which has no `credentials_updated_at`).

  This is the seam the supervisor WILL call once data-plane wiring lands (DEFERRED — see "Considered & deferred"); it is NOT wired into the supervisor in this PR. It is fully unit-tested here so the wiring PR can adopt it as-is.

- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(broker-accounts): resolve_for_spawn with last-accessed audit + secret-age gauge"`.

> **End of PR 3a backend scope.** Tasks 13-16 are PR 3b (CLI + UI). They depend only on the API (Task 10) being live.

### Task 13: CLI `broker` sub-app

**Files:**

- Modify: `backend/src/msai/cli.py`
- Test: `backend/tests/integration/test_broker_cli.py` (invoke via Typer's `CliRunner` against a mocked `_api_call`, OR as a thin smoke that asserts wiring; full behavior is the CLI E2E UC)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/integration/test_broker_cli.py
from typer.testing import CliRunner
from msai.cli import app


def test_broker_subapp_registered():
    result = CliRunner().invoke(app, ["broker", "--help"])
    assert result.exit_code == 0
    assert "add" in result.stdout and "list" in result.stdout


def test_broker_add_takes_password_from_env_not_argv(monkeypatch):
    # Codex iter-1 P0#2: password NEVER on argv (shell history / `ps` leak). It comes from
    # MSAI_BROKER_TWS_PASSWORD (scriptable) or an interactive getpass prompt.
    # Codex iter-1 P2#9: _api_call has signature (method, path, *, json_body=..., ...) -> httpx.Response.
    import httpx
    captured = {}

    def fake_api_call(method, path, *, json_body=None, **kw):
        captured["body"] = json_body
        return httpx.Response(201, json={"id": "uuid-1", "ib_account_id": "DU1",
                                         "status": "active", "gateway_slot": "slot-a"},
                              request=httpx.Request(method, f"http://test{path}"))

    monkeypatch.setattr("msai.cli._api_call", fake_api_call)
    monkeypatch.setenv("MSAI_BROKER_TWS_PASSWORD", "secretpw")
    result = CliRunner().invoke(app, ["broker", "add", "--ib-account-id", "DU1",
                                      "--ib-login-key", "L1", "--trading-mode", "paper",
                                      "--tws-userid", "u"])
    assert result.exit_code == 0
    assert "secretpw" not in result.stdout              # never echo the password
    assert "DU1" in result.stdout
    assert captured["body"]["tws_password"] == "secretpw"  # but it IS sent to the API body


def test_broker_add_has_no_password_flag():
    # the flag must not exist at all — no --tws-password on argv, ever
    result = CliRunner().invoke(app, ["broker", "add", "--help"])
    assert "--tws-password" not in result.stdout
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the `broker_app = typer.Typer(help="Broker account management")` + `app.add_typer(broker_app, name="broker")` (mirror `account_app`/`live_app`, cli.py:90-120). Commands: `add` (POST), `list` (GET, render a table), `show <id>` (GET one), `rotate <id>` (POST rotate), `archive <id> --yes` (confirmation gate like `live kill-all`). **Credential handling (Codex iter-1 P0#2):** `add` and `rotate` take `--tws-userid` as a flag but read the PASSWORD ONLY from `$MSAI_BROKER_TWS_PASSWORD` (for scripting/E2E) or, if unset and stdin is a TTY, an interactive `getpass.getpass()` prompt — **never a `--tws-password` flag** (argv leaks via shell history + `ps`). All commands proxy the REST API via `_api_call(method, path, json_body=...)` with `X-API-Key`; never print credential values.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(broker-accounts): msai broker CLI sub-app"`.

### Task 14: Frontend typed API client

**Files:**

- Create: `frontend/src/lib/api/broker-accounts.ts`
- Test: covered by the UI E2E UC (no separate vitest — per the `drop-vitest-for-one-off-pure-helpers` discipline; the client is thin wrappers over the shared `apiGet`/`apiPost`)

- [ ] **Step 1: Implement** (mirror `frontend/src/lib/api/live-portfolios.ts`): typed `BrokerAccount` interface (metadata only — no secret), `BrokerAccountCreate` (includes `tws_userid`/`tws_password` for the request), and functions `listBrokerAccounts(token)`, `getBrokerAccount(id, token)`, `createBrokerAccount(body, token)`, `rotateBrokerAccountCredentials(id, body, token)`, `archiveBrokerAccount(id, token)` using the shared `apiGet`/`apiPost` from `@/lib/api` with `encodeURIComponent` on `id`.
- [ ] **Step 2: Typecheck** — `cd frontend && pnpm tsc --noEmit` (or `pnpm lint`) → no errors.
- [ ] **Step 3: Commit** — `git commit -m "feat(broker-accounts): frontend typed API client"`.

### Task 15: Settings list + detail UI

**Files:**

- Create: `frontend/src/app/settings/broker-accounts/page.tsx`
- Create: `frontend/src/components/broker-accounts/broker-accounts-table.tsx`
- Create: `frontend/src/components/broker-accounts/broker-account-detail.tsx`

- [ ] **Step 1: Implement** (Product UI mode; mirror `app/live-trading/page.tsx` list + `useAuth().getToken()`):
  - Table with `data-testid="broker-accounts-table"`, a row per account (`data-testid={`broker-account-row-${id}`}`) showing ib_account_id, status badge, slot, trading_mode, credentials_secret_version (metadata). Empty state: "No broker accounts yet" + a primary "Add account" button.
  - Detail (sheet or `[id]` view): shows credential METADATA only (ref, version, updated-at/by, last-accessed) — NEVER a password field. Actions: "Rotate credentials" + "Archive" (Trust-First confirmation dialog stating consequences: "Archiving frees slot X and deletes the stored secret. This cannot trade after archiving.").
  - All states: loading skeleton, error (via `describeApiError`), empty, populated.
- [ ] **Step 2: Manual smoke** — `cd frontend && pnpm build` succeeds; page renders against the dev API.
- [ ] **Step 3: Commit** — `git commit -m "feat(broker-accounts): settings list + detail UI"`.

### Task 16: Add/Edit wizard dialog

**Files:**

- Create: `frontend/src/components/broker-accounts/broker-account-wizard.tsx`
- Modify: `frontend/src/app/settings/broker-accounts/page.tsx` (wire the "Add account" button to the wizard)

- [ ] **Step 1: Implement** (multi-step dialog; mirror `components/live/portfolio-start-dialog.tsx` stage machine): step 1 identity (ib_account_id, ib_login_key, trading_mode, optional slot), step 2 credentials (`type="password"` masked inputs, `data-testid="broker-account-tws-userid"`/`-password`), step 3 review (shows everything EXCEPT the password), Create → `createBrokerAccount`. Trust-First overrides: masked credential inputs, never pre-fill credentials on edit (edit shows metadata + a separate "Rotate credentials" sub-flow). Success → toast naming the account + refresh the list. Inline error via `describeApiError` (e.g. 409 duplicate, 409 no free slot).
- [ ] **Step 2: Manual smoke** — wizard completes against dev API; new row appears; reload persists.
- [ ] **Step 3: Commit** — `git commit -m "feat(broker-accounts): add/edit account wizard"`.

### Task 17: Operator pre-merge writable-KV spike (gated; documentation + checklist)

**Files:**

- Modify: `docs/decisions/multi-account-broker-fleet.md` (record spike result) OR `docs/runbooks/` note.

- [ ] **Step 1:** This is the council-flagged falsifying test, **NOT runnable in dev** (no KV). Document the exact operator steps in the PR body + a runbook note: on the prod VM (or a rehearsal RG), grant the managed identity "Key Vault Secrets Officer", then run a one-off script exercising `AzureKvBrokerCredentialsStore.put → get(version) → rotate → get(old) → delete` against the real vault; confirm version pinning + read-back. If it surfaces a blocker, fall back to the envelope-encryption Fourth Option. **This is an operator step surfaced explicitly before merge — not auto-runnable.** Record the result in the decision doc.
- [ ] **Step 2: Commit** — `git commit -m "docs(broker-accounts): record writable-KV spike procedure + RBAC requirement"`.

---

## Dispatch Plan

Sequential mode is NOT required, but most backend tasks share files (`broker_credentials_store.py`, `broker_account_service.py`, `main.py`) — encode via `Depends on`. Concurrency cap 3.

| Task ID | Depends on | Writes (concrete file paths)                                                                                                                                                                              |
| ------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T1      | —          | `backend/src/msai/models/broker_account.py`, `backend/src/msai/models/__init__.py`, `backend/tests/unit/test_broker_account_model.py`                                                                     |
| T2      | T1         | `backend/alembic/versions/*_create_broker_accounts.py`, `backend/tests/integration/test_broker_accounts_migration.py`                                                                                     |
| T3      | —          | `backend/pyproject.toml`, `backend/uv.lock`, `backend/src/msai/services/live/broker_credentials_store.py`, `backend/tests/unit/test_broker_credentials_store.py`                                          |
| T4      | T3         | `.gitignore`, `backend/src/msai/services/live/broker_credentials_store.py`, `backend/tests/unit/test_broker_credentials_store.py`                                                                         |
| T5      | T4         | `backend/src/msai/services/live/broker_credentials_store.py`, `backend/tests/unit/test_broker_credentials_store.py`                                                                                       |
| T6      | T5, T1     | `backend/src/msai/services/live/broker_credentials_store.py`, `backend/src/msai/core/config.py`, `backend/src/msai/main.py`, `docker-compose.prod.yml`, `scripts/msai-render-env.service`, `.env.example` |
| T7      | —          | `backend/src/msai/services/observability/broker_account_metrics.py`, `backend/tests/unit/test_broker_account_metrics.py`                                                                                  |
| T8      | —          | `backend/src/msai/schemas/broker_account.py`, `backend/tests/unit/test_broker_account_schemas.py`                                                                                                         |
| T9      | T1,T3,T4   | `backend/src/msai/services/live/broker_account_service.py`, `backend/tests/integration/test_broker_account_service.py`, `backend/tests/integration/conftest.py`                                           |
| T10     | T8,T9,T6   | `backend/src/msai/api/broker_accounts.py`, `backend/src/msai/main.py`, `backend/tests/integration/api/test_broker_accounts_api.py`                                                                        |
| T11     | T2         | `backend/alembic/versions/*_backfill_broker_accounts_lvp_hvp.py`, `backend/tests/integration/test_broker_accounts_migration.py`                                                                           |
| T12     | T9,T7      | `backend/src/msai/services/live/broker_account_service.py`, `backend/tests/integration/test_broker_account_service.py`                                                                                    |
| T13     | T10        | `backend/src/msai/cli.py`, `backend/tests/integration/test_broker_cli.py`                                                                                                                                 |
| T14     | T10        | `frontend/src/lib/api/broker-accounts.ts`                                                                                                                                                                 |
| T15     | T14        | `frontend/src/app/settings/broker-accounts/page.tsx`, `frontend/src/components/broker-accounts/broker-accounts-table.tsx`, `frontend/src/components/broker-accounts/broker-account-detail.tsx`            |
| T16     | T15        | `frontend/src/components/broker-accounts/broker-account-wizard.tsx`, `frontend/src/app/settings/broker-accounts/page.tsx`                                                                                 |
| T17     | T10        | `docs/decisions/multi-account-broker-fleet.md`                                                                                                                                                            |

Note: T6 and T10 both modify `main.py` → serialized (T10 depends on T6). T3/T4/T5/T6 all modify `broker_credentials_store.py` → strict chain. T9 and T12 both modify `broker_account_service.py` → T12 depends on T9. T15 and T16 both modify the settings page → T16 depends on T15.

---

## Self-Review

**Spec coverage:** US-1 Add → T1/T2/T8/T9/T10 (+CLI T13, UI T15/T16). US-2 List/Get + no-secret-leak → T8 (schema invariant test) + T10 (API test) + T15 (UI metadata-only). US-3 Edit/Rotate → T9/T10 (rotate, version advance, half-rotation) + T8 (update schema, no ib_account_id). US-4 Archive → T9 (frees slot + deletes secret + in-use block) + T15 (Trust-First confirm). US-5 Migrate → T11 (idempotent backfill, legacy_env, no material moved). US-6 Runtime safety/fail-loud → T3 (classifier) + T6 (boot probe) + T12 (resolve_for_spawn fail-loud + metrics). Council 8 conditions: #1 backend-writes-KV (T5/T9), #2 dev EnvFile no emulator (T4), #3 pinned version (T5/T8/T12), #4 dedicated store interface (T3), #5 audit cols (T1/T9), #6 alerts+boot probe (T6/T7/T12), #7 fail-LOUD SPAWN_FAILED reasons (T3/T12), #8 deletion+rotation semantics (T9 archive+rotate). E2E: API/CLI/UI all covered.

**Placeholder scan:** Router (T10), CLI (T13), and UI (T15/T16) tasks reference established analog files with exact paths rather than repeating boilerplate — acceptable per "follow established patterns"; the novel logic (model, classifier, store adapters, service ordering, migrations) has full code.

**Type consistency:** `Credentials(tws_userid, tws_password)`, `CredentialWriteResult(secret_ref, version)`, `KvFailureReason`, `BrokerAccountStatus`/`CredentialsBackend`, `secret_ref = f"broker-cred-{uuid}"`, `resolve_for_spawn` — names consistent across T3-T12. Service exceptions (`NoFreeSlotError`, `DuplicateAccountError`, `AccountInUseError`, `AccountNotFoundError`) consistent T9↔T10 error mapping.

**Plan-review iter-1 resolutions (Codex 2 P0 + 5 P1 + 2 P2; Claude 1 P2 + 1 P3 — all applied):**
P0#1 dev-creds file now under gitignored `data/broker_credentials/` + explicit `.gitignore` step (T4/T6).
P0#2 CLI password off argv → `$MSAI_BROKER_TWS_PASSWORD`/getpass, no `--tws-password` flag (T13, UC-CLI-1).
P1#3 control/data-plane boundary reframed — `resolve_for_spawn` ships tested-but-unwired; data-plane wiring deferred (header, briefing, deferred list, T12).
P1#4 `DECRYPT_FAILED` reason added; Azure adapter validates payload + rejects null version; `resolve_for_spawn` fails closed on null version for non-legacy (T3/T5/T12).
P1#5 active-deployment status set `("running","ready","starting","building")` for boot probe + archive block (T6/T9).
P1#6 slot allocation now an `IntegrityError`-retry loop mirroring `fleet_router.py:1968` (T9).
P1#7 legacy_env ref encodes paired `env:USERID|PASSWORD`; resolution + missing-material + null-version tests added (T11/T12).
P2#8 stale NOT-NULL deferral removed (already enforced by `t8o9p0q1r2s3`/`u9p0q1r2s3t4`).
P2#9 CLI test fixed to `_api_call(..., json_body=...) -> httpx.Response`.
C-P2-1 list returns bare `list[...]` (matches `portfolios.py:97`). C-P3-1 `resolve_user_id` for `created_by`.

**Remaining (not blockers):** the live writable-KV spike (T17) is operator-gated on prod KV access + the managed-identity RBAC widening (research Open Risk #2) — both surfaced explicitly in the PR body, not auto-runnable in dev.
