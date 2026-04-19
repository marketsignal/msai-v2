"""Integration tests for ``run_subprocess_async`` (Phase 1 task 1.8).

Exercises the full subprocess lifecycle with a fake ``node_factory``
so every correctness property (order-of-operations, pid self-write,
failure paths, terminal writes, dispose-in-finally) is tested
without touching Nautilus or IB Gateway.

SAFETY: dedicated Postgres testcontainer per module.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base, LiveDeployment, LiveNodeProcess, Strategy, User
from msai.services.live.failure_kind import FailureKind
from msai.services.nautilus.trading_node_subprocess import (
    TradingNodePayload,
    run_subprocess_async,
)
from tests.integration._deployment_factory import make_live_deployment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


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
async def seeded_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> LiveNodeProcess:
    """Insert a User → Strategy → LiveDeployment → LiveNodeProcess chain
    in the 'starting' state, matching what the supervisor's phase A
    would leave on disk before spawning the child."""
    async with session_factory() as session:
        user = User(
            id=uuid4(),
            entra_id=f"sub-{uuid4().hex}",
            email=f"sub-{uuid4().hex}@example.com",
            role="operator",
        )
        session.add(user)
        await session.flush()

        strategy = Strategy(
            id=uuid4(),
            name="sub-test",
            file_path="strategies/example/ema_cross.py",
            strategy_class="EMACrossStrategy",
            created_by=user.id,
        )
        session.add(strategy)
        await session.flush()

        dep = await make_live_deployment(session, user=user, strategy=strategy, status="starting")

        row = LiveNodeProcess(
            id=uuid4(),
            deployment_id=dep.id,
            gateway_session_key="msai-paper-primary:localhost:4002",
            pid=None,  # supervisor leaves this NULL — subprocess self-writes
            host="test-host",
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            status="starting",
        )
        session.add(row)
        await session.commit()
        return row


def _make_payload(row_id, deployment_id, deployment_slug: str) -> TradingNodePayload:
    return TradingNodePayload(
        row_id=row_id,
        deployment_id=deployment_id,
        deployment_slug=deployment_slug,
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={"fast_ema_period": 10, "slow_ema_period": 30},
        paper_symbols=["AAPL"],
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_account_id="DU1234567",
        database_url="",  # unused — tests pass their own session factory
        startup_health_timeout_s=1.0,
    )


# ---------------------------------------------------------------------------
# Fake node helpers
# ---------------------------------------------------------------------------


class _RecordedCall:
    """Records the time a lifecycle method was called so tests can
    assert on ordering between event loops and threads."""

    def __init__(self) -> None:
        self.ts: float | None = None

    def record(self) -> None:
        import time

        self.ts = time.monotonic()


class _FakeTrader:
    """Mimics ``node.kernel.trader`` with a flip-on-Nth-poll is_running."""

    def __init__(self, *, flip_on_poll: int | None = 1) -> None:
        self._flip_on_poll = flip_on_poll
        self._polls = 0

    @property
    def is_running(self) -> bool:
        self._polls += 1
        if self._flip_on_poll is None:
            return False
        return self._polls >= self._flip_on_poll


class _FakeKernel:
    def __init__(self, trader: _FakeTrader) -> None:
        self.trader = trader
        self.data_engine = type("_DE", (), {"check_connected": staticmethod(lambda: False)})()
        self.exec_engine = type(
            "_EE",
            (),
            {"check_connected": staticmethod(lambda: False), "_clients": {}},
        )()
        self.portfolio = type("_P", (), {"initialized": False})()
        self.cache = type("_C", (), {"instruments": staticmethod(list)})()


class _FakeNode:
    """Stand-in for ``nautilus_trader.live.node.TradingNode`` with just
    the methods the subprocess actually calls. The lifecycle flags let
    tests assert ordering + cleanup.

    Codex batch 3 iter10 P0: matches the REAL Nautilus 1.223.0
    ``TradingNode`` API — there is no ``start_async()`` method, and
    ``run()`` doesn't block when a loop is running. The async entry
    point is ``run_async()``, which internally does
    ``await kernel.start_async()`` (flipping ``trader.is_running``)
    and then blocks on a gather over engine queue tasks until
    ``stop_async`` is called.

    The fake's ``run_async()`` mirrors that contract:
    1. Records start
    2. Flips the trader state (so ``wait_until_ready`` succeeds)
    3. Awaits an internal stop event until ``stop_async`` is called
    4. Returns
    """

    def __init__(
        self,
        *,
        build_raises: BaseException | None = None,
        run_async_raises: BaseException | None = None,
        run_async_raises_after_ready: BaseException | None = None,
        never_becomes_ready: bool = False,
        block_run_async: bool = False,
    ) -> None:
        # The trader's ``is_running`` is bound to ``_run_started``
        # so wait_until_ready only sees True after run_async has
        # set it (matching real Nautilus where the FSM transitions
        # inside ``kernel.start_async``).
        self._stop_event = asyncio.Event()
        self._run_started = False
        self._never_becomes_ready = never_becomes_ready
        self._block_run_async = block_run_async

        class _Trader:
            def __init__(inner) -> None:  # noqa: N805
                inner._polls = 0

            @property
            def is_running(inner) -> bool:  # noqa: N805
                inner._polls += 1
                return self._run_started

        self.kernel = _FakeKernel(_Trader())
        self.build_call = _RecordedCall()
        self.run_async_call = _RecordedCall()
        self.stop_async_call = _RecordedCall()
        self.dispose_call = _RecordedCall()
        self._build_raises = build_raises
        self._run_async_raises = run_async_raises
        self._run_async_raises_after_ready = run_async_raises_after_ready

    # --- lifecycle methods (real Nautilus 1.223.0 API) -------------------
    def build(self) -> None:
        self.build_call.record()
        if self._build_raises is not None:
            raise self._build_raises

    async def run_async(self) -> None:
        """Real Nautilus ``TradingNode.run_async`` does
        ``await kernel.start_async()`` (which sets is_running=True)
        then blocks on engine queue tasks until shutdown.

        Default behavior here: set is_running=True and return
        immediately. That mirrors the OLD fake's no-op ``run()``
        and lets happy-path tests run quickly without needing to
        externally call ``stop_async``. Tests that need run_async
        to actually block (e.g. shutdown checkpoints) opt in via
        ``block_run_async=True``.
        """
        self.run_async_call.record()
        if self._run_async_raises is not None:
            raise self._run_async_raises
        # Flip trader.is_running True so wait_until_ready unblocks,
        # UNLESS the test asked us to simulate a wedged startup
        # where the FSM never transitions.
        if not self._never_becomes_ready:
            self._run_started = True
        if self._run_async_raises_after_ready is not None:
            # Yield once so wait_until_ready can observe is_running
            await asyncio.sleep(0)
            raise self._run_async_raises_after_ready
        if self._block_run_async or self._never_becomes_ready:
            # Tests asking for a long-running node — block until
            # stop_async fires or the task is cancelled.
            await self._stop_event.wait()
        # Default: yield once so wait_until_ready can observe
        # is_running, then return cleanly.
        await asyncio.sleep(0)

    async def stop_async(self) -> None:
        self.stop_async_call.record()
        self._run_started = False
        self._stop_event.set()

    def dispose(self) -> None:
        self.dispose_call.record()


async def _fetch_row(session_factory: async_sessionmaker[AsyncSession], row_id) -> LiveNodeProcess:
    async with session_factory() as session:
        row = await session.get(LiveNodeProcess, row_id)
        assert row is not None
        return row


# ---------------------------------------------------------------------------
# Happy path + ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_writes_pid_before_build(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex v5 P0 regression: ``pid`` must be populated on the row
    BEFORE ``node.build()`` runs, so the supervisor's ``/stop`` path
    can read it even if phase-C never wrote it.

    Verified at the Python level via call-order timestamps on
    ``_self_write_pid`` (the function that persists the pid) and
    ``node.build``. Combined with the post-run assertion that
    ``row.pid == os.getpid()``, this proves the pid was durably on
    the row before build ran.
    """
    import time

    from msai.services.nautilus import trading_node_subprocess as mod

    call_log: list[tuple[str, float]] = []

    # Wrap _self_write_pid so we know exactly when it completes.
    original_self_write = mod._self_write_pid

    async def _spying_self_write(sf, row_id) -> None:
        await original_self_write(sf, row_id)
        call_log.append(("self_write_pid_done", time.monotonic()))

    monkeypatch.setattr(mod, "_self_write_pid", _spying_self_write)

    real_node = _FakeNode()
    original_build = real_node.build

    def _recording_build() -> None:
        call_log.append(("build_called", time.monotonic()))
        original_build()

    real_node.build = _recording_build  # type: ignore[method-assign]

    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: real_node,
    )
    assert exit_code == 0

    # Ordering assertion
    names = [name for name, _ in call_log]
    assert names.index("self_write_pid_done") < names.index("build_called"), (
        f"self_write_pid must complete before node.build runs; log was {names}"
    )

    # And the pid is durably on the row
    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.pid == os.getpid()


