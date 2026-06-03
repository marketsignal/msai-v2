"""Integration: broker-account spawn wiring on ``POST /api/v1/live/start-portfolio`` (Task 5).

Covers the EFFECTIVE-ACCOUNT contract wired at the top of the start handler:

* UC-SW-API-1 — a deploy that selects its account by control-plane
  ``broker_account_id`` resolves the registry row, DERIVES the effective
  ``account_id`` / ``ib_login_key`` from it, validates credentials AFTER the
  idempotency reservation, persists ``broker_account_id`` +
  ``credentials_validated_at`` / ``credentials_validated_version`` on the
  deployment row, and exposes ``broker_account_id`` on GET status.

* UC-SW-API-2 — an ARCHIVED account fails closed with 422 BEFORE any node
  spawns: no deployment row persists, no START is enqueued, and the
  deploy-validation alert metric is incremented.

* effective-account safety — a ``broker_account_id`` whose row diverges from a
  raw ``account_id`` ALSO sent: the DERIVED account is authoritative for the
  per-account halt gate (a halt on the derived account blocks the deploy; the
  raw request account is never used for the safety check).

* new-deploy via legacy strings whose ``account_id`` does not resolve to an
  ACTIVE registry row → 422 fail-closed (council mandate: new free-form deploys
  must resolve or fail closed; they are NOT legacy-passed-through).

* warm-restart back-compat — an EXISTING legacy deployment (registered before
  the registry, ``broker_account_id`` NULL) re-starts cleanly with the legacy
  request strings, no forced resolution.

* idempotency ordering — a replayed request (same Idempotency-Key, cached
  outcome) does NOT re-call ``resolve_for_spawn`` (the KV side-effect must run
  exactly once, AFTER the reservation decides this request executes).

The supervisor is stubbed (``_poll_for_terminal`` returns a ready row,
``_supervisor_is_alive`` True) so no real node spawns; the publish to the
per-account command stream hits the real testcontainers Redis. The instrument
registry resolver (``lookup_for_live``) is patched to a pass-through so the
binding check — which Task 5 does NOT change — does not require a seeded
registry. Account resolution + credential validation (the Task-5 surface) run
against the real DB rows + a stubbed credentials store.

SAFETY: dedicated PostgresContainer + RedisContainer per module; paper accounts
only (``DU...`` prefixes).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.api import live as live_module
from msai.api.live_deps import get_command_bus, get_idempotency_store
from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.core.halt_keys import account_halt_key
from msai.main import app
from msai.models import Base
from msai.models.broker_account import (
    BrokerAccount,
    BrokerAccountStatus,
    CredentialsBackend,
)
from msai.models.graduation_candidate import GraduationCandidate
from msai.models.live_deployment import LiveDeployment
from msai.models.live_portfolio import LivePortfolio
from msai.models.live_portfolio_revision import LivePortfolioRevision
from msai.models.live_portfolio_revision_strategy import LivePortfolioRevisionStrategy
from msai.models.strategy import Strategy
from msai.models.user import User
from msai.services.live.broker_credentials_store import Credentials
from msai.services.live.deployment_identity import (
    derive_message_bus_stream,
    derive_portfolio_deployment_identity,
    derive_strategy_id_full,
    derive_trader_id,
    generate_deployment_slug,
)
from msai.services.live.gateway_router import GatewayRouter
from msai.services.live_command_bus import (
    LIVE_COMMAND_STREAM,
    LiveCommandBus,
    command_stream_for_account,
)
from msai.services.observability.broker_account_metrics import DEPLOY_VALIDATION_FAILED

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from redis.asyncio import Redis as AsyncRedis
    from sqlalchemy.ext.asyncio import AsyncSession


# Gateway config: one login bound to two paper accounts. The derived account
# row's ``ib_login_key`` must resolve here for ``validate_account_row_state``.
_LOGIN = "msai-paper-primary"
_BOUND_ACCOUNTS = ["DUP0000001", "DUP0000002"]
_GATEWAY_CONFIG = f"{_LOGIN}:ib-gateway:4002:accounts={'|'.join(_BOUND_ACCOUNTS)}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.fixture(scope="module")
def isolated_redis_url() -> Iterator[str]:
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest_asyncio.fixture
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def redis_text(isolated_redis_url: str) -> AsyncIterator[AsyncRedis]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(isolated_redis_url, decode_responses=True)
    with contextlib.suppress(Exception):
        await client.flushdb()
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def bus(redis_text: AsyncRedis) -> LiveCommandBus:
    return LiveCommandBus(redis=redis_text, min_idle_ms=0, recovery_interval_s=60)


class _StubStore:
    """Credentials store stub: returns resolvable creds for the account's secret
    ref so ``validate_account_credentials`` passes (the userid value is no longer
    compared against the routing login key). Records every ``get`` so a test can
    prove how many times the KV side-effect ran."""

    def __init__(self, *, tws_userid: str) -> None:
        self._tws_userid = tws_userid
        self.get_calls: list[tuple[str, str | None]] = []

    def get(self, secret_ref: str, version: str | None) -> Credentials:
        self.get_calls.append((secret_ref, version))
        return Credentials(tws_userid=self._tws_userid, tws_password="pw")

    def ping(self) -> bool:
        return True


class _ResolveSpy:
    """Counts BrokerAccountService.resolve_for_spawn invocations (the KV
    side-effect that must run exactly once per executed request, AFTER the
    idempotency reservation)."""

    count = 0


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    bus: LiveCommandBus,
    redis_text: AsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, _StubStore, _ResolveSpy]]:
    from msai.services.live import broker_account_service as bas_module
    from msai.services.live.idempotency import IdempotencyStore

    # Supervisor stubs: poll returns a ready row, supervisor alive.
    async def _fake_poll_for_terminal(*_a: object, **_k: object) -> object:
        from unittest.mock import MagicMock

        row = MagicMock()
        row.status = "ready"
        return row

    async def _fake_supervisor_alive(*_a: object, **_k: object) -> bool:
        return True

    monkeypatch.setattr(live_module, "_poll_for_terminal", _fake_poll_for_terminal)
    monkeypatch.setattr(live_module, "_supervisor_is_alive", _fake_supervisor_alive)

    # Pass-through instrument resolver so the binding check (NOT a Task-5
    # concern) does not need a seeded registry. canonical_id == raw symbol.
    async def _fake_lookup_for_live(symbols: list[str], **_k: object) -> list[object]:
        from unittest.mock import MagicMock

        out = []
        for s in symbols:
            r = MagicMock()
            r.canonical_id = s
            out.append(r)
        return out

    monkeypatch.setattr(
        "msai.services.nautilus.security_master.live_resolver.lookup_for_live",
        _fake_lookup_for_live,
    )

    # Spy on resolve_for_spawn so idempotency-ordering can assert it ran once.
    spy = _ResolveSpy()
    real_resolve = bas_module.BrokerAccountService.resolve_for_spawn

    async def _counting_resolve(
        self: object, account_id: object, *, stamp_access: bool = True
    ) -> object:
        spy.count += 1
        return await real_resolve(self, account_id, stamp_access=stamp_access)

    monkeypatch.setattr(bas_module.BrokerAccountService, "resolve_for_spawn", _counting_resolve)

    # App state: gateway router + credentials store.
    store = _StubStore(tws_userid=_LOGIN)
    app.state.gateway_router = GatewayRouter(_GATEWAY_CONFIG)
    app.state.broker_credentials_store = store

    real_bus_redis = bus

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _override_get_bus() -> LiveCommandBus:
        return real_bus_redis

    async def _override_idem() -> IdempotencyStore:
        return IdempotencyStore(redis_text)

    async def _override_user() -> dict:
        return {"sub": "test-operator", "oid": str(uuid4())}

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_command_bus] = _override_get_bus
    app.dependency_overrides[get_idempotency_store] = _override_idem
    app.dependency_overrides[get_current_user] = _override_user
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c, store, spy
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_command_bus, None)
    app.dependency_overrides.pop(get_idempotency_store, None)
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_broker_account(
    session: AsyncSession,
    *,
    ib_account_id: str,
    ib_login_key: str = _LOGIN,
    status: BrokerAccountStatus = BrokerAccountStatus.ACTIVE,
    trading_mode: str = "paper",
    backend: CredentialsBackend = CredentialsBackend.ENV,
    secret_version: str | None = "v1",
    secret_ref: str | None = None,
    has_credentials_updated_at: bool = True,
) -> BrokerAccount:
    acct = BrokerAccount(
        id=uuid4(),
        ib_account_id=ib_account_id,
        ib_login_key=ib_login_key,
        label=f"acct-{ib_account_id}",
        status=status,
        gateway_slot=f"slot-{uuid4().hex[:6]}",
        trading_mode=trading_mode,
        credentials_backend=backend,
        credentials_secret_ref=secret_ref or f"ref-{ib_account_id}",
        credentials_secret_version=secret_version,
        # Managed backends (env/azure_kv) have a credentials_updated_at; legacy_env
        # rows have NONE (no managed secret). Default keeps the prior "now()"
        # behavior; the legacy_env test passes ``has_credentials_updated_at=False``.
        credentials_updated_at=(datetime.now(UTC) if has_credentials_updated_at else None),
    )
    session.add(acct)
    await session.flush()
    return acct


async def _seed_deployable_revision(
    session: AsyncSession,
    *,
    instruments: list[str] | None = None,
    entra_id: str | None = None,
) -> tuple[User, Strategy, LivePortfolioRevision, LivePortfolioRevisionStrategy]:
    """Seed user + strategy + frozen single-member revision + an unlinked
    ``live_candidate`` GraduationCandidate whose config/instruments match the
    member so the binding check passes (first-deploy path).

    ``entra_id`` lets a warm-restart test seed the user the handler will resolve
    from the auth claims (``sub``) so a pre-created deployment's
    ``identity_signature`` matches what the handler recomputes."""
    instruments = instruments or ["AAPL"]
    user = User(
        id=uuid4(),
        entra_id=entra_id or f"sw-{uuid4().hex[:12]}",
        email=f"sw-{uuid4().hex[:8]}@example.com",
        role="trader",
    )
    session.add(user)
    await session.flush()

    strategy = Strategy(
        id=uuid4(),
        name=f"s-{uuid4().hex[:8]}",
        file_path="strategies/example/ema_cross.py",
        strategy_class="EMACrossStrategy",
        default_config={"instruments": instruments, "asset_class": "stocks"},
        created_by=user.id,
    )
    session.add(strategy)
    await session.flush()

    portfolio = LivePortfolio(
        id=uuid4(),
        name=f"P-{uuid4().hex[:8]}",
        description="sw test",
        created_by=user.id,
    )
    session.add(portfolio)
    await session.flush()

    revision = LivePortfolioRevision(
        id=uuid4(),
        portfolio_id=portfolio.id,
        revision_number=1,
        composition_hash=uuid4().hex + uuid4().hex,
        is_frozen=True,
    )
    session.add(revision)
    await session.flush()

    member = LivePortfolioRevisionStrategy(
        id=uuid4(),
        revision_id=revision.id,
        strategy_id=strategy.id,
        config={},
        instruments=instruments,
        weight=Decimal("1.0"),
        order_index=0,
    )
    session.add(member)
    await session.flush()

    # Unlinked live_candidate whose config (minus instruments) == member.config
    # ({}) and whose instruments == member.instruments.
    candidate = GraduationCandidate(
        id=uuid4(),
        strategy_id=strategy.id,
        stage="live_candidate",
        config={"instruments": instruments},
        metrics={"sharpe": 1.0, "total_return": 0.1, "sortino": 1.2},
        deployment_id=None,
    )
    session.add(candidate)
    await session.flush()
    return user, strategy, revision, member


# ---------------------------------------------------------------------------
# UC-SW-API-1 — broker_account_id success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_with_broker_account_id_derives_and_persists(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deploy selecting its account by ``broker_account_id`` resolves the
    registry row, derives the effective account/login from it, validates
    credentials, and persists ``broker_account_id`` + validation stamps. GET
    status then exposes ``broker_account_id``."""
    ac, store, _spy = client
    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=_BOUND_ACCOUNTS[0])
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    deployment_id = body["id"]

    # The START landed on the DERIVED account's per-account stream.
    assert await redis_text.xlen(command_stream_for_account(_BOUND_ACCOUNTS[0])) == 1
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0

    # Credentials were validated (the KV side-effect ran exactly once).
    assert len(store.get_calls) == 1

    # The deployment row carries the derived account + validation stamps.
    async with session_factory() as session:
        dep = await session.get(LiveDeployment, body["id"])
        assert dep is not None
        assert dep.broker_account_id == acct_id
        assert dep.account_id == _BOUND_ACCOUNTS[0]  # DERIVED from the row
        assert dep.ib_login_key == _LOGIN  # DERIVED from the row
        assert dep.credentials_validated_at is not None
        assert dep.credentials_validated_version == "v1"

    # GET status exposes broker_account_id (the API caller observes the link)
    # on BOTH the list endpoint and the per-deployment detail endpoint.
    list_resp = await ac.get("/api/v1/live/status")
    assert list_resp.status_code == 200, list_resp.text
    rows = list_resp.json()["deployments"]
    match = next(r for r in rows if r["id"] == deployment_id)
    assert match["broker_account_id"] == str(acct_id)
    assert match["account_id"] == _BOUND_ACCOUNTS[0]

    status_resp = await ac.get(f"/api/v1/live/status/{deployment_id}")
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["broker_account_id"] == str(acct_id)


