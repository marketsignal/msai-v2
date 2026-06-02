"""Unit tests for the reaper's US-2 halt-gated bounded auto-restart (PR 2 / T6).

When a trading subprocess crashes, the supervisor's reaper
(:meth:`FleetRouter._on_child_exit`) records the terminal row state and then
decides whether to auto-restart the dead node. The decision is made through
:meth:`FleetRouter._maybe_auto_restart`, which is the seam these tests exercise:

1. **Halt gate FIRST.** Before queuing any restart the gate checks
   ``fleet_halt_key()`` OR ``account_halt_key(account_id)``. ``/kill-all``
   sets ONLY the fleet key; ``/drain`` sets ONLY the account key — so an
   account-only gate would let an auto-restart fight a fleet emergency
   kill-all. Restart is suppressed if EITHER key is set (Codex iter-8).
2. **Bounded policy.** A non-suppressed crash goes through
   :class:`RestartPolicy`: ``RESTART`` → respawn now; ``BACKOFF`` → wait then
   respawn; ``PAUSED`` → never respawn (ceiling tripped).
3. **Cancellable backoff.** A fleet OR account halt arriving DURING the
   backoff abandons the restart (event-wait, not a bare ``asyncio.sleep`` —
   Claude iter-1 3-B).
4. **Reuse the spawn path.** A restart calls
   :meth:`FleetRouter.spawn_with_outcome` with a ``_RestartCarry``, which runs
   ``_phase_a_reserve_slot`` — the existing partial-unique-index serialisation
   point. Phase A resets the terminal deployment to ``starting`` and counts
   the attempt on the NEW per-spawn row (carried forward). The duplicate
   (re-scan racing PEL recovery) hits ``ALREADY_ACTIVE`` and does NOT
   double-count an attempt. ``_maybe_auto_restart`` reports a restart ONLY
   when ``spawn_with_outcome`` confirms a process genuinely started.

These tests stub the DB-loading seam (``_load_restart_context``), the halt
seam (``_halt_active``), the injected :class:`RestartPolicy`, and
:meth:`FleetRouter.spawn_with_outcome` — so they stay true unit tests with no
real Postgres / Redis. The REAL DB-query behaviour (carry-forward, ceiling
trip, terminal-deployment reset, stale-active re-scan) is pinned by the
integration test ``tests/integration/test_auto_restart_db.py``
(prior-review P2: the orchestration stubs cannot catch the cross-generation
counter read or the Phase-A NOT_STARTABLE interaction).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from msai.live_supervisor.fleet_router import (
    FleetRouter,
    _CachedNode,
    _RestartContext,
    _RestartOutcome,
)
from msai.live_supervisor.restart_policy import (
    RestartAction,
    RestartDecision,
    RestartPolicy,
)

_ACCOUNT_ID = "DUP733214"
_DEPLOYMENT_SLUG = "abcdef0123456789"


def _make_context(
    *,
    deployment_id: UUID | None = None,
    account_id: str | None = _ACCOUNT_ID,
    row_id: UUID | None = None,
) -> _RestartContext:
    """A restart context as ``_load_restart_context`` would return it."""
    proc = MagicMock()
    proc.id = row_id or uuid4()
    proc.auto_restart_paused = False
    proc.auto_restart_pause_reason = None
    proc.consecutive_respawn_failures = 0
    proc.last_restart_at = None
    return _RestartContext(
        deployment_id=deployment_id or uuid4(),
        deployment_slug=_DEPLOYMENT_SLUG,
        account_id=account_id,
        gateway_session_key="sess-1",
        process=proc,
    )


def _make_pm(
    *,
    decision: RestartDecision,
    halt_active: bool = False,
    context: _RestartContext | None = None,
    process_started: bool = True,
) -> FleetRouter:
    """FleetRouter with the auto-restart collaborators all stubbed.

    ``decision`` drives the injected policy's ``decide``; ``halt_active``
    drives the halt gate; ``context`` is what ``_load_restart_context``
    returns (``None`` means "row vanished").

    The respawn seam stubbed here is :meth:`FleetRouter.spawn_with_outcome`
    (prior-review P1: ``_maybe_auto_restart`` inspects the structured
    ``(ack, process_started)`` outcome so it only counts/reports a restart
    when a process GENUINELY started). ``process_started`` controls the
    second element of that tuple. The attempt is counted INSIDE Phase A of
    the real spawn (carried onto the new row), NOT by ``_maybe_auto_restart``
    — so these unit tests no longer assert ``record_failure`` was called from
    the orchestration layer; the real DB integration test
    (``test_auto_restart_db.py``) pins the counting + carry behaviour.
    """
    policy = MagicMock(spec=RestartPolicy)
    policy.decide = MagicMock(return_value=decision)
    policy.record_failure = MagicMock(return_value=None)
    policy.record_success = MagicMock(return_value=None)

    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )

    ctx = context if context is not None else _make_context()

    async def _load_ctx(_dep_id: UUID) -> _RestartContext | None:
        return ctx

    async def _halt(_account_id: str | None) -> bool:
        return halt_active

    # FINDING 1 (P1): ``_attempt_auto_restart`` re-reads the durable
    # operator-stop intent via the ``_operator_stop_requested`` DB seam (a
    # fresh ``FOR UPDATE`` read). These unit tests stub the DB-loading
    # collaborators (``_load_restart_context`` / ``_halt_active``); stub this
    # one too — default "no operator stop requested" — so it doesn't hit the
    # MagicMock ``db`` (the real read is pinned by the integration tests in
    # ``test_auto_restart_db.py``).
    async def _stop_requested(_dep_id: UUID) -> bool:
        return False

    pm._load_restart_context = _load_ctx  # type: ignore[assignment]
    pm._halt_active = _halt  # type: ignore[assignment]
    pm._operator_stop_requested = _stop_requested  # type: ignore[assignment]
    pm.spawn_with_outcome = AsyncMock(return_value=(True, process_started))  # type: ignore[method-assign]
    pm._test_ctx = ctx  # type: ignore[attr-defined]
    pm._test_policy = policy  # type: ignore[attr-defined]
    return pm


@pytest.mark.asyncio
async def test_non_halted_crashed_node_auto_restarts() -> None:
    """A non-halted crashed node whose policy says RESTART is respawned via
    ``spawn_with_outcome`` (which re-runs ``_phase_a_reserve_slot``) and the
    respawn carries a ``_RestartCarry`` so Phase A resets the terminal
    deployment + counts the attempt on the new row."""
    from msai.live_supervisor.fleet_router import _RestartCarry

    pm = _make_pm(decision=RestartDecision(action=RestartAction.RESTART), halt_active=False)

    restarted = await pm._maybe_auto_restart(pm._test_ctx.deployment_id)  # type: ignore[attr-defined]

    assert restarted is True
    pm.spawn_with_outcome.assert_awaited_once()  # type: ignore[attr-defined]
    call = pm.spawn_with_outcome.await_args  # type: ignore[attr-defined]
    assert call.kwargs["deployment_id"] == pm._test_ctx.deployment_id  # type: ignore[attr-defined]
    assert call.kwargs["deployment_slug"] == _DEPLOYMENT_SLUG
    # The respawn preserves the per-session serialization key.
    assert call.kwargs["gateway_session_key"] == "sess-1"
    # The respawn is a DELIBERATE auto-restart: it carries the prior row's
    # counter forward so Phase A can reset the terminal deployment + count
    # the attempt under the slot-reservation lock (prior-review P0/P1).
    assert isinstance(call.kwargs["restart_carry"], _RestartCarry)


@pytest.mark.asyncio
async def test_account_halted_node_not_restarted() -> None:
    """An account under its own halt/drain latch is NOT auto-restarted —
    the gate suppresses the restart before the policy is consulted."""
    pm = _make_pm(decision=RestartDecision(action=RestartAction.RESTART), halt_active=True)

    restarted = await pm._maybe_auto_restart(pm._test_ctx.deployment_id)  # type: ignore[attr-defined]

    assert restarted is False
    pm.spawn_with_outcome.assert_not_awaited()  # type: ignore[attr-defined]
    # Gate fires BEFORE the policy — no decision is taken under a halt.
    pm._test_policy.decide.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_fleet_halted_node_not_restarted_even_with_account_key_unset() -> None:
    """FLEET-halted (``/kill-all``) crashed node → NOT restarted even though
    its ``account_halt_key`` is unset.

    ``/kill-all`` sets ONLY ``fleet_halt_key``. The gate must be
    fleet-OR-account (Codex iter-8): an account-only gate would let
    auto-restart fight a fleet emergency kill-all. We verify the gate is
    consulted with the deployment's account_id and that a fleet-set latch
    (modeled here by the gate returning True) suppresses the restart.
    """
    from msai.core.halt_keys import account_halt_key, fleet_halt_key

    # Real Redis stub: ONLY the fleet key is set; the account key is NOT.
    redis = MagicMock()
    fleet_key = fleet_halt_key()
    account_key = account_halt_key(_ACCOUNT_ID)
    seen: list[str] = []

    async def _exists(key: str) -> int:
        seen.append(key)
        return 1 if key == fleet_key else 0

    redis.exists = AsyncMock(side_effect=_exists)

    policy = MagicMock(spec=RestartPolicy)
    policy.decide = MagicMock(return_value=RestartDecision(action=RestartAction.RESTART))
    pm = FleetRouter(
        db=MagicMock(),
        redis=redis,
        spawn_target=lambda: None,
        restart_policy=policy,
    )
    ctx = _make_context()

    async def _load_ctx(_dep_id: UUID) -> _RestartContext | None:
        return ctx

    pm._load_restart_context = _load_ctx  # type: ignore[assignment]
    pm.spawn_with_outcome = AsyncMock(return_value=(True, True))  # type: ignore[method-assign]

    restarted = await pm._maybe_auto_restart(ctx.deployment_id)

    assert restarted is False, "fleet kill-all must suppress auto-restart"
    pm.spawn_with_outcome.assert_not_awaited()  # type: ignore[attr-defined]
    policy.decide.assert_not_called()
    # The gate consulted the FLEET key (the one /kill-all sets). It
    # short-circuits there — proving the gate is NOT account-only (an
    # account-only gate would have ignored the fleet latch and restarted).
    # The companion test ``test_account_key_only_set_suppresses_restart``
    # proves the OTHER half of the fleet-OR-account semantics.
    assert fleet_key in seen
    assert account_key not in seen, "fleet-set short-circuits before the account check"


@pytest.mark.asyncio
async def test_account_key_only_set_suppresses_restart() -> None:
    """Symmetric to the fleet case: ``/drain`` sets ONLY the account key,
    and the fleet-OR-account gate must suppress the restart on it too."""
    from msai.core.halt_keys import account_halt_key, fleet_halt_key

    redis = MagicMock()
    account_key = account_halt_key(_ACCOUNT_ID)

    async def _exists(key: str) -> int:
        return 1 if key == account_key else 0

    redis.exists = AsyncMock(side_effect=_exists)
    policy = MagicMock(spec=RestartPolicy)
    policy.decide = MagicMock(return_value=RestartDecision(action=RestartAction.RESTART))
    pm = FleetRouter(
        db=MagicMock(),
        redis=redis,
        spawn_target=lambda: None,
        restart_policy=policy,
    )
    ctx = _make_context()

    async def _load_ctx(_dep_id: UUID) -> _RestartContext | None:
        return ctx

    pm._load_restart_context = _load_ctx  # type: ignore[assignment]
    pm.spawn_with_outcome = AsyncMock(return_value=(True, True))  # type: ignore[method-assign]

    assert await pm._maybe_auto_restart(ctx.deployment_id) is False
    pm.spawn_with_outcome.assert_not_awaited()  # type: ignore[attr-defined]
    _ = fleet_halt_key()  # documents the unset fleet key for the reader


@pytest.mark.asyncio
async def test_paused_node_not_restarted() -> None:
    """When the policy is PAUSED (crash-loop ceiling tripped) the reaper
    must NOT respawn — an operator has to intervene."""
    pm = _make_pm(
        decision=RestartDecision(
            action=RestartAction.PAUSED, pause_reason="max respawn ceiling reached"
        ),
        halt_active=False,
    )

    restarted = await pm._maybe_auto_restart(pm._test_ctx.deployment_id)  # type: ignore[attr-defined]

    assert restarted is False
    pm.spawn_with_outcome.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_backoff_then_restart_completes_when_no_halt() -> None:
    """A BACKOFF decision waits the (tiny, test-sized) delay and then
    respawns, because no halt arrives during the wait."""
    pm = _make_pm(
        decision=RestartDecision(action=RestartAction.BACKOFF, delay_s=0.05),
        halt_active=False,
    )

    restarted = await pm._maybe_auto_restart(pm._test_ctx.deployment_id)  # type: ignore[attr-defined]

    assert restarted is True
    pm.spawn_with_outcome.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_halt_arriving_during_backoff_abandons_restart() -> None:
    """A fleet OR account halt arriving DURING the backoff abandons the
    restart (cancellable backoff — Claude iter-1 3-B).

    The halt seam returns False initially (so the policy chose BACKOFF) then
    flips to True mid-wait. The backoff must observe the flip and abandon
    without calling ``spawn``.
    """
    policy = MagicMock(spec=RestartPolicy)
    policy.decide = MagicMock(
        return_value=RestartDecision(action=RestartAction.BACKOFF, delay_s=5.0)
    )
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )
    ctx = _make_context()

    async def _load_ctx(_dep_id: UUID) -> _RestartContext | None:
        return ctx

    calls = {"n": 0}

    async def _halt(_account_id: str | None) -> bool:
        # Not halted at the gate (call 0); halted partway through backoff.
        calls["n"] += 1
        return calls["n"] >= 2

    pm._load_restart_context = _load_ctx  # type: ignore[assignment]
    pm._halt_active = _halt  # type: ignore[assignment]

    # FINDING 1 (P1): stub the operator-stop DB seam (default: no
    # stop requested) so _attempt_auto_restart doesn't hit the MagicMock db.
    async def _no_stop(_dep_id: object) -> bool:
        return False

    pm._operator_stop_requested = _no_stop  # type: ignore[assignment]
    pm.spawn_with_outcome = AsyncMock(return_value=(True, True))  # type: ignore[method-assign]

    restarted = await asyncio.wait_for(pm._maybe_auto_restart(ctx.deployment_id), timeout=3.0)

    assert restarted is False, "halt during backoff must abandon the restart"
    pm.spawn_with_outcome.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_backoff_abandoned_on_supervisor_shutdown() -> None:
    """A backoff in flight when the supervisor is shutting down
    (``stop_event`` set) abandons the restart — no respawn during drain."""
    policy = MagicMock(spec=RestartPolicy)
    policy.decide = MagicMock(
        return_value=RestartDecision(action=RestartAction.BACKOFF, delay_s=30.0)
    )
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )
    ctx = _make_context()

    async def _load_ctx(_dep_id: UUID) -> _RestartContext | None:
        return ctx

    async def _halt(_account_id: str | None) -> bool:
        return False

    pm._load_restart_context = _load_ctx  # type: ignore[assignment]
    pm._halt_active = _halt  # type: ignore[assignment]

    # FINDING 1 (P1): stub the operator-stop DB seam (default: no
    # stop requested) so _attempt_auto_restart doesn't hit the MagicMock db.
    async def _no_stop(_dep_id: object) -> bool:
        return False

    pm._operator_stop_requested = _no_stop  # type: ignore[assignment]
    pm.spawn_with_outcome = AsyncMock(return_value=(True, True))  # type: ignore[method-assign]

    stop_event = asyncio.Event()
    stop_event.set()  # already shutting down
    pm._reap_stop_event = stop_event  # type: ignore[attr-defined]

    restarted = await asyncio.wait_for(pm._maybe_auto_restart(ctx.deployment_id), timeout=3.0)

    assert restarted is False
    pm.spawn_with_outcome.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_restart_carries_counter_forward_to_phase_a() -> None:
    """The attempt is counted INSIDE Phase A (carried onto the new row),
    not by ``_maybe_auto_restart``: the orchestration no longer pre-counts
    via ``record_failure`` (prior-review P1/P2 — the counter must live on the
    NEW per-spawn row under the slot-reservation lock). Here we pin that the
    orchestration passes the prior counter state as a ``_RestartCarry`` and
    does NOT call ``record_failure`` itself.
    """
    from msai.live_supervisor.fleet_router import _RestartCarry

    ctx = _make_context()
    ctx.process.consecutive_respawn_failures = 3
    ctx.process.last_restart_at = datetime.now(UTC)
    pm = _make_pm(
        decision=RestartDecision(action=RestartAction.RESTART),
        halt_active=False,
        context=ctx,
    )

    await pm._maybe_auto_restart(ctx.deployment_id)

    # The orchestration layer does NOT count — Phase A does (under the lock).
    pm._test_policy.record_failure.assert_not_called()  # type: ignore[attr-defined]
    call = pm.spawn_with_outcome.await_args  # type: ignore[attr-defined]
    carry = call.kwargs["restart_carry"]
    assert isinstance(carry, _RestartCarry)
    # The prior terminal-row state is carried forward so Phase A's
    # ``record_failure`` increments from the right base (the crash-loop
    # ceiling survives the per-spawn row recreate).
    assert carry.prior_consecutive_respawn_failures == 3


@pytest.mark.asyncio
async def test_restart_not_reported_when_no_process_started() -> None:
    """``_maybe_auto_restart`` returns ``False`` (and does NOT report a
    restart) when ``spawn_with_outcome`` ACK-dropped / transient-dropped
    without starting a process — e.g. the idempotent loser of a re-scan/PEL
    race, a halt that raced in, or a payload error (prior-review P1: the
    spawn bool was previously discarded and every outcome counted as a
    restart, corrupting the crash-loop accounting + the /live/status tally).
    """
    pm = _make_pm(
        decision=RestartDecision(action=RestartAction.RESTART),
        halt_active=False,
        process_started=False,  # spawn ACKed but started no process
    )

    restarted = await pm._maybe_auto_restart(pm._test_ctx.deployment_id)  # type: ignore[attr-defined]

    # spawn WAS attempted (we reached the respawn step) ...
    pm.spawn_with_outcome.assert_awaited_once()  # type: ignore[attr-defined]
    # ... but no process started, so no restart is reported.
    assert restarted is False


@pytest.mark.asyncio
async def test_no_restart_when_context_missing() -> None:
    """If the failed row vanished (operator deleted the deployment) the
    reaper must not crash — no context → no restart."""
    pm = _make_pm(decision=RestartDecision(action=RestartAction.RESTART), halt_active=False)

    async def _load_none(_dep_id: UUID) -> _RestartContext | None:
        return None

    pm._load_restart_context = _load_none  # type: ignore[assignment]

    restarted = await pm._maybe_auto_restart(uuid4())

    assert restarted is False
    pm.spawn_with_outcome.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_auto_restart_off_when_no_policy_injected() -> None:
    """A FleetRouter built without a ``restart_policy`` (the legacy / test
    path) never auto-restarts — ``_on_child_exit`` only records terminal
    state, preserving backward compatibility."""
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
    )
    assert pm._restart_policy is None

    # _maybe_auto_restart is a no-op (returns False) with no policy.
    restarted = await pm._maybe_auto_restart(uuid4())
    assert restarted is False


@pytest.mark.asyncio
async def test_reconciliation_timeout_exit_feeds_policy_as_failure() -> None:
    """A reconciliation timeout (subprocess exit code 2 →
    ``RECONCILIATION_FAILED``) is a failed terminal state that feeds the
    restart policy like any other crash — the restarted node never accepted
    orders (that guarantee is the subprocess's), and the supervisor counts
    the attempt. Here the policy PAUSES (ceiling already reached), so no
    respawn — proving a reconciliation-timeout is treated as a failure, not
    a clean stop.
    """
    proc = MagicMock()
    proc.id = uuid4()
    proc.auto_restart_paused = True
    proc.auto_restart_pause_reason = "max respawn ceiling reached"
    proc.consecutive_respawn_failures = 5
    proc.last_restart_at = datetime.now(UTC)
    ctx = _RestartContext(
        deployment_id=uuid4(),
        deployment_slug=_DEPLOYMENT_SLUG,
        account_id=_ACCOUNT_ID,
        gateway_session_key="sess-1",
        process=proc,
    )
    pm = _make_pm(
        decision=RestartDecision(
            action=RestartAction.PAUSED, pause_reason="max respawn ceiling reached"
        ),
        halt_active=False,
        context=ctx,
    )

    restarted = await pm._maybe_auto_restart(ctx.deployment_id)

    assert restarted is False
    pm.spawn_with_outcome.assert_not_awaited()  # type: ignore[attr-defined]


# --- _on_child_exit → _maybe_auto_restart dispatch wiring --------------------


class _FakeRow:
    """Minimal stand-in for the ``live_node_processes`` row ``_on_child_exit``
    mutates, so the dispatch can be tested without a real session.

    Council #3 (PR 2 T6) added two reaper-authority columns: ``stop_requested_at``
    (durable operator-stop intent, suppresses auto-restart even on a non-zero
    exit) and ``restart_dispatched_at`` (idempotency sentinel)."""

    def __init__(
        self,
        status: str = "running",
        *,
        stop_requested_at: datetime | None = None,
        restart_dispatched_at: datetime | None = None,
    ) -> None:
        # FIX 2 (P2): the reaper captures ``row.id`` as the OWNED row so the
        # give-up cleanup targets it by id (not "the latest row"). The real
        # ``LiveNodeProcess`` always has an ``id``; the fake mirrors that.
        self.id: UUID = uuid4()
        self.status = status
        self.failure_kind: str | None = None
        self.error_message: str | None = None
        self.exit_code: int | None = None
        self.stop_requested_at = stop_requested_at
        self.restart_dispatched_at = restart_dispatched_at
        # PR 2 T10 account-scoped logging: the reaper reads these off the row to
        # enrich its restart-decision logs (pid + heartbeat-age in scope).
        self.pid: int | None = 4242
        self.last_heartbeat_at: datetime | None = datetime.now(UTC)


class _FakeResult:
    def __init__(self, row: object) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object:
        return self._row


class _FakeSession:
    """Async-context session that returns ``row`` for the terminal-state
    SELECT in ``_on_child_exit`` and is a no-op transaction otherwise.

    The reaper's classify read chains ``.with_for_update()`` on the SELECT
    statement; this fake ignores the statement entirely so the chained builder
    call is transparent.

    PR 2 T10 (account-scoped logging): ``_on_child_exit`` now issues a SECOND
    ``execute`` after the row SELECT — a ``LiveDeployment.account_id`` scalar
    read so the restart-decision logs carry account context. This fake returns
    ``row`` for the FIRST execute and ``account_id`` (a scalar string, as the
    real column is) for every subsequent execute, matching the real query order.
    Existing tests that don't assert on account_id are unaffected."""

    def __init__(self, row: object, *, account_id: str | None = _ACCOUNT_ID) -> None:
        self._row = row
        self._account_id = account_id
        self._calls = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def begin(self) -> _FakeSession:
        return self

    async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
        self._calls += 1
        # 1st execute → the node-process row; 2nd+ → the account_id scalar.
        if self._calls == 1:
            return _FakeResult(self._row)
        return _FakeResult(self._account_id)

    async def get(self, *_args: object, **_kwargs: object) -> object:
        # PR 2 F2 — the give-up cleanup (``_clear_sentinel_and_refail_after_
        # giveup``) does ``session.get(LiveDeployment, ...)``. Council 2026-06-01
        # (Finding 2): ``_on_child_exit`` now ALSO locks the parent deployment via
        # ``session.get(LiveDeployment, ..., with_for_update=True)`` FIRST. Return
        # None so both the account-scoping read and the deployment-sync branch are
        # skipped here — these unit tests exercise ONLY the dispatch wiring + the
        # node-process row write (the real deployment-sync is covered by the
        # integration tests in test_pr2_lock_order_concurrency.py).
        return None


def _pm_with_fake_db(row: object, *, with_policy: bool) -> FleetRouter:
    policy = MagicMock(spec=RestartPolicy) if with_policy else None

    def _db() -> _FakeSession:
        return _FakeSession(row)

    pm = FleetRouter(
        db=_db,  # type: ignore[arg-type]
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )
    # The detached restart task calls ``_attempt_auto_restart`` (the structured
    # seam — council #4 OPT C Part 2). Stub it to a genuine restart so the
    # reaper-dispatch wiring is observable without a real spawn.
    pm._attempt_auto_restart = AsyncMock(return_value=_RestartOutcome.RESTARTED)  # type: ignore[method-assign]
    return pm


@pytest.mark.asyncio
async def test_on_child_exit_dispatches_auto_restart_on_failure() -> None:
    """A non-zero exit (failure) on a STALE-ACTIVE row (SIGKILL/OOM — finally
    never ran, row still ``running``) routes the dead deployment to
    ``_maybe_auto_restart`` after the terminal-state write commits."""
    row = _FakeRow()
    pm = _pm_with_fake_db(row, with_policy=True)
    dep_id = uuid4()

    await pm._on_child_exit(dep_id, exit_code=1)
    # F1: the dispatch is now a detached per-account task — drain it so the
    # awaited-assertion is deterministic (the reaper itself does NOT block).
    await pm._await_restart_tasks_for_test()  # type: ignore[attr-defined]

    assert row.status == "failed"
    pm._attempt_auto_restart.assert_awaited_once_with(dep_id)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_on_child_exit_dispatches_auto_restart_on_already_failed_row() -> None:
    """Council #3 P1 — the COMMON crash. The subprocess writes its OWN terminal
    ``failed`` row LAST in its ``finally`` ("Terminal write LAST") before exiting,
    so by the time the reaper sees the exit the row is ALREADY ``failed``. The
    reaper MUST still classify it as a non-zero-exit failure and dispatch
    ``_maybe_auto_restart`` — the old active-set-only SELECT early-returned here
    so auto-restart NEVER fired at runtime (only SIGKILL/OOM reached it).
    """
    row = _FakeRow(status="failed")  # subprocess already wrote terminal state
    pm = _pm_with_fake_db(row, with_policy=True)
    dep_id = uuid4()

    await pm._on_child_exit(dep_id, exit_code=1)
    await pm._await_restart_tasks_for_test()  # type: ignore[attr-defined]

    # The reaper does NOT overwrite the subprocess's richer terminal state ...
    assert row.status == "failed"
    # ... but it DOES classify + dispatch the restart (the headline fix).
    pm._attempt_auto_restart.assert_awaited_once_with(dep_id)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_on_child_exit_does_not_restart_already_stopped_clean_row() -> None:
    """A graceful ``/stop`` whose subprocess wrote ``stopped`` (exit 0) LAST is
    NOT auto-restarted: the reaper classifies the already-terminal ``stopped``
    row by exit code and never dispatches."""
    row = _FakeRow(status="stopped")
    pm = _pm_with_fake_db(row, with_policy=True)

    await pm._on_child_exit(uuid4(), exit_code=0)

    assert row.status == "stopped"
    pm._attempt_auto_restart.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_on_child_exit_does_not_auto_restart_on_clean_stop() -> None:
    """A clean exit (code 0 — operator stop) is terminal ``stopped`` and is
    NEVER auto-restarted."""
    row = _FakeRow()
    pm = _pm_with_fake_db(row, with_policy=True)

    await pm._on_child_exit(uuid4(), exit_code=0)

    assert row.status == "stopped"
    pm._attempt_auto_restart.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_on_child_exit_no_dispatch_without_policy() -> None:
    """Without a ``restart_policy`` a failure records terminal state but does
    NOT dispatch auto-restart (legacy/test wiring)."""
    row = _FakeRow()
    pm = _pm_with_fake_db(row, with_policy=False)

    await pm._on_child_exit(uuid4(), exit_code=1)

    assert row.status == "failed"
    pm._attempt_auto_restart.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_on_child_exit_does_not_auto_restart_operator_stop_that_crashed() -> None:
    """Council #3 F5 / prior-review P1: auto-restart must NOT fight an operator
    ``/stop`` — even when the subprocess's terminal write clobbered ``status``.

    A plain ``/stop`` sets ``stop_requested_at`` under a row lock and SIGTERMs the
    node. It sets NO halt latch (only ``/drain`` sets the account latch,
    ``/kill-all`` the fleet latch), so the halt gate would NOT suppress a restart.
    Critically, the subprocess's "Terminal write LAST" overwrites the row to
    ``failed``/``stopped`` BEFORE the reaper sees the exit — so the old
    ``status == 'stopping'`` signal is GONE by reap time once we start classifying
    terminal rows. The durable ``stop_requested_at`` column is the only signal
    that survives: on a non-zero exit (graceful shutdown then crashed) the reaper
    sees ``stop_requested_at`` set and MUST NOT dispatch. Otherwise auto-restart
    would resurrect a node the operator deliberately stopped (real-money: a
    position left being traded after /stop).
    """
    row = _FakeRow(status="failed", stop_requested_at=datetime.now(UTC))
    pm = _pm_with_fake_db(row, with_policy=True)

    await pm._on_child_exit(uuid4(), exit_code=1)

    # The terminal state stays failed (the shutdown crashed) ...
    assert row.status == "failed"
    # ... but no auto-restart: the operator asked for this node to stop, and
    # the intent is durable through the terminal write (stop_requested_at).
    pm._attempt_auto_restart.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_on_child_exit_idempotency_sentinel_blocks_second_dispatch() -> None:
    """Council #3 case 6: a duplicate reaper invocation on the same ``failed``
    terminal row must NOT dispatch a second restart — ``restart_dispatched_at``
    is the idempotency sentinel. The reaper sets it under its ``FOR UPDATE`` lock
    BEFORE the first dispatch and skips dispatch when it is already set, so the
    reaper never re-dispatches against a still-``failed`` terminal row.
    """
    # First pass: pristine terminal row → sentinel unset → dispatches + stamps it.
    row = _FakeRow(status="failed")
    pm = _pm_with_fake_db(row, with_policy=True)
    dep_id = uuid4()

    await pm._on_child_exit(dep_id, exit_code=1)
    await pm._await_restart_tasks_for_test()  # type: ignore[attr-defined]
    pm._attempt_auto_restart.assert_awaited_once_with(dep_id)  # type: ignore[attr-defined]
    assert row.restart_dispatched_at is not None, (
        "the reaper must stamp the idempotency sentinel before dispatching"
    )

    # Second pass on the SAME row (sentinel now set) → no second dispatch.
    pm._attempt_auto_restart.reset_mock()  # type: ignore[attr-defined]
    await pm._on_child_exit(dep_id, exit_code=1)
    await pm._await_restart_tasks_for_test()  # type: ignore[attr-defined]
    pm._attempt_auto_restart.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_on_child_exit_skipped_no_row_emits_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Council #3 case 8: when the reaper sees a dead node but there is NO row to
    classify (operator deleted the deployment between crash and reap), it emits
    the NEW ``auto_restart_skipped_no_row`` structured log so an operator can
    alert on a silent no-op — and does not crash or dispatch."""
    import logging

    def _db() -> _FakeSession:
        return _FakeSession(None)  # SELECT returns nothing

    pm = FleetRouter(
        db=_db,  # type: ignore[arg-type]
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=MagicMock(spec=RestartPolicy),
    )
    pm._attempt_auto_restart = AsyncMock(return_value=_RestartOutcome.RESTARTED)  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger="msai.live_supervisor.fleet_router"):
        await pm._on_child_exit(uuid4(), exit_code=1)

    pm._attempt_auto_restart.assert_not_awaited()  # type: ignore[attr-defined]
    assert any(r.message == "auto_restart_skipped_no_row" for r in caplog.records), (
        "the reaper must emit auto_restart_skipped_no_row when it sees a dead node "
        "but no row to act on"
    )


@pytest.mark.asyncio
async def test_on_child_exit_emits_dispatched_log_on_restart(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Council #3 case 8 (positive): a reaper decision that dispatches a restart
    emits the ``auto_restart_dispatched`` structured log with deployment context."""
    import logging

    row = _FakeRow(status="failed")
    pm = _pm_with_fake_db(row, with_policy=True)
    dep_id = uuid4()

    with caplog.at_level(logging.INFO, logger="msai.live_supervisor.fleet_router"):
        await pm._on_child_exit(dep_id, exit_code=1)
        await pm._await_restart_tasks_for_test()  # type: ignore[attr-defined]

    pm._attempt_auto_restart.assert_awaited_once_with(dep_id)  # type: ignore[attr-defined]
    dispatched = [r for r in caplog.records if r.message == "auto_restart_dispatched"]
    assert dispatched, "the reaper must emit auto_restart_dispatched on a dispatch"
    assert str(dep_id) in str(getattr(dispatched[0], "deployment_id", "")), (
        "the dispatch log must carry deployment_id context"
    )


# --- reap_once must not delete a same-pass respawn's fresh handle ------------


class _FakeDeadProc:
    """A picklable-free stand-in for an exited ``mp.Process`` handle: already
    dead, joins instantly, surfaces an exit code + a pid."""

    def __init__(self, exitcode: int, pid: int = 4242) -> None:
        self.exitcode = exitcode
        self.pid = pid

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:  # noqa: ARG002
        return None


@pytest.mark.asyncio
async def test_reap_once_preserves_handle_replaced_by_same_pass_auto_restart() -> None:
    """Prior-review P1: ``reap_once`` must NOT delete a handle that a same-pass
    auto-restart already replaced with a fresh live process.

    In production the reaper drives ``reap_once`` → ``_on_child_exit`` →
    ``_maybe_auto_restart`` → ``spawn_with_outcome``, and the respawn installs
    the NEW live process (a fresh ``_CachedNode``) under the SAME
    ``deployment_id`` key in ``node_handle_cache``. If ``reap_once`` then
    unconditionally ``del``-eted that key it would drop the fresh handle, so the
    reaper could no longer observe the restarted node's NEXT crash via the
    instant fast-path cache (US-2 capability degrades to the slow heartbeat
    sweep).

    Fix: only delete the dead cache entry if it is STILL the one in the cache.
    """
    dep_id = uuid4()
    owned = uuid4()
    dead = _CachedNode(proc=_FakeDeadProc(exitcode=1), owned_row_id=owned)
    new_cached = _CachedNode(proc=MagicMock(name="respawned-handle"), owned_row_id=uuid4())

    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
    )
    pm.node_handle_cache[dep_id] = dead

    seen: dict[str, object] = {}

    async def _on_exit(
        _dep: UUID,
        _code: int | None,
        *,
        owned_row_id: UUID | None = None,
        proc_pid: int | None = None,
    ) -> None:
        # Capture what reap_once threaded so we can assert the cache identity
        # made it through.
        seen["owned_row_id"] = owned_row_id
        seen["proc_pid"] = proc_pid
        # Simulate the same-pass auto-restart: the respawn's
        # ``spawn_with_outcome`` installs the NEW _CachedNode under the SAME key.
        pm.node_handle_cache[dep_id] = new_cached

    pm._on_child_exit = _on_exit  # type: ignore[assignment]

    await pm.reap_once()

    assert pm.node_handle_cache.get(dep_id) is new_cached, (
        "reap_once dropped the fresh _CachedNode a same-pass auto-restart installed"
    )
    # reap_once must thread the cached node's owned_row_id + the dead pid.
    assert seen["owned_row_id"] == owned
    assert seen["proc_pid"] == 4242