@pytest.mark.asyncio
async def test_subprocess_order_of_operations(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """Canonical order from decision #17 / v6 spec:
    self-write pid → build → start_async → wait_until_ready → ready →
    run → stop_async → dispose → terminal write."""
    node = _FakeNode()
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
    )

    assert exit_code == 0
    # Every lifecycle method was called
    assert node.build_call.ts is not None
    assert node.run_async_call.ts is not None
    assert node.stop_async_call.ts is not None
    assert node.dispose_call.ts is not None
    # And in the right order:
    # build → run_async (which internally does kernel.start_async +
    # blocks) → stop_async → dispose. iter10 P0 fix removed the
    # fictional separate start_async / run pair.
    assert node.build_call.ts < node.run_async_call.ts
    assert node.run_async_call.ts < node.stop_async_call.ts
    assert node.stop_async_call.ts < node.dispose_call.ts


@pytest.mark.asyncio
async def test_clean_exit_marks_row_stopped_with_failure_kind_none(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    node = _FakeNode()
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
    )
    assert exit_code == 0

    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "stopped"
    assert row.failure_kind == FailureKind.NONE.value
    assert row.exit_code == 0
    assert row.error_message is None
    assert row.pid == os.getpid()

    # Fix A (2026-04-15): _mark_terminal must also sync the parent
    # LiveDeployment.status so the logical deployment view doesn't
    # linger at "starting"/"running" after the process exits cleanly.
    async with session_factory() as session:
        dep = await session.get(LiveDeployment, row.deployment_id)
        assert dep is not None
        assert dep.status == "stopped"


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_health_check_failure_marks_reconciliation_failed(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """Decision #14 + plan v8 Codex v7 P1: a
    :class:`StartupHealthCheckFailed` must be captured as
    ``FailureKind.RECONCILIATION_FAILED`` with the diagnosis in
    ``error_message`` and exit code 2. Dispose MUST still run."""
    # Node whose is_running never flips True — wait_until_ready will
    # time out and raise StartupHealthCheckFailed. With the iter10
    # API rewrite, this means run_async() ran but never set the
    # trader to running, so wait_until_ready hits its deadline.
    node = _FakeNode(never_becomes_ready=True)
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
    )
    assert exit_code == 2

    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "failed"
    assert row.failure_kind == FailureKind.RECONCILIATION_FAILED.value
    assert row.exit_code == 2
    assert row.error_message is not None
    assert "trader.is_running=False" in row.error_message
    # Dispose must have been called (gotcha #20)
    assert node.dispose_call.ts is not None

    # Fix A (2026-04-15): _mark_terminal also syncs the parent
    # LiveDeployment.status so /live/status shows "failed" instead of
    # lingering at "starting" after reconciliation fails.
    async with session_factory() as session:
        dep = await session.get(LiveDeployment, row.deployment_id)
        assert dep is not None
        assert dep.status == "failed"

    # build() and run_async() were called (run_async is the SINGLE
    # async entry point in the real Nautilus API). The
    # health-check failure cancels the run_async task.
    assert node.build_call.ts is not None
    assert node.run_async_call.ts is not None


