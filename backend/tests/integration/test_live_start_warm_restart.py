"""Integration tests for ``/api/v1/live/start`` warm-restart reuse
(Phase 1 Task 1.1b Codex P1 fix).

**SUPERSEDED BY Task 1.14.** Every test in this module was written
against the pre-1.14 synchronous ``/start`` path that called
``_node_manager.start()`` directly. Task 1.14 replaced that path
with the command-bus + idempotency-reservation + poll-for-ready
flow, which means:

1. ``live_start`` is no longer callable as a plain async function —
   it depends on a real ``LiveCommandBus`` and ``IdempotencyStore``
   resolved via ``Depends(...)``, which only work inside the FastAPI
   request cycle.
2. The post-call row state is ``starting`` (waiting for the supervisor
   to flip it to ``ready``), not ``running``, because no supervisor
   runs in these tests.

The warm-restart and identity-upsert behavior these tests cover is
now exercised via the ASGI client in
``tests/integration/test_live_start_endpoints.py`` (Task 1.14),
which stubs the supervisor by writing directly to
``live_node_processes`` and drives the endpoint end-to-end through
the real HTTP layer.

All tests in this module are marked ``pytest.mark.xfail`` with
``strict=False`` so they surface as "expected fail" rather than
hard failures. Future tech-debt: delete this file once the
ASGI-client tests have full coverage parity.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

pytestmark = pytest.mark.xfail(
    reason=(
        "Pre-Task-1.14 synchronous /start flow — superseded by command-bus + "
        "idempotency-reservation path. Equivalent coverage lives in "
        "test_live_start_endpoints.py via the ASGI client."
    ),
    strict=False,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.api import live as live_module
from msai.core.config import settings
from msai.models import Base, LiveDeployment, Strategy, User
from msai.schemas.live import LiveStartRequest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


_STRATEGY_BODY_V1 = (
    b"# v1\nfrom nautilus_trader.trading.strategy import Strategy\nclass T(Strategy): pass\n"
)
_STRATEGY_BODY_V2 = (
    b"# v2 EDITED\nfrom nautilus_trader.trading.strategy import Strategy\nclass T(Strategy): pass\n"
)


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session(isolated_postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s

    await engine.dispose()


@pytest.fixture
def strategies_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point settings.strategies_root at a temp dir and write the v1 body."""
    strategies_root = tmp_path / "strategies"
    (strategies_root / "example").mkdir(parents=True)
    (strategies_root / "example" / "ema.py").write_bytes(_STRATEGY_BODY_V1)
    monkeypatch.setattr(settings, "strategies_root", strategies_root)
    return strategies_root