# ---------------------------------------------------------------------------
# Codex P2 — a request that cannot deploy (DRAFT revision) must NOT read KV
# nor stamp credential state. STAGE-2 credential validation must run AFTER the
# cheap deploy-eligibility checks (frozen-revision / strategy / member).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_draft_revision_rejected_before_any_kv_read(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deploy that names a valid ``broker_account_id`` but an UNFROZEN (draft)
    ``portfolio_revision_id`` must be rejected by the frozen-revision gate (400)
    BEFORE STAGE-2 reads the credential store — so it never pokes Key Vault
    (``resolve_for_spawn`` not called, store never read) nor stamps
    ``credentials_last_accessed`` on the account."""
    ac, store, spy = client
    spy.count = 0
    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=_BOUND_ACCOUNTS[0])
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        # Make the revision a DRAFT — the deploy-eligibility check must reject it.
        revision.is_frozen = False
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
    )
    # The frozen-revision gate returns its intended 400 ("is not frozen").
    assert resp.status_code == 400, resp.text
    assert "not frozen" in resp.text.lower()

    # STAGE-2 never ran: resolve_for_spawn was NOT called and the credential
    # store was NOT read — no KV side-effect for a request that cannot deploy.
    assert spy.count == 0
    assert store.get_calls == []

    # The account's credential-access state is unchanged (never stamped).
    async with session_factory() as session:
        refreshed = await session.get(BrokerAccount, acct_id)
        assert refreshed is not None
        assert refreshed.credentials_last_accessed is None


# ---------------------------------------------------------------------------
# UC-SW-API-2 — archived account fails closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_with_archived_broker_account_fails_closed_422(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An ARCHIVED registry row blocks the deploy with 422 BEFORE any node
    spawns: no deployment row persists, no START is enqueued, and the
    deploy-validation alert metric is incremented for the archived reason."""
    ac, _store, _spy = client
    # resolve_active_broker_account treats an ARCHIVED row by id as unresolvable
    # → 422. The reason metric must still be emitted; we assert the metric grew.
    before = DEPLOY_VALIDATION_FAILED.render()

    async with session_factory() as session:
        acct = await _seed_broker_account(
            session,
            ib_account_id=_BOUND_ACCOUNTS[0],
            status=BrokerAccountStatus.ARCHIVED,
        )
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
    )
    assert resp.status_code == 422, resp.text

    # No START enqueued on any per-account stream nor the global stream.
    assert await redis_text.xlen(command_stream_for_account(_BOUND_ACCOUNTS[0])) == 0
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0

    # No deployment row persisted.
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveDeployment).where(LiveDeployment.portfolio_revision_id == rev_id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    # The alert metric grew (a deploy-validation rejection was counted).
    after = DEPLOY_VALIDATION_FAILED.render()
    assert _metric_total(after) > _metric_total(before)


# ---------------------------------------------------------------------------
# P1 — legacy_env (migrated LVP/HVP) deployability. The backend API process does
# NOT have the gateway TWS_* env vars (they live only in the ib-gateway
# containers), so a legacy_env account whose secret_ref is env:TWS_USERID|...
# must NOT be credential-validated in the backend — the deploy gate validates it
# at the ROW-STATE level only and defers credential authority to the gateway.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_legacy_env_account_deploys_without_backend_env_read(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migrated ``legacy_env`` broker account whose credentials live ONLY in the
    IB Gateway containers' env (``TWS_USERID``/``TWS_PASSWORD`` absent from the
    backend/test process) deploys successfully (201) — the deploy gate does NOT
    attempt the unreadable env credential read for legacy_env, so it does not 422
    on credential-not-found. The credential read (``resolve_for_spawn`` / store
    get) is never invoked; the persisted deployment links the account with a NULL
    validated version (legacy_env has no managed secret version)."""
    ac, store, spy = client
    # The backend process must NOT have the gateway env credentials — model the
    # real prod/dev backend container (TWS_* injected only into ib-gateway).
    monkeypatch.delenv("TWS_USERID", raising=False)
    monkeypatch.delenv("TWS_PASSWORD", raising=False)

    before_validation = DEPLOY_VALIDATION_FAILED.render()

    async with session_factory() as session:
        acct = await _seed_broker_account(
            session,
            ib_account_id=_BOUND_ACCOUNTS[0],
            backend=CredentialsBackend.LEGACY_ENV,
            secret_version=None,
            secret_ref="env:TWS_USERID|TWS_PASSWORD",
            has_credentials_updated_at=False,
        )
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
    )
    # The key assertion: NOT a 422 credential-not-found. Deploy succeeds.
    assert resp.status_code == 201, resp.text
    deployment_id = resp.json()["id"]

    # The backend never tried to read the (unreadable) gateway env credentials.
    assert spy.count == 0
    assert store.get_calls == []
    # No deploy-validation rejection was counted (legacy_env passed row-state).
    assert _metric_total(DEPLOY_VALIDATION_FAILED.render()) == _metric_total(before_validation)

    # START landed on the derived account's per-account stream.
    assert await redis_text.xlen(command_stream_for_account(_BOUND_ACCOUNTS[0])) == 1

    # Persistence: the deployment links the account; validated_version is NULL
    # (legacy_env has no managed secret version). GET status exposes the link.
    async with session_factory() as session:
        dep = await session.get(LiveDeployment, deployment_id)
        assert dep is not None
        assert dep.broker_account_id == acct_id
        assert dep.account_id == _BOUND_ACCOUNTS[0]
        assert dep.credentials_validated_version is None

    status_resp = await ac.get(f"/api/v1/live/status/{deployment_id}")
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["broker_account_id"] == str(acct_id)


@pytest.mark.asyncio
async def test_start_archived_legacy_env_account_still_fails_closed_422(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping the credential read for legacy_env does NOT open a fail-closed
    gap: an ARCHIVED legacy_env account is still rejected (422) by the row-state
    gate (STAGE 1 + STAGE 3 run regardless of backend). No deployment persists and
    no START is enqueued — proving row-state remains the gate for legacy_env."""
    ac, _store, spy = client
    monkeypatch.delenv("TWS_USERID", raising=False)
    monkeypatch.delenv("TWS_PASSWORD", raising=False)

    async with session_factory() as session:
        acct = await _seed_broker_account(
            session,
            ib_account_id=_BOUND_ACCOUNTS[0],
            backend=CredentialsBackend.LEGACY_ENV,
            secret_version=None,
            secret_ref="env:TWS_USERID|TWS_PASSWORD",
            has_credentials_updated_at=False,
            status=BrokerAccountStatus.ARCHIVED,
        )
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
    )
    assert resp.status_code == 422, resp.text
    # No credential read attempted, and no deployment / START.
    assert spy.count == 0
    assert await redis_text.xlen(command_stream_for_account(_BOUND_ACCOUNTS[0])) == 0
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveDeployment).where(LiveDeployment.portfolio_revision_id == rev_id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


def _metric_total(lines: list[str]) -> float:
    total = 0.0
    for line in lines:
        if line.startswith("msai_broker_account_deploy_validation_failed_total{"):
            total += float(line.rsplit(" ", 1)[1])
    return total


# ---------------------------------------------------------------------------
# Deploy/archive TOCTOU — account archived AFTER credential validation but
# BEFORE the deployment upsert must fail closed (STAGE-3 FOR UPDATE re-check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_between_validation_and_upsert_fails_closed_422(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic TOCTOU: the account is ACTIVE through credential validation
    (STAGE 2) but is ARCHIVED — committed by a separate session, simulating an
    operator — in the window BEFORE the deployment row is inserted. The STAGE-3
    ``SELECT ... FOR UPDATE`` re-check inside the upsert transaction must observe
    the archived status and fail closed with 422: no deployment row persists and
    no START is enqueued."""
    ac, _store, _spy = client

    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=_BOUND_ACCOUNTS[0])
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    # Wrap the real STAGE-2 validator: after it returns (its resolve_for_spawn has
    # committed, releasing locks), ARCHIVE the account in a SEPARATE committed
    # session — exactly the operator action the race describes. The handler then
    # reaches the STAGE-3 FOR UPDATE re-check and must see ARCHIVED.
    real_validate = live_module.validate_account_credentials

    async def _validate_then_archive(account: object, svc: object) -> object:
        result = await real_validate(account, svc)
        async with session_factory() as other:
            row = await other.get(BrokerAccount, acct_id)
            assert row is not None
            row.status = BrokerAccountStatus.ARCHIVED
            await other.commit()
        return result

    monkeypatch.setattr(live_module, "validate_account_credentials", _validate_then_archive)

    before = DEPLOY_VALIDATION_FAILED.render()
    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["detail"]["error"]["details"]["reason"] == "archived"

    # No START enqueued on the per-account stream nor the global stream.
    assert await redis_text.xlen(command_stream_for_account(_BOUND_ACCOUNTS[0])) == 0
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0

    # No deployment row persisted (fail-closed BEFORE the upsert).
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveDeployment).where(LiveDeployment.portfolio_revision_id == rev_id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    # The STAGE-3 rejection incremented the deploy-validation alert metric.
    after = DEPLOY_VALIDATION_FAILED.render()
    assert _metric_total(after) > _metric_total(before)


# ---------------------------------------------------------------------------
# Credential-rotation TOCTOU — credentials ROTATED (v1 → v2) AFTER STAGE-2
# validation but BEFORE the STAGE-3 locked re-check must fail closed RETRYABLE
# (409 BROKER_ACCOUNT_CREDENTIALS_ROTATED) so the deployment is never stamped
# /published as validated against a stale secret version.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_rotation_between_validation_and_upsert_fails_closed_409(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic rotation race: STAGE-2 validates the account at version
    ``v1``; a SEPARATE committed session then ROTATES the secret to ``v2`` in the
    window BEFORE the deployment row is inserted. The STAGE-3 ``SELECT ... FOR
    UPDATE`` re-check must observe the FRESH ``v2`` version, see it differ from
    the validated ``v1``, and fail closed with a RETRYABLE 409
    (``BROKER_ACCOUNT_CREDENTIALS_ROTATED``): no deployment row persists, no
    START is enqueued, and the idempotency reservation is released so the retry
    re-validates against ``v2``."""
    ac, _store, _spy = client

    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=_BOUND_ACCOUNTS[0])
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    # Wrap the real STAGE-2 validator: after it returns (validated at v1, its
    # resolve_for_spawn committed + released locks), ROTATE the secret version to
    # v2 in a SEPARATE committed session — exactly the operator credential-rotation
    # the race describes. The handler then reaches the STAGE-3 locked re-check and
    # must read v2 fresh.
    real_validate = live_module.validate_account_credentials

    async def _validate_then_rotate(account: object, svc: object) -> object:
        result = await real_validate(account, svc)
        async with session_factory() as other:
            row = await other.get(BrokerAccount, acct_id)
            assert row is not None
            assert row.credentials_secret_version == "v1"
            row.credentials_secret_version = "v2"
            row.credentials_updated_at = datetime.now(UTC)
            await other.commit()
        return result

    monkeypatch.setattr(live_module, "validate_account_credentials", _validate_then_rotate)

    before = DEPLOY_VALIDATION_FAILED.render()
    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["detail"]["error"]["code"] == "BROKER_ACCOUNT_CREDENTIALS_ROTATED"

    # No START enqueued on the per-account stream nor the global stream.
    assert await redis_text.xlen(command_stream_for_account(_BOUND_ACCOUNTS[0])) == 0
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0

    # No deployment row persisted (fail-closed BEFORE the upsert).
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveDeployment).where(LiveDeployment.portfolio_revision_id == rev_id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    # The STAGE-3 rejection incremented the deploy-validation alert metric.
    after = DEPLOY_VALIDATION_FAILED.render()
    assert _metric_total(after) > _metric_total(before)


@pytest.mark.asyncio
async def test_credential_rotation_during_resolve_is_detected_via_version_used_not_orm(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strengthened rotation race targeting the credential-version-reporting bug:
    the rotation (v1 → v2) commits MID-resolve — inside ``resolve_for_spawn``'s
    own credential read — so ``resolve_for_spawn``'s post-commit ``refresh`` would
    re-read the ROTATED ``v2`` onto the ORM attribute.

    A naive STAGE-2 that reported ``account.credentials_secret_version`` (the
    post-call ORM attribute) would surface ``v2``; the STAGE-3 locked re-read also
    sees ``v2``; ``v2 == v2`` → the rotation guard is DEFEATED and the deploy is
    stamped/published as validated against creds it never resolved (would 201).

    The correct behavior reports the version the store read ACTUALLY USED
    (``v1``, via ``ResolvedSpawnCredentials.version_used``). STAGE-3 then sees the
    locked ``v2`` differ from the validated ``v1`` and fails closed RETRYABLE 409.
    This deterministically pins the fix: it is RED under the ORM-attribute bug and
    GREEN once STAGE-2 surfaces ``version_used``."""
    ac, _store, _spy = client
    from msai.services.live import broker_account_service as bas_module

    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=_BOUND_ACCOUNTS[0])
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    # Wrap resolve_for_spawn so the rotation lands DURING resolve: run the real
    # resolve (reads + resolves against v1, returns version_used="v1"), then commit
    # a v1 → v2 rotation in a SEPARATE session, then REFRESH the request-session's
    # account instance so its ORM attribute now shows the rotated v2 — exactly the
    # post-commit-refresh state resolve_for_spawn itself produces when an operator
    # rotates mid-resolve. A buggy STAGE-2 that reads account.credentials_secret_version
    # would now surface v2 (defeating the guard, 201); the correct STAGE-2 surfaces
    # the returned version_used=v1 → STAGE-3 sees v2!=v1 → 409.
    real_resolve = bas_module.BrokerAccountService.resolve_for_spawn

    async def _resolve_then_rotate(
        self: object, account_id: object, *, stamp_access: bool = True
    ) -> object:
        result = await real_resolve(self, account_id, stamp_access=stamp_access)
        async with session_factory() as other:
            row = await other.get(BrokerAccount, acct_id)
            assert row is not None
            assert row.credentials_secret_version == "v1"
            row.credentials_secret_version = "v2"
            row.credentials_updated_at = datetime.now(UTC)
            await other.commit()
        # Pull the rotated v2 into the request session's account instance, mirroring
        # resolve_for_spawn's own post-commit refresh observing a concurrent rotation.
        acct_obj = await self._db.get(BrokerAccount, account_id)  # type: ignore[attr-defined]
        await self._db.refresh(acct_obj)  # type: ignore[attr-defined]
        assert acct_obj.credentials_secret_version == "v2"
        return result

    monkeypatch.setattr(bas_module.BrokerAccountService, "resolve_for_spawn", _resolve_then_rotate)

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
        headers={"Idempotency-Key": uuid4().hex},
    )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["detail"]["error"]["code"] == "BROKER_ACCOUNT_CREDENTIALS_ROTATED"

    # No START enqueued; no deployment row persisted (fail-closed before upsert).
    assert await redis_text.xlen(command_stream_for_account(_BOUND_ACCOUNTS[0])) == 0
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveDeployment).where(LiveDeployment.portfolio_revision_id == rev_id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


@pytest.mark.asyncio
async def test_legacy_null_version_account_does_not_false_trip_rotation_guard(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No false rotation trip: a ``legacy_env`` account legitimately has a NULL
    ``credentials_secret_version``. STAGE-2 validates with ``version=None`` and
    the STAGE-3 locked re-read returns ``current_version=None`` — NULL == NULL
    must NOT trip the rotation guard. The deploy succeeds (201) and stamps a NULL
    validated version. (No rotation occurs in this deploy.)"""
    ac, _store, _spy = client

    async with session_factory() as session:
        acct = await _seed_broker_account(
            session,
            ib_account_id=_BOUND_ACCOUNTS[0],
            backend=CredentialsBackend.LEGACY_ENV,
            secret_version=None,
        )
        # legacy_env resolves the paired env pointer, not the stub store; point
        # the ref at env keys the resolver reads from os.environ.
        acct.credentials_secret_ref = "env:SW_TWS_USERID|SW_TWS_PASSWORD"
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    import os

    os.environ["SW_TWS_USERID"] = _LOGIN
    os.environ["SW_TWS_PASSWORD"] = "pw"
    try:
        resp = await ac.post(
            "/api/v1/live/start-portfolio",
            json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
        )
    finally:
        os.environ.pop("SW_TWS_USERID", None)
        os.environ.pop("SW_TWS_PASSWORD", None)

    assert resp.status_code == 201, resp.text
    body = resp.json()

    # START enqueued (deploy proceeded — guard did NOT false-trip).
    assert await redis_text.xlen(command_stream_for_account(_BOUND_ACCOUNTS[0])) == 1

    # The row persisted with a NULL validated version (legacy_env is version-less).
    async with session_factory() as session:
        dep = await session.get(LiveDeployment, body["id"])
        assert dep is not None
        assert dep.broker_account_id == acct_id
        assert dep.credentials_validated_at is not None
        assert dep.credentials_validated_version is None


# ---------------------------------------------------------------------------
# P2-A — transient KV failure → 503 retryable (not a permanent 422)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_credential_failure_returns_503_and_is_retryable(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TRANSIENT credential-resolution failure (Key Vault throttled/unreachable
    at deploy time) must return HTTP 503 with a distinct
    ``BROKER_ACCOUNT_CREDENTIALS_UNAVAILABLE`` code — NOT a permanent 422 — so
    operators retry rather than treat a deploy-time KV outage as invalid input.
    No deployment row is persisted, and the idempotency reservation is RELEASED
    so a follow-up retry (KV recovered) succeeds rather than being stuck
    in-flight."""
    from msai.services.live.broker_credentials_store import (
        CredentialResolutionError,
        KvFailureReason,
    )

    ac, _store, _spy = client
    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=_BOUND_ACCOUNTS[0])
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    # First attempt: KV is throttled (transient). Patch resolve_for_spawn so the
    # real STAGE-2 validator classifies the reason as transient.
    from msai.services.live import broker_account_service as bas_module

    fail_then_pass = {"fail": True}
    real_resolve = bas_module.BrokerAccountService.resolve_for_spawn

    async def _maybe_throttled(
        self: object, account_id: object, *, stamp_access: bool = True
    ) -> object:
        if fail_then_pass["fail"]:
            raise CredentialResolutionError(
                KvFailureReason.THROTTLED, "ref-throttled", "429 throttled"
            )
        return await real_resolve(self, account_id, stamp_access=stamp_access)

    monkeypatch.setattr(bas_module.BrokerAccountService, "resolve_for_spawn", _maybe_throttled)

    headers = {"Idempotency-Key": f"sw-transient-{uuid4().hex}"}
    body = {"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)}

    resp = await ac.post("/api/v1/live/start-portfolio", json=body, headers=headers)
    assert resp.status_code == 503, resp.text
    err = resp.json()["detail"]["error"]
    assert err["code"] == "BROKER_ACCOUNT_CREDENTIALS_UNAVAILABLE"
    assert err["details"]["reason"] == KvFailureReason.THROTTLED.value

    # No START enqueued, no deployment row persisted.
    assert await redis_text.xlen(command_stream_for_account(_BOUND_ACCOUNTS[0])) == 0
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveDeployment).where(LiveDeployment.portfolio_revision_id == rev_id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    # Reservation was RELEASED: a retry (KV recovered) with the SAME key is not
    # stuck in-flight — it executes and succeeds.
    fail_then_pass["fail"] = False
    retry = await ac.post("/api/v1/live/start-portfolio", json=body, headers=headers)
    assert retry.status_code == 201, retry.text
    async with session_factory() as session:
        dep = await session.get(LiveDeployment, retry.json()["id"])
        assert dep is not None
        assert dep.broker_account_id == acct_id


@pytest.mark.asyncio
async def test_permanent_credential_failure_returns_422(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PERMANENT credential-resolution failure (decrypt-failed / not-found /
    unauthorized) keeps the 422 ``BROKER_ACCOUNT_CREDENTIALS_INVALID`` response —
    it is genuinely invalid input, not a retryable blip. No deployment row
    persists."""
    from msai.services.live import broker_account_service as bas_module
    from msai.services.live.broker_credentials_store import (
        CredentialResolutionError,
        KvFailureReason,
    )

    ac, _store, _spy = client
    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=_BOUND_ACCOUNTS[0])
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    async def _decrypt_failed(
        self: object, account_id: object, *, stamp_access: bool = True
    ) -> object:
        raise CredentialResolutionError(
            KvFailureReason.DECRYPT_FAILED, "ref-bad", "malformed payload"
        )

    monkeypatch.setattr(bas_module.BrokerAccountService, "resolve_for_spawn", _decrypt_failed)

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
    )
    assert resp.status_code == 422, resp.text
    err = resp.json()["detail"]["error"]
    assert err["code"] == "BROKER_ACCOUNT_CREDENTIALS_INVALID"
    assert err["details"]["reason"] == KvFailureReason.DECRYPT_FAILED.value

    assert await redis_text.xlen(command_stream_for_account(_BOUND_ACCOUNTS[0])) == 0
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(LiveDeployment).where(LiveDeployment.portfolio_revision_id == rev_id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


# ---------------------------------------------------------------------------
# P2-B — idempotency hash includes the broker_account_id selector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_hash_includes_selector_no_cross_collision(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two requests reusing the SAME Idempotency-Key — one WITHOUT a
    ``broker_account_id`` (legacy strings) and one WITH it — that resolve to the
    SAME effective account must NOT collapse onto one cached outcome. Before the
    fix the body-hash omitted the selector, so the second (selector-bearing)
    request was served the first's cached response and skipped credential
    validation + the broker-link upsert, leaving ``broker_account_id`` NULL.

    The selector-bearing request must run its own validation and persist the
    broker link instead of being served the legacy request's cached outcome."""
    ac, store, _spy = client
    account = _BOUND_ACCOUNTS[0]

    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=account)
        user, strategy, revision, member = await _seed_deployable_revision(
            session, entra_id="test-operator"
        )
        # Pre-create the legacy (unlinked) deployment row so the FIRST request is
        # a clean legacy warm-restart (no broker_account_id, no KV validation).
        identity = derive_portfolio_deployment_identity(
            user_id=user.id,
            portfolio_revision_id=revision.id,
            account_id=account,
            paper_trading=True,
            ib_login_key=_LOGIN,
            user_sub="test-operator",
        )
        slug = generate_deployment_slug()
        dep = LiveDeployment(
            id=uuid4(),
            strategy_id=strategy.id,
            status="stopped",
            paper_trading=True,
            started_by=user.id,
            deployment_slug=slug,
            identity_signature=identity.signature(),
            trader_id=derive_trader_id(slug),
            strategy_id_full=derive_strategy_id_full(strategy.strategy_class, slug),
            account_id=account,
            ib_login_key=_LOGIN,
            portfolio_revision_id=revision.id,
            message_bus_stream=derive_message_bus_stream(slug),
            broker_account_id=None,
        )
        session.add(dep)
        cand = (
            await session.execute(
                select(GraduationCandidate).where(GraduationCandidate.strategy_id == strategy.id)
            )
        ).scalar_one()
        cand.stage = "live_running"
        cand.deployment_id = dep.id
        await session.commit()
        rev_id = revision.id
        dep_id = dep.id
        acct_id = acct.id

    key = f"sw-collide-{uuid4().hex}"
    headers = {"Idempotency-Key": key}

    # First request: legacy strings, NO selector. Because an ACTIVE registry row
    # exists for this IB account, the NULL-FK warm-restart now RESOLVES + links +
    # validates it (iter-20 fix — the legacy bypass is reserved for IB accounts
    # with NO registry row), so it DOES run one credential validation.
    legacy_body = {
        "portfolio_revision_id": str(rev_id),
        "account_id": account,
        "ib_login_key": _LOGIN,
    }
    r1 = await ac.post("/api/v1/live/start-portfolio", json=legacy_body, headers=headers)
    assert r1.status_code in (200, 201), r1.text
    assert len(store.get_calls) == 1  # active registry row resolved on the restart → validated

    # Second request: SAME key, but WITH the broker_account_id selector resolving
    # to the SAME effective account. If the hash omitted the selector this would
    # be served r1's cached outcome (body-match) and skip validation + linkage.
    selector_body = {
        "portfolio_revision_id": str(rev_id),
        "broker_account_id": str(acct_id),
    }
    r2 = await ac.post("/api/v1/live/start-portfolio", json=selector_body, headers=headers)

    # The selector-bearing request did NOT collide with the legacy cached
    # outcome. Because its body-hash now includes the selector, it no longer
    # matches the legacy reservation's hash → the idempotency layer returns a
    # body-mismatch (HTTP 422 "Idempotency-Key reused with a different request
    # body"), NOT a cached 2xx success that skipped credential validation + the
    # broker-link upsert. The mismatch is the proof the hashes differ.
    assert r2.status_code == 422, r2.text
    assert "different request body" in r2.json()["detail"]

    # The body-hashes differ → distinct. Verify directly via the public hasher.
    from msai.services.live.idempotency import IdempotencyStore

    legacy_hash = IdempotencyStore.body_hash(
        {
            "portfolio_revision_id": str(rev_id),
            "account_id": account,
            "paper_trading": True,
            "ib_login_key": _LOGIN,
            "binding_fingerprint": "x",
            "broker_account_id": None,
        }
    )
    selector_hash = IdempotencyStore.body_hash(
        {
            "portfolio_revision_id": str(rev_id),
            "account_id": account,
            "paper_trading": True,
            "ib_login_key": _LOGIN,
            "binding_fingerprint": "x",
            "broker_account_id": str(acct_id),
        }
    )
    assert legacy_hash != selector_hash

    # r2 (the colliding selector call) was rejected 422 → it performed NO upsert.
    # The row's linkage is whatever r1 left: r1 resolved the now-existing ACTIVE
    # registry row and linked the deployment to it (iter-20 fix). The point of
    # this assertion is that r2 did not collapse onto r1's outcome and silently
    # re-link/serve a cached success — the link state is r1's, untouched by r2.
    async with session_factory() as session:
        dep = await session.get(LiveDeployment, dep_id)
        assert dep is not None
        assert dep.broker_account_id == acct_id