@pytest.mark.asyncio
async def test_build_exception_marks_spawn_failed_permanent(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """A non-health-check exception during build must become
    ``FailureKind.SPAWN_FAILED_PERMANENT`` with the traceback in
    ``error_message`` and exit code 1."""
    node = _FakeNode(build_raises=RuntimeError("build went boom"))
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
    )
    assert exit_code == 1

    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "failed"
    assert row.failure_kind == FailureKind.SPAWN_FAILED_PERMANENT.value
    assert row.exit_code == 1
    assert row.error_message is not None
    assert "build went boom" in row.error_message
    # Dispose is still called
    assert node.dispose_call.ts is not None


@pytest.mark.asyncio
async def test_run_async_exception_after_ready_marks_spawn_failed_permanent(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """A post-ready exception inside ``node.run_async()`` (e.g.
    one of the engine queue tasks crashes after the trader is
    running) is still a permanent failure from the supervisor's POV
    (the endpoint's cached 201 is already correct; the next
    attempt goes through a fresh identity_signature check)."""
    node = _FakeNode(
        run_async_raises_after_ready=RuntimeError("strategy crashed mid-run"),
    )
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
    )
    assert exit_code == 1

    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "failed"
    assert row.failure_kind == FailureKind.SPAWN_FAILED_PERMANENT.value
    assert row.exit_code == 1
    assert "strategy crashed mid-run" in (row.error_message or "")

    # run_async was called, but the post-ready raise propagated up
    # through ``await node_run_task`` and was caught by the generic
    # exception handler.
    assert node.run_async_call.ts is not None
    assert node.dispose_call.ts is not None