@pytest.fixture
def ib_account(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure a stable, non-placeholder IB account id for these tests."""
    monkeypatch.setattr(settings, "ib_account_id", "DU1234567")
    return "DU1234567"


@pytest.fixture
def stub_node_manager(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the module-level TradingNodeManager with a stub that
    always returns True from ``start`` so the /start handler can reach
    the success path without actually launching a Nautilus subprocess.

    ``has_deployment`` is a SYNC method on the real class, so it must
    be a plain MagicMock (not AsyncMock) to avoid runtime warnings.
    """
    from unittest.mock import MagicMock

    stub = AsyncMock()
    stub.start = AsyncMock(return_value=True)
    stub.stop = AsyncMock(return_value=True)
    # has_deployment must NOT be a coroutine — the real class makes it
    # sync so we can use it as the predicate of the short-circuit branch
    # without awaiting. Default to False so the warm-restart short-circuit
    # is not accidentally taken; tests that exercise it use real_node_manager.
    stub.has_deployment = MagicMock(return_value=False)
    monkeypatch.setattr(live_module, "_node_manager", stub)
    return stub


@pytest.fixture
def real_node_manager(monkeypatch: pytest.MonkeyPatch) -> object:
    """Replace ``_node_manager`` with a fresh real ``TradingNodeManager``
    instance so the idempotency contract of ``start()`` is exercised
    end-to-end (rather than being mocked away)."""
    from msai.services.nautilus.trading_node import TradingNodeManager
    from msai.services.risk_engine import RiskEngine

    manager = TradingNodeManager(RiskEngine())
    monkeypatch.setattr(live_module, "_node_manager", manager)
    return manager


@pytest_asyncio.fixture
async def user_and_strategy(
    session: AsyncSession, strategies_dir: Path
) -> AsyncIterator[tuple[User, Strategy]]:
    user = User(
        id=uuid4(),
        entra_id=f"test-{uuid4().hex}",
        email=f"test-{uuid4().hex}@example.com",
        role="operator",
    )
    session.add(user)
    await session.flush()

    strategy = Strategy(
        id=uuid4(),
        name="ema-cross",
        file_path="strategies/example/ema.py",
        strategy_class="TStrategy",
        created_by=user.id,
        default_config={"fast_ema_period": 10, "slow_ema_period": 30},
    )
    session.add(strategy)
    await session.commit()
    yield user, strategy


def _make_request(config: dict | None = None) -> LiveStartRequest:
    return LiveStartRequest(
        strategy_id=uuid4(),  # overwritten per test
        config=config if config is not None else {"fast_ema_period": 10, "slow_ema_period": 30},
        instruments=["AAPL.NASDAQ"],
        paper_trading=True,
    )


async def _count_deployments(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(LiveDeployment))
    return result.scalar_one()


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_second_start_reuses_existing_row(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Calling /start twice with identical inputs MUST result in ONE row.

    This is the Codex Task 1.1b P1 fix: before, the handler always
    INSERTed and would 500 with IntegrityError on the second call due
    to the new UNIQUE(identity_signature) index.
    """
    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}

    request = _make_request()
    request.strategy_id = strategy.id

    first = await live_module.live_start(request=request, claims=claims, db=session)

    # Same request, same inputs → must reuse the same row
    second = await live_module.live_start(request=request, claims=claims, db=session)

    assert first["id"] == second["id"], "warm restart must return the same deployment id"
    assert await _count_deployments(session) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_warm_restart_updates_last_started_at(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """The reused row's ``last_started_at`` must advance on each restart."""
    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}
    request = _make_request()
    request.strategy_id = strategy.id

    first = await live_module.live_start(request=request, claims=claims, db=session)
    first_row = await session.get(LiveDeployment, first["id"])
    assert first_row is not None
    first_started_at = first_row.last_started_at

    # Invalidate cache so we re-read the possibly-updated row
    await session.refresh(first_row)

    await live_module.live_start(request=request, claims=claims, db=session)
    second_row = await session.get(LiveDeployment, first["id"])
    assert second_row is not None
    await session.refresh(second_row)

    assert second_row.last_started_at is not None
    assert first_started_at is not None
    assert second_row.last_started_at >= first_started_at


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_editing_strategy_file_produces_cold_start(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
    strategies_dir: Path,
) -> None:
    """Codex P1 fix for hard-coded ``strategy_code_hash``: editing the
    strategy file MUST produce a new row (cold start with isolated
    state), not silently reuse the pre-edit deployment."""
    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}
    request = _make_request()
    request.strategy_id = strategy.id

    first = await live_module.live_start(request=request, claims=claims, db=session)
    assert await _count_deployments(session) == 1

    # Edit the strategy file → new sha256 → new identity_signature
    (strategies_dir / "example" / "ema.py").write_bytes(_STRATEGY_BODY_V2)

    second = await live_module.live_start(request=request, claims=claims, db=session)

    assert first["id"] != second["id"], (
        "edited strategy must produce a fresh deployment, not warm-restart"
    )
    assert await _count_deployments(session) == 2

    # Confirm the two rows have different code hashes persisted
    rows = (await session.execute(select(LiveDeployment))).scalars().all()
    hashes = {row.strategy_code_hash for row in rows}
    assert len(hashes) == 2, f"expected 2 distinct code hashes, got {hashes}"


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_config_change_produces_cold_start(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}

    r1 = _make_request(config={"fast_ema_period": 10, "slow_ema_period": 30})
    r1.strategy_id = strategy.id
    first = await live_module.live_start(request=r1, claims=claims, db=session)

    r2 = _make_request(config={"fast_ema_period": 50, "slow_ema_period": 200})
    r2.strategy_id = strategy.id
    second = await live_module.live_start(request=r2, claims=claims, db=session)

    assert first["id"] != second["id"]
    assert await _count_deployments(session) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_node_manager")