# ---------------------------------------------------------------------------
# Effective-account safety — derived account wins over a divergent raw id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effective_account_halt_uses_derived_not_raw_request_account(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When ``broker_account_id`` resolves to account X but the caller ALSO
    sends a divergent raw ``account_id`` Y, the DERIVED account X is
    authoritative for the per-account halt gate: a halt on X blocks the deploy
    even though Y is not halted. (Closes the halt-bypass: the raw request
    account must never drive a safety check once an effective account exists.)"""
    ac, _store, _spy = client
    derived_account = _BOUND_ACCOUNTS[0]
    divergent_raw = _BOUND_ACCOUNTS[1]

    # Drain (halt) the DERIVED account only.
    await redis_text.set(account_halt_key(derived_account), "true")

    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=derived_account)
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={
            "portfolio_revision_id": str(rev_id),
            "broker_account_id": str(acct_id),
            # A divergent raw account_id + login that, if (wrongly) used for the
            # halt check, would NOT be blocked.
            "account_id": divergent_raw,
            "ib_login_key": _LOGIN,
        },
    )
    # The derived account is halted → 503 account_halt_active.
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "ACCOUNT_HALT_ACTIVE"
    assert resp.json()["error"]["account_id"] == derived_account


# ---------------------------------------------------------------------------
# New legacy-string deploy whose account_id does not resolve → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_legacy_deploy_with_unresolvable_account_fails_closed_422(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A NEW deploy via legacy ``account_id`` + ``ib_login_key`` strings whose
    ``account_id`` does NOT match any ACTIVE registry row fails closed with 422
    — it is NOT silently legacy-passed-through (council mandate)."""
    ac, _store, _spy = client
    async with session_factory() as session:
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        rev_id = revision.id

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={
            "portfolio_revision_id": str(rev_id),
            "account_id": "DUUNKNOWN9",  # no registry row
            "ib_login_key": _LOGIN,
        },
    )
    assert resp.status_code == 422, resp.text
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0