@pytest.mark.asyncio
async def test_reap_once_deletes_handle_when_no_respawn_replaced_it() -> None:
    """The common path is unchanged: when no respawn replaced the dead handle,
    ``reap_once`` still removes the exited child from the cache (no leak)."""
    dep_id = uuid4()
    dead = _CachedNode(proc=_FakeDeadProc(exitcode=0), owned_row_id=uuid4())

    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
    )
    pm.node_handle_cache[dep_id] = dead

    async def _on_exit(
        _dep: UUID,
        _code: int | None,
        *,
        owned_row_id: UUID | None = None,  # noqa: ARG001
        proc_pid: int | None = None,  # noqa: ARG001
    ) -> None:
        return None  # no respawn; handle stays the dead one

    pm._on_child_exit = _on_exit  # type: ignore[assignment]

    await pm.reap_once()

    assert dep_id not in pm.node_handle_cache, "the dead handle must be reaped"


# --- reap_loop must be exception-guarded (prior-review P1) --------------------


@pytest.mark.asyncio
async def test_reap_loop_survives_reap_once_raising() -> None:
    """Prior-review P1: ``reap_loop`` MUST NOT die when ``reap_once`` raises.

    T6 newly routes un-guarded DB work through the reaper:
    ``reap_once`` → ``_on_child_exit`` → ``_maybe_auto_restart`` →
    ``_load_restart_context`` (DB read) / ``spawn_with_outcome`` →
    ``_phase_a_reserve_slot`` (SELECT-FOR-UPDATE / flush / commit). A transient
    Postgres error in any of those raises up through ``reap_once``. Unlike its
    guarded sibling loops (``watchdog_loop`` / ``HeartbeatMonitor.run_forever``,
    which wrap their body so errors never crash the supervisor), ``reap_loop``
    used to call ``reap_once()`` bare. The reap task is a bare
    ``asyncio.create_task`` with no supervision, so an unhandled exception
    permanently killed the fast-path crash reaper AND silently disabled US-2
    auto-restart fleet-wide — while ``router_heartbeat`` kept reporting the
    supervisor healthy. Fix: wrap ``reap_once()`` in try/except-log-continue.
    """
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
    )

    stop_event = asyncio.Event()
    calls = 0

    async def _boom() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            # First pass: a transient DB error bubbles up from the auto-restart
            # decision. The loop MUST swallow it and keep going.
            raise RuntimeError("transient Postgres blip during auto-restart decision")
        # Second pass reached → the loop survived the first crash. Now stop.
        stop_event.set()

    pm.reap_once = _boom  # type: ignore[method-assign]

    # If reap_loop re-raises the RuntimeError this await fails the test; the
    # timeout guards against a hang if the loop somehow never advances.
    await asyncio.wait_for(pm.reap_loop(stop_event), timeout=3.0)

    assert calls >= 2, "reap_loop did not survive a raising reap_once pass"