@pytest.mark.asyncio
async def test_self_write_pid_exception_still_persists_terminal_row(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex batch 3 iter7 P3 regression: if ``_self_write_pid`` raises
    (e.g. transient DB blip), the catch-all ``except`` MUST capture
    it and the ``finally`` MUST persist a structured terminal row.
    Before the fix, ``_self_write_pid`` ran outside the try block,
    so an exception there would skip ``_mark_terminal`` entirely and
    the operator would only see the reap loop's generic ``child
    exited with code 1`` instead of the actual traceback."""
    from msai.services.nautilus import trading_node_subprocess as mod

    async def _broken_self_write(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated DB blip during pid self-write")

    monkeypatch.setattr(mod, "_self_write_pid", _broken_self_write)

    factory_called = False

    def _factory(_p: TradingNodePayload) -> Any:
        nonlocal factory_called
        factory_called = True
        return _FakeNode()

    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=_factory,
    )
    assert exit_code == 1
    # The factory must NOT have been called — we failed before reaching it
    assert factory_called is False

    # The finally block's terminal write MUST have run with the
    # structured failure kind + traceback in error_message.
    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "failed"
    assert row.failure_kind == FailureKind.SPAWN_FAILED_PERMANENT.value
    assert row.exit_code == 1
    assert row.error_message is not None
    assert "simulated DB blip" in row.error_message


@pytest.mark.asyncio
async def test_heartbeat_start_exception_still_persists_terminal_row(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """Codex batch 3 iter7 P3 regression (heartbeat path): if
    ``heartbeat.start()`` raises (e.g. thread spawn failure), the
    same guard must capture the failure and persist a structured
    terminal row."""

    class _BrokenHeartbeat:
        def start(self) -> None:
            raise RuntimeError("simulated thread-start failure")

        def stop(self) -> None:
            pass

    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: _FakeNode(),
        heartbeat_factory=lambda _p: _BrokenHeartbeat(),
    )
    assert exit_code == 1

    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "failed"
    assert row.failure_kind == FailureKind.SPAWN_FAILED_PERMANENT.value
    assert row.error_message is not None
    assert "simulated thread-start failure" in row.error_message


@pytest.mark.asyncio
async def test_node_factory_exception_marks_spawn_failed_permanent(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """If the node_factory itself raises (e.g. config validation
    failed), the subprocess must still write a terminal row."""

    def _bad_factory(_payload: TradingNodePayload) -> Any:
        raise ValueError("bad config")

    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=_bad_factory,
    )
    assert exit_code == 1

    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "failed"
    assert row.failure_kind == FailureKind.SPAWN_FAILED_PERMANENT.value
    assert "bad config" in (row.error_message or "")


# ---------------------------------------------------------------------------
# Self-write pid behavior in isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_write_pid_transitions_status_to_building(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """The self-write step must flip the status from 'starting'
    (supervisor insert) to 'building' so the HeartbeatMonitor's
    exclusion of startup statuses kicks in immediately."""
    from msai.services.nautilus.trading_node_subprocess import _self_write_pid

    assert seeded_row.status == "starting"

    await _self_write_pid(session_factory, seeded_row.id)

    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "building"
    assert row.pid == os.getpid()
    assert row.last_heartbeat_at > seeded_row.last_heartbeat_at


# ---------------------------------------------------------------------------
# Shutdown-mid-startup (Codex batch 3 iter2 P1 regression)
#
# The SIGTERM handler inside ``run_subprocess_async`` sets a shutdown
# event and schedules ``node.stop_async()``, but neither of those
# interrupts ``node.build()`` / ``node.start_async()`` /
# ``wait_until_ready()`` on its own. Without explicit checkpoints at
# each phase boundary, a signal delivered during startup is lost and
# the subprocess marches straight into ``node.run()``. These tests
# inject a shared ``asyncio.Event`` the fake node can set from its
# lifecycle methods and assert that each checkpoint causes a clean
# abort to ``status='stopped'`` without reaching later phases.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_requested_before_node_factory_skips_build(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """Codex batch 3 iter6 P2 regression: if the shutdown event is
    already set when ``run_subprocess_async`` enters the try block,
    ``node_factory`` and ``node.build()`` MUST NOT be called. In
    production this corresponds to SIGTERM landing between
    ``loop.add_signal_handler`` and the first checkpoint — the
    handler has already flipped the flag and we shouldn't waste
    seconds on Nautilus construction before honoring the stop."""
    import asyncio

    shutdown = asyncio.Event()
    shutdown.set()  # already requested before run_subprocess_async runs

    factory_called = False

    def _factory(_p: TradingNodePayload) -> Any:
        nonlocal factory_called
        factory_called = True
        return _FakeNode()

    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=_factory,
        shutdown_event=shutdown,
    )
    assert exit_code == 0
    assert factory_called is False, (
        "node_factory must not be called when shutdown_requested is "
        "already set on entry — the iter6 P2 fix's earliest checkpoint "
        "must skip Nautilus construction entirely."
    )

    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "stopped"
    assert row.failure_kind == FailureKind.NONE.value


@pytest.mark.asyncio
async def test_shutdown_requested_during_build_aborts_to_stopped(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """If the shutdown event fires while ``node.build()`` is running,
    the post-build checkpoint must abort startup: ``start_async``,
    ``wait_until_ready``, and ``run`` must NOT be called; the row is
    marked ``stopped``/``FailureKind.NONE``; dispose still runs."""
    import asyncio

    shutdown = asyncio.Event()
    node = _FakeNode()

    original_build = node.build

    def _build_that_fires_shutdown() -> None:
        original_build()
        shutdown.set()

    node.build = _build_that_fires_shutdown  # type: ignore[method-assign]

    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
        shutdown_event=shutdown,
    )
    assert exit_code == 0

    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "stopped"
    assert row.failure_kind == FailureKind.NONE.value
    assert row.exit_code == 0

    # build was called, but run_async was never scheduled — the
    # after_build checkpoint short-circuited.
    assert node.build_call.ts is not None
    assert node.run_async_call.ts is None
    # Dispose still runs (gotcha #20)
    assert node.dispose_call.ts is not None


# Note: the iter2 ``test_shutdown_requested_during_start_async_aborts_to_stopped``
# test is intentionally removed in iter10 — there is no longer a
# separate ``start_async`` step in the lifecycle. ``run_async`` is the
# single async entry point and the after-build / after-wait-until-ready
# / before-node-run checkpoints already cover every shutdown window
# the old test exercised.


# Note: the iter2 ``test_shutdown_requested_before_run_aborts_to_stopped``
# test was removed in iter10 — the same checkpoint window is now
# exercised by ``test_wait_until_ready_respects_shutdown_event``
# below, which uses a never-ready trader and a poll-triggered
# shutdown to drive the after-wait-until-ready abort path. With the
# real ``run_async`` API, the ``before_node_run`` checkpoint and
# the ``after_wait_until_ready`` checkpoint catch the same race
# from different angles.


# ---------------------------------------------------------------------------
# Terminal-write-after-cleanup ordering (Codex batch 3 iter3 P1 regression)
#
# Persisting the terminal row before the finally block's cleanup
# completes would drop the row out of the active-status set while
# this subprocess is still holding the IB sockets + Rust-side
# logger. A fast stop/restart could then reserve a new row and
# spawn a second child for the same deployment before this one has
# finished releasing its resources. The regression tests below
# spy on ``_mark_terminal`` to assert it runs AFTER ``node.dispose``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_write_happens_after_dispose_on_clean_exit(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean exit path: ``_mark_terminal`` must run AFTER
    ``node.dispose()``. If it runs before, the row drops out of the
    active-status set while IB sockets are still held and a restart
    race can spawn a second child (Codex batch 3 iter3 P1)."""
    import time

    from msai.services.nautilus import trading_node_subprocess as mod

    mark_terminal_ts: list[float] = []

    original = mod._mark_terminal

    async def _spy(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        mark_terminal_ts.append(time.monotonic())

    monkeypatch.setattr(mod, "_mark_terminal", _spy)

    node = _FakeNode()
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
    )
    assert exit_code == 0
    assert node.dispose_call.ts is not None
    assert len(mark_terminal_ts) == 1
    assert mark_terminal_ts[0] > node.dispose_call.ts, (
        "Terminal row must be persisted AFTER node.dispose() returns — "
        "otherwise the row drops out of the active-status set while IB "
        "sockets + Rust logger are still held, enabling a restart race "
        "to spawn a second child for the same deployment."
    )


@pytest.mark.asyncio
async def test_terminal_write_happens_after_dispose_on_build_failure(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same ordering invariant on the exception path: a crash during
    ``build()`` must still persist the terminal row AFTER dispose
    runs."""
    import time

    from msai.services.nautilus import trading_node_subprocess as mod

    mark_terminal_ts: list[float] = []

    original = mod._mark_terminal

    async def _spy(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        mark_terminal_ts.append(time.monotonic())

    monkeypatch.setattr(mod, "_mark_terminal", _spy)

    node = _FakeNode(build_raises=RuntimeError("build exploded"))
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
    )
    assert exit_code == 1
    assert node.dispose_call.ts is not None
    assert len(mark_terminal_ts) == 1
    assert mark_terminal_ts[0] > node.dispose_call.ts


@pytest.mark.asyncio
async def test_terminal_write_happens_after_dispose_on_health_check_failure(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same ordering invariant on the RECONCILIATION_FAILED path:
    ``wait_until_ready`` timing out must still persist the
    ``failed/reconciliation_failed`` row AFTER dispose runs."""
    import time

    from msai.services.nautilus import trading_node_subprocess as mod

    mark_terminal_ts: list[float] = []

    original = mod._mark_terminal

    async def _spy(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)
        mark_terminal_ts.append(time.monotonic())

    monkeypatch.setattr(mod, "_mark_terminal", _spy)

    node = _FakeNode(never_becomes_ready=True)  # is_running never True
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
    )
    assert exit_code == 2

    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "failed"
    assert row.failure_kind == FailureKind.RECONCILIATION_FAILED.value

    assert node.dispose_call.ts is not None
    assert len(mark_terminal_ts) == 1
    assert mark_terminal_ts[0] > node.dispose_call.ts


@pytest.mark.asyncio
async def test_heartbeat_stops_after_dispose(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """Codex batch 3 iter4 P1 regression: the heartbeat thread MUST
    keep running through ``node.stop_async()`` and ``dispose()`` so
    a slow cleanup (IB socket teardown, Rust logger flush) can't
    exceed the HeartbeatMonitor's stale threshold and let the
    monitor flip the still-live row to ``failed``. The fix
    guarantees ``heartbeat.stop()`` is called AFTER
    ``node.dispose()`` returns."""
    import time

    class _StubHeartbeat:
        def __init__(self) -> None:
            self.stop_ts: float | None = None
            self.start_ts: float | None = None

        def start(self) -> None:
            self.start_ts = time.monotonic()

        def stop(self) -> None:
            self.stop_ts = time.monotonic()

    stub_hb = _StubHeartbeat()
    node = _FakeNode()
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
        heartbeat_factory=lambda _p: stub_hb,
    )
    assert exit_code == 0
    assert node.dispose_call.ts is not None
    assert stub_hb.stop_ts is not None
    assert stub_hb.stop_ts > node.dispose_call.ts, (
        "Heartbeat must stop AFTER node.dispose() — otherwise a slow "
        "dispose can exceed the stale threshold and let the "
        "HeartbeatMonitor flip the row to failed before the terminal "
        "write runs, opening the duplicate-spawn window the iter3 fix "
        "was supposed to close."
    )


@pytest.mark.asyncio
async def test_wait_until_ready_respects_shutdown_event(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """Codex batch 3 iter3 P2 regression: if SIGTERM arrives while
    ``wait_until_ready`` is polling (and the trader never flips to
    running), the subprocess must observe the shutdown and abort to
    ``stopped`` quickly — NOT wait out ``startup_health_timeout_s``
    and record ``failed/reconciliation_failed``."""
    import asyncio

    shutdown = asyncio.Event()

    class _NeverReadyNode(_FakeNode):
        """Trader that never becomes ready AND fires the shutdown
        event on the second poll — simulates SIGTERM arriving during
        a wedged startup."""

        def __init__(self) -> None:
            super().__init__(never_becomes_ready=True)

            class _FiringTrader:
                def __init__(self) -> None:
                    self._polls = 0

                @property
                def is_running(self_inner) -> bool:  # noqa: N805
                    self_inner._polls += 1
                    # Fire the shutdown on the second poll so the
                    # first poll path is exercised normally.
                    if self_inner._polls >= 2:
                        shutdown.set()
                    return False

            self.kernel.trader = _FiringTrader()

    node = _NeverReadyNode()
    # Huge timeout — if the shutdown isn't observed, the test would
    # hang for 60s instead of exiting in well under 1s.
    payload = TradingNodePayload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={"fast_ema_period": 10, "slow_ema_period": 30},
        paper_symbols=["AAPL"],
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_account_id="DU1234567",
        database_url="",
        startup_health_timeout_s=60.0,
    )

    import time

    started = time.monotonic()
    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
        shutdown_event=shutdown,
    )
    elapsed = time.monotonic() - started

    # Must have exited quickly — well under the 60s timeout — and
    # as a clean shutdown, not a RECONCILIATION_FAILED.
    assert exit_code == 0, f"expected clean exit 0, got {exit_code}"
    assert elapsed < 5.0, (
        f"subprocess took {elapsed:.1f}s — wait_until_ready did NOT "
        f"observe the shutdown_event and waited out the full timeout."
    )

    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "stopped"
    assert row.failure_kind == FailureKind.NONE.value