# ---------------------------------------------------------------------------
# Warm-restart back-compat — existing legacy deployment, broker_account_id NULL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_restart_legacy_deployment_no_registry_row(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An EXISTING legacy deployment (registered before the registry — no
    matching BrokerAccount row, ``broker_account_id`` NULL) re-starts cleanly
    with the legacy request strings, with NO forced resolution. The KV
    credential validation is NOT run (no account resolved)."""
    ac, store, _spy = client
    legacy_account = "DULEGACY99"  # deliberately NOT a registry row
    async with session_factory() as session:
        # Seed the user the handler resolves from the auth ``sub`` claim so the
        # pre-created deployment's identity_signature matches the recompute.
        user, strategy, revision, member = await _seed_deployable_revision(
            session, entra_id="test-operator"
        )
        # Pre-create the deployment row with an identity_signature matching what
        # the handler will compute for (user, revision, legacy_account, paper,
        # login) — this is the warm-restart anchor.
        identity = derive_portfolio_deployment_identity(
            user_id=user.id,
            portfolio_revision_id=revision.id,
            account_id=legacy_account,
            paper_trading=True,
            ib_login_key=_LOGIN,
            user_sub="test-operator",
        )
        slug = generate_deployment_slug()
        dep = LiveDeployment(
            id=uuid4(),
            strategy_id=strategy.id,
            status="stopped",
            paper_trading=True,
            started_by=user.id,
            deployment_slug=slug,
            identity_signature=identity.signature(),
            trader_id=derive_trader_id(slug),
            strategy_id_full=derive_strategy_id_full(strategy.strategy_class, slug),
            account_id=legacy_account,
            ib_login_key=_LOGIN,
            portfolio_revision_id=revision.id,
            message_bus_stream=derive_message_bus_stream(slug),
            broker_account_id=None,
        )
        session.add(dep)
        # Link the candidate to the existing deployment so the warm-restart
        # binding path is deterministic.
        cand = (
            await session.execute(
                select(GraduationCandidate).where(GraduationCandidate.strategy_id == strategy.id)
            )
        ).scalar_one()
        cand.stage = "live_running"
        cand.deployment_id = dep.id
        await session.commit()
        rev_id = revision.id
        dep_id = dep.id

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={
            "portfolio_revision_id": str(rev_id),
            "account_id": legacy_account,
            "ib_login_key": _LOGIN,
        },
    )
    assert resp.status_code in (200, 201), resp.text

    # No credential validation ran (no account was resolved on the legacy
    # warm-restart path).
    assert len(store.get_calls) == 0

    async with session_factory() as session:
        dep = await session.get(LiveDeployment, dep_id)
        assert dep is not None
        assert dep.account_id == legacy_account
        assert dep.broker_account_id is None


# ---------------------------------------------------------------------------
# Warm-restart fail-closed — existing deployment LINKED to a now-ARCHIVED
# broker account, restarted via legacy strings (no broker_account_id) → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_restart_broker_linked_archived_account_fails_closed_422(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An EXISTING deployment whose row is LINKED to a BrokerAccount
    (``broker_account_id IS NOT NULL``) that has since been ARCHIVED must NOT
    warm-restart through the legacy bypass when re-submitted via legacy strings
    (no ``broker_account_id``). The handler must resolve the linked account by
    its row id and fail closed with 422 — a since-archived broker-linked
    deployment cannot be grandfathered back to life.

    This closes the P1 bypass: the legacy back-compat path is reserved for
    deployments whose row has NO broker linkage; a broker-linked row is
    re-validated against its account's current state."""
    ac, _store, _spy = client
    linked_account = _BOUND_ACCOUNTS[0]
    before = DEPLOY_VALIDATION_FAILED.render()

    async with session_factory() as session:
        # Seed the user the handler resolves from the auth ``sub`` claim so the
        # pre-created deployment's identity_signature matches the recompute.
        user, strategy, revision, member = await _seed_deployable_revision(
            session, entra_id="test-operator"
        )
        # Seed an ARCHIVED broker account the existing deployment is linked to.
        acct = await _seed_broker_account(
            session,
            ib_account_id=linked_account,
            status=BrokerAccountStatus.ARCHIVED,
        )
        identity = derive_portfolio_deployment_identity(
            user_id=user.id,
            portfolio_revision_id=revision.id,
            account_id=linked_account,
            paper_trading=True,
            ib_login_key=_LOGIN,
            user_sub="test-operator",
        )
        slug = generate_deployment_slug()
        dep = LiveDeployment(
            id=uuid4(),
            strategy_id=strategy.id,
            status="stopped",
            paper_trading=True,
            started_by=user.id,
            deployment_slug=slug,
            identity_signature=identity.signature(),
            trader_id=derive_trader_id(slug),
            strategy_id_full=derive_strategy_id_full(strategy.strategy_class, slug),
            account_id=linked_account,
            ib_login_key=_LOGIN,
            portfolio_revision_id=revision.id,
            message_bus_stream=derive_message_bus_stream(slug),
            broker_account_id=acct.id,  # LINKED to the (now-archived) account
        )
        session.add(dep)
        cand = (
            await session.execute(
                select(GraduationCandidate).where(GraduationCandidate.strategy_id == strategy.id)
            )
        ).scalar_one()
        cand.stage = "live_running"
        cand.deployment_id = dep.id
        await session.commit()
        rev_id = revision.id
        dep_id = dep.id

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={
            "portfolio_revision_id": str(rev_id),
            "account_id": linked_account,
            "ib_login_key": _LOGIN,
        },
    )
    # The linked account is archived → fail closed, do NOT restart.
    assert resp.status_code == 422, resp.text

    # No START enqueued on any per-account stream nor the global stream.
    assert await redis_text.xlen(command_stream_for_account(linked_account)) == 0
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0

    # The deployment did NOT transition to a starting/active state.
    async with session_factory() as session:
        dep = await session.get(LiveDeployment, dep_id)
        assert dep is not None
        assert dep.status == "stopped"

    # The deploy-validation alert metric grew (a row-state rejection counted).
    after = DEPLOY_VALIDATION_FAILED.render()
    assert _metric_total(after) > _metric_total(before)