# --- transient auto-restart respawn must not strand at 'starting' (P2) -------


@pytest.mark.asyncio
async def test_transient_respawn_refails_deployment_not_left_starting() -> None:
    """Prior-review P2: a TRANSIENT auto-restart respawn must re-fail the
    deployment, never strand it at ``starting`` with no live node.

    The reaper-driven respawn calls ``spawn_with_outcome`` directly — there is
    NO command-consumer / PEL entry behind it. Phase A resets the terminal
    deployment row to ``starting`` once it commits to reserving a slot. If
    Phase B then hits a TRANSIENT payload-factory failure it returns
    ``(False, False)`` (no ACK) WITHOUT a terminal ``_mark_failed`` — correct
    for the PEL-retry path, but on the auto-restart path there is no caller to
    re-drive the ``False``. The deployment would linger at ``starting`` (shown
    as "coming up" in /live/status) while the account is actually flat and
    unmonitored, self-healing only via the 30-min startup watchdog.

    Fix: ``_maybe_auto_restart`` treats a transient ``(ack=False,
    process_started=False)`` outcome as terminal — it re-fails the deployment
    (idempotent) so the next rescan / heartbeat path can retry — and returns
    ``False`` (no restart happened).
    """
    pm = _make_pm(
        decision=RestartDecision(action=RestartAction.RESTART),
        process_started=False,
    )
    # Transient no-ACK outcome (the PEL-retry shape) on the auto-restart path.
    pm.spawn_with_outcome = AsyncMock(return_value=(False, False))  # type: ignore[method-assign]
    refail = AsyncMock(return_value=None)
    pm._refail_stranded_restart = refail  # type: ignore[attr-defined]

    restarted = await pm._maybe_auto_restart(pm._test_ctx.deployment_id)  # type: ignore[attr-defined]

    assert restarted is False
    # The deployment must be re-failed so it's rescannable, not left at 'starting'.
    # Council 2026-05-31 (item 3): the call now threads the owned row id; the
    # mocked ``spawn_with_outcome`` reserves no row, so owned_row_id is None (the
    # legacy latest-row fallback — no concurrent-restart row to protect here).
    refail.assert_awaited_once_with(  # type: ignore[attr-defined]
        pm._test_ctx.deployment_id, owned_row_id=None
    )