# ---------------------------------------------------------------------------
# IB disconnect handler sibling-task wiring (Phase 4 task 4.2 iter-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_handler_factory_none_means_no_task_spawned(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """Default behavior (and all pre-existing tests): if the
    caller passes no ``disconnect_handler_factory``, no disconnect
    task is spawned and the subprocess lifecycle is unchanged.
    """
    node = _FakeNode()
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
        disconnect_handler_factory=None,
    )

    assert exit_code == 0


@pytest.mark.asyncio
async def test_disconnect_handler_spawn_and_clean_cancel(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """The factory returns a handler whose ``run`` method blocks
    on the shared ``shutdown_event`` (standard handler contract).
    Verify: factory called with (payload, node), run task spawned,
    exits cleanly on shutdown, and aclose hook fires for cleanup.
    """
    # block_run_async=True so the node's run_async doesn't
    # return until stop_async is called — gives the handler
    # task time to actually start running.
    node = _FakeNode(block_run_async=True)
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    factory_calls: list[tuple[Any, Any]] = []
    aclose_fired = {"count": 0}
    run_entered = asyncio.Event()

    class _FakeHandler:
        async def run(self, stop_event: asyncio.Event) -> None:
            run_entered.set()
            await stop_event.wait()

        async def aclose(self) -> None:
            aclose_fired["count"] += 1

    def _factory(p: TradingNodePayload, n: Any) -> _FakeHandler:
        factory_calls.append((p, n))
        return _FakeHandler()

    shutdown = asyncio.Event()

    async def _trigger_shutdown() -> None:
        # Wait until the handler's ``run`` actually started, then
        # simulate what SIGTERM would do in production: set the
        # shared shutdown event AND stop the node (since we're
        # running with ``install_signal_handlers=False`` nothing
        # else drives ``node.stop_async``).
        await run_entered.wait()
        await asyncio.sleep(0.05)
        shutdown.set()
        await node.stop_async()

    trigger = asyncio.create_task(_trigger_shutdown())
    try:
        exit_code = await run_subprocess_async(
            payload,
            session_factory=session_factory,
            node_factory=lambda _p: node,
            disconnect_handler_factory=_factory,
            shutdown_event=shutdown,
        )
    finally:
        trigger.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await trigger

    assert exit_code == 0
    assert len(factory_calls) == 1
    assert factory_calls[0][0] is payload
    assert factory_calls[0][1] is node
    assert run_entered.is_set()
    # aclose hook fires in the finally block regardless of
    # whether the handler exited normally or was cancelled
    assert aclose_fired["count"] == 1


@pytest.mark.asyncio
async def test_disconnect_handler_cancelled_when_still_running_at_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """If the handler is still running when the cleanup block
    begins (e.g. it ignores ``stop_event`` or is blocking on a
    Redis operation), the cleanup path MUST cancel it so the
    subprocess shutdown isn't blocked by a wedged handler."""
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    run_cancelled = {"value": False}
    run_entered = asyncio.Event()

    class _StubbornHandler:
        """Handler that ignores ``stop_event`` and can only be
        shut down via ``asyncio.CancelledError`` — simulates a
        handler wedged on a Redis call."""

        async def run(self, stop_event: asyncio.Event) -> None:  # noqa: ARG002
            run_entered.set()
            try:
                await asyncio.Event().wait()  # forever
            except asyncio.CancelledError:
                run_cancelled["value"] = True
                raise

    # block_run_async=True so the subprocess actually waits
    # for shutdown (otherwise run_async returns immediately
    # and the handler never gets a chance to run).
    node = _FakeNode(block_run_async=True)

    shutdown = asyncio.Event()

    async def _trigger() -> None:
        # Simulate SIGTERM behavior: flip shutdown + stop the node.
        # ``install_signal_handlers=False`` here so the test is
        # responsible for both actions that the production signal
        # handler would perform.
        await run_entered.wait()
        await asyncio.sleep(0.05)
        shutdown.set()
        await node.stop_async()

    trigger = asyncio.create_task(_trigger())
    try:
        exit_code = await run_subprocess_async(
            payload,
            session_factory=session_factory,
            node_factory=lambda _p: node,
            disconnect_handler_factory=lambda _p, _n: _StubbornHandler(),
            shutdown_event=shutdown,
        )
    finally:
        trigger.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await trigger

    assert exit_code == 0
    # The wedged handler was cancelled by the finally block so
    # the subprocess could finish its shutdown sequence
    assert run_cancelled["value"] is True