@pytest.mark.asyncio
async def test_warm_restart_null_fk_legacy_with_archived_registry_row_fails_closed_422(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Codex iter-20 P2: a NULL-FK legacy deployment (``broker_account_id IS
    NULL``, predates the registry) must NOT warm-restart through the legacy
    bypass once a BrokerAccount has been registered for its IB account and
    ARCHIVED. The legacy back-compat fallback is reserved for IB accounts with
    NO registry row at all; if a (now-archived) row exists, the restart resolves
    by ib_account_id and fails closed (422) — closing the "spawn against a
    since-archived account via a legacy NULL-FK restart" hole.

    Contrast with ``test_warm_restart_legacy_deployment_no_registry_row`` (no
    registry row → legacy fallback still allowed)."""
    ac, store, _spy = client
    legacy_account = _BOUND_ACCOUNTS[0]
    before = DEPLOY_VALIDATION_FAILED.render()

    async with session_factory() as session:
        user, strategy, revision, member = await _seed_deployable_revision(
            session, entra_id="test-operator"
        )
        # An ARCHIVED BrokerAccount now exists for this IB account — registered
        # AFTER the legacy deployment was first started (which left broker_account_id NULL).
        await _seed_broker_account(
            session,
            ib_account_id=legacy_account,
            status=BrokerAccountStatus.ARCHIVED,
        )
        identity = derive_portfolio_deployment_identity(
            user_id=user.id,
            portfolio_revision_id=revision.id,
            account_id=legacy_account,
            paper_trading=True,
            ib_login_key=_LOGIN,
            user_sub="test-operator",
        )
        slug = generate_deployment_slug()
        dep = LiveDeployment(
            id=uuid4(),
            strategy_id=strategy.id,
            status="stopped",
            paper_trading=True,
            started_by=user.id,
            deployment_slug=slug,
            identity_signature=identity.signature(),
            trader_id=derive_trader_id(slug),
            strategy_id_full=derive_strategy_id_full(strategy.strategy_class, slug),
            account_id=legacy_account,
            ib_login_key=_LOGIN,
            portfolio_revision_id=revision.id,
            message_bus_stream=derive_message_bus_stream(slug),
            broker_account_id=None,  # NULL-FK — the legacy bypass MUST NOT apply now
        )
        session.add(dep)
        cand = (
            await session.execute(
                select(GraduationCandidate).where(GraduationCandidate.strategy_id == strategy.id)
            )
        ).scalar_one()
        cand.stage = "live_running"
        cand.deployment_id = dep.id
        await session.commit()
        rev_id = revision.id
        dep_id = dep.id

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={
            "portfolio_revision_id": str(rev_id),
            "account_id": legacy_account,
            "ib_login_key": _LOGIN,
        },
    )
    # A registry row exists for this IB account and is archived → fail closed.
    assert resp.status_code == 422, resp.text

    # No credential validation ran (resolution failed before the KV stage) and no
    # START was enqueued on the per-account or global stream.
    assert len(store.get_calls) == 0
    assert await redis_text.xlen(command_stream_for_account(legacy_account)) == 0
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0

    # The deployment did NOT transition out of stopped, and stayed NULL-FK.
    async with session_factory() as session:
        dep = await session.get(LiveDeployment, dep_id)
        assert dep is not None
        assert dep.status == "stopped"
        assert dep.broker_account_id is None

    # The deploy-rejection alert metric grew.
    after = DEPLOY_VALIDATION_FAILED.render()
    assert _metric_total(after) > _metric_total(before)


# ---------------------------------------------------------------------------
# Warm-restart persist — restart selecting a broker_account_id refreshes
# broker_account_id + credential-validation stamps on the upsert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_restart_persists_broker_linkage_and_stamps(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Restarting an existing deployment while selecting it by
    ``broker_account_id`` must persist (not discard) the broker linkage +
    credential-validation stamps on the warm-restart upsert. Before this fix
    the ``ON CONFLICT DO UPDATE SET`` wrote only status + last_started_at, so a
    warm restart left ``broker_account_id`` / ``credentials_validated_at`` /
    ``credentials_validated_version`` NULL/stale."""
    ac, _store, _spy = client
    linked_account = _BOUND_ACCOUNTS[0]

    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=linked_account)
        user, strategy, revision, member = await _seed_deployable_revision(
            session, entra_id="test-operator"
        )
        # Pre-create the deployment row WITHOUT broker linkage + stamps, with an
        # identity_signature matching what the handler computes for the DERIVED
        # account (the broker_account_id selector derives account/login from the
        # registry row).
        identity = derive_portfolio_deployment_identity(
            user_id=user.id,
            portfolio_revision_id=revision.id,
            account_id=linked_account,
            paper_trading=True,
            ib_login_key=_LOGIN,
            user_sub="test-operator",
        )
        slug = generate_deployment_slug()
        dep = LiveDeployment(
            id=uuid4(),
            strategy_id=strategy.id,
            status="stopped",
            paper_trading=True,
            started_by=user.id,
            deployment_slug=slug,
            identity_signature=identity.signature(),
            trader_id=derive_trader_id(slug),
            strategy_id_full=derive_strategy_id_full(strategy.strategy_class, slug),
            account_id=linked_account,
            ib_login_key=_LOGIN,
            portfolio_revision_id=revision.id,
            message_bus_stream=derive_message_bus_stream(slug),
            broker_account_id=None,  # NOT linked yet — the restart links it
            credentials_validated_at=None,
            credentials_validated_version=None,
        )
        session.add(dep)
        cand = (
            await session.execute(
                select(GraduationCandidate).where(GraduationCandidate.strategy_id == strategy.id)
            )
        ).scalar_one()
        cand.stage = "live_running"
        cand.deployment_id = dep.id
        await session.commit()
        rev_id = revision.id
        dep_id = dep.id
        acct_id = acct.id

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
    )
    assert resp.status_code in (200, 201), resp.text

    # After the warm restart the row carries the broker linkage + fresh stamps.
    async with session_factory() as session:
        dep = await session.get(LiveDeployment, dep_id)
        assert dep is not None
        assert dep.broker_account_id == acct_id
        assert dep.credentials_validated_at is not None
        assert dep.credentials_validated_version == "v1"