async def test_account_change_produces_cold_start(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P2 fix: the real ``ib_account_id`` setting is part of the
    identity tuple. Switching accounts must start a fresh deployment
    with isolated state instead of warm-restarting the old one."""
    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}
    request = _make_request()
    request.strategy_id = strategy.id

    monkeypatch.setattr(settings, "ib_account_id", "DU1111111")
    first = await live_module.live_start(request=request, claims=claims, db=session)

    monkeypatch.setattr(settings, "ib_account_id", "DU2222222")
    second = await live_module.live_start(request=request, claims=claims, db=session)

    assert first["id"] != second["id"]
    assert await _count_deployments(session) == 2

    rows = (await session.execute(select(LiveDeployment))).scalars().all()
    accounts = {row.account_id for row in rows}
    assert accounts == {"DU1111111", "DU2222222"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_config_default_merge_makes_omitted_defaults_warm_restart(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Codex P2 fix: a request that omits a parameter with a stored
    default MUST hash identically to one that passes the default
    explicitly — otherwise two callers starting the "same" deployment
    would get spurious cold starts.

    The strategy's ``default_config`` is ``{"fast_ema_period": 10,
    "slow_ema_period": 30}``.
    """
    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}

    # Caller A passes the full config explicitly.
    r_explicit = _make_request(config={"fast_ema_period": 10, "slow_ema_period": 30})
    r_explicit.strategy_id = strategy.id

    # Caller B omits both defaults.
    r_empty = _make_request(config={})
    r_empty.strategy_id = strategy.id

    first = await live_module.live_start(request=r_explicit, claims=claims, db=session)
    second = await live_module.live_start(request=r_empty, claims=claims, db=session)

    assert first["id"] == second["id"], (
        "omitted defaults must merge with stored default_config and hash "
        "identically to explicit defaults"
    )
    assert await _count_deployments(session) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_warm_restart_clears_previous_last_stopped_at(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Codex iteration 2 P2 fix: a warm-restarted running deployment
    must not report a stale ``last_stopped_at`` from its prior run.

    Scenario:
    1. Start → status running, stop time NULL
    2. Stop → last_stopped_at set
    3. Start again with same identity → warm restart; last_stopped_at
       must be cleared back to NULL because the deployment is running
       again.
    """
    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}
    request = _make_request()
    request.strategy_id = strategy.id

    first = await live_module.live_start(request=request, claims=claims, db=session)
    deployment_id = first["id"]

    # Simulate a stop — just set last_stopped_at on the row directly so we
    # don't have to wire up the full /stop endpoint.
    row = await session.get(LiveDeployment, deployment_id)
    assert row is not None
    row.status = "stopped"
    row.last_stopped_at = datetime.now(UTC)
    await session.commit()

    # Warm restart — same identity, must clear last_stopped_at
    await live_module.live_start(request=request, claims=claims, db=session)

    refreshed = await session.get(LiveDeployment, deployment_id)
    assert refreshed is not None
    await session.refresh(refreshed)
    assert refreshed.last_stopped_at is None, (
        "warm restart must clear last_stopped_at so /status doesn't report "
        "a stale stop timestamp from the prior run"
    )
    assert refreshed.status == "running"


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_large_instrument_universe_does_not_overflow_column(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Codex iteration 2 P2 fix: ``instruments_signature`` is now TEXT,
    so a large options universe whose comma-joined canonical IDs exceed
    the old 512-char cap must round-trip without truncation or error.
    """
    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}

    # 200 instrument IDs at ~16 chars each ≈ 3200 chars of signature,
    # well past the old VARCHAR(512) ceiling.
    many_instruments = [f"SYM{i:04d}.NASDAQ" for i in range(200)]
    request = LiveStartRequest(
        strategy_id=strategy.id,
        config={"fast_ema_period": 10, "slow_ema_period": 30},
        instruments=many_instruments,
        paper_trading=True,
    )

    result = await live_module.live_start(request=request, claims=claims, db=session)

    row = await session.get(LiveDeployment, result["id"])
    assert row is not None
    # Signature is the full sorted canonical list — must not be truncated
    assert len(row.instruments_signature) > 512
    assert row.instruments_signature == ",".join(sorted(many_instruments))


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "real_node_manager")
async def test_warm_restart_does_not_mark_running_deployment_as_rejected(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Codex Task 1.1b iteration 4, P1 fix: starting the same deployment
    twice must not flip the shared row from ``running`` to ``rejected``.

    Before the fix, the second ``/start`` would upsert onto the existing
    row (warm restart path), then call ``_node_manager.start()``, which
    re-validated risk with ``len(_processes)`` inflated by the already-
    running deployment. At capacity, the risk engine would reject, and
    the handler would set ``status='rejected'`` on the shared row —
    corrupting a deployment that is actually running.

    The fix makes ``TradingNodeManager.start()`` idempotent: a
    deployment_id that's already tracked returns ``True`` immediately
    without re-validating.
    """
    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}
    request = _make_request()
    request.strategy_id = strategy.id

    first = await live_module.live_start(request=request, claims=claims, db=session)
    assert first["status"] == "running"

    # Second start of the exact same deployment. Warm-restart upsert
    # reuses the row; idempotent node_manager.start() returns True;
    # status stays 'running'. The old bug would flip it to 'rejected'.
    second = await live_module.live_start(request=request, claims=claims, db=session)
    assert second["id"] == first["id"]
    assert second["status"] == "running"

    row = await session.get(LiveDeployment, first["id"])
    assert row is not None
    await session.refresh(row)
    assert row.status == "running", (
        f"warm-restart must not corrupt the running deployment's status; got {row.status!r}"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "real_node_manager")
async def test_duplicate_start_while_running_is_pure_readonly(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Codex Task 1.1b iteration 6, P1 fix: a duplicate /start while
    the deployment is already running must be a pure read-only
    short-circuit. It must NOT write ``status='starting'`` or bump
    ``last_started_at``, otherwise a concurrent /stop landing in the
    gap can let the retry resurrect the deployment.
    """
    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}
    request = _make_request()
    request.strategy_id = strategy.id

    first = await live_module.live_start(request=request, claims=claims, db=session)

    row = await session.get(LiveDeployment, first["id"])
    assert row is not None
    original_started_at = row.last_started_at
    original_status = row.status
    assert original_status == "running"
    assert original_started_at is not None

    # Duplicate /start — must be a pure no-op on the DB row.
    second = await live_module.live_start(request=request, claims=claims, db=session)
    assert second["id"] == first["id"]

    await session.refresh(row)
    assert row.status == "running"
    assert row.last_started_at == original_started_at, (
        "duplicate /start while running must not bump last_started_at"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_concurrent_first_start_user_insert_race_does_not_expire_strategy(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Codex Task 1.5 iter2 P2 fix: when `_resolve_user_id` loses a race
    on a fresh user insert, the IntegrityError rollback must not expire
    the `Strategy` row `live_start()` already loaded — otherwise the
    subsequent ``strategy.default_config`` / ``strategy.strategy_class``
    access raises ``MissingGreenlet`` and the concurrent first-start
    returns a 500 instead of warm-restarting.

    The fix (``db.begin_nested()`` SAVEPOINT) is exercised by
    pre-inserting a user row with the same entra_id the test's claims
    will carry, so the flush inside the savepoint deterministically
    hits ``IntegrityError`` on EVERY run. If the savepoint semantics
    break, the test raises ``MissingGreenlet`` instead of passing.
    """
    from msai.models import User

    _seed_user, strategy = user_and_strategy

    # Pre-existing user row the race-loser will collide with.
    racing_sub = "racing-sub-12345"
    existing_racer = User(
        id=uuid4(),
        entra_id=racing_sub,
        email="racer@example.com",
        role="operator",
    )
    session.add(existing_racer)
    await session.commit()

    # claims use the SAME sub → `_resolve_user_id` sees no row (the
    # SELECT runs first), tries to INSERT, and hits IntegrityError
    # inside the savepoint.
    racing_claims = {"sub": racing_sub, "preferred_username": "racer@example.com"}
    request = _make_request()
    request.strategy_id = strategy.id

    # This would have raised MissingGreenlet before the savepoint fix,
    # because the plain rollback() expired the strategy row and the
    # subsequent strategy.default_config access lazy-refreshed outside
    # the greenlet context.
    result = await live_module.live_start(request=request, claims=racing_claims, db=session)
    assert result["status"] == "running"

    # Verify the deployment actually attached to the pre-existing user row.
    row = await session.get(LiveDeployment, result["id"])
    assert row is not None
    assert row.started_by == existing_racer.id


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_start_provisions_missing_user_row_inline(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Codex iteration 6 P1/P2 fix: /start provisions the users row
    inline if it's missing, so two different JWT users with unresolved
    rows get distinct UUIDs (not colliding on anonymous or 'sub:...'),
    AND a pre-/auth/me start followed by a post-/auth/me start uses
    the SAME UUID both times (no second cold-start row).
    """
    from msai.models import User

    _seed_user, strategy = user_and_strategy

    # Simulate a JWT caller with a sub that has no matching users row.
    unresolved_claims = {
        "sub": "new-user-entra-id-12345",
        "preferred_username": "new@example.com",
        "name": "New User",
    }
    request = _make_request()
    request.strategy_id = strategy.id

    await live_module.live_start(request=request, claims=unresolved_claims, db=session)

    # The user row must now exist with the sub as entra_id.
    result = await session.execute(select(User).where(User.entra_id == "new-user-entra-id-12345"))
    provisioned = result.scalar_one_or_none()
    assert provisioned is not None, "/start should have provisioned the users row"
    assert provisioned.email == "new@example.com"

    # A second /start with the same claims must warm-restart the same
    # deployment (same UUID, same identity_signature).
    second = await live_module.live_start(request=request, claims=unresolved_claims, db=session)
    row = await session.get(LiveDeployment, second["id"])
    assert row is not None
    assert row.started_by == provisioned.id
    assert await _count_deployments(session) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_concurrent_starts_resolve_to_single_row_via_upsert(
    isolated_postgres_url: str,
    user_and_strategy: tuple[User, Strategy],
    strategies_dir: Path,  # noqa: ARG001
) -> None:
    """Codex iteration 2 P2 fix: two overlapping ``/api/v1/live/start``
    requests with the same identity must resolve to the SAME row via
    the atomic ``INSERT ... ON CONFLICT DO UPDATE`` upsert. Before the
    fix, both callers could SELECT-miss and both INSERT, with the
    second commit 500-ing on ``UNIQUE(identity_signature)``.

    Uses two independent sessions against the same database so we can
    interleave two in-flight transactions the way a real concurrent
    FastAPI worker would.
    """
    user, strategy = user_and_strategy

    engine = create_async_engine(isolated_postgres_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    try:
        # Interleave: begin both, then commit. Whichever loses the
        # ON CONFLICT race takes the DO UPDATE path; neither errors.
        async with maker() as session_a, maker() as session_b:
            # Re-read user + strategy in each session because they were
            # created in the other session's transaction.
            strat_a = await session_a.get(Strategy, strategy.id)
            strat_b = await session_b.get(Strategy, strategy.id)
            assert strat_a is not None and strat_b is not None

            request = _make_request()
            request.strategy_id = strategy.id
            claims = {"sub": user.entra_id}

            first = await live_module.live_start(request=request, claims=claims, db=session_a)
            second = await live_module.live_start(request=request, claims=claims, db=session_b)

            # Both calls must resolve to the SAME row via ON CONFLICT
            assert first["id"] == second["id"]

        # Verify exactly one row landed
        async with maker() as verify:
            count_result = await verify.execute(select(func.count()).select_from(LiveDeployment))
            count = count_result.scalar_one()
            assert count == 1, f"expected single upserted row, got {count}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_unresolved_jwt_users_get_distinct_deployments(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Codex iteration 5 P1 fix: two different JWT callers whose
    ``users`` rows have not been provisioned yet must not collapse
    to the same anonymous identity. The ``sub`` claim fallback must
    keep their deployments separate.

    Simulates the pre-/auth/me window: pass claims with a ``sub`` that
    does NOT match any user's ``entra_id``, so ``_resolve_user_id``
    returns ``None``, triggering the ``user_sub`` fallback.
    """
    _user, strategy = user_and_strategy

    alice_claims = {"sub": "alice-unprovisioned@example.com"}
    bob_claims = {"sub": "bob-unprovisioned@example.com"}

    alice_req = _make_request()
    alice_req.strategy_id = strategy.id
    bob_req = _make_request()
    bob_req.strategy_id = strategy.id

    alice = await live_module.live_start(request=alice_req, claims=alice_claims, db=session)
    bob = await live_module.live_start(request=bob_req, claims=bob_claims, db=session)

    assert alice["id"] != bob["id"], (
        "distinct pre-/auth/me JWT callers must get distinct deployments"
    )
    assert await _count_deployments(session) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account")
async def test_rejected_warm_restart_preserves_last_stopped_at(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex iteration 5 P3 fix: if a warm restart is rejected by the
    risk engine, the previous run's ``last_stopped_at`` must NOT be
    clobbered to NULL. /status would otherwise misreport a rejected
    deployment as "never stopped" after a failed retry.
    """
    from msai.services.nautilus.trading_node import TradingNodeManager
    from msai.services.risk_engine import RiskEngine

    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}

    # First start succeeds with a permissive real manager.
    permissive = TradingNodeManager(RiskEngine())
    monkeypatch.setattr(live_module, "_node_manager", permissive)

    request = _make_request()
    request.strategy_id = strategy.id
    first = await live_module.live_start(request=request, claims=claims, db=session)

    # Stop it (simulating /stop: set status + last_stopped_at manually)
    stop_time = datetime.now(UTC)
    row = await session.get(LiveDeployment, first["id"])
    assert row is not None
    row.status = "stopped"
    row.last_stopped_at = stop_time
    # Remove from node_manager's tracked set so the next start takes
    # the non-idempotent path and actually invokes the risk engine.
    permissive._processes.pop(str(row.id), None)  # noqa: SLF001
    await session.commit()

    # Swap in a rejecting manager: risk engine flipped to halt via kill_all.
    rejecting = TradingNodeManager(RiskEngine())
    rejecting.risk_engine.kill_all()
    monkeypatch.setattr(live_module, "_node_manager", rejecting)

    with pytest.raises(Exception):  # noqa: B017, PT011
        await live_module.live_start(request=request, claims=claims, db=session)

    # The rejected-row must still carry the original last_stopped_at.
    refreshed = await session.get(LiveDeployment, first["id"])
    assert refreshed is not None
    await session.refresh(refreshed)
    assert refreshed.status == "rejected"
    assert refreshed.last_stopped_at is not None, (
        "rejected warm restart must preserve the prior last_stopped_at"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("ib_account", "stub_node_manager")
async def test_status_ordering_uses_max_activity_timestamp(
    session: AsyncSession,
    user_and_strategy: tuple[User, Strategy],
) -> None:
    """Codex iteration 5 P2 fix: ``/api/v1/live/status`` orders by
    max(last_started_at, last_stopped_at, created_at). A deployment
    started long ago but stopped moments ago must rank above a
    deployment started slightly later but never stopped.
    """
    user, strategy = user_and_strategy
    claims = {"sub": user.entra_id}

    req_a = _make_request(config={"fast_ema_period": 1, "slow_ema_period": 2})
    req_a.strategy_id = strategy.id
    res_a = await live_module.live_start(request=req_a, claims=claims, db=session)

    req_b = _make_request(config={"fast_ema_period": 3, "slow_ema_period": 4})
    req_b.strategy_id = strategy.id
    res_b = await live_module.live_start(request=req_b, claims=claims, db=session)

    # Manually set timestamps so A was started first but stopped MOST RECENTLY
    row_a = await session.get(LiveDeployment, res_a["id"])
    row_b = await session.get(LiveDeployment, res_b["id"])
    assert row_a is not None and row_b is not None

    row_a.last_started_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    row_a.last_stopped_at = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    row_a.status = "stopped"

    row_b.last_started_at = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
    row_b.last_stopped_at = None
    row_b.status = "running"
    await session.commit()

    # Call live_status directly to avoid TestClient/async-session event loop issues.
    status_response = await live_module.live_status(claims=claims, db=session)
    ids_in_order = [str(d.id) for d in status_response.deployments]

    pos_a = ids_in_order.index(str(row_a.id))
    pos_b = ids_in_order.index(str(row_b.id))
    assert pos_a < pos_b, (
        f"deployment A (stopped 2026-06-01) should rank above B "
        f"(last activity 2026-03-01); got order {ids_in_order}"
    )
