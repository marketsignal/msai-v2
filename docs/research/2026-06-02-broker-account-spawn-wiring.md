# Research: broker-account-spawn-wiring

**Date:** 2026-06-02
**Feature:** Add a nullable `LiveDeployment.broker_account_id` FK → `broker_accounts.id` and a pre-spawn control-plane validation gate that resolves exactly one active `BrokerAccount` before a live node spawns.
**Researcher:** research-first agent

This is a lean brief — the feature is mostly internal wiring on already-integrated libraries. One area is genuinely version/operation-sensitive (the additive FK migration on a busy prod table); the other two are confirm-no-change checks.

## Libraries Touched

| Library                     | Our Version | Latest Stable | Breaking Changes (vs ours)                                       | Source                                                                                                                                                    |
| --------------------------- | ----------- | ------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SQLAlchemy                  | 2.0.47      | 2.0.x         | None relevant (additive FK column)                               | [SQLAlchemy 2.0 ORM docs](https://docs.sqlalchemy.org/en/20/orm/relationship_persistence.html) (2026-06-02)                                               |
| Alembic                     | 1.18.4      | 1.18.x        | None relevant (`op.add_column` / `op.create_foreign_key` stable) | [Alembic Operations](https://alembic.sqlalchemy.org/en/latest/ops.html) (2026-06-02)                                                                      |
| PostgreSQL (target DDL)     | 16          | 16/17         | N/A (server)                                                     | [PostgreSQL 16 ALTER TABLE](https://www.postgresql.org/docs/16/sql-altertable.html) (2026-06-02)                                                          |
| NautilusTrader (IB adapter) | 1.223.0     | 1.223.x       | None relevant (node never consumes TWS creds)                    | installed `nautilus_trader/adapters/interactive_brokers/config.py` (2026-06-02)                                                                           |
| azure-keyvault-secrets      | 4.10.0      | 4.10.0        | None affecting read-only `get_secret`                            | [azure-keyvault-secrets CHANGELOG](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/keyvault/azure-keyvault-secrets/CHANGELOG.md) (2026-06-02) |

Note: `backend/pyproject.toml` pins floors (`sqlalchemy>=2.0.36`, `alembic>=1.14.0`, `nautilus_trader[ib]>=1.222.0`, `azure-keyvault-secrets>=4.9.0`); the versions above are what is actually installed in `backend/.venv` and are what this feature builds against.

---

## Per-Library Analysis

### PRIMARY — SQLAlchemy 2.0 + Alembic + PostgreSQL: additive nullable FK on a busy `live_deployments` table

**Versions:** SQLAlchemy ours=2.0.47; Alembic ours=1.18.4; PostgreSQL target=16.

**Breaking changes since ours:** None. `op.add_column`, `op.create_foreign_key`, `op.drop_constraint`, `op.drop_column`, and `mapped_column(ForeignKey(...))` are all stable in these versions (verified against the live Alembic ops docs and SQLAlchemy 2.0 docs, 2026-06-02).

**Deprecations:** None relevant to this feature.

#### Finding 1 — the migration lock profile (the version-sensitive part)

From the **PostgreSQL 16 `ALTER TABLE` docs** (quoted verbatim, accessed 2026-06-02):

- **`ADD COLUMN` (nullable, no volatile default):** metadata-only, no table rewrite. The docs: _"If no `DEFAULT` is specified, NULL is used. In neither case is a rewrite of the table required."_ So `op.add_column("live_deployments", sa.Column("broker_account_id", sa.Uuid(), nullable=True))` is fast and does **not** rewrite the table. (It still briefly takes an `ACCESS EXCLUSIVE` lock to update the catalog, but holds it only momentarily since there is no scan/rewrite.)

- **`ADD FOREIGN KEY` (the constraint, not the column):** _"Although most forms of `ADD table_constraint` require an `ACCESS EXCLUSIVE` lock, `ADD FOREIGN KEY` requires only a `SHARE ROW EXCLUSIVE` lock. Note that `ADD FOREIGN KEY` also acquires a `SHARE ROW EXCLUSIVE` lock on the referenced table"_ (i.e. on `broker_accounts` too). `SHARE ROW EXCLUSIVE` blocks writes (INSERT/UPDATE/DELETE) on **both** `live_deployments` and `broker_accounts` for the duration of the **validation scan** of existing rows.

- **`NOT VALID` + later `VALIDATE CONSTRAINT` (the lower-lock path):** _"The main purpose of the `NOT VALID` constraint option is to reduce the impact of adding a constraint on concurrent updates. With `NOT VALID`, the `ADD CONSTRAINT` command does not scan the table and can be committed immediately."_ Then _"validation acquires only a `SHARE UPDATE EXCLUSIVE` lock on the table being altered. (If the constraint is a foreign key then a `ROW SHARE` lock is also required on the table referenced by the constraint.)"_ `SHARE UPDATE EXCLUSIVE` and `ROW SHARE` do **not** block concurrent writes.

**Practical sizing:** `live_deployments` is a control-plane table (one row per logical deployment, not a high-volume event table — it is keyed by `identity_signature` and dedup'd on warm restart per `models/live_deployment.py:88-94`). It has _some_ prod rows but is small and low-write. For a table of this size the single-statement `op.create_foreign_key(...)` (default = scan + `SHARE ROW EXCLUSIVE`) completes near-instantly and the brief write-block is operationally negligible.

**Alembic mechanics:** Alembic does **not** expose a first-class `NOT VALID` flag (confirmed against the Operations docs, 2026-06-02). The two-step pattern, when warranted, is done with raw SQL inside the migration:

```python
op.execute(
    "ALTER TABLE live_deployments "
    "ADD CONSTRAINT fk_live_deployments_broker_account "
    "FOREIGN KEY (broker_account_id) REFERENCES broker_accounts(id) "
    "ON DELETE RESTRICT NOT VALID"
)
op.execute("ALTER TABLE live_deployments VALIDATE CONSTRAINT fk_live_deployments_broker_account")
```

(The `VALIDATE` step should be a separate statement; if you want zero write-block it can even be a separate migration, but that is overkill at this table's size.)

#### Finding 2 — `ondelete` choice for "broker account must not vanish under a deployment"

Reality in this codebase (verified): `broker_accounts` rows are **soft-deleted, never hard-deleted** — `BrokerAccount.status` flips to `'archived'`, enforced by the partial-unique indexes `uq_broker_accounts_active_*` (`alembic/versions/d87c2aa5f751_create_broker_accounts.py:65-80`). There is no hard-`DELETE` path in PR 3's CRUD. So `ondelete` is a **defense-in-depth backstop against an out-of-band manual `DELETE`**, not a runtime path.

PostgreSQL `ondelete` semantics for a parent (`broker_accounts`) row referenced by a child (`live_deployments`):

- **`RESTRICT`** — refuses the parent delete immediately if any child references it (checks at statement time; not deferrable).
- **`NO ACTION`** (PostgreSQL default) — same refusal, but the check can be deferred to end-of-transaction with `DEFERRABLE`.
- **`SET NULL`** — allows the parent delete and nulls the child's FK. **Wrong here:** it would silently sever a deployment's audit linkage to the account it traded — directly contradicts PRD §6 _"a deployment's bound `BrokerAccount` … must be traceable."_

**Recommended:** `ondelete="RESTRICT"` (or `NO ACTION` — functionally equivalent for our non-deferred case). It guarantees an accidental/manual hard-delete of an account that still has deployment rows fails loudly instead of corrupting the audit trail. SQLAlchemy passes `ondelete=...` through verbatim to the DDL; the repo already uses this idiom (`ForeignKey("users.id", ondelete="SET NULL")` at `d87c2aa5f751:46`). No `passive_deletes` tuning is needed because the deployment side is not cascading deletes.

#### Finding 3 — SQLAlchemy 2.0 model + relationship shape

Match the existing `live_deployment.py` style (other FKs there use `ForeignKey("...")` + `index=True`, `nullable=True/False`):

```python
broker_account_id: Mapped[UUID | None] = mapped_column(
    ForeignKey("broker_accounts.id", ondelete="RESTRICT"),
    index=True,        # database.md rule: ALWAYS index FK columns
    nullable=True,     # additive + nullable per PRD non-goal "not NOT NULL this PR"
)
```

A `relationship()` to `BrokerAccount` is optional (only add it if a caller needs ORM navigation). If added, use `lazy="selectin"` per the repo's `database.md` eager-load rule to avoid N+1, and a `TYPE_CHECKING` import for the annotation (the file already does this for `LivePortfolioRevision`/`Strategy`/`User`).

#### Finding 4 — rollback safety

A nullable additive FK is rollback-safe per the repo's additive-only migration discipline (`database.md` — "✅ ADD nullable foreign key"). The `downgrade()` drops the constraint then the column:

```python
def downgrade() -> None:
    op.drop_constraint("fk_live_deployments_broker_account", "live_deployments", type_="foreignkey")
    op.drop_column("live_deployments", "broker_account_id")
```

`type_="foreignkey"` is required for cross-dialect correctness (the Alembic docs flag this for `drop_constraint`). Because the column is nullable and the legacy `account_id`/`ib_login_key` string columns are untouched (PRD non-goal), prior-release code that ignores `broker_account_id` keeps working after a SHA rollback — exactly the deploy-pipeline rollback contract in `CLAUDE.md`.

**Sources:**

1. [PostgreSQL 16 — ALTER TABLE (lock levels, NOT VALID, VALIDATE CONSTRAINT)](https://www.postgresql.org/docs/16/sql-altertable.html) — accessed 2026-06-02
2. [Alembic — Operations reference (`op.add_column`, `op.create_foreign_key`, `op.drop_constraint`, `op.drop_column`)](https://alembic.sqlalchemy.org/en/latest/ops.html) — accessed 2026-06-02
3. [SQLAlchemy 2.0 — relationship persistence / FK ON DELETE](https://docs.sqlalchemy.org/en/20/orm/relationship_persistence.html) — accessed 2026-06-02
4. Repo precedent: `backend/alembic/versions/d87c2aa5f751_create_broker_accounts.py:43-80` and `backend/src/msai/models/live_deployment.py:64-125` — read 2026-06-02

**Design impact:**

- Add `broker_account_id` as `op.add_column(... nullable=True)` (metadata-only, no rewrite) **separately** from the FK constraint.
- Use **`ondelete="RESTRICT"`** (NOT `SET NULL`) — preserves the deployment↔account audit linkage the PRD requires; safe because accounts are only ever soft-archived, so RESTRICT never fires on the normal path and only backstops manual hard-deletes.
- A single-statement `op.create_foreign_key(...)` is acceptable at this table's size (brief `SHARE ROW EXCLUSIVE`, no rewrite). Offer the `NOT VALID` + `VALIDATE CONSTRAINT` (raw `op.execute`) two-step in the plan as the documented option **only if** the operator wants zero write-block; do not over-engineer it for a small low-write control-plane table.
- `index=True` on the FK column (repo `database.md` rule).
- Downgrade = `drop_constraint(..., type_="foreignkey")` then `drop_column(...)`; column stays nullable and legacy string columns untouched → rollback-safe.

**Test implication:**

- Migration round-trip test: `upgrade` adds nullable `broker_account_id` + FK constraint; `downgrade` removes both and leaves `account_id`/`ib_login_key` intact (additive-only proof).
- A test inserting a `live_deployments` row with `broker_account_id = NULL` succeeds (legacy/grandfather path still works — PRD US-001 edge case "Legacy existing deployment (no FK)").
- A test that a hard `DELETE` of a `broker_accounts` row referenced by a deployment **raises** (RESTRICT backstop) — proves the audit-linkage guarantee. (This is the one behavior that distinguishes RESTRICT from SET NULL; without this test the ondelete choice is unverified.)
- A test that an active-account resolution sets `broker_account_id` to the resolved id and that the column is queryable/eager-loadable without N+1 if a `relationship()` is added.

---

### CONFIRM-NO-CHANGE 1 — NautilusTrader IB adapter (node does not consume TWS credentials)

**no-change confirmed.** Read directly from the installed source `backend/.venv/.../nautilus_trader/adapters/interactive_brokers/config.py` (NautilusTrader **1.223.0**, accessed 2026-06-02):

- `InteractiveBrokersExecClientConfig` (line 246) fields: `ibg_host`, `ibg_port`, `ibg_client_id`, `account_id: str | None`, `dockerized_gateway`, `connection_timeout`, `request_timeout_secs`, `fetch_all_open_orders`, `track_option_exercise_from_position_update`. It takes **`account_id`** but **no `username`/`password`/`tws_password`** field.
- `InteractiveBrokersDataClientConfig` (line 198) likewise has no credential fields.
- The only `username`/`password` fields in the module belong to `DockerizedIBGatewayConfig` (lines 42-63), which is the optional **gateway-launcher** helper — exactly the env/render credential path the PRD declares unchanged (Non-Goal: _"Injecting TWS credentials into the TradingNode/TradingNodePayload (the node never consumes them)"_).

The "node-doesn't-consume-creds" premise holds for the pinned version. This feature changes no node config. **Source:** installed `config.py:198-291` (2026-06-02).

### CONFIRM-NO-CHANGE 2 — Azure Key Vault (read-only `get_secret` at deploy time)

**no-change confirmed.** azure-keyvault-secrets installed = **4.10.0** (pyproject floor `>=4.9.0`). Per the official CHANGELOG (accessed 2026-06-02), there are **no breaking changes to `SecretClient.get_secret`** between 4.9.0 and 4.10.0. The only consumer-visible change in 4.10.0 is dropping Python 3.8 support (we run 3.12, unaffected) and bumping the default service API version to 7.6 (a backend wire-version change that does not alter the `get_secret` call shape or return type). PR 3's `resolve_for_spawn` reads version-pinned secrets read-only; that call is unaffected. **Source:** [azure-keyvault-secrets CHANGELOG](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/keyvault/azure-keyvault-secrets/CHANGELOG.md) (2026-06-02).

---

## Not Researched (with justification)

- **FastAPI / Pydantic / asyncpg** — the new API request/response shape and the resolve-to-one-active-account logic use these libraries exactly as the existing `/live/start-portfolio` path already does; no version-sensitive new usage. Standard coverage applies.
- **Redis / arq (supervisor command bus)** — the spawn-enqueue path is unchanged by this feature (PRD Success Metric: "No KV read on the supervisor critical path"; validation happens at deploy time, not in the supervisor). No new library usage.

## Open Risks

- **Existing-row backfill / grandfather map (PRD Open Question §7):** rows with the legacy `"default"` sentinel `account_id` must stay `broker_account_id = NULL` unless an unambiguous single active `BrokerAccount` matches. This is a data-mapping decision, **not** a library risk — but the migration must NOT attempt an automatic non-unique backfill (would violate "resolve to exactly one active account or fail closed"). Leave NULL on ambiguity; resolve in design.
- **Concurrent-write window during FK validation:** if the operator runs the single-statement `create_foreign_key` while a deploy/stop is mid-write, the `SHARE ROW EXCLUSIVE` lock briefly blocks that write. Negligible at this table's size, but the migration should run during a quiet window OR use the `NOT VALID`+`VALIDATE` two-step if the operator wants zero contention. Documented, not blocking.
- **`ondelete` is a backstop, not a runtime guarantee:** RESTRICT only fires on a hard `DELETE`, which PR 3 never issues (soft-archive only). It does not prevent _archiving_ an account that has live deployments — that "can't deploy against an archived account" guarantee is enforced by the **application-level validation gate** (PRD US-002), not the FK. Design must not conflate the two.
- **Validation-gate KV transient failure (PRD US-002 edge case):** a deploy-time `get_secret` timeout must fail closed with a retryable error and must NOT touch the supervisor warm-restart path. No library-version risk; correctness lives in the gate's error handling.