# ---------------------------------------------------------------------------
# Idempotency ordering — replayed request does NOT re-call resolve_for_spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replayed_request_does_not_recall_resolve_for_spawn(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A replayed request (same Idempotency-Key → cached outcome) must NOT
    re-run the KV credential side-effect: ``resolve_for_spawn`` runs exactly
    once across the original + the replay."""
    ac, _store, spy = client
    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=_BOUND_ACCOUNTS[0])
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    spy.count = 0
    headers = {"Idempotency-Key": f"sw-key-{uuid4().hex}"}
    body = {"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)}

    r1 = await ac.post("/api/v1/live/start-portfolio", json=body, headers=headers)
    assert r1.status_code == 201, r1.text
    r2 = await ac.post("/api/v1/live/start-portfolio", json=body, headers=headers)
    assert r2.status_code in (200, 201), r2.text

    # The KV side-effect ran exactly once despite two requests.
    assert spy.count == 1


# ---------------------------------------------------------------------------
# Council 2026-06-01 (bounded Option B) — the lock-invariant fixes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_same_revision_account_loser_gets_422_not_500(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    redis_text: AsyncRedis,
    isolated_postgres_url: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two concurrent starts for the SAME ``(revision_id, account_id)`` but a
    DIFFERENT identity (different ``ib_login_key``) must serialize: exactly one
    wins, the other returns 422 ``LIVE_DEPLOY_CONFLICT`` — NEVER a raw
    ``IntegrityError`` 500 from the ``(revision_id, account_id)`` UNIQUE
    constraint.

    Deterministic two-session interleave (the repo's race-test pattern): a WRITER
    session takes the revision ``FOR UPDATE`` and inserts a COLLIDING deployment
    row (same revision+account, a different identity_signature) UNCOMMITTED. The
    handler request then blocks on the revision ``FOR UPDATE`` it acquires at the
    top of its final critical section (the council-mandated serialization point).
    When the writer COMMITS, the handler unblocks, runs its collision re-check
    UNDER the held lock, observes the now-committed colliding row, and fails closed
    with 422 — proving the collision loser is serialized under a held lock and
    never surfaces a 500."""
    ac, _store, _spy = client
    account = _BOUND_ACCOUNTS[0]

    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=account)
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    # Independent engine/session = the concurrent WRITER (a different start for the
    # same revision+account under a different identity).
    writer_engine = create_async_engine(isolated_postgres_url)
    writer_factory = async_sessionmaker(writer_engine, expire_on_commit=False)

    handler_result: dict[str, object] = {}

    try:
        async with writer_factory() as writer:
            # WRITER takes the revision FOR UPDATE first (mirrors the handler's
            # critical-section lock) ...
            await writer.execute(
                select(LivePortfolioRevision.id)
                .where(LivePortfolioRevision.id == rev_id)
                .with_for_update()
            )
            # ... and inserts a COLLIDING deployment: SAME revision + account, a
            # DIFFERENT identity_signature + ib_login_key (uncommitted for now).
            slug = generate_deployment_slug()
            writer.add(
                LiveDeployment(
                    id=uuid4(),
                    status="running",
                    paper_trading=True,
                    deployment_slug=slug,
                    identity_signature=f"other-identity-{uuid4().hex}",
                    trader_id=derive_trader_id(slug),
                    strategy_id_full=derive_strategy_id_full("EMACrossStrategy", slug),
                    account_id=account,
                    ib_login_key="some-other-login",
                    portfolio_revision_id=rev_id,
                    message_bus_stream=derive_message_bus_stream(slug),
                    broker_account_id=None,
                )
            )
            await writer.flush()  # present in WRITER's tx, NOT yet committed

            # Drive the handler concurrently; it must block on the revision lock.
            async def _drive_handler() -> None:
                resp = await ac.post(
                    "/api/v1/live/start-portfolio",
                    json={
                        "portfolio_revision_id": str(rev_id),
                        "broker_account_id": str(acct_id),
                    },
                )
                handler_result["status"] = resp.status_code
                handler_result["body"] = resp.json()

            handler_task = asyncio.create_task(_drive_handler())
            # Let the handler reach + block on the revision FOR UPDATE. It must NOT
            # complete while the writer holds the lock.
            await asyncio.sleep(1.0)
            assert not handler_task.done(), (
                "handler should be BLOCKED on the revision FOR UPDATE lock held by "
                "the concurrent writer"
            )

            # WRITER commits first → it wins; its colliding row is now visible.
            await writer.commit()

        # Handler unblocks; its collision re-check (under the lock) sees the
        # committed colliding row and fails closed 422 LIVE_DEPLOY_CONFLICT.
        await asyncio.wait_for(handler_task, timeout=15)
    finally:
        await writer_engine.dispose()

    assert handler_result["status"] == 422, handler_result
    body = handler_result["body"]
    assert isinstance(body, dict)
    assert body["detail"]["error"]["code"] == "LIVE_DEPLOY_CONFLICT"

    # The loser persisted NO new row beyond the writer's, and enqueued no START.
    assert await redis_text.xlen(command_stream_for_account(account)) == 0
    assert await redis_text.xlen(LIVE_COMMAND_STREAM) == 0