@pytest.mark.asyncio
async def test_ack_drop_respawn_does_not_refail_deployment() -> None:
    """An ACK-without-spawn outcome (``ack=True, process_started=False`` —
    halt raced / paused / NOT_STARTABLE) must NOT trigger the re-fail path.

    Those ACK-drops happen BEFORE Phase A resets the deployment to ``starting``
    (the reset only fires once a real slot is reserved), so the deployment is
    already ``failed`` / terminal and re-failing it would be a redundant write
    against a possibly-concurrent operator action. Only the transient no-ACK
    shape (which CAN strand at ``starting``) is re-failed.
    """
    pm = _make_pm(
        decision=RestartDecision(action=RestartAction.RESTART),
        process_started=False,
    )
    # ACK-drop shape: ack=True, no process started.
    pm.spawn_with_outcome = AsyncMock(return_value=(True, False))  # type: ignore[method-assign]
    refail = AsyncMock(return_value=None)
    pm._refail_stranded_restart = refail  # type: ignore[attr-defined]

    restarted = await pm._maybe_auto_restart(pm._test_ctx.deployment_id)  # type: ignore[attr-defined]

    assert restarted is False
    refail.assert_not_awaited()


# --- F1: the reaper must NOT block on the restart backoff --------------------
#
# The single ``reap_once`` loop calls ``_on_child_exit`` SYNCHRONOUSLY for each
# dead child. Before F1, ``_on_child_exit`` awaited ``_maybe_auto_restart``,
# which awaited the cancellable backoff (up to ``BACKOFF_CAP_S`` = 300s) — so a
# BACKOFF restart for account A stalled classification of account B's exit. F1
# relocates the backoff + spawn into a SEPARATE per-account asyncio task so
# ``_on_child_exit`` returns to the reaper immediately.