@pytest.mark.asyncio
async def test_disconnect_handler_async_factory_awaited(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """The production factory is async (it opens an aioredis
    client via ``await``). Verify the subprocess path awaits
    an async factory's return value via ``_maybe_await``."""
    node = _FakeNode()
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    built: list[str] = []

    class _FakeHandler:
        async def run(self, stop_event: asyncio.Event) -> None:
            stop_event.set()

    async def _async_factory(p: TradingNodePayload, n: Any) -> _FakeHandler:
        await asyncio.sleep(0)
        built.append("built")
        return _FakeHandler()

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
        disconnect_handler_factory=_async_factory,
    )

    assert exit_code == 0
    assert built == ["built"]


@pytest.mark.asyncio
async def test_disconnect_handler_factory_failure_does_not_fail_deployment(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """If the disconnect handler factory raises during
    construction, the subprocess logs loudly but does NOT
    fail the deployment — the heartbeat watchdog is the
    fallback safety net, and losing disconnect monitoring
    is a degraded-service condition, not a cause to tear
    down a running deployment."""
    node = _FakeNode()
    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    def _bad_factory(p: TradingNodePayload, n: Any) -> Any:
        raise RuntimeError("fake redis construction failed")

    exit_code = await run_subprocess_async(
        payload,
        session_factory=session_factory,
        node_factory=lambda _p: node,
        disconnect_handler_factory=_bad_factory,
    )

    assert exit_code == 0
    row = await _fetch_row(session_factory, seeded_row.id)
    assert row.status == "stopped"
    assert row.failure_kind == FailureKind.NONE.value


@pytest.mark.asyncio
async def test_disconnect_handler_cancelled_before_node_stop(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_row: LiveNodeProcess,
) -> None:
    """Cleanup ordering: a still-running disconnect task MUST
    be cancelled BEFORE ``node.stop_async()`` so the handler
    isn't probing the data engine while it's tearing down.
    Uses a stubborn handler that only exits via cancellation
    to make the cancellation observable in the events log.
    """
    events: list[str] = []
    run_entered = asyncio.Event()
    captured_node: dict[str, Any] = {}

    class _OrderingNode(_FakeNode):
        def __init__(self) -> None:
            super().__init__(block_run_async=True)

        async def stop_async(self) -> None:
            events.append("node_stop_async")
            await super().stop_async()

    class _OrderingHandler:
        async def run(self, stop_event: asyncio.Event) -> None:  # noqa: ARG002
            run_entered.set()
            try:
                await asyncio.Event().wait()  # forever
            except asyncio.CancelledError:
                events.append("handler_cancelled")
                raise

    payload = _make_payload(
        row_id=seeded_row.id,
        deployment_id=seeded_row.deployment_id,
        deployment_slug="abcd1234abcd1234",
    )

    shutdown = asyncio.Event()

    def _node_factory(_p: TradingNodePayload) -> Any:
        node = _OrderingNode()
        captured_node["node"] = node
        return node

    async def _trigger() -> None:
        # Wait until handler's ``run`` is in-flight, then cancel
        # ``node_run_task`` directly to drive cleanup. In the real
        # system the SIGTERM handler would call ``node.stop_async``
        # which unblocks ``run_async``; the ordering assertion only
        # cares that the disconnect handler cancel happens BEFORE
        # ``node.stop_async()``, so we don't want the test to call
        # ``stop_async`` itself from this trigger.
        await run_entered.wait()
        await asyncio.sleep(0.05)
        # Cancel run_async by setting the stop_event on the fake —
        # this mirrors what stop_async would do from the perspective
        # of run_async but keeps the stop_async CALL for the finally
        # block to make.
        node = captured_node["node"]
        node._run_started = False  # type: ignore[attr-defined]
        node._stop_event.set()  # type: ignore[attr-defined]
        shutdown.set()

    trigger = asyncio.create_task(_trigger())
    try:
        exit_code = await run_subprocess_async(
            payload,
            session_factory=session_factory,
            node_factory=_node_factory,
            disconnect_handler_factory=lambda _p, _n: _OrderingHandler(),
            shutdown_event=shutdown,
        )
    finally:
        trigger.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await trigger

    assert exit_code == 0
    handler_idx = events.index("handler_cancelled")
    stop_idx = events.index("node_stop_async")
    assert handler_idx < stop_idx, (
        f"disconnect handler must be cancelled before node.stop_async; events={events}"
    )