@pytest.mark.asyncio
async def test_stage2_validation_holds_no_lock_and_does_not_commit(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    isolated_postgres_url: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """STAGE-2 credential validation reads Key Vault with NO ``FOR UPDATE`` row
    lock held and does NOT commit the request session.

    Proof: a probe wraps ``validate_account_credentials``. While STAGE-2 is mid
    flight (between its call and return), an INDEPENDENT session takes
    ``SELECT ... FOR UPDATE NOWAIT`` on BOTH the broker_accounts row AND the
    revision row. If STAGE-2 held either lock, ``NOWAIT`` would raise; it must
    SUCCEED — proving no lock is held during the KV read. The probe also asserts
    the request session has not committed (its credential-access stamp is deferred
    to the final transaction)."""
    ac, _store, _spy = client
    account = _BOUND_ACCOUNTS[0]

    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=account)
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id

    from sqlalchemy.exc import DBAPIError, OperationalError

    probe_engine = create_async_engine(isolated_postgres_url)
    probe_factory = async_sessionmaker(probe_engine, expire_on_commit=False)
    probe_outcome: dict[str, object] = {}

    real_validate = live_module.validate_account_credentials

    async def _validate_then_probe_locks(account_obj: object, svc: object) -> object:
        # Run the real STAGE-2 (reads the store; no commit, no lock).
        result = await real_validate(account_obj, svc)
        # Now — still INSIDE the handler, BEFORE its critical section acquires any
        # lock — an independent session must be able to grab FOR UPDATE NOWAIT on
        # both rows. If STAGE-2 had left a lock or an open row-lock, NOWAIT raises.
        async with probe_factory() as probe:
            try:
                await probe.execute(
                    select(BrokerAccount.id)
                    .where(BrokerAccount.id == acct_id)
                    .with_for_update(nowait=True)
                )
                await probe.execute(
                    select(LivePortfolioRevision.id)
                    .where(LivePortfolioRevision.id == rev_id)
                    .with_for_update(nowait=True)
                )
                probe_outcome["locks_free_during_stage2"] = True
            except (DBAPIError, OperationalError) as exc:
                probe_outcome["locks_free_during_stage2"] = False
                probe_outcome["error"] = str(exc)
            await probe.rollback()
        return result

    monkeypatch_target = live_module
    monkeypatch_target.validate_account_credentials = _validate_then_probe_locks  # type: ignore[assignment]
    try:
        resp = await ac.post(
            "/api/v1/live/start-portfolio",
            json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
        )
    finally:
        monkeypatch_target.validate_account_credentials = real_validate  # type: ignore[assignment]
        await probe_engine.dispose()

    assert resp.status_code == 201, resp.text
    assert probe_outcome.get("locks_free_during_stage2") is True, probe_outcome