@pytest.mark.asyncio
async def test_on_child_exit_does_not_block_reaper_on_backoff() -> None:
    """FALSIFICATION (F1): a BACKOFF restart for one deployment must NOT block
    ``_on_child_exit`` — the reaper returns promptly and can classify a second
    account's exit while the first is still backing off.

    The first deployment's ``_maybe_auto_restart`` is stubbed to a long sleep
    (modeling the in-task backoff). If ``_on_child_exit`` awaited it inline (the
    pre-F1 behaviour), the call below would not return within the timeout. F1
    schedules the restart as a detached task, so the await returns at once.
    """
    from datetime import UTC, datetime

    dep_a = uuid4()

    class _StallRow:
        def __init__(self) -> None:
            # FIX 2 (P2): the reaper captures ``row.id`` as the owned row.
            self.id: UUID = uuid4()
            self.status = "failed"
            self.failure_kind: str | None = None
            self.error_message: str | None = None
            self.exit_code: int | None = None
            self.stop_requested_at: datetime | None = None
            self.restart_dispatched_at: datetime | None = None
            self.pid: int | None = 4242
            self.last_heartbeat_at: datetime | None = datetime.now(UTC)

    row = _StallRow()
    pm = _pm_with_fake_db(row, with_policy=True)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_restart(_dep: UUID) -> _RestartOutcome:
        started.set()
        await release.wait()  # models a long in-task backoff
        return _RestartOutcome.RESTARTED

    pm._attempt_auto_restart = _slow_restart  # type: ignore[assignment]

    # _on_child_exit must return PROMPTLY even though the restart will block.
    await asyncio.wait_for(pm._on_child_exit(dep_a, exit_code=1), timeout=1.0)

    # The restart task is in flight (started) but _on_child_exit already returned.
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert dep_a in pm._restart_tasks  # type: ignore[attr-defined]
    assert not pm._restart_tasks[dep_a].done()  # type: ignore[attr-defined]

    # Cleanup: release + drain so the task doesn't leak into other tests.
    release.set()
    await asyncio.wait_for(pm._restart_tasks[dep_a], timeout=1.0)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_second_account_classified_while_first_backs_off() -> None:
    """FALSIFICATION (F1, fleet isolation): while account A's restart is backing
    off (detached task), the reaper processes account B's exit and dispatches
    B's restart WITHOUT waiting for A's backoff to finish — the core PR-2
    per-account failure-isolation property."""

    # Each ``_on_child_exit`` opens its own session + classifies ITS OWN
    # deployment's latest row — so a fresh row per ``_db()`` call (not one
    # shared object whose ``restart_dispatched_at`` sentinel would bleed across
    # the two deployments).
    def _db() -> _FakeSession:
        return _FakeSession(_FakeRow(status="failed"))

    policy = MagicMock(spec=RestartPolicy)
    pm = FleetRouter(
        db=_db,  # type: ignore[arg-type]
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )

    dep_a, dep_b = uuid4(), uuid4()
    a_release = asyncio.Event()
    b_done = asyncio.Event()
    classified: list[UUID] = []

    async def _restart(dep: UUID) -> _RestartOutcome:
        classified.append(dep)
        if dep == dep_a:
            await a_release.wait()  # A backs off indefinitely
        else:
            b_done.set()  # B proceeds immediately
        return _RestartOutcome.RESTARTED

    pm._attempt_auto_restart = _restart  # type: ignore[assignment]

    # A crashes → its restart task is scheduled and starts backing off.
    await asyncio.wait_for(pm._on_child_exit(dep_a, exit_code=1), timeout=1.0)
    # B crashes next → reaper is NOT stalled by A's backoff.
    await asyncio.wait_for(pm._on_child_exit(dep_b, exit_code=1), timeout=1.0)

    # B's restart ran to completion even though A is still backing off.
    await asyncio.wait_for(b_done.wait(), timeout=1.0)
    assert dep_b in classified
    assert not pm._restart_tasks[dep_a].done()  # type: ignore[attr-defined]

    a_release.set()
    await asyncio.wait_for(pm._restart_tasks[dep_a], timeout=1.0)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_restart_task_deduped_per_deployment() -> None:
    """An in-flight restart task for a deployment is NOT duplicated: a second
    schedule for the same deployment while the first task is still running is a
    no-op (no two live nodes for one account)."""
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=MagicMock(spec=RestartPolicy),
    )
    dep = uuid4()
    release = asyncio.Event()
    runs = {"n": 0}

    async def _restart(_dep: UUID) -> _RestartOutcome:
        runs["n"] += 1
        await release.wait()
        return _RestartOutcome.RESTARTED

    pm._attempt_auto_restart = _restart  # type: ignore[assignment]

    pm._schedule_restart_task(dep, _ACCOUNT_ID)  # type: ignore[attr-defined]
    first = pm._restart_tasks[dep]  # type: ignore[attr-defined]
    await asyncio.sleep(0)  # let the task start running
    # Second schedule while the first is in flight → deduped (no second task).
    pm._schedule_restart_task(dep, _ACCOUNT_ID)  # type: ignore[attr-defined]
    assert pm._restart_tasks[dep] is first  # type: ignore[attr-defined]

    release.set()
    await asyncio.wait_for(first, timeout=1.0)
    assert runs["n"] == 1, "the restart body must run exactly once for one deployment"


