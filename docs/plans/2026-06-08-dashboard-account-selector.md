# Dashboard Account Selector — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global dashboard account selector (view-scope only), an explicit confirmed deploy-target with a server-side real-money identity gate enforced identically on UI/API/CLI, and an explicit `account_class`/`is_real_money` attribute on `BrokerAccount` so real-money status is never inferred from the account-id string.

**Architecture:** Backend adds an additive `account_class` String-enum column to `broker_accounts` (paper/test/real) + a derived `is_real_money`, surfaced in `BrokerAccountResponse`. `PortfolioStartRequest` gains an optional `confirm_account_id` (body-only preconditions via `@model_validator`); the DB-dependent identity match runs as an `HTTPException(422)` in `live_start_portfolio` right after `_resolve_effective_account` (one seam ⇒ enforced for UI/API/CLI). Frontend mounts a React Context (persisted via `usehooks-ts.useLocalStorage`, default "all") inside `AppShell`, renders a shadcn `Select` in `Header`, filters the live-trading fleet by `account_id`, and replaces the `PortfolioStartDialog` free-text/prefix-parse with a registry-backed explicit target picker that sends `confirm_account_id`.

**Tech Stack:** Python 3.12 / FastAPI / Pydantic V2 / SQLAlchemy 2.0 / Alembic / Typer; Next.js 15 App Router / React 19 / shadcn (radix-ui) `Select` / `usehooks-ts` / @tanstack/react-query.

**Source of truth:** PRD `docs/prds/dashboard-account-selector.md`; council verdict `docs/prds/dashboard-account-selector-discussion.md` Round 2; research `docs/research/2026-06-08-dashboard-account-selector.md`.

---

## Resolved design decisions (flagged for Codex plan-review scrutiny)

**D1 — `account_class` enum, not a boolean.** New column `account_class` is a `StrEnum` (`paper` / `test` / `real`) mapped onto `String(16)` with `server_default="test"`, matching the existing `status`/`trading_mode` convention (`models/broker_account.py:46-50`) and sidestepping the Alembic native-PG-enum `CREATE TYPE` gotcha (research §4). A derived `is_real_money` property (`account_class == AccountClass.REAL`) is the single fact the gate + UI read. Rationale: future-proof for PR5 risk overlays; the gate only needs the boolean projection.

**D2 — "real-money" (echo-gated) == the production fund only.** The identity-echo gate fires when `account_class == real`. LVP/HVP are `test` (live IB mode, limited capital, NOT the fund) → explicit target still required, standard confirm, **no** identity echo. This matches the PRD US-003 edge case verbatim ("Target is a test (non-real-money) account → Standard confirm, no identity echo required") and the council finding that "only the id string separates the fund". **Codex: challenge if the safety posture should extend the echo to ALL live accounts (test+real); cost is one extra typed confirm on the frequently-run LVP/HVP drills.**

**D3 — Confirm UX = typed account-id echo.** The real-money branch requires the operator to type the resolved `ib_account_id`, reusing the dialog's existing `confirmMatches` muscle (`portfolio-start-dialog.tsx:370-374`); that typed value is sent as `confirm_account_id` so the server is the authority. Test/paper deploys require only an explicit target pick (no echo).

**D4 — Gate applies only when a registry row resolved (`broker_account is not None`).** The genuine pre-registry legacy warm-restart path (`_resolve_effective_account` returns `broker_account=None`, `api/live.py:617-621`) keeps today's behavior (no echo) — the fund is always a registered account, so this cannot weaken fund protection, and it preserves warm-restart back-compat (research open-risk #5).