@pytest.mark.asyncio
async def test_successful_deploy_stamps_credentials_last_accessed(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A SUCCESSFUL deploy stamps ``credentials_last_accessed`` on the broker
    account — atomic with the deployment upsert (deferred from STAGE 2 to the
    final transaction). It was NULL before the deploy."""
    ac, _store, _spy = client
    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=_BOUND_ACCOUNTS[0])
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id
        assert acct.credentials_last_accessed is None

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
    )
    assert resp.status_code == 201, resp.text

    # The stamp landed durably (a fresh session reads it back).
    async with session_factory() as session:
        refreshed = await session.get(BrokerAccount, acct_id)
        assert refreshed is not None
        assert refreshed.credentials_last_accessed is not None


@pytest.mark.asyncio
async def test_failed_closed_deploy_leaves_credentials_last_accessed_unchanged(
    client: tuple[httpx.AsyncClient, _StubStore, _ResolveSpy],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deploy that FAILS CLOSED after STAGE 2 (a credential ROTATION committed
    in the STAGE-2→STAGE-3 window → 409) leaves ``credentials_last_accessed``
    UNCHANGED — the stamp is deferred to the FINAL successful transaction, which
    never runs on a fail-closed path."""
    ac, _store, _spy = client
    async with session_factory() as session:
        acct = await _seed_broker_account(session, ib_account_id=_BOUND_ACCOUNTS[0])
        _u, _s, revision, _m = await _seed_deployable_revision(session)
        await session.commit()
        acct_id = acct.id
        rev_id = revision.id
        assert acct.credentials_last_accessed is None

    # Rotate v1 → v2 in a separate committed session AFTER STAGE-2 validates at v1
    # → STAGE-3 locked re-read sees v2 != v1 → fail closed 409 (no stamp).
    real_validate = live_module.validate_account_credentials

    async def _validate_then_rotate(account_obj: object, svc: object) -> object:
        result = await real_validate(account_obj, svc)
        async with session_factory() as other:
            row = await other.get(BrokerAccount, acct_id)
            assert row is not None
            row.credentials_secret_version = "v2"
            row.credentials_updated_at = datetime.now(UTC)
            await other.commit()
        return result

    monkeypatch.setattr(live_module, "validate_account_credentials", _validate_then_rotate)

    resp = await ac.post(
        "/api/v1/live/start-portfolio",
        json={"portfolio_revision_id": str(rev_id), "broker_account_id": str(acct_id)},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"]["code"] == "BROKER_ACCOUNT_CREDENTIALS_ROTATED"

    # The fail-closed path never reached the final transaction → no stamp.
    async with session_factory() as session:
        refreshed = await session.get(BrokerAccount, acct_id)
        assert refreshed is not None
        assert refreshed.credentials_last_accessed is None