@pytest.mark.asyncio
async def test_restart_tasks_cancelled_on_cleanup() -> None:
    """Shutdown cleanup cancels every in-flight restart task (no leaked respawn
    after the supervisor starts draining)."""
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=MagicMock(spec=RestartPolicy),
    )
    dep = uuid4()
    started = asyncio.Event()

    async def _restart(_dep: UUID) -> _RestartOutcome:
        started.set()
        await asyncio.Event().wait()  # never completes on its own
        return _RestartOutcome.RESTARTED

    pm._attempt_auto_restart = _restart  # type: ignore[assignment]
    pm._schedule_restart_task(dep, _ACCOUNT_ID)  # type: ignore[attr-defined]
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await pm.cancel_restart_tasks()  # type: ignore[attr-defined]

    assert pm._restart_tasks == {}, "cleanup must drain the restart-task registry"  # type: ignore[attr-defined]


# Council 2026-06-01 (item 3): the test for the per-deployment
# ``cancel_restart_task`` operator-stop fast-path was REMOVED along with the
# method it covered. The operator /stop no longer force-cancels the restart
# task (the naked cancel orphaned a committed ``starting`` row — the F2 bug);
# correctness now rests on the durable ``stop_requested_at`` intent + the
# serialized Phase-A OPERATOR_STOPPED re-check + the pre-start stop-intent gate,
# exercised in tests/integration/live_supervisor/test_pr2_lock_order_concurrency.py.