**D5 — `account_class` on create is explicit-with-safe-default.** `BrokerAccountCreateRequest.account_class: AccountClass | None = None`. When omitted: derive `paper` if `trading_mode == "paper"` else `test`. Explicitly passing `real` requires `trading_mode == "live"` (paper can't be real money → 422). The fund is registered (post-PR-3) by explicitly passing `account_class="real"`.

**D6 — Divergence audit is server-side + additive.** `PortfolioStartRequest` gains an optional `selector_context_account_id: str | None`. The UI populates it with the active global-selector value at deploy time. The handler emits a `DEPLOY_TARGET_DIVERGENCE` metric + structured log when the context is a **concrete account** (NOT `"all"` / `"unassigned"` — those are not a focused target, so deploying to a concrete account while viewing "All" is not a divergence) AND differs from the resolved effective account. API/CLI omit it (no global selector). Satisfies council objection #7 durably (server-side), not client-only. (Codex plan-review iter-1 P2: "all" must be excluded — done here + in Task 5.)

---

## File Structure

**Backend (create):**

- `backend/alembic/versions/<rev>_add_account_class_to_broker_accounts.py` — additive column + backfill.

**Backend (modify):**

- `backend/src/msai/models/broker_account.py` — `AccountClass` enum, `account_class` column, `is_real_money` property.
- `backend/src/msai/schemas/broker_account.py` — `account_class` + `is_real_money` on response; `account_class` on create + validator.
- `backend/src/msai/services/live/broker_account_service.py` — `create()` threads `account_class` into the `BrokerAccount(...)` construction (iter-1 P1#1; sig at `:232`, construct at `:354`).
- `backend/src/msai/api/broker_accounts.py` — `create_broker_account` passes `body.account_class` to `svc.create(...)` (`:101`).
- `backend/src/msai/schemas/live.py` — `confirm_account_id` + `selector_context_account_id` on `PortfolioStartRequest` + body-only validator.
- `backend/src/msai/api/live.py` — real-money identity gate after `_resolve_effective_account`; divergence metric.
- `backend/src/msai/services/observability/broker_account_metrics.py` — new `DEPLOY_TARGET_DIVERGENCE = _r.counter(...)` next to `DEPLOY_VALIDATION_FAILED` (`:56`) (iter-1 P2#6 — correct module + factory).
- `backend/src/msai/api/account.py` — add connected `account_id` to `/account/health` response (iter-1 P2#9).
- `backend/src/msai/cli.py` — `--confirm-account-id` on `live start-portfolio` AND `live start` (both call `_resolve_cli_account_payload`) (iter-1 P2#8).

**Frontend (create):**

- `frontend/src/lib/account-scope.tsx` — `AccountScopeProvider` + `useAccountScope` (Context + `useLocalStorage`).
- `frontend/src/components/layout/account-selector.tsx` — `AccountSelector` widget (shadcn `Select`).

**Frontend (modify):**

- `frontend/src/components/layout/app-shell.tsx` — wrap shell in `AccountScopeProvider`.
- `frontend/src/components/layout/header.tsx` — render `AccountSelector` in the left zone.
- `frontend/src/app/live-trading/page.tsx` — filter `deployments` by scoped account.
- `frontend/src/app/dashboard/page.tsx` — scope `runningCount` + `ActiveStrategies` deployments by the selector (iter-1 P1#2; uses unfiltered `deployments` at `:64-65,135`).
- `frontend/src/lib/api.ts` — `startPortfolio` body gains `broker_account_id?`, `confirm_account_id?`, `selector_context_account_id?`; `AccountSummary`/health type gains connected `account_id`.
- `frontend/src/lib/api/broker-accounts.ts` — `BrokerAccount` type gains `account_class`, `is_real_money`.
- `frontend/src/components/live/portfolio-start-dialog.tsx` — registry-backed explicit target picker; All/Unassigned disable Deploy; real-money typed echo → `confirm_account_id`; human labels.
- `frontend/src/components/broker-accounts/{broker-accounts-table,broker-account-wizard,broker-account-detail}.tsx` — real-money badge/label keys off `is_real_money`, NOT `trading_mode==="live"`; wizard gains an account-class control so the fund is registered as `real` (iter-1 P1#4).
- `frontend/src/app/dashboard/page.tsx` balance/account cards — label connected gateway account id (iter-1 P2#9 / US-005).

**Tests (create/modify):** see each task + the E2E section.

---

## Task 1: `AccountClass` enum + `account_class` column + `is_real_money` on the model

**Files:**

- Modify: `backend/src/msai/models/broker_account.py`
- Test: `backend/tests/unit/test_broker_account_model.py`

- [ ] **Step 1: Write the failing test** (append to `test_broker_account_model.py`)

```python
def test_account_class_column_present_and_string_backed() -> None:
    from msai.models.broker_account import BrokerAccount
    col = {c.name: c for c in BrokerAccount.__table__.columns}["account_class"]
    assert col.nullable is False
    # String-backed (NOT native PG enum) — matches status/trading_mode convention.
    assert col.type.__class__.__name__ == "String"
    assert col.server_default is not None  # existing rows backfill to the default


def test_is_real_money_only_true_for_real_class() -> None:
    from msai.models.broker_account import AccountClass, BrokerAccount
    real = BrokerAccount(account_class=AccountClass.REAL)
    test = BrokerAccount(account_class=AccountClass.TEST)
    paper = BrokerAccount(account_class=AccountClass.PAPER)
    assert real.is_real_money is True
    assert test.is_real_money is False
    assert paper.is_real_money is False
```

- [ ] **Step 2: Run to verify it fails** — `cd backend && uv run pytest tests/unit/test_broker_account_model.py -k account_class -v` → FAIL (`account_class` undefined).

- [ ] **Step 3: Implement** in `models/broker_account.py` — add the enum after `CredentialsBackend` (line 32) and the column + property inside `BrokerAccount`:

```python
class AccountClass(StrEnum):
    PAPER = "paper"  # paper-trading account (no real funds; DU/DF prefix)
    TEST = "test"  # live IB account used for testing with limited capital (LVP/HVP)
    REAL = "real"  # the production fund — identity-echo gated, must never be hit by accident
```

```python
    # Explicit real-money classification. NEVER inferred from ib_account_id
    # string prefix (PRD §6). String-backed StrEnum + server_default so the
    # column is additive and existing rows backfill (migration sets paper rows
    # to 'paper'; live rows stay 'test'; the fund is registered explicitly as
    # 'real' post-PR-3). See plan D1/D2/D5.
    account_class: Mapped[AccountClass] = mapped_column(
        String(16), nullable=False, default=AccountClass.TEST, server_default="test"
    )

    @property
    def is_real_money(self) -> bool:
        """True only for the production fund (``account_class == real``). The
        single fact the deploy identity-echo gate + UI real-money label read.

        Robust to the String-backed reload: this column is a plain ``String(16)``
        (status/trading_mode convention), so a refreshed row's ``account_class`` is
        a bare ``str``, not an ``AccountClass`` member. ``StrEnum`` equality makes
        ``"real" == AccountClass.REAL`` True, so this comparison holds for both the
        freshly-assigned enum and the reloaded str (iter-3 P1#2)."""
        return self.account_class == AccountClass.REAL
```

- [ ] **Step 4: Run to verify it passes** — same command → PASS.

- [ ] **Step 5: Commit** — `git add backend/src/msai/models/broker_account.py backend/tests/unit/test_broker_account_model.py && git commit -m "feat(broker-account): add account_class enum + is_real_money property"`

---

## Task 2: Additive Alembic migration (column + backfill)

**Files:**

- Create: `backend/alembic/versions/<rev>_add_account_class_to_broker_accounts.py`
- Test: `backend/tests/integration/test_broker_accounts_migration.py` (or a new `test_account_class_migration.py` modeled on it)

- [ ] **Step 1: Generate the revision skeleton** — `cd backend && uv run alembic revision -m "add account_class to broker_accounts"`. Note the generated `<rev>` filename. Find the current head to set `down_revision` correctly: `uv run alembic heads`.

- [ ] **Step 2: Write the failing migration test** using the REAL harness (iter-3 P2 — VERIFIED: migration tests use a dedicated `PostgresContainer` fixture that does NOT read `DATABASE_URL`, plus `tests.integration._alembic_subprocess.run_alembic(url, *args)`; raw seed INSERTs via an async engine — see `test_broker_account_fk_migration.py:33-130`). New file `backend/tests/integration/test_account_class_migration.py`:

```python
import sqlalchemy as sa
import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration._alembic_subprocess import run_alembic

# Parent revision = the head BEFORE this migration. Fill from `alembic heads`
# before this PR (the account_class migration's down_revision).
_DOWN_REVISION = "<current_head_before_this_PR>"


@pytest.fixture(scope="module")
def isolated_postgres_url_account_class():
    from testcontainers.postgres import PostgresContainer
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


async def _seed_two_accounts(url: str) -> None:
    # broker_accounts exists at _DOWN_REVISION (created by d87c2aa5f751). Insert
    # one paper + one live row BEFORE the account_class column exists.
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for ib_acct, mode, slot in [("DUTEST01", "paper", "slot-a"), ("UTEST01", "live", "slot-b")]:
            await conn.execute(sa.text(
                "INSERT INTO broker_accounts "
                "(id, ib_account_id, ib_login_key, status, gateway_slot, trading_mode, "
                " credentials_backend, credentials_secret_ref, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :a, :k, 'active', :s, :m, 'env', 'ref', now(), now())"
            ), {"a": ib_acct, "k": f"key-{ib_acct}", "s": slot, "m": mode})
    await engine.dispose()


@pytest.mark.asyncio
async def test_account_class_column_added_and_backfilled(isolated_postgres_url_account_class) -> None:
    url = isolated_postgres_url_account_class
    # ARRANGE: bring the schema up to the revision just below this migration, seed rows.
    run_alembic(url, "upgrade", _DOWN_REVISION)
    await _seed_two_accounts(url)
    # ACT: run this PR's migration.
    run_alembic(url, "upgrade", "head")

    def _account_class_col(sync_conn):
        return {c["name"]: c for c in inspect(sync_conn).get_columns("broker_accounts")}.get("account_class")

    # ASSERT (post-upgrade): column present + NOT NULL, and rows backfilled.
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        col = await conn.run_sync(_account_class_col)  # run_sync — inspect needs a sync conn
        assert col is not None, "account_class column missing after upgrade"
        assert col["nullable"] is False
        rows = (await conn.execute(sa.text(
            "SELECT trading_mode, account_class FROM broker_accounts ORDER BY trading_mode"
        ))).all()
    by_mode = {tm: ac for tm, ac in rows}
    assert by_mode["paper"] == "paper"
    assert by_mode["live"] == "test"

    # ASSERT (post-downgrade): the additive column is gone, nothing else broke.
    run_alembic(url, "downgrade", "-1")
    async with engine.connect() as conn:
        assert await conn.run_sync(_account_class_col) is None, "account_class column not dropped on downgrade"
    await engine.dispose()
```

> Implementer note: mirror `test_broker_account_fk_migration.py` exactly for the fixture scaffolding + the `conn.run_sync(lambda c: inspect(c)...)` idiom (iter-4 P2). Replace `_DOWN_REVISION` with the real id from `uv run alembic heads`.

- [ ] **Step 3: Run to verify it fails** — `uv run pytest tests/integration/test_account_class_migration.py -v` → FAIL.

- [ ] **Step 4: Write the migration body** (`upgrade`/`downgrade`), mirroring `d87c2aa5f751_create_broker_accounts.py:34-36`:

```python
def upgrade() -> None:
    op.add_column(
        "broker_accounts",
        sa.Column(
            "account_class",
            sa.String(16),
            nullable=False,
            server_default="test",
        ),
    )
    # One-time backfill heuristic (operator-run, NOT runtime inference):
    # paper-mode rows are paper-class; existing live-mode rows are the
    # LVP/HVP test accounts (class 'test'). No 'real' row exists yet — the
    # fund is registered explicitly as 'real' post-PR-3 (plan D2/D5).
    op.execute("UPDATE broker_accounts SET account_class = 'paper' WHERE trading_mode = 'paper'")
    # live rows already default to 'test' via server_default; explicit for clarity:
    op.execute(
        "UPDATE broker_accounts SET account_class = 'test' "
        "WHERE trading_mode = 'live' AND account_class = 'test'"
    )


def downgrade() -> None:
    op.drop_column("broker_accounts", "account_class")
```

- [ ] **Step 5: Run to verify it passes** — `uv run pytest tests/integration/test_account_class_migration.py -v` → PASS. Also confirm `uv run alembic upgrade head` then `uv run alembic downgrade -1` round-trips cleanly against a scratch DB.

- [ ] **Step 6: Commit** — `git add backend/alembic/versions/ backend/tests/integration/test_account_class_migration.py && git commit -m "feat(db): additive account_class migration + backfill"`

---

## Task 3: Surface `account_class`/`is_real_money` on responses; accept `account_class` on create

**Files:**

- Modify: `backend/src/msai/schemas/broker_account.py`
- Test: `backend/tests/unit/test_broker_account_schemas.py`

- [ ] **Step 1: Write failing tests** (append):

```python
def test_response_carries_account_class_and_is_real_money() -> None:
    from uuid import uuid4
    from datetime import UTC, datetime
    from msai.models.broker_account import AccountClass, BrokerAccount
    from msai.schemas.broker_account import BrokerAccountResponse

    acct = BrokerAccount(
        id=uuid4(), ib_account_id="U4715997", ib_login_key="mshvp000",
        label="HVP", status="active", gateway_slot="slot-1", trading_mode="live",
        account_class=AccountClass.REAL, credentials_backend="env",
        credentials_secret_ref="ref", credentials_secret_version=None,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    resp = BrokerAccountResponse.model_validate(acct)
    assert resp.account_class == "real"
    assert resp.is_real_money is True


def test_create_defaults_account_class_from_trading_mode() -> None:
    from msai.schemas.broker_account import BrokerAccountCreateRequest
    paper = BrokerAccountCreateRequest(
        ib_account_id="DU123", ib_login_key="k", trading_mode="paper",
        tws_userid="u", tws_password="p",
    )
    assert paper.account_class == "paper"
    live = BrokerAccountCreateRequest(
        ib_account_id="U123", ib_login_key="k", trading_mode="live",
        tws_userid="u", tws_password="p",
    )
    assert live.account_class == "test"


def test_create_rejects_real_class_on_paper_account() -> None:
    import pytest
    from pydantic import ValidationError
    from msai.schemas.broker_account import BrokerAccountCreateRequest
    with pytest.raises(ValidationError):
        BrokerAccountCreateRequest(
            ib_account_id="DU123", ib_login_key="k", trading_mode="paper",
            account_class="real", tws_userid="u", tws_password="p",
        )
```

- [ ] **Step 2: Run to verify it fails** — `cd backend && uv run pytest tests/unit/test_broker_account_schemas.py -k "account_class or real_money" -v` → FAIL.

- [ ] **Step 3: Implement** — in `schemas/broker_account.py`:

Add to `BrokerAccountResponse` (after `trading_mode`, line 105):

```python
    account_class: str
    # Derived convenience so the frontend reads real-money status from an
    # explicit field, NEVER by parsing ib_account_id (PRD §6). Populated from
    # the model property via from_attributes.
    is_real_money: bool
```

Add to `BrokerAccountCreateRequest` (after `trading_mode`, line 36) + a validator:

```python
    account_class: str | None = Field(default=None, pattern=r"^(paper|test|real)$")
```

```python
    @model_validator(mode="after")
    def _default_and_validate_account_class(self) -> BrokerAccountCreateRequest:
        """Default account_class from trading_mode when omitted (paper→paper,
        live→test) and reject 'real' on a paper account (a paper account can
        never be real money). Explicit 'real' is the deliberate fund-registration
        path (plan D5)."""
        if self.account_class is None:
            object.__setattr__(
                self, "account_class", "paper" if self.trading_mode == "paper" else "test"
            )
        if self.account_class == "real" and self.trading_mode != "live":
            raise ValueError("account_class='real' requires trading_mode='live'")
        return self
```

> Note: Pydantic v2 models are mutable by default (no frozen config here), so `self.account_class = ...` works directly; use plain assignment rather than `object.__setattr__` unless the model is frozen. Verify against the class config and use the simplest form that passes mypy.

- [ ] **Step 4: Run to verify it passes** — same command → PASS.

- [ ] **Step 5: Thread `account_class` through the service + router (iter-1 P1#1 — VERIFIED: `broker_account_service.create` at `:232` and the `BrokerAccount(...)` construction at `:354` omit it, so without this the create silently drops `account_class`).**

  Write the failing service test first in `backend/tests/integration/test_broker_account_service.py` (iter-6 P1 — this suite is under `integration/`, uses a real session):

```python
async def test_create_persists_account_class_real(broker_account_service) -> None:
    acct = await broker_account_service.create(
        ib_account_id="U4715997", ib_login_key="mshvp000", trading_mode="live",
        gateway_slot=None, creds=..., actor="op", account_class="real",
    )
    # iter-3 P1#2: the column is a plain String(16) (matches the status/trading_mode
    # convention), so after the service flush/refresh `account_class` reloads as a
    # bare `str`, NOT an AccountClass member — assert via `== "real"` (StrEnum
    # equality), never `.value`. `is_real_money` works either way (the property
    # compares `== AccountClass.REAL`, and StrEnum `"real" == AccountClass.REAL`).
    assert acct.account_class == "real"
    assert acct.is_real_money is True
```

Add `account_class: str | None = None` (keyword-only) to `BrokerAccountService.create(...)` (`:232-243`). When `None`, derive it from `trading_mode` IN THE SERVICE — `paper` if `trading_mode == "paper"` else `test` — so a direct service caller (not just the router) gets the D5-correct default and a paper account never becomes `"test"` (iter-2 P2: a hardcoded `"test"` default contradicts D5 for direct paper creates). Then pass it into the `BrokerAccount(...)` construction (`:354`):

```python
        resolved_class = account_class or ("paper" if trading_mode == "paper" else "test")
        # ...
            acct = BrokerAccount(
                id=new_id,
                ib_account_id=ib_account_id,
                ib_login_key=ib_login_key,
                label=label,
                status=BrokerAccountStatus.ACTIVE,
                gateway_slot=slot,
                trading_mode=trading_mode,
                account_class=AccountClass(resolved_class),  # iter-1 P1#1 / iter-2 P2
                credentials_backend=self._backend,
                ...
            )
```

Add service tests for BOTH omitted paths: omitted + `trading_mode="paper"` → `account_class == "paper"`; omitted + `trading_mode="live"` → `"test"`; explicit `"real"` + live → `"real"`.

And in `api/broker_accounts.py:create_broker_account` (`:89`), pass it through the `svc.create(...)` call (`:101`): add `account_class=body.account_class`. (`body.account_class` is non-None after Task 3's validator defaults it; the service's own default is the belt-and-suspenders for direct callers.)

- [ ] **Step 6: Run to verify it passes + Commit** — `cd backend && uv run pytest tests/integration/test_broker_account_service.py -k account_class -v` → PASS, then `git add backend/src/msai/schemas/broker_account.py backend/src/msai/services/live/broker_account_service.py backend/src/msai/api/broker_accounts.py backend/tests/unit/test_broker_account_schemas.py backend/tests/integration/test_broker_account_service.py && git commit -m "feat(broker-account): expose + persist account_class/is_real_money on create"`

---

## Task 4: `confirm_account_id` + `selector_context_account_id` on `PortfolioStartRequest` (body-only preconditions)

**Files:**

- Modify: `backend/src/msai/schemas/live.py`
- Test: `backend/tests/unit/test_portfolio_start_schema.py`

- [ ] **Step 1: Write failing tests** (append):

```python
def test_confirm_account_id_blank_rejected() -> None:
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        PortfolioStartRequest(
            portfolio_revision_id=uuid4(), broker_account_id=uuid4(),
            confirm_account_id="   ",
        )


def test_confirm_account_id_all_or_unassigned_rejected() -> None:
    import pytest
    from pydantic import ValidationError
    for bad in ("all", "ALL", "unassigned", "Unassigned"):
        with pytest.raises(ValidationError):
            PortfolioStartRequest(
                portfolio_revision_id=uuid4(), broker_account_id=uuid4(),
                confirm_account_id=bad,
            )


def test_confirm_account_id_concrete_value_accepted() -> None:
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(), broker_account_id=uuid4(),
        confirm_account_id="U4715997",
    )
    assert req.confirm_account_id == "U4715997"


def test_selector_context_is_optional_and_passthrough() -> None:
    req = PortfolioStartRequest(
        portfolio_revision_id=uuid4(), broker_account_id=uuid4(),
        selector_context_account_id="all",
    )
    assert req.selector_context_account_id == "all"
```

- [ ] **Step 2: Run to verify it fails** — `cd backend && uv run pytest tests/unit/test_portfolio_start_schema.py -k "confirm_account_id or selector_context" -v` → FAIL.

- [ ] **Step 3: Implement** in `schemas/live.py` `PortfolioStartRequest` (after `ib_login_key`, line 38):

```python
    # PR4: real-money identity confirmation. Body-only preconditions are
    # checked here (blank / "all" / "unassigned" → 422). The SEMANTIC identity
    # match (confirm_account_id == resolved ib_account_id when the account is
    # real-money) CANNOT run here — it needs a DB lookup — so it runs in the
    # handler after _resolve_effective_account (plan §gate; research §3).
    confirm_account_id: str | None = None
    # PR4: the active global-selector value the UI was scoped to at deploy time
    # (None on API/CLI — no global selector). The handler emits a divergence
    # metric/log when this is present and != the resolved effective account
    # (council objection #7). NEVER drives a safety decision (plan D6).
    selector_context_account_id: str | None = None
```

```python
    @field_validator("confirm_account_id")
    @classmethod
    def _normalize_confirm_account_id(cls, v: str | None) -> str | None:
        """Body-only precondition: reject blank / 'all' / 'unassigned'. A present
        confirm token must be a concrete account id; the bucket sentinels are
        never a real target (PRD US-003)."""
        if v is None:
            return None
        normalized = v.strip()
        if not normalized:
            raise ValueError("confirm_account_id cannot be empty / whitespace-only")
        if normalized.lower() in ("all", "unassigned"):
            raise ValueError("confirm_account_id must be a concrete account id, not 'all'/'unassigned'")
        return normalized
```

- [ ] **Step 4: Run to verify it passes** — same command → PASS.

- [ ] **Step 5: Commit** — `git add backend/src/msai/schemas/live.py backend/tests/unit/test_portfolio_start_schema.py && git commit -m "feat(live): add confirm_account_id + selector_context_account_id to PortfolioStartRequest"`

---

## Task 5: Server-side real-money identity gate + divergence audit in `live_start_portfolio`

**Files:**

- Modify: `backend/src/msai/api/live.py` (gate after `_resolve_effective_account`, line ~1205)
- Modify: `backend/src/msai/services/observability/broker_account_metrics.py` (add `DEPLOY_TARGET_DIVERGENCE`)
- Test: `backend/tests/integration/api/test_live_start_broker_account.py` (iter-5 P2 — this is the broker-account-AWARE harness that installs `app.state.gateway_router = GatewayRouter(_GATEWAY_CONFIG)` + `app.state.broker_credentials_store` at `:245-248`; `_resolve_effective_account` validates the GatewayRouter + resolves the registry row BEFORE the new gate, so the bare `test_live_start_endpoints.py` harness can't exercise it. Add the gate tests HERE, or copy its router/credentials-store + broker-account-seed setup.)

- [ ] **Step 1: Write failing integration tests in `test_live_start_broker_account.py`** (iter-5 P2). Reuse that module's setup: `app.state.gateway_router = GatewayRouter(<config>)` + `app.state.broker_credentials_store` (`:245-248`) + its `_seed_broker_account` helper + `get_current_user`/`get_db`/`get_command_bus` overrides.

  **CRITICAL fixture binding (iter-7 P1 — VERIFIED the module's `_GATEWAY_CONFIG` binds DU-only paper accounts `_BOUND_ACCOUNTS=["DUP0000001","DUP0000002"]` under a paper login `:105-107`).** A `real`/`test` account has `trading_mode="live"`, so its `ib_account_id` MUST be `U`-prefixed (`assert_account_mode_consistent`) AND it must be bound in the `GatewayRouter` config, or `_resolve_effective_account`'s `validate_account_row_state` fails with `route_not_found`/`not_router_bound` (a DIFFERENT 422) BEFORE the new real-money gate ever runs. So these tests need their OWN GatewayRouter config that binds the U account ids under a LIVE login, e.g.:

```python
_LIVE_LOGIN = "msai-live-primary"
_REAL_ACCT = "U4715997"   # account_class="real"
_TEST_ACCT = "U4705114"   # account_class="test" (live, not the fund)
_GATEWAY_CONFIG_LIVE = f"{_LIVE_LOGIN}:ib-gateway:4001:accounts={_REAL_ACCT}|{_TEST_ACCT}"
# in the test's app-state setup:
app.state.gateway_router = GatewayRouter(_GATEWAY_CONFIG_LIVE)
```

Seed `real_broker_account` (ib_account_id=`_REAL_ACCT`, trading_mode="live", account_class="real", ib_login_key=`_LIVE_LOGIN`) and `test_broker_account` (`_TEST_ACCT`, live, account_class="test") via `_seed_broker_account` so row-state validation passes and execution reaches the new gate. The gate fires BEFORE the supervisor poll, so no fake-supervisor readiness is needed. (It's account binding in GATEWAY_CONFIG that matters, NOT the `gateway_slot` string.)

**Two harness-mechanics fixes the snippets below depend on (iter-8 P1 — VERIFIED):**

- **`client` is a TUPLE.** The module's `client` fixture (`:193`) yields `(ac, store, spy)`; every existing test unpacks `ac, _store, _spy = client` and calls `await ac.post(...)`. The snippets below write `client.post(...)` for brevity — in real code, unpack the tuple first and call `ac.post(...)`.
- **Extend `_seed_broker_account` (`:283-310`) with an `account_class: str = "test"` kwarg** and pass it into the `BrokerAccount(...)` it builds (it currently omits the field). Then seed the fixtures via `_seed_broker_account(session, ib_account_id=_REAL_ACCT, trading_mode="live", account_class="real", ib_login_key=_LIVE_LOGIN)` (and `_TEST_ACCT` / `account_class="test"`).

```python
async def test_real_money_deploy_without_confirm_rejected_422(client, real_broker_account, frozen_revision):
    resp = await client.post("/api/v1/live/start-portfolio", json={
        "portfolio_revision_id": str(frozen_revision.id),
        "broker_account_id": str(real_broker_account.id),
        "paper_trading": False,
    })
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["error"]["code"] == "REAL_MONEY_CONFIRM_REQUIRED"


async def test_real_money_deploy_confirm_mismatch_rejected_422(client, real_broker_account, frozen_revision):
    resp = await client.post("/api/v1/live/start-portfolio", json={
        "portfolio_revision_id": str(frozen_revision.id),
        "broker_account_id": str(real_broker_account.id),
        "paper_trading": False,
        "confirm_account_id": "U0000000",  # != real_broker_account.ib_account_id
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "REAL_MONEY_CONFIRM_MISMATCH"


async def test_real_money_deploy_confirm_match_passes_gate(client, real_broker_account, frozen_revision):
    # confirm matches → gate passes; deploy proceeds past the gate (may then hit
    # the fake-supervisor path / 503 no-supervisor — assert it is NOT a 422 gate reject).
    resp = await client.post("/api/v1/live/start-portfolio", json={
        "portfolio_revision_id": str(frozen_revision.id),
        "broker_account_id": str(real_broker_account.id),
        "paper_trading": False,
        "confirm_account_id": real_broker_account.ib_account_id,
    })
    assert resp.status_code != 422 or resp.json()["detail"]["error"]["code"] not in (
        "REAL_MONEY_CONFIRM_REQUIRED", "REAL_MONEY_CONFIRM_MISMATCH",
    )


async def test_test_account_deploy_needs_no_identity_echo(client, test_broker_account, frozen_revision):
    # account_class='test' (live LVP/HVP) → no confirm_account_id required; the
    # gate does not reject for confirmation (explicit target still required, which
    # broker_account_id satisfies).
    resp = await client.post("/api/v1/live/start-portfolio", json={
        "portfolio_revision_id": str(frozen_revision.id),
        "broker_account_id": str(test_broker_account.id),
        "paper_trading": False,
    })
    assert not (resp.status_code == 422 and resp.json()["detail"]["error"]["code"].startswith("REAL_MONEY_CONFIRM"))


async def test_conflicting_broker_account_id_and_legacy_account_id_rejected_422(
    client, test_broker_account, frozen_revision
):
    # iter-2 P1#2: broker_account_id resolves to one account, but the body ALSO
    # names a different legacy account_id → ambiguous target → 422.
    resp = await client.post("/api/v1/live/start-portfolio", json={
        "portfolio_revision_id": str(frozen_revision.id),
        "broker_account_id": str(test_broker_account.id),
        "account_id": "U0000000",  # != test_broker_account.ib_account_id
        "ib_login_key": "whatever",
        "paper_trading": False,
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "AMBIGUOUS_DEPLOY_TARGET"
```

- [ ] **Step 2: Run to verify it fails** — `cd backend && uv run pytest tests/integration/api/test_live_start_broker_account.py -k "real_money or identity_echo or test_account_deploy or ambiguous" -v` → FAIL.

- [ ] **Step 3: Implement the gate** in `api/live.py` immediately after `effective_ib_login_key = effective.ib_login_key` (line 1205), before the `_resolve_binding_for_start_portfolio` call (line 1207):

```python
    # ------------------------------------------------------------------
    # PR4: ambiguous-target rejection (iter-2 P1#2 / PRD US-003 "ambiguous
    # deploy target → 422"). The either/or schema validator accepts
    # broker_account_id ALONGSIDE legacy account_id for back-compat, and the
    # resolver silently PREFERS the registry row. If the caller also sent a
    # legacy account_id that names a DIFFERENT account than the one resolved,
    # the intended target is ambiguous — fail closed rather than silently
    # deploying to the resolved (registry) account. Matching legacy strings
    # (the documented back-compat shape) pass. Post-PR4 UI/CLI send ONLY
    # broker_account_id, so this only ever bites a genuinely conflicting body.
    # ------------------------------------------------------------------
    if (
        request.broker_account_id is not None
        and request.account_id is not None
        and request.account_id != effective_account_id
    ):
        _deploy_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "AMBIGUOUS_DEPLOY_TARGET",
            (
                f"broker_account_id resolves to {effective_account_id} but the request "
                f"also names account_id={request.account_id}. Send exactly one target "
                "(omit account_id when using broker_account_id)."
            ),
            {"resolved_account_id": effective_account_id, "request_account_id": request.account_id},
        )

    # ------------------------------------------------------------------
    # PR4: real-money identity gate. Runs on the RESOLVED effective account
    # (never the raw request string — same halt-bypass concern as the
    # account-halt latch below). Fires ONLY when a registry row resolved and
    # it is the production fund (is_real_money). Genuine pre-registry legacy
    # warm-restarts (broker_account is None) keep today's behavior (plan D4).
    # Enforced here so UI/API/CLI share one server-side gate (PRD US-003).
    # ------------------------------------------------------------------
    resolved = effective.broker_account
    if resolved is not None and resolved.is_real_money:
        confirm = request.confirm_account_id  # already body-normalized
        if confirm is None:
            _deploy_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "REAL_MONEY_CONFIRM_REQUIRED",
                (
                    f"Account {effective_account_id} is a real-money (fund) account. "
                    "Re-send with confirm_account_id set to the exact account id to deploy."
                ),
                {"account_id": effective_account_id},
            )
        if confirm != effective_account_id:
            _deploy_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "REAL_MONEY_CONFIRM_MISMATCH",
                (
                    "confirm_account_id does not match the resolved real-money account. "
                    "Deploy refused."
                ),
                {"account_id": effective_account_id},
            )

    # PR4: divergence audit (council #7) — non-gating. If the UI told us which
    # account it was scoped to and it differs from the actual deploy target,
    # record it (durable, server-side). API/CLI omit selector_context.
    selector_ctx = request.selector_context_account_id
    # iter-1 P2: "all"/"unassigned" are not a focused target → not a divergence.
    if (
        selector_ctx is not None
        and selector_ctx not in ("all", "unassigned")
        and selector_ctx != effective_account_id
    ):
        DEPLOY_TARGET_DIVERGENCE.inc(account_id=effective_account_id)
        log.warning(
            "deploy_target_divergence",
            extra={
                "selector_context_account_id": selector_ctx,
                "deploy_target_account_id": effective_account_id,
            },
        )
```

Add the metric in `backend/src/msai/services/observability/broker_account_metrics.py` next to `DEPLOY_VALIDATION_FAILED` (`:56`) — VERIFIED the factory is the project's hand-rolled `_r.counter(name, help)` with labels supplied at `.inc(...)` time, NOT a `labelnames=` arg (iter-1 P2#6):

```python
DEPLOY_TARGET_DIVERGENCE = _r.counter(
    "msai_broker_account_deploy_target_divergence_total",
    "Deploys where the UI global-selector context differed from the resolved target account",
)
```

Import it into `api/live.py` alongside the existing `DEPLOY_VALIDATION_FAILED` import (`api/live.py:101`).

- [ ] **Step 4: Run to verify it passes** — same command → PASS.

- [ ] **Step 5: Commit** — `git add backend/src/msai/api/live.py backend/src/msai/services/observability/broker_account_metrics.py backend/tests/integration/api/test_live_start_broker_account.py && git commit -m "feat(live): server-side real-money identity gate + divergence audit on /start-portfolio"`

---

## Task 6: CLI `--confirm-account-id` (third surface for the same server gate)

**Files:**

- Modify: `backend/src/msai/cli.py` — `_resolve_cli_account_payload` + `live start-portfolio` **AND** `live start` (both call the shared helper, iter-1 P2#8); `broker add` + `broker list` for `account_class` parity (iter-4 P2)
- Test: `backend/tests/unit/test_cli_live_portfolio.py` + `backend/tests/integration/test_broker_cli.py` (iter-6 P1 — broker CLI tests are under `integration/`)

- [ ] **Step 1: Write failing test** — assert the payload includes `confirm_account_id` when the option is given (use Typer's `CliRunner` like the existing CLI tests; mock `_api_call` to capture the `json_body`).

```python
def test_start_portfolio_sends_confirm_account_id(monkeypatch) -> None:
    captured = {}
    def fake_api_call(method, path, *, json_body=None, **kw):
        captured["body"] = json_body
        class R:  # minimal response stub
            def json(self): return {"status": "ok"}
        return R()
    monkeypatch.setattr("msai.cli._api_call", fake_api_call)
    from typer.testing import CliRunner
    from msai.cli import app
    result = CliRunner().invoke(app, [
        "live", "start-portfolio", "--revision", "11111111-1111-1111-1111-111111111111",
        "--broker-account-id", "22222222-2222-2222-2222-222222222222",
        "--no-paper", "--confirm-account-id", "U4715997",
    ], input="y\n")  # answer the typer.confirm prompt
    assert result.exit_code == 0
    assert captured["body"]["confirm_account_id"] == "U4715997"
```

- [ ] **Step 2: Run to verify it fails** — `cd backend && uv run pytest tests/unit/test_cli_live_portfolio.py -k confirm_account_id -v` → FAIL.

- [ ] **Step 3: Implement** — add the option to `live_start_portfolio` (after `paper`, line 958):

```python
    confirm_account_id: str = typer.Option(
        "",
        "--confirm-account-id",
        help=(
            "Real-money identity confirmation: the exact IB account id. REQUIRED by "
            "the server when the resolved account is a real-money (fund) account; "
            "harmless for test/paper accounts."
        ),
    ),
```

Thread it through `_resolve_cli_account_payload` — add a `confirm_account_id: str = ""` keyword param and include it in the returned payload when non-blank:

```python
    payload = (
        {"broker_account_id": trimmed_broker_account_id}
        if trimmed_broker_account_id
        else {"account_id": trimmed_account, "ib_login_key": ib_login_key.strip()}
    )
    if confirm_account_id.strip():
        payload["confirm_account_id"] = confirm_account_id.strip()
    return payload
```

Add the SAME `--confirm-account-id` option to the `live start` command (it also calls `_resolve_cli_account_payload` → posts to `/start-portfolio`, so the gate would otherwise be un-satisfiable from `live start`; iter-1 P2#8) and pass `confirm_account_id=confirm_account_id` from BOTH call sites. Add a matching CliRunner test for `live start`.

- [ ] **Step 4: CLI broker-account `account_class` parity (iter-4 P2 — VERIFIED `broker add` at `cli.py:1261` takes `--trading-mode` but no `--account-class`, and `broker list` at `:1307` doesn't show it).** Without this the fund can be registered as `real` from the UI wizard (Task 12) but NOT from the CLI — a surface gap, and `broker list` can't show which account is the fund.
  - Add `--account-class` (paper|test|real) to `broker add` (`cli.py:1261`), include it in the `POST /broker-accounts` payload (the schema validator defaults it when omitted — Task 3). Write a CliRunner test asserting `--account-class real` is sent.
  - Add an `account_class` column to the `broker list` table output (`cli.py:1307`, alongside `trading_mode`).
  - Relabel the `live start-portfolio` / `live start` `--no-paper` confirm prompt (`_resolve_cli_account_payload`, `cli.py:211-227`): change "REAL-MONEY trading on {target}" → "LIVE trading on {target}" (live test accounts are NOT the fund under the new taxonomy; the fund-specific identity gate is the server's `--confirm-account-id` check). Keep the `typer.confirm(..., abort=True)`.

- [ ] **Step 5: Run to verify it passes** — `cd backend && uv run pytest tests/unit/test_cli_live_portfolio.py tests/integration/test_broker_cli.py -k "confirm_account_id or account_class" -v` → PASS.

- [ ] **Step 6: Commit** — `git add backend/src/msai/cli.py backend/tests/unit/test_cli_live_portfolio.py backend/tests/integration/test_broker_cli.py && git commit -m "feat(cli): --confirm-account-id + broker account_class set/show; relabel live prompt"`

---

## Task 7: Account-scope React Context (persisted, hydration-safe)

**Files:**

- Create: `frontend/src/lib/account-scope.tsx`

- [ ] **Step 1: Implement the provider/hook** (research §1 — `usehooks-ts.useLocalStorage` is SSR-safe; default `"all"`):

```tsx
"use client";

import { createContext, useContext } from "react";
import { useLocalStorage } from "usehooks-ts";

/** The selected global account scope. "all" = no filter (default);
 * "unassigned" = legacy deployments with no account_id; otherwise a concrete
 * ib_account_id. View-scope ONLY — never a deploy target (PRD US-001/US-002). */
export type AccountScope = string; // "all" | "unassigned" | <ib_account_id>

interface AccountScopeValue {
  scope: AccountScope;
  setScope: (next: AccountScope) => void;
}

const AccountScopeContext = createContext<AccountScopeValue | null>(null);
const STORAGE_KEY = "msai.accountScope";

export function AccountScopeProvider({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  // initializeWithValue:false forces the default ("all") on the server AND the
  // first client render, then syncs the persisted value after mount — this is
  // the explicit hydration-safe form (iter-1 P2#7: the bare 2-arg call can read
  // the persisted client value on first render → hydration mismatch).
  const [scope, setScope] = useLocalStorage<AccountScope>(STORAGE_KEY, "all", {
    initializeWithValue: false,
  });
  return (
    <AccountScopeContext.Provider value={{ scope, setScope }}>
      {children}
    </AccountScopeContext.Provider>
  );
}

export function useAccountScope(): AccountScopeValue {
  const ctx = useContext(AccountScopeContext);
  if (!ctx)
    throw new Error("useAccountScope must be used within AccountScopeProvider");
  return ctx;
}
```

- [ ] **Step 2: Mount the provider** in `app-shell.tsx` — wrap the returned protected shell (the `<div className="flex h-screen ...">` block at line 65) in `<AccountScopeProvider>…</AccountScopeProvider>`. Import at top. (Provider sits above the `<main>` route outlet so client navigations never unmount it → navigation persistence; localStorage → reload persistence.)

- [ ] **Step 3: Commit** — `git add frontend/src/lib/account-scope.tsx frontend/src/components/layout/app-shell.tsx && git commit -m "feat(ui): account-scope context persisted via useLocalStorage"`

> Coverage note: this hook's behavior (persist across nav + reload) is verified by the UI E2E use case (US-001), which is the right level — a unit test of a 1-string context is low value (memory `feedback_drop_vitest_for_one_off_pure_helpers`). No new vitest standup.

---

## Task 8: `AccountSelector` widget in the header (union population)

**Files:**

- Create: `frontend/src/components/layout/account-selector.tsx`
- Modify: `frontend/src/components/layout/header.tsx`

- [ ] **Step 1: Implement the selector** using shadcn `Select` (`@/components/ui/select`), fetching the union via react-query (`listBrokerAccounts` + `getLiveStatus`). Options = `All` + each registered account (label + id + real-money badge) + each `account_id` seen in `/live/status` not in the registry (shown as "Unknown/retired account <id>") + `Unassigned` if any deployment has `account_id == null`. Use stable testids.

```tsx
"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useAccountScope } from "@/lib/account-scope";
import { useAuth } from "@/lib/auth";
import { getLiveStatus } from "@/lib/api";
import {
  listBrokerAccounts,
  type BrokerAccount,
} from "@/lib/api/broker-accounts";

interface Option {
  value: string;
  label: string;
  realMoney: boolean;
  unknown?: boolean;
}

function buildOptions(
  accounts: BrokerAccount[],
  deploymentAccountIds: (string | null)[],
): Option[] {
  const opts: Option[] = [
    { value: "all", label: "All accounts", realMoney: false },
  ];
  const registered = new Set<string>();
  for (const a of accounts) {
    registered.add(a.ib_account_id);
    opts.push({
      value: a.ib_account_id,
      label: a.is_real_money
        ? `REAL FUND — ${a.label ?? a.ib_account_id} (${a.ib_account_id}) — LIVE MONEY`
        : `${a.label ?? a.ib_account_id} (${a.ib_account_id})`,
      realMoney: a.is_real_money,
    });
  }
  for (const id of deploymentAccountIds) {
    if (id && !registered.has(id)) {
      opts.push({
        value: id,
        label: `Unknown/retired account ${id}`,
        realMoney: false,
        unknown: true,
      });
      registered.add(id);
    }
  }
  if (deploymentAccountIds.some((id) => id == null)) {
    opts.push({
      value: "unassigned",
      label: "Unassigned (legacy)",
      realMoney: false,
    });
  }
  return opts;
}

export function AccountSelector(): React.ReactElement | null {
  const { scope, setScope } = useAccountScope();
  const { getToken } = useAuth();
  const accountsQ = useQuery({
    queryKey: ["broker-accounts"],
    queryFn: async () => listBrokerAccounts(await getToken()),
  });
  const statusQ = useQuery({
    queryKey: ["live-status-accounts"],
    queryFn: async () => getLiveStatus(await getToken()),
  });
  const accounts = accountsQ.data ?? [];
  const deploymentAccountIds = (statusQ.data?.deployments ?? []).map(
    (d) => d.account_id ?? null,
  );
  const options = buildOptions(accounts, deploymentAccountIds);

  // iter-1 P1#3 / iter-5 P1: reconcile PRD US-001 ("selected account later
  // archived → fall back to All; no crash") with US-004 ("unknown/retired
  // deployment-only ids shown explicitly, not folded into All"). The reset
  // rule: reset to "all" ONLY when the persisted scope is absent from the
  // ENTIRE option union (registered-active ∪ deployment-seen ∪ all/unassigned)
  // — i.e. the account fully vanished (no registry row AND no deployment
  // references it). An account that was archived but STILL has deployment
  // history remains a valid "Unknown/retired account <id>" option (US-004) — the
  // operator can keep viewing its terminal deployments; it does NOT reset. Gate
  // on isSuccess of BOTH queries so a transient fetch error never wipes a valid
  // selection (and so a deployment-only id isn't reset just because /live/status
  // is briefly erroring while /broker-accounts succeeded).
  const optionsReady = accountsQ.isSuccess && statusQ.isSuccess;
  useEffect(() => {
    if (!optionsReady) return;
    if (!options.some((o) => o.value === scope)) setScope("all");
  }, [optionsReady, options, scope, setScope]);

  return (
    <Select value={scope} onValueChange={setScope}>
      <SelectTrigger
        data-testid="account-scope-selector"
        aria-label="Account scope"
        className="h-8 w-[16rem] text-sm"
      >
        <SelectValue placeholder="All accounts" />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem
            key={o.value}
            value={o.value}
            data-testid={`account-scope-option-${o.value}`}
            className={
              o.realMoney ? "font-semibold text-destructive" : undefined
            }
          >
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
```

- [ ] **Step 2: Render in `header.tsx`** — add `<AccountSelector />` in the left zone next to `<MobileSidebarTrigger />` (line 25-27):

```tsx
<div className="flex items-center gap-2">
  <MobileSidebarTrigger />
  <AccountSelector />
</div>
```

Import `AccountSelector`.

- [ ] **Step 3: Verify build/lint** — `cd frontend && pnpm lint && pnpm build` (or `pnpm tsc --noEmit`). Expected: clean.

- [ ] **Step 4: Commit** — `git add frontend/src/components/layout/account-selector.tsx frontend/src/components/layout/header.tsx && git commit -m "feat(ui): global account selector in header (union population)"`

---

## Task 9: Live-trading page filters by scoped account

**Files:**

- Modify: `frontend/src/app/live-trading/page.tsx`

- [ ] **Step 1: Implement the filter** — after `deployments` is set (line 48 / used at 159+), derive a `scopedDeployments` from `useAccountScope()`:

```tsx
import { useAccountScope } from "@/lib/account-scope";
// ...
const { scope } = useAccountScope();
const scopedDeployments = useMemo(() => {
  if (scope === "all") return deployments;
  if (scope === "unassigned") return deployments.filter((d) => !d.account_id);
  return deployments.filter((d) => d.account_id === scope);
}, [deployments, scope]);
```

Use `scopedDeployments` for the `<StrategyStatus deployments=... />` prop (the deployment table) ONLY. Leave `riskHalted`/`routerHeartbeatAgeS` (fleet-global) unscoped — and per the CRITICAL note below, do NOT scope `activeRealDeployment` or `activeCount` (they feed the WS stream + the fleet-wide KillSwitch). (iter-6 P1: removed the earlier instruction that scoped `activeRealDeployment`/`activeCount`, which contradicted the fleet-wide KillSwitch requirement.)

**Also scope the positions/P&L (iter-2 P1#1 — VERIFIED `LivePositionItem` carries `deployment_id`, `api.ts:363-371`).** When scoped to a single account, the summary cards (Daily/Unrealized P&L, Total Market Value) and `PositionsTable` currently use unfiltered `restPositions`/`livePositions`, so they show fleet-wide P&L even though the deployment table re-scoped — an inconsistency. Filter positions to the scoped deployments' ids:

```tsx
const scopedDeploymentIds = useMemo(
  () => new Set(scopedDeployments.map((d) => d.id)),
  [scopedDeployments],
);
// iter-5 P1: scoped DISPLAY positions come from the REST FLEET source
// (`restPositions`), NOT `positionsForTable`. `positionsForTable` is the WS
// stream of the SINGLE `activeRealDeployment` when connected, so filtering it
// by another account's deployment ids would show empty/partial P&L even though
// that account has positions. `restPositions` (GET /live/positions) is the
// fleet-complete source.
//   - scope === "all": keep today's behavior (positionsForTable, WS-preferred).
//   - scoped:         restPositions filtered to the scoped account's deployments.
const scopedPositions = useMemo(
  () =>
    scope === "all"
      ? positionsForTable
      : restPositions.filter((p) => scopedDeploymentIds.has(p.deployment_id)),
  [scope, positionsForTable, restPositions, scopedDeploymentIds],
);
```

Feed the P&L `useMemo`s + `<PositionsTable>` from `scopedPositions`. Balance cards from `/account/summary` stay gateway-bound (US-005, Step 3) — those are NOT positions-derived and must NOT be filtered.

**CRITICAL (iter-3 P1#1 + iter-4 P1): do NOT touch the `<KillSwitch>` wiring at all — leave `activeCount`, `positionCount`, and `activeRealDeployment` EXACTLY as the current code computes them.** `KillSwitch` (`kill-switch.tsx:24-25,60,86-93`) calls `killAllLive()` which stops **all** running strategies fleet-wide ("stop all trading activity"); scoping its counts would understate the emergency-stop blast radius — a safety regression. (Note iter-4: `positionsForTable` is the ONE-deployment WS list when the stream is connected and the fleet REST list otherwise, so it must NOT be repurposed as a "fleet count" either — just don't change KillSwitch's existing inputs.) Concretely: keep today's `const activeCount = deployments.filter(running).length`, `const positionCount = positionsForTable.length`, and `activeRealDeployment = deployments.find(running)` UNCHANGED. Introduce the scoped variables as ADDITIVE locals used ONLY by:

- `<StrategyStatus deployments={scopedDeployments} />` (the deployment table), and
- the P&L summary `useMemo`s + `<PositionsTable livePositions={scopedPositions} />` (display only).

`scopedPositions` (above) sources from `restPositions` (fleet) when scoped and `positionsForTable` (WS-preferred) only for "all", so the P&L cards reflect the selected account even when the WS happens to stream a different deployment. This keeps the emergency control fleet-wide while the informational views re-scope correctly.

- [ ] **Step 2: Scope the dashboard page too (iter-1 P1#2 — PRD US-001 names "dashboard deployment cards").** In `frontend/src/app/dashboard/page.tsx`, apply the same `useAccountScope()` filter: `dashboard/page.tsx:64-65` computes `runningCount` from unfiltered `deployments`, and `:135` passes `deployments` to `<ActiveStrategies>`. Derive `scopedDeployments` identically (all/unassigned/concrete) and feed `runningCount` + `ActiveStrategies` from it. Leave `accountQuery` (gateway-bound balances) unscoped.

- [ ] **Step 3: Surface the connected gateway account id for the balance label (iter-1 P2#9 / iter-2 P2 / US-005 — VERIFIED `/account/summary` + `/account/health` carry NO account id AND `IBAccountSnapshot` stores no account identity today, so the snapshot MUST be modified).**

  Backend, `backend/src/msai/services/ib_account_snapshot.py`: capture the connected account id during the existing refresh. The snapshot already runs `accountSummaryAsync`/portfolio fetches against the IB connection; add a cached `self._account_id: str | None` populated from `ib.managedAccounts()` (or the `account` tag on the summary rows) inside the refresh loop, and expose a read-only `account_id` property (returns the cached value, `None` before the first successful refresh). Write the failing unit test first (mock the IB client's managed-accounts to return one id; assert the property reflects it; assert `None` before first success).

  Backend, `backend/src/msai/api/account.py:218` (`account_health`): add `"account_id": get_snapshot().account_id` to the returned dict. **Do NOT add a `Depends(_get_snapshot_dep)` parameter** (iter-7 P2 — VERIFIED `test_account_probe_lifecycle.py:175` calls `account_health(claims=...)` DIRECTLY, so a new `Depends` param would arrive as the unresolved sentinel and break it). Read the module-level singleton directly, the same way the handler already reads module-level `_ib_probe`. **No guard / try-except needed (iter-8 P2 — VERIFIED `get_snapshot()` at `:476-491` LAZY-CREATES the singleton and never raises):** `get_snapshot().account_id` always returns a value — the connected account id, or `None` before the first successful refresh (the `account_id` property added above returns `None` pre-first-success; the lazy-created `IBAccountSnapshot(host, port)` performs no IO until `.start()`). Update the return type annotation to `dict[str, str | bool | int | None]`. Add a unit assertion that the health body includes the `account_id` key (value may be `None` in the cold-start path) and CONFIRM `test_account_probe_lifecycle.py:175` still passes (the direct call lazy-creates a snapshot and reads `account_id=None`, correct when no refresh has run).

Frontend — dashboard (iter-3 P2#1 — VERIFIED the label lives in `portfolio-summary.tsx:129-151`, hardcoded "From IB Gateway", and `dashboard/page.tsx:42-47` fetches only `/account/summary`): extend the health response type in `api.ts` with `account_id: string | null`; add a `getAccountHealth` query to `dashboard/page.tsx` (react-query, same auth pattern as `accountQuery`) and pass its `account_id` into `<PortfolioSummary>` as a new optional prop; in `portfolio-summary.tsx` change the StatCard `change` text from the hardcoded "From IB Gateway" to "Account `<id>`" when present (falling back to "From IB Gateway" when `null`). Update the `portfolio-summary` render test if one exists.

Frontend — `/account` page (iter-5 P1#3 — VERIFIED `frontend/src/app/account/page.tsx:83-104` renders `<AccountSummaryCard>` + `<AccountPortfolioTable>` and ALREADY has a `health` query in scope): since the global selector is visible on `/account` too but summary/portfolio are gateway-bound, pass `health.data?.account_id` into `AccountSummaryCard` + `AccountPortfolioTable` and have each render a "connected account only — `<id>`" caption (fallback "connected account only (gateway-bound)" when `null`). This is where the gateway-bound caveat matters MOST — it stops an operator who scoped to account B from reading account A's full IB balance as B's.

Do NOT fabricate per-account balances for non-connected accounts anywhere.

- [ ] **Step 4: Verify** — `cd frontend && pnpm lint && pnpm build` → clean; `cd backend && uv run pytest tests/ -k "account_health or account_snapshot" -v` → PASS.

- [ ] **Step 5: Commit** — `git add frontend/src/app/live-trading/page.tsx frontend/src/app/dashboard/page.tsx frontend/src/components/dashboard/portfolio-summary.tsx frontend/src/app/account/page.tsx frontend/src/components/account/ backend/src/msai/api/account.py backend/src/msai/services/ib_account_snapshot.py frontend/src/lib/api.ts && git commit -m "feat(ui): scope live-trading + dashboard by account; label connected gateway account on dashboard + /account"`

---

## Task 10: Frontend types + `startPortfolio` client contract widening

**Files:**

- Modify: `frontend/src/lib/api/broker-accounts.ts` (`BrokerAccount` type)
- Modify: `frontend/src/lib/api.ts` (`startPortfolio` body)

- [ ] **Step 1: Widen `BrokerAccount`** — add after `trading_mode` (line 31):

```ts
/** "paper" | "test" | "real" — explicit account class (PR4). */
account_class: string;
/** True only for the production fund — drives the real-money label + gate. */
is_real_money: boolean;
```

- [ ] **Step 2: Widen `startPortfolio` body** (`api.ts:967-973`) — make it additive (all new fields optional):

```ts
  body: {
    portfolio_revision_id: string;
    account_id?: string;
    broker_account_id?: string;
    paper_trading: boolean;
    ib_login_key?: string;
    confirm_account_id?: string;
    selector_context_account_id?: string;
  },
```

(The existing callers pass `account_id`/`ib_login_key`; the rewritten dialog will pass `broker_account_id` + `confirm_account_id`. Keep `account_id`/`ib_login_key` optional so no caller breaks — research open-risk #5.)

- [ ] **Step 3: Verify** — `cd frontend && pnpm tsc --noEmit` → clean.

- [ ] **Step 4: Commit** — `git add frontend/src/lib/api/broker-accounts.ts frontend/src/lib/api.ts && git commit -m "feat(ui): account_class/is_real_money types; startPortfolio accepts broker_account_id + confirm_account_id"`

---

## Task 11: Rewrite `PortfolioStartDialog` — registry-backed explicit target + server-authoritative confirm

**Files:**

- Modify: `frontend/src/components/live/portfolio-start-dialog.tsx`

This replaces the free-text `account_id` Input + browser `startsWith` prefix-parse + client-only confirm with a registry-backed `Select` target picker, pre-filled from the global selector only when it is a single concrete account, `All`/`Unassigned` disabling Deploy, and a real-money typed echo sent as `confirm_account_id`.

- [ ] **Step 1: Replace Stage-1 form** — fetch broker accounts (`listBrokerAccounts`), render a shadcn `Select` of registered accounts (label + id + real-money badge) as the **target** field (`data-testid="portfolio-start-target-account"`). State: `selectedAccount: BrokerAccount | null` (replaces free-text `accountId`/`ibLoginKey`). Pre-fill: if `useAccountScope().scope` is a concrete registered account id, default `selectedAccount` to it; if scope is `"all"`/`"unassigned"`, leave unset and disable the Preview/Deploy CTA with an inline "Pick a target account to deploy" notice (US-002). Drop the `paperTrading` checkbox's role in account validation — `paper_trading` is now derived from the chosen account's `trading_mode` (`paper` → true). Remove the `startsWith("DU"/"DF"/"U")` browser checks entirely (US-004 / PRD §6 — no prefix inference).

- [ ] **Step 2: Real-money confirm stage** — show the typed-echo confirm stage ONLY when `selectedAccount.is_real_money` (replaces the `!paperTrading` trigger). The challenge string is `selectedAccount.ib_account_id`. `confirmMatches = confirmInput.trim() === selectedAccount.ib_account_id`. The unmistakable label reads e.g. "⚠ REAL FUND — U4715997 — LIVE MONEY". For `test`/`paper` accounts skip the echo stage (explicit target already chosen).

- [ ] **Step 3: Submit** — send the registry-backed body:

```tsx
const result = await startPortfolio(
  {
    portfolio_revision_id: revision.id,
    broker_account_id: selectedAccount.id,
    paper_trading: selectedAccount.trading_mode === "paper",
    confirm_account_id: selectedAccount.is_real_money
      ? confirmInput.trim()
      : undefined,
    selector_context_account_id: scope, // from useAccountScope()
  },
  idempotencyKey,
  token,
);
```

Decode the new 422 codes in the catch block alongside the existing `BINDING_MISMATCH`/`LIVE_DEPLOY_CONFLICT`: `REAL_MONEY_CONFIRM_REQUIRED` / `REAL_MONEY_CONFIRM_MISMATCH` → surface `body.error.message` in the confirm-stage callout (these should be unreachable from the UI happy path since the dialog gates client-side too, but the server is the authority and the message must render if it ever returns).

- [ ] **Step 4: Idempotency-key rotation** — update the `useEffect` dep list (line 232-234) from `[accountId, ibLoginKey, paperTrading]` to `[selectedAccount?.id]` (the new identity-bearing input).

- [ ] **Step 5: Verify** — `cd frontend && pnpm lint && pnpm build` → clean. Manually confirm testids: `portfolio-start-target-account`, `portfolio-start-confirm-input`, `portfolio-start-deploy-button` retained for E2E.

- [ ] **Step 6: Commit** — `git add frontend/src/components/live/portfolio-start-dialog.tsx && git commit -m "feat(ui): registry-backed explicit deploy target + real-money typed echo (server-authoritative)"`

> MOUNT POINT (iter-1 P3#11 — VERIFIED, corrects the earlier stale claim): `PortfolioStartDialog` IS reachable today — it is mounted at `frontend/src/app/portfolio/runs/[runId]/page.tsx:548`, opened by the "Deploy as Live Portfolio" promote flow on the portfolio-run results page (the `/live-trading/portfolio` route redirects to `/portfolio/new` per the 2026-05-17 decision; the composer there backtests, then promotes from the run results). That page is inside `AppShell`, so `useAccountScope()` is available. **The UI E2E use case (UC-UI-2) must drive the dialog from the portfolio-run-results promote flow, NOT from `/live-trading`.** No new entry point is needed.

---

## Task 12: Settings broker-account UI — real-money keys off `is_real_money`, not `trading_mode` (iter-1 P1#4)

**Files:**

- Modify: `frontend/src/components/broker-accounts/broker-accounts-table.tsx` (badge at `:55-56`)
- Modify: `frontend/src/components/broker-accounts/broker-account-wizard.tsx` (`:249,272-278`)
- Modify: `frontend/src/components/broker-accounts/broker-account-detail.tsx` (`:374-375`)

**Why (VERIFIED):** the settings UI today equates `trading_mode === "live"` with "real money" (`broker-accounts-table.tsx:55-56` styles live as distinct "real money"; the wizard labels the dropdown "Live (real money)" `:249` and shows the REAL MONEY warning for any `live` `:272-278`). Under PR4's model LVP/HVP are `live` but `account_class==test` — NOT the fund — so the current UI wrongly flags them as fund-grade real money, and the fund cannot be registered as `real` from the UI at all. This is exactly the "never infer real-money from mode/string in the frontend" the PRD §6 bans. (P1, not deferrable — NO BUGS LEFT BEHIND.)

- [ ] **Step 1: Table badge keys off `is_real_money`.** In `broker-accounts-table.tsx`, the real-money/fund badge styling + label reads `account.is_real_money` (the new field from Task 10), NOT `account.trading_mode === "live"`. Render three honest states: paper (`trading_mode==="paper"`), "Live · Test" (`live` + `!is_real_money`), and "Live · REAL FUND" (`is_real_money`, destructive styling). Add/extend the table test (`broker-accounts.spec.ts` exists) or a render assertion.

- [ ] **Step 2: Wizard gains an account-class control for live accounts.** In `broker-account-wizard.tsx`, when `tradingMode === "live"` show a Test/Fund choice (default **Test**), and send `account_class` ("test" or "real") on create via `BrokerAccountCreate` (Task 10 type widened to include it — add `account_class?: string`). Relabel the trading-mode dropdown item from "Live (real money)" to just "Live"; the REAL-MONEY warning fires only when the operator picks the **Fund** class (not merely `live`). Keep the existing DU/DF-vs-U prefix UX pre-check (it's a trading_mode/prefix guard, unrelated to the real-money inference being removed).

- [ ] **Step 3: Detail shows `account_class` read-only.** In `broker-account-detail.tsx`, display the account class (paper/test/REAL FUND) read-only alongside `trading_mode`; do NOT make `account_class` editable (the fund's class is set at registration). Relabel its "Live (real money)" select item to "Live".

- [ ] **Step 4: Add `account_class` to the FE `BrokerAccountCreate` type** in `broker-accounts.ts` (`account_class?: string`), so the wizard can send it (additive; backend Task 3 validator defaults it when omitted).

- [ ] **Step 5: Verify** — `cd frontend && pnpm lint && pnpm build` → clean.

- [ ] **Step 6: Commit** — `git add frontend/src/components/broker-accounts/ frontend/src/lib/api/broker-accounts.ts && git commit -m "feat(ui): settings broker UI real-money keys off is_real_money; wizard sets account_class"`

---

## E2E Use Cases (Phase 3.2b)

**Surface coverage decision** (`surfaces: [API, CLI, UI]` per CLAUDE.md):

- **API — Covered** (UC-API-1): the deploy contract + real-money gate is public/operator API.
- **CLI — Covered** (UC-CLI-1): `msai live start-portfolio` is the operator CLI surface for the same capability; PR4 adds `--confirm-account-id`.
- **UI — Covered** (UC-UI-1 global selector, UC-UI-2 explicit confirmed target).

Run order (fullstack): **API first, then CLI, then UI.**

### UC-API-1 — Real-money deploy demands a matching identity confirmation

- **Actor:** API/CLI operator wiring a deploy script against the fund account.
- **Scenario:** They have a frozen portfolio revision and a registered real-money (fund) broker account. They must place the deploy programmatically and prove that a wrong/absent confirmation is refused while a matching one is accepted — so the script can never hit the fund by accident.
- **Interface:** API
- **Intent:** The operator confirms the platform refuses a real-money deploy unless the confirmation matches the account, then succeeds when it matches.
- **Setup:** Register a broker account with `account_class="real"` via `POST /api/v1/broker-accounts` (sanctioned API); freeze a portfolio revision via the public live-portfolio API. (Do NOT pre-deploy — that's the action under test.)
- **Steps:** (1) `POST /api/v1/live/start-portfolio` with `broker_account_id` + `paper_trading:false` and NO `confirm_account_id`. (2) Repeat with a wrong `confirm_account_id`. (3) Repeat with `confirm_account_id == ib_account_id`.
- **Verification:** Step 1 → 422, error body code `REAL_MONEY_CONFIRM_REQUIRED` with an actionable message naming the account; Step 2 → 422 `REAL_MONEY_CONFIRM_MISMATCH`; Step 3 → not a confirm-gate 422 (the deploy proceeds past the gate; the response body advances to deploy/halt/supervisor handling). The operator can read the error body to fix the script.
- **Persistence:** Re-request `GET /api/v1/live/status` after the matching deploy; the deployment (if it reached running) lists under the fund `account_id`; the two rejected attempts created no deployment row (status list does not contain a deployment for the rejected attempts).

### UC-CLI-1 — Operator deploys to the fund from the CLI with identity confirmation

- **Actor:** Operator running `msai live start-portfolio` on the prod VM toolbox.
- **Scenario:** They need to deploy a frozen revision to the fund account from the shell and want the CLI to carry the same identity confirmation the server enforces, so a fat-fingered account can't slip through.
- **Interface:** CLI
- **Intent:** The operator deploys to the fund and the CLI both prompts for the real-money confirm AND sends the identity token the server checks.
- **Setup:** Register the real-money broker account via `msai`/API; freeze a revision. Export `MSAI_API_URL`/`MSAI_API_KEY`.
- **Steps:** (1) Run `msai live start-portfolio --revision <id> --broker-account-id <fund-uuid> --no-paper` WITHOUT `--confirm-account-id`. (2) Re-run WITH `--confirm-account-id <fund-ib-account-id>`.
- **Verification:** Step 1 → the server returns 422 and the CLI stderr explains `REAL_MONEY_CONFIRM_REQUIRED` (non-zero exit). Step 2 → after the typed `y` real-money prompt, stdout shows the deploy JSON (no confirm-gate 422); the human-readable error in step 1 names the account.
- **Persistence:** Run `msai live status`; the fund deployment from step 2 (if running) appears bound to the fund account id; step 1 left no deployment.

### UC-UI-1 — Operator focuses the dashboard on one account and it sticks

- **Actor:** Fleet operator managing LVP/HVP/fund from the web dashboard.
- **Scenario:** With multiple accounts live, they want to focus the live-trading fleet on one account and have that focus survive navigating away and reloading, so tomorrow's session opens on the same scope.
- **Interface:** UI
- **Intent:** The operator scopes the dashboard to one account and trusts it persists across navigation and reload.
- **Setup:** Authenticate via the documented dev/E2E auth path; have ≥1 registered account and ≥1 live deployment (seed via public API). (Do NOT pre-set the selector — that's the action under test.)
- **Steps:** Open `/live-trading` → open the top-bar account selector → choose a single account → navigate to another page and back → reload the page.
- **Verification:** The fleet view re-scopes to show only that account's deployments; the selector trigger reads the chosen account's label; after navigating back AND after a full reload the selector still reads the chosen account and the fleet stays scoped (no hydration console error).
- **Persistence:** Reload `/live-trading`; the selector still shows the chosen account and the fleet view is still scoped to it.

### UC-UI-2 — Operator deploys to an explicit, confirmed target (never implicit)

- **Actor:** Fleet operator about to deploy a revision.
- **Scenario:** They've scoped the dashboard to "All accounts" and need to deploy a revision; the platform must force them to pick a single explicit target (and, for the fund, confirm its identity) before Deploy is enabled — so they never deploy to the fund by accident.
- **Interface:** UI
- **Intent:** The operator deploys only after explicitly choosing and confirming the target account.
- **Setup:** Authenticate via the documented dev/E2E auth path; register a real-money (fund) account + a test account via the public API; compose + backtest a portfolio via `/portfolio/new` and open its run-results page (`/portfolio/runs/<runId>`) where "Deploy as Live Portfolio" mounts `PortfolioStartDialog` (the real mount point — see Task 11 MOUNT POINT). Global selector left on "All". (Do NOT pre-select a target — that's under test.)
- **Steps:** From the portfolio run results, with the global selector on "All", click "Deploy as Live Portfolio" → **complete the promote modal that opens first** (iter-8 P2 — VERIFIED `portfolio/runs/[runId]/page.tsx:485` opens a promote modal; `PortfolioStartDialog` mounts only AFTER promote succeeds at `:547`) → once the deploy dialog opens, observe Deploy is disabled with a "pick a target" notice → pick the fund account as the explicit target → observe the real-money confirm field → type the exact fund account id → Deploy.
- **Verification:** With "All" selected the Deploy action is disabled with an inline "pick a target account" message; selecting the fund reveals an unmistakable "REAL FUND … LIVE MONEY" label and a typed-confirm field; Deploy enables only once the typed id matches the fund account id; on submit the operator sees the deploy succeed (toast/redirect) OR a server error rendered from the 422 body. Selecting the test account instead shows NO typed-echo field (explicit target suffices).
- **Persistence:** Navigate to `/live-trading`, scope the global selector to the fund, and confirm the new fund deployment appears in the fleet under that account; re-opening the deploy dialog with the selector on "All" still shows the Deploy-disabled behavior.

---

## Self-Review

**Spec coverage:** US-001→Tasks 7-9 (live-trading **and** dashboard scoping) + UC-UI-1; US-002→Task 11 + UC-UI-2; US-003→Tasks 4-6 + UC-API-1/UC-CLI-1; US-004→Tasks 1-3,8,11,12; US-005→Task 9 Step 3 (connected gateway account id on `/account/health` + honest label). Council objections #1-#7 all mapped (1→Task4/5; 2,5→Task8/11/12 labels; 3→Task5; 4→Task1-3; 6→Task8 Unassigned; 7→Task5 divergence). Open questions resolved in D1-D3; legacy-null-row existence verified at implementation (Task 8/9 read `/live/status`, no assumption).

**Plan-review iter-1 fixes folded in:** P1#1 (Task 3 Step 5 — service+router persist `account_class`), P1#2 (Task 9 Step 2 — dashboard page scoped), P1#3 (Task 8 — invalid-scope reset effect), P1#4 (new Task 12 — settings UI keys off `is_real_money`); P2#5 (D6/Task 5 — exclude "all"/"unassigned" from divergence), P2#6 (Task 5 — correct `broker_account_metrics._r.counter` factory), P2#7 (Task 7 — `initializeWithValue:false`), P2#8 (Task 6 — `--confirm-account-id` on `live start` too), P2#9 (Task 9 Step 3 — `/account/health` connected account id); P3#10 (D2 — Codex agreed, no change), P3#11 (Task 11 MOUNT POINT + UC-UI-2 — corrected to the portfolio-run promote flow).

**Plan-review iter-2 fixes folded in:** P1#1 (Task 9 Step 1 — scope REST positions/P&L by `deployment_id`, verified `LivePositionItem.deployment_id` exists), P1#2 (Task 5 — `AMBIGUOUS_DEPLOY_TARGET` 422 when `broker_account_id` + a conflicting legacy `account_id` are both sent, + test); P2 (Task 3 Step 5 — service derives `account_class` from `trading_mode` when omitted, not hardcoded `"test"`, + omitted-path tests), P2 (Task 9 Step 3 — explicitly modify `ib_account_snapshot.py` to cache + expose the connected `account_id`, honest `None` fallback, + test).

**Plan-review iter-3 fixes folded in:** P1#1 (Task 9 Step 1 — `<KillSwitch>` counts stay FLEET-WIDE, NOT scoped, so the emergency-stop blast radius is never understated; only the table + P&L cards scope), P1#2 (Task 1/Task 3 — String-backed `account_class` reloads as `str`; tests assert `== "real"` not `.value`; `is_real_money` relies on robust `StrEnum` equality); P2 (Task 9 Step 3 — connected-account label lives in `portfolio-summary.tsx:129-151`; dashboard adds an `/account/health` query + passes `account_id` to `PortfolioSummary`, file added to git), P2 (Task 2 — migration test rewritten to the REAL harness: dedicated `PostgresContainer` + `run_alembic(url, …)` + raw seed INSERTs + downgrade-to-parent + assert backfill).

**Plan-review iter-4 fixes folded in:** P1 (Task 9 Step 1 — leave `<KillSwitch>` `activeCount`/`positionCount`/`activeRealDeployment` EXACTLY as today; `positionsForTable` is WS-one-deployment when connected so it is never repurposed as a fleet count; scoped vars are additive, display-only); P2 (Task 2 — migration test asserts column present+NOT NULL via `conn.run_sync(inspect)` after upgrade AND absent after `downgrade -1`); P2 (Task 6 Step 4 — CLI `account_class` parity: `broker add --account-class`, `broker list` shows it, `--no-paper` prompt relabeled "LIVE" not "REAL-MONEY" since the fund-specific gate is the server's `--confirm-account-id`).

**Plan-review iter-5 fixes folded in:** P1 (Task 9 Step 1 — scoped DISPLAY positions source from `restPositions` (fleet REST) when scoped, NOT the WS one-deployment `positionsForTable`, so the P&L cards reflect the selected account even when the WS streams a different deployment), P1 (Task 8 — reset rule reconciled with US-004: reset to "all" ONLY when the scope is absent from the WHOLE option union; an archived account with residual deployments stays as "Unknown/retired"), P1 (Task 9 Step 3 — US-005 label also applied to the `/account` page summary/portfolio cards, which already have a `health` query — the page where the gateway-bound caveat matters most); P2 (Task 5 — gate tests live in the broker-account-AWARE harness `test_live_start_broker_account.py` that installs `gateway_router`+`broker_credentials_store`, since resolution runs before the gate).

**Plan-review iter-6→8 fixes folded in (all narrow consistency / test-harness mechanics; architecture unchanged):** iter-6 — corrected a contradictory scope sentence (Task 9 no longer scopes `activeRealDeployment`/`activeCount`) + two wrong test-file dirs (`test_broker_account_service.py` and `test_broker_cli.py` are under `integration/`, not `unit/`). iter-7 — Task 5 gate-test fixtures need a LIVE `GatewayRouter` config binding the U-prefix accounts (the harness binds DU-only) or row-state validation 422s before the gate; `account_health` reads the connected id via the module-level snapshot accessor (NOT a `Depends` param) so the direct-call test `test_account_probe_lifecycle.py:175` survives. iter-8 — Task 5 snippets unpack the `client` tuple (`ac, _, _ = client`) + `_seed_broker_account` extended with `account_class`; the `account_health` guard was dropped (`get_snapshot()` lazy-creates, never raises); UC-UI-2 completes the promote modal before the deploy dialog mounts.

**Placeholder scan:** code shown for every code step; the migration-backfill-harness step references the existing `test_broker_account_fk_migration.py` pattern with a concrete fallback; the `PortfolioStartDialog` mount point is now VERIFIED (`portfolio/runs/[runId]/page.tsx:548`) and the UI UC targets it — no remaining ambiguity.

**Type consistency:** `account_class` (str enum value) + `is_real_money` (bool) names identical across model (Task 1), response/create (Task 3), FE types (Task 10), selector (Task 8), dialog (Task 11), settings UI (Task 12). `confirm_account_id`/`selector_context_account_id` identical across schema (Task 4), gate (Task 5), CLI (Task 6), FE client (Task 10), dialog (Task 11). Error codes `REAL_MONEY_CONFIRM_REQUIRED`/`REAL_MONEY_CONFIRM_MISMATCH` identical across Task 5 + UC-API-1/UC-CLI-1 + Task 11 decode.
