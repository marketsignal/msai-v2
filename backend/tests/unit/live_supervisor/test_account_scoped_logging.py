"""Unit tests for PR 2 T10 — account-scoped supervision logging.

Every supervision log/error site in :mod:`msai.live_supervisor.fleet_router`
and :mod:`msai.live_supervisor.main` must carry account-scoped structured
context — the key field being ``account_id`` (so a fleet operator can grep one
account's supervision trail), alongside ``deployment_id`` and the cheap-in-scope
extras (DB status, pid, restart decision).

These modules emit through the stdlib ``logging`` module (``log =
logging.getLogger(__name__)``, then ``log.<level>("event", extra={...})``), so
the assertions read the structured fields off the ``LogRecord`` attributes via
pytest's ``caplog`` fixture (matching the existing ``test_auto_restart.py``
convention). ``ENVIRONMENT=test`` is set at the top of ``conftest.py`` before
any ``msai.*`` import so the logging stack is test-friendly.

The three log classes the task names explicitly are pinned here:

1. **Restart decision** — ``auto_restart_dispatched`` (the headline US-2 reaper
   decision) carries ``account_id`` + ``deployment_id`` + ``restart_decision``.
2. **Spawn** — ``spawn_blocked_by_account_halt`` carries ``account_id`` +
   ``deployment_id``.
3. **Reap / skip** — ``auto_restart_skipped_no_row`` (the reaper saw an exit but
   no node-process row) carries ``account_id`` (resolved off the surviving
   deployment) + ``deployment_id``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from msai.live_supervisor.fleet_router import FleetRouter
from msai.live_supervisor.restart_policy import RestartPolicy

_ACCOUNT_ID = "DUP733214"
_DEPLOYMENT_SLUG = "abcdef0123456789"


class _NodeRow:
    """Minimal ``live_node_processes`` row stand-in for ``_on_child_exit``.

    Carries just the columns the reaper reads/mutates: ``status`` (DB status the
    log surfaces), ``pid``, ``last_heartbeat_at`` (heartbeat-age source), and the
    council-#3 restart-authority columns ``stop_requested_at`` /
    ``restart_dispatched_at``."""

    def __init__(self, *, status: str = "running") -> None:
        # FIX 2 (P2): the reaper captures ``row.id`` as the owned row so the
        # give-up cleanup targets it by id (real ``LiveNodeProcess`` has an id).
        self.id = uuid4()
        self.status = status
        self.pid: int | None = 4242
        self.last_heartbeat_at: datetime | None = datetime.now(UTC)
        self.failure_kind: str | None = None
        self.error_message: str | None = None
        self.exit_code: int | None = None
        self.stop_requested_at: datetime | None = None
        self.restart_dispatched_at: datetime | None = None


class _ScalarResult:
    """A SQLAlchemy-result stand-in exposing ``scalar_one_or_none``."""

    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _DeploymentRow:
    """Minimal ``live_deployments`` row stand-in.

    Council 2026-06-01 (Finding 2): ``_on_child_exit`` now locks the parent
    deployment FOR UPDATE FIRST (``session.get(LiveDeployment, ...,
    with_for_update=True)``) and reads ``account_id`` off THAT row (the account-
    scoping read is now free under the deployment lock — no separate SELECT). The
    reaper also syncs ``status`` on the stale-active terminal write, so this stub
    carries a mutable ``status``."""

    def __init__(self, *, account_id: str | None, status: str = "running") -> None:
        self.account_id = account_id
        self.status = status


class _SequencedSession:
    """Async-context session that returns a SCRIPTED sequence of ``execute``
    results AND a fixed ``get`` deployment row.

    Council 2026-06-01 (Finding 2 — deployment-FIRST): ``_on_child_exit`` now
    issues a single ``session.get(LiveDeployment, ..., with_for_update=True)`` for
    the parent deployment (account-scoping read off that locked row), then ONE
    ``session.execute`` for the FOR-UPDATE classify node SELECT. This fake returns
    ``deployment`` from ``get`` and pops ``results`` for each ``execute`` so the
    test controls exactly what each query returns. A no-op transaction otherwise;
    the chained ``.with_for_update()`` / ``.where()`` builder calls on the
    statement are ignored (the statement arg is discarded)."""

    def __init__(self, results: list[object], deployment: object) -> None:
        self._results = list(results)
        self._deployment = deployment

    async def __aenter__(self) -> _SequencedSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def begin(self) -> _SequencedSession:
        return self

    async def get(self, *_args: object, **_kwargs: object) -> object:
        return self._deployment

    async def execute(self, *_args: object, **_kwargs: object) -> _ScalarResult:
        return _ScalarResult(self._results.pop(0))


def _reaper_pm(results: list[object], *, account_id: str | None = _ACCOUNT_ID) -> FleetRouter:
    """A FleetRouter whose DB returns ``results`` from ``execute`` (the classify
    node SELECT) and a deployment carrying ``account_id`` from ``get`` (the
    deployment-first locked read, Finding 2), and whose ``_attempt_auto_restart``
    is stubbed (the dispatch is the seam under test — the detached restart task
    calls ``_attempt_auto_restart``, council #4 OPT C Part 2)."""
    from msai.live_supervisor.fleet_router import _RestartOutcome

    policy = MagicMock(spec=RestartPolicy)
    deployment = _DeploymentRow(account_id=account_id, status="running")

    def _db() -> _SequencedSession:
        return _SequencedSession(results, deployment)

    pm = FleetRouter(
        db=_db,  # type: ignore[arg-type]
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )
    pm._attempt_auto_restart = AsyncMock(return_value=_RestartOutcome.RESTARTED)  # type: ignore[method-assign]
    return pm


def _field(record: logging.LogRecord, name: str) -> Any:
    """Read a structured ``extra=`` field off the LogRecord (None if absent)."""
    return getattr(record, name, None)


@pytest.mark.asyncio
async def test_reaper_restart_dispatch_log_carries_account_and_deployment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REQUIRED (restart-decision site): a non-operator-stop crash that dispatches
    a restart emits ``auto_restart_dispatched`` carrying ``account_id`` +
    ``deployment_id`` + ``restart_decision`` so an operator can grep one account's
    restart trail."""
    # Arrange — stale-active row (SIGKILL/OOM path); the deployment's account_id is
    # read off the deployment-first locked row (Finding 2), so only the classify
    # node SELECT is scripted via ``execute``.
    row = _NodeRow(status="running")
    dep_id = uuid4()
    pm = _reaper_pm([row])  # execute → classify node row; account_id via get

    # Act
    with caplog.at_level(logging.INFO, logger="msai.live_supervisor.fleet_router"):
        await pm._on_child_exit(dep_id, exit_code=1)
        # F1: the dispatch is a detached per-account task — drain it so the
        # awaited-assertion is deterministic (the reaper itself does NOT block).
        await pm._await_restart_tasks_for_test()  # type: ignore[attr-defined]

    # Assert — the dispatch decision fired with full account context.
    pm._attempt_auto_restart.assert_awaited_once_with(dep_id)  # type: ignore[attr-defined]
    dispatched = [r for r in caplog.records if r.message == "auto_restart_dispatched"]
    assert dispatched, "the reaper must emit auto_restart_dispatched on a dispatch"
    rec = dispatched[0]
    assert _field(rec, "account_id") == _ACCOUNT_ID
    assert _field(rec, "deployment_id") == str(dep_id)
    assert _field(rec, "restart_decision") == "dispatched"
    # Cheap-in-scope extras the same site now carries.
    assert _field(rec, "db_status") is not None
    assert _field(rec, "pid") == 4242


@pytest.mark.asyncio
async def test_reaper_skipped_no_row_log_carries_account_and_deployment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REQUIRED (reap/skip site): when the reaper sees an exit but the
    node-process row is gone, ``auto_restart_skipped_no_row`` still resolves the
    surviving deployment's ``account_id`` (one extra column, same txn) so the
    silent no-op is account-attributable."""
    # Arrange — the classify node SELECT returns None (row gone); the account_id is
    # resolved off the surviving deployment via the deployment-first locked
    # ``get`` (Finding 2).
    dep_id = uuid4()
    pm = _reaper_pm([None])

    # Act
    with caplog.at_level(logging.INFO, logger="msai.live_supervisor.fleet_router"):
        await pm._on_child_exit(dep_id, exit_code=1)

    # Assert — no dispatch (nothing to restart) but the skip log is account-scoped.
    pm._attempt_auto_restart.assert_not_awaited()  # type: ignore[attr-defined]
    skipped = [r for r in caplog.records if r.message == "auto_restart_skipped_no_row"]
    assert skipped, "the reaper must emit auto_restart_skipped_no_row when no row exists"
    rec = skipped[0]
    assert _field(rec, "account_id") == _ACCOUNT_ID
    assert _field(rec, "deployment_id") == str(dep_id)
    assert _field(rec, "exit_code") == 1


@pytest.mark.asyncio
async def test_reaper_operator_stop_suppression_log_carries_account(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A crash during an operator /stop is suppressed; the
    ``auto_restart_suppressed_operator_stop`` decision log carries ``account_id``
    + ``deployment_id`` + ``restart_decision`` so the suppression is attributable
    to the right account."""
    # Arrange — durable operator-stop intent set on the row. The account_id is read
    # off the deployment-first locked deployment row (Finding 2).
    row = _NodeRow(status="running")
    row.stop_requested_at = datetime.now(UTC)
    dep_id = uuid4()
    pm = _reaper_pm([row])

    # Act
    with caplog.at_level(logging.INFO, logger="msai.live_supervisor.fleet_router"):
        await pm._on_child_exit(dep_id, exit_code=1)

    # Assert — suppressed, with account context.
    pm._attempt_auto_restart.assert_not_awaited()  # type: ignore[attr-defined]
    suppressed = [r for r in caplog.records if r.message == "auto_restart_suppressed_operator_stop"]
    assert suppressed, "an operator-stop crash must emit the suppression log"
    rec = suppressed[0]
    assert _field(rec, "account_id") == _ACCOUNT_ID
    assert _field(rec, "deployment_id") == str(dep_id)
    assert _field(rec, "restart_decision") == "suppressed_operator_stop"


@pytest.mark.asyncio
async def test_spawn_blocked_by_account_halt_log_carries_account_and_deployment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """REQUIRED (spawn site): when a START is refused because the account drain
    latch is set, ``spawn_blocked_by_account_halt`` carries ``account_id`` +
    ``deployment_id`` so the operator sees which account's drain blocked it."""
    # Arrange — Phase A reserved a slot (returns a real row_id); the fleet halt
    # key is clear but the ACCOUNT halt latch is set.
    row_id = uuid4()
    dep_id = uuid4()

    redis = MagicMock()
    redis.exists = AsyncMock(return_value=True)  # account_halt_key EXISTS → blocked

    pm = FleetRouter(
        db=MagicMock(),
        redis=redis,
        spawn_target=lambda: None,
    )
    # Reserve-slot returns a real row id (so spawn proceeds to the halt checks).
    pm._phase_a_reserve_slot = AsyncMock(return_value=row_id)  # type: ignore[method-assign]
    # The deployment resolves to our account_id (cheap-in-scope at the log site).
    pm._account_id_for = AsyncMock(return_value=_ACCOUNT_ID)  # type: ignore[method-assign]
    pm._mark_failed = AsyncMock(return_value=None)  # type: ignore[method-assign]

    # The first ``_redis.exists`` call is the FLEET halt re-check — make it clear,
    # then the account-halt EXISTS returns True. Sequence the two EXISTS results.
    redis.exists = AsyncMock(side_effect=[False, True])

    # Act
    with caplog.at_level(logging.INFO, logger="msai.live_supervisor.fleet_router"):
        ack, started = await pm.spawn_with_outcome(
            deployment_id=dep_id,
            deployment_slug=_DEPLOYMENT_SLUG,
            payload={},
            idempotency_key="k",
        )

    # Assert — ACKed without starting a process, and the block log is account-scoped.
    assert ack is True
    assert started is False
    blocked = [r for r in caplog.records if r.message == "spawn_blocked_by_account_halt"]
    assert blocked, "a START under an account drain latch must emit spawn_blocked_by_account_halt"
    rec = blocked[0]
    assert _field(rec, "account_id") == _ACCOUNT_ID
    assert _field(rec, "deployment_id") == str(dep_id)