@pytest.mark.asyncio
async def test_restart_task_exception_does_not_leak_or_crash() -> None:
    """An exception inside the restart task is caught + logged (account-scoped)
    and the task handle is removed — it must not crash the reaper or leak."""
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=MagicMock(spec=RestartPolicy),
    )
    dep = uuid4()

    async def _boom(_dep: UUID) -> _RestartOutcome:
        raise RuntimeError("transient Postgres blip in the restart task")

    pm._attempt_auto_restart = _boom  # type: ignore[assignment]
    pm._schedule_restart_task(dep, _ACCOUNT_ID)  # type: ignore[attr-defined]
    # Drain: the task should complete (swallowing the error) and deregister.
    await asyncio.wait_for(pm._await_restart_tasks_for_test(), timeout=1.0)  # type: ignore[attr-defined]
    assert dep not in pm._restart_tasks  # type: ignore[attr-defined]


# --- F2: transient dispatch failure must be RETRYABLE, not stranded ----------
#
# Pre-F2, ``_on_child_exit`` committed ``restart_dispatched_at`` BEFORE the
# dispatch. A transient dispatch failure (DB/Redis blip raising, or a
# transient no-ACK ``(False, False)`` strand) left the deployment ``failed``
# with the sentinel SET — and nothing re-drives it at runtime (the handle is
# dropped; rescan is one-shot at startup) → flat-and-unmonitored until a
# supervisor restart. F2: the per-account restart task bounded-RETRIES the
# transient dispatch; double-spawn is prevented by the Phase-A unique index +
# the in-flight-task dedupe, NOT by the sentinel being set-before-dispatch.


@pytest.mark.asyncio
async def test_restart_task_retries_transient_dispatch_then_succeeds() -> None:
    """A transient dispatch failure (``_maybe_auto_restart`` raises) is retried
    by the restart task; a later attempt succeeds and the deployment is NOT left
    permanently stranded."""
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=MagicMock(spec=RestartPolicy),
    )
    # Tiny retry backoff so the test is fast.
    pm._restart_retry_backoff_s = 0.01  # type: ignore[attr-defined]
    dep = uuid4()
    attempts = {"n": 0}

    async def _flaky(_dep: UUID) -> _RestartOutcome:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient Postgres blip during dispatch")
        return _RestartOutcome.RESTARTED  # third attempt starts a process

    pm._attempt_auto_restart = _flaky  # type: ignore[assignment]
    pm._schedule_restart_task(dep, _ACCOUNT_ID)  # type: ignore[attr-defined]
    await asyncio.wait_for(pm._await_restart_tasks_for_test(), timeout=2.0)  # type: ignore[attr-defined]

    assert attempts["n"] == 3, "the task must retry the transient dispatch until it succeeds"
    assert dep not in pm._restart_tasks  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_restart_task_gives_up_after_bounded_retries_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the transient dispatch keeps failing, the task gives up after a
    BOUNDED number of attempts and logs a LOUD, alertable account-scoped error
    — it does NOT retry forever (which would hammer a recovering gateway)."""
    import logging

    # A working fake DB so the give-up cleanup (clear-sentinel + re-fail) runs
    # cleanly instead of blowing up on a bare MagicMock session.
    def _db() -> _FakeSession:
        return _FakeSession(_FakeRow(status="failed"))

    pm = FleetRouter(
        db=_db,  # type: ignore[arg-type]
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=MagicMock(spec=RestartPolicy),
    )
    pm._restart_retry_backoff_s = 0.01  # type: ignore[attr-defined]
    dep = uuid4()
    attempts = {"n": 0}

    async def _always_transient(_dep: UUID) -> _RestartOutcome:
        attempts["n"] += 1
        raise RuntimeError("persistent transient dispatch failure")

    pm._attempt_auto_restart = _always_transient  # type: ignore[assignment]

    with caplog.at_level(logging.ERROR, logger="msai.live_supervisor.fleet_router"):
        pm._schedule_restart_task(dep, _ACCOUNT_ID)  # type: ignore[attr-defined]
        await asyncio.wait_for(pm._await_restart_tasks_for_test(), timeout=2.0)  # type: ignore[attr-defined]

    # Bounded: it tried more than once but a finite number of times.
    assert attempts["n"] >= 2
    assert attempts["n"] <= 10
    assert dep not in pm._restart_tasks  # type: ignore[attr-defined]
    # Loud: an alertable error log naming the account/deployment.
    assert any(r.message == "auto_restart_task_gave_up" for r in caplog.records), (
        "giving up must emit a loud, alertable log so the deployment isn't silently stranded"
    )


@pytest.mark.asyncio
async def test_halt_arriving_after_dispatch_abandons_restart_in_task() -> None:
    """The cancellable-backoff property is PRESERVED in the relocated task: a
    halt arriving while the task is backing off abandons the restart (the task
    observes the halt and exits without spawning)."""
    policy = MagicMock(spec=RestartPolicy)
    policy.decide = MagicMock(
        return_value=RestartDecision(action=RestartAction.BACKOFF, delay_s=5.0)
    )
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )
    ctx = _make_context()

    async def _load_ctx(_dep_id: UUID) -> _RestartContext | None:
        return ctx

    calls = {"n": 0}

    async def _halt(_account_id: str | None) -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # not halted at the gate; halted mid-backoff

    pm._load_restart_context = _load_ctx  # type: ignore[assignment]
    pm._halt_active = _halt  # type: ignore[assignment]

    # FINDING 1 (P1): stub the operator-stop DB seam (default: no
    # stop requested) so _attempt_auto_restart doesn't hit the MagicMock db.
    async def _no_stop(_dep_id: object) -> bool:
        return False

    pm._operator_stop_requested = _no_stop  # type: ignore[assignment]
    pm.spawn_with_outcome = AsyncMock(return_value=(True, True))  # type: ignore[method-assign]

    # Drive through the SCHEDULED task (not _maybe_auto_restart directly) so the
    # relocation is exercised end-to-end.
    pm._schedule_restart_task(ctx.deployment_id, ctx.account_id)  # type: ignore[attr-defined]
    await asyncio.wait_for(pm._await_restart_tasks_for_test(), timeout=3.0)  # type: ignore[attr-defined]

    pm.spawn_with_outcome.assert_not_awaited()  # type: ignore[attr-defined]
    assert ctx.deployment_id not in pm._restart_tasks  # type: ignore[attr-defined]


# --- Council #4 (OPT C) Part 2: transient NO-ACK is RETRYABLE on the fast path
#
# Pre-OPT-C, ``_run_restart_task`` only retried when ``_maybe_auto_restart``
# RAISED. A transient no-ACK outcome — ``spawn_with_outcome`` returning
# ``(ack=False, process_started=False)`` for ``CONCURRENT_STARTUP`` or a
# transient post-Phase-A payload-factory failure — returned WITHOUT raising and
# was therefore treated as TERMINAL → no retry → the common transient strand.
# OPT C Part 2: surface a transient-no-ACK signal from the restart attempt and
# RETRY it on the fast path; a DELIBERATE terminal suppression (halt / paused /
# permanent / operator-stop / ack-drop / already-active) is NOT retried.


@pytest.mark.asyncio
async def test_restart_task_retries_transient_no_ack_concurrent_startup() -> None:
    """Council #4 test 1: a transient NO-ACK ``(False, False)`` outcome
    (``CONCURRENT_STARTUP`` / transient payload-factory) is RETRIED on the fast
    path — NOT treated as a terminal decision.

    Modeled by ``spawn_with_outcome`` returning ``(False, False)`` (the
    transient no-ACK shape) for the first attempts, then ``(True, True)`` (a
    genuine respawn) — the task must keep retrying until the process starts."""
    policy = MagicMock(spec=RestartPolicy)
    policy.decide = MagicMock(return_value=RestartDecision(action=RestartAction.RESTART))
    policy.record_failure = MagicMock(return_value=None)
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )
    pm._restart_retry_backoff_s = 0.01  # type: ignore[attr-defined]
    ctx = _make_context()

    async def _load_ctx(_dep_id: UUID) -> _RestartContext | None:
        return ctx

    async def _halt(_account_id: str | None) -> bool:
        return False

    spawn_calls = {"n": 0}

    async def _spawn(**_kwargs: object) -> tuple[bool, bool]:
        spawn_calls["n"] += 1
        # First two attempts return the TRANSIENT no-ACK shape (no process,
        # no ACK → CONCURRENT_STARTUP / transient payload-factory); the third
        # genuinely starts a process.
        if spawn_calls["n"] < 3:
            return False, False
        return True, True

    pm._load_restart_context = _load_ctx  # type: ignore[assignment]
    pm._halt_active = _halt  # type: ignore[assignment]

    # FINDING 1 (P1): stub the operator-stop DB seam (default: no
    # stop requested) so _attempt_auto_restart doesn't hit the MagicMock db.
    async def _no_stop(_dep_id: object) -> bool:
        return False

    pm._operator_stop_requested = _no_stop  # type: ignore[assignment]
    pm.spawn_with_outcome = _spawn  # type: ignore[method-assign]
    # Don't let the transient-strand re-fail helper touch a bare MagicMock DB.
    pm._refail_stranded_restart = AsyncMock(return_value=None)  # type: ignore[method-assign]

    pm._schedule_restart_task(ctx.deployment_id, ctx.account_id)  # type: ignore[attr-defined]
    await asyncio.wait_for(pm._await_restart_tasks_for_test(), timeout=3.0)  # type: ignore[attr-defined]

    assert spawn_calls["n"] == 3, (
        "a transient no-ACK respawn outcome must be RETRIED on the fast path "
        "until a process genuinely starts"
    )
    assert ctx.deployment_id not in pm._restart_tasks  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_restart_task_does_not_retry_terminal_ack_drop_suppression() -> None:
    """Council #4 test 1 (negative half): a DELIBERATE terminal suppression —
    modeled here as an ACK-drop ``(ack=True, process_started=False)`` (halt
    raced / terminal / paused / payload PERMANENT) — is NOT retried.

    Retrying a deliberate suppression would churn a halted/paused/operator-
    stopped/permanent deployment. The task must run the dispatch exactly ONCE
    and stop."""
    policy = MagicMock(spec=RestartPolicy)
    policy.decide = MagicMock(return_value=RestartDecision(action=RestartAction.RESTART))
    policy.record_failure = MagicMock(return_value=None)
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )
    pm._restart_retry_backoff_s = 0.01  # type: ignore[attr-defined]
    ctx = _make_context()

    async def _load_ctx(_dep_id: UUID) -> _RestartContext | None:
        return ctx

    async def _halt(_account_id: str | None) -> bool:
        return False

    spawn_calls = {"n": 0}

    async def _spawn(**_kwargs: object) -> tuple[bool, bool]:
        spawn_calls["n"] += 1
        # ACK-drop: a process did NOT start but the command WAS ACKed — a
        # deliberate terminal suppression (e.g. halt raced post-decision /
        # payload PERMANENT). Must NOT be retried.
        return True, False

    pm._load_restart_context = _load_ctx  # type: ignore[assignment]
    pm._halt_active = _halt  # type: ignore[assignment]

    # FINDING 1 (P1): stub the operator-stop DB seam (default: no
    # stop requested) so _attempt_auto_restart doesn't hit the MagicMock db.
    async def _no_stop(_dep_id: object) -> bool:
        return False

    pm._operator_stop_requested = _no_stop  # type: ignore[assignment]
    pm.spawn_with_outcome = _spawn  # type: ignore[method-assign]

    pm._schedule_restart_task(ctx.deployment_id, ctx.account_id)  # type: ignore[attr-defined]
    await asyncio.wait_for(pm._await_restart_tasks_for_test(), timeout=3.0)  # type: ignore[attr-defined]

    assert spawn_calls["n"] == 1, "a deliberate terminal suppression must NOT be retried"


@pytest.mark.asyncio
async def test_restart_task_does_not_retry_halt_suppression() -> None:
    """A halt-gated suppression (gate returns True) is terminal — the task must
    NOT retry it (no churn of a halted/drained account)."""
    policy = MagicMock(spec=RestartPolicy)
    policy.decide = MagicMock(return_value=RestartDecision(action=RestartAction.RESTART))
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )
    pm._restart_retry_backoff_s = 0.01  # type: ignore[attr-defined]
    ctx = _make_context()

    async def _load_ctx(_dep_id: UUID) -> _RestartContext | None:
        return ctx

    halt_calls = {"n": 0}

    async def _halt(_account_id: str | None) -> bool:
        halt_calls["n"] += 1
        return True  # halted at the gate every call

    pm._load_restart_context = _load_ctx  # type: ignore[assignment]
    pm._halt_active = _halt  # type: ignore[assignment]

    # FINDING 1 (P1): stub the operator-stop DB seam (default: no
    # stop requested) so _attempt_auto_restart doesn't hit the MagicMock db.
    async def _no_stop(_dep_id: object) -> bool:
        return False

    pm._operator_stop_requested = _no_stop  # type: ignore[assignment]
    pm.spawn_with_outcome = AsyncMock(return_value=(True, False))  # type: ignore[method-assign]

    pm._schedule_restart_task(ctx.deployment_id, ctx.account_id)  # type: ignore[attr-defined]
    await asyncio.wait_for(pm._await_restart_tasks_for_test(), timeout=3.0)  # type: ignore[attr-defined]

    pm.spawn_with_outcome.assert_not_awaited()  # type: ignore[attr-defined]
    assert halt_calls["n"] == 1, (
        "a halt suppression is terminal — the gate is consulted once, no retry"
    )


@pytest.mark.asyncio
async def test_restart_task_does_not_retry_paused_suppression() -> None:
    """A PAUSED policy decision (ceiling tripped) is terminal — the task must
    NOT retry, so repeated reconciliation cannot churn a paused deployment
    (Council #4 test 4 on the fast path)."""
    policy = MagicMock(spec=RestartPolicy)
    policy.decide = MagicMock(
        return_value=RestartDecision(action=RestartAction.PAUSED, pause_reason="ceiling")
    )
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )
    pm._restart_retry_backoff_s = 0.01  # type: ignore[attr-defined]
    ctx = _make_context()

    async def _load_ctx(_dep_id: UUID) -> _RestartContext | None:
        return ctx

    async def _halt(_account_id: str | None) -> bool:
        return False

    pm._load_restart_context = _load_ctx  # type: ignore[assignment]
    pm._halt_active = _halt  # type: ignore[assignment]

    # FINDING 1 (P1): stub the operator-stop DB seam (default: no
    # stop requested) so _attempt_auto_restart doesn't hit the MagicMock db.
    async def _no_stop(_dep_id: object) -> bool:
        return False

    pm._operator_stop_requested = _no_stop  # type: ignore[assignment]
    pm.spawn_with_outcome = AsyncMock(return_value=(True, False))  # type: ignore[method-assign]

    pm._schedule_restart_task(ctx.deployment_id, ctx.account_id)  # type: ignore[attr-defined]
    await asyncio.wait_for(pm._await_restart_tasks_for_test(), timeout=3.0)  # type: ignore[attr-defined]

    pm.spawn_with_outcome.assert_not_awaited()  # type: ignore[attr-defined]
    # decide() consulted once; no respawn, no churn.
    assert policy.decide.call_count == 1, "a PAUSED suppression must not be retried (no churn)"
