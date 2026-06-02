"""Unit tests for ``FleetRouter.spawn``'s account-halt re-check.

Codex iter 1 P2-1 of PR 1 (multi-account-broker-fleet). The
``/api/v1/live/drain/{account_id}`` endpoint writes
``account_halt_key(account_id)`` to block the drained sub-account.
The supervisor's phase B already re-checks the fleet ``msai:risk:halt``
latch; this fix mirrors that pattern for the account-scoped latch so a
queued or reclaimed-from-PEL START can't spawn the drained account
right after a drain.

These tests stub the DB + Redis surfaces directly (rather than booting
the testcontainers used by ``tests/integration/test_fleet_router.py``)
because the contract here is:

1. ``FleetRouter._account_id_for`` reads ``LiveDeployment.account_id``
2. Phase B checks ``account_halt_key(account_id)`` after the fleet check
3. When the latch is set: row → ``failed`` with ``ACCOUNT_HALT_ACTIVE``,
   spawn returns True (ACKed — no retry until the latch clears)
4. When the latch is NOT set: spawn proceeds normally
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from msai.core.halt_keys import account_halt_key, fleet_halt_key
from msai.live_supervisor.fleet_router import FleetRouter, _PhaseAOutcome
from msai.services.live.failure_kind import FailureKind

_FLEET_KEY = fleet_halt_key()
_DEPLOYMENT_SLUG = "abcdef0123456789"
_ACCOUNT_ID = "DUP733214"


@pytest.fixture
def fake_redis() -> MagicMock:
    """Fake Redis whose ``exists`` returns 0 for every key by default."""
    fake = MagicMock()
    fake.exists = AsyncMock(return_value=0)
    return fake


def _make_pm(fake_redis: MagicMock, *, account_id: str | None = _ACCOUNT_ID) -> FleetRouter:
    """Build a FleetRouter whose phase A always succeeds and whose
    DB returns the given account_id for the deployment slug."""
    row_id = uuid4()

    async def _phase_a_stub(**_kwargs: object) -> UUID:
        return row_id

    async def _account_id_stub(_slug: str) -> str | None:
        return account_id

    # Council 2026-06-01: the pre-start stop-intent gate reads the latest node
    # row's ``stop_requested_at`` via ``_latest_stop_requested_at`` (a real DB
    # read) right before ``process.start()``. These unit tests stub the DB
    # (``db=MagicMock()``), so stub the gate's read to "no stop intent" — these
    # tests exercise the ACCOUNT-HALT branch, not the stop-intent gate.
    async def _no_stop_intent(_deployment_id: UUID) -> None:
        return None

    mark_failed_calls: list[dict[str, object]] = []

    async def _mark_failed_stub(*, row_id: UUID, reason: str, failure_kind: FailureKind) -> None:
        mark_failed_calls.append({"row_id": row_id, "reason": reason, "failure_kind": failure_kind})

    pm = FleetRouter(
        db=MagicMock(),  # unused — phase A is stubbed
        redis=fake_redis,
        spawn_target=lambda: None,
    )
    pm._phase_a_reserve_slot = _phase_a_stub  # type: ignore[assignment]
    pm._account_id_for = _account_id_stub  # type: ignore[assignment]
    pm._latest_stop_requested_at = _no_stop_intent  # type: ignore[assignment]
    pm._mark_failed = _mark_failed_stub  # type: ignore[assignment]
    pm._test_mark_failed_calls = mark_failed_calls  # type: ignore[attr-defined]
    pm._test_row_id = row_id  # type: ignore[attr-defined]
    return pm


def _spawn_kwargs(idem: str) -> dict[str, object]:
    return {
        "deployment_id": uuid4(),
        "deployment_slug": _DEPLOYMENT_SLUG,
        "payload": {"deployment_slug": _DEPLOYMENT_SLUG},
        "idempotency_key": idem,
    }


@pytest.mark.asyncio
async def test_spawn_blocks_when_account_halt_latch_set(fake_redis: MagicMock) -> None:
    """When ``account_halt_key(account_id)`` is set in Redis, ``spawn``
    MUST mark the row failed with ``ACCOUNT_HALT_ACTIVE`` and return
    True (ACKed). The subprocess is never started."""

    async def _exists_side_effect(key: str) -> int:
        return 1 if key == account_halt_key(_ACCOUNT_ID) else 0

    fake_redis.exists.side_effect = _exists_side_effect

    pm = _make_pm(fake_redis)

    ok = await pm.spawn(**_spawn_kwargs("idem-1"))  # type: ignore[arg-type]

    assert ok is True, "ACK so the START doesn't redeliver until the latch clears"
    mark_failed_calls = pm._test_mark_failed_calls  # type: ignore[attr-defined]
    assert len(mark_failed_calls) == 1
    call = mark_failed_calls[0]
    assert call["failure_kind"] == FailureKind.ACCOUNT_HALT_ACTIVE
    assert _ACCOUNT_ID in str(call["reason"])
    assert pm.node_handle_cache == {}


@pytest.mark.asyncio
async def test_spawn_proceeds_when_account_halt_not_set(fake_redis: MagicMock) -> None:
    """When neither the fleet nor the account latch is set, spawn must
    fall through past the latch checks. We assert the
    ``ACCOUNT_HALT_ACTIVE`` mark-failed branch did NOT fire."""
    fake_redis.exists.return_value = 0

    pm = _make_pm(fake_redis)
    pm._spawn_args = ()
    fake_proc = MagicMock()
    fake_proc.start = MagicMock(return_value=None)
    fake_proc.pid = 9999
    spawn_ctx_mock = MagicMock()
    spawn_ctx_mock.Process = MagicMock(return_value=fake_proc)
    pm._spawn_ctx = spawn_ctx_mock
    pm._record_pid = AsyncMock(return_value=None)  # type: ignore[attr-defined]

    # Downstream of the account-halt check may raise when the DB stub
    # is exercised; the contract here is bounded by the halt branch.
    with contextlib.suppress(Exception):
        await pm.spawn(**_spawn_kwargs("idem-2"))  # type: ignore[arg-type]

    mark_failed_calls = pm._test_mark_failed_calls  # type: ignore[attr-defined]
    assert not any(
        c["failure_kind"] == FailureKind.ACCOUNT_HALT_ACTIVE for c in mark_failed_calls
    ), "account-halt branch fired even though latch was clear"


@pytest.mark.asyncio
async def test_spawn_skips_account_halt_check_when_account_id_missing(
    fake_redis: MagicMock,
) -> None:
    """A deployment row without ``account_id`` short-circuits the
    account-halt re-check. We assert no Redis EXISTS hit any
    account-halt key."""
    seen_keys: list[str] = []

    async def _exists_recorder(key: str) -> int:
        seen_keys.append(key)
        return 0

    fake_redis.exists.side_effect = _exists_recorder

    pm = _make_pm(fake_redis, account_id=None)
    fake_proc = MagicMock()
    fake_proc.start = MagicMock(return_value=None)
    fake_proc.pid = 9999
    spawn_ctx_mock = MagicMock()
    spawn_ctx_mock.Process = MagicMock(return_value=fake_proc)
    pm._spawn_ctx = spawn_ctx_mock
    pm._record_pid = AsyncMock(return_value=None)  # type: ignore[attr-defined]

    with contextlib.suppress(Exception):
        await pm.spawn(**_spawn_kwargs("idem-3"))  # type: ignore[arg-type]

    assert _FLEET_KEY in seen_keys, "fleet halt check still runs first"
    account_key_calls = [k for k in seen_keys if k.startswith(f"{_FLEET_KEY}:account:")]
    assert not account_key_calls, (
        "account-halt re-check fired even though the deployment has no account_id"
    )


@pytest.mark.asyncio
async def test_spawn_account_halt_latch_keyed_by_account_id_not_login(
    fake_redis: MagicMock,
) -> None:
    """Council 2026-05-29 obj #11: the account latch MUST key on
    ``account_id`` (DUP733214) so two sub-accounts under the same TWS
    login (DUP733214 + DUP733215) have independent latches. Draining
    one MUST NOT block the sibling."""
    sibling_account = "DUP733215"

    async def _exists_side_effect(key: str) -> int:
        return 1 if key == account_halt_key(sibling_account) else 0

    fake_redis.exists.side_effect = _exists_side_effect

    pm = _make_pm(fake_redis, account_id=_ACCOUNT_ID)
    fake_proc = MagicMock()
    fake_proc.start = MagicMock(return_value=None)
    fake_proc.pid = 9999
    spawn_ctx_mock = MagicMock()
    spawn_ctx_mock.Process = MagicMock(return_value=fake_proc)
    pm._spawn_ctx = spawn_ctx_mock
    pm._record_pid = AsyncMock(return_value=None)  # type: ignore[attr-defined]

    with contextlib.suppress(Exception):
        await pm.spawn(**_spawn_kwargs("idem-4"))  # type: ignore[arg-type]

    mark_failed_calls = pm._test_mark_failed_calls  # type: ignore[attr-defined]
    assert not any(
        c["failure_kind"] == FailureKind.ACCOUNT_HALT_ACTIVE for c in mark_failed_calls
    ), "sibling account's drain incorrectly halted this deployment"


@pytest.mark.asyncio
async def test_spawn_blocks_when_account_halt_set_during_payload_factory(
    fake_redis: MagicMock,
) -> None:
    """F3 fix (Codex iter 2 P1 / silent-failure-hunter F4): the
    account-halt latch can be set DURING the payload-factory await
    (which performs DB reads + module imports — seconds of wall clock).
    The supervisor MUST re-check the latch AFTER the payload factory
    completes, before ``process.start()``. Otherwise a ``/drain`` firing
    mid-spawn races past the gate and the subprocess spawns under an
    active drain.

    This test simulates the race: the first ``exists(account_halt_key)``
    call returns 0 (latch not set yet); the second call (after the
    payload factory returned) returns 1.
    """
    account_key = account_halt_key(_ACCOUNT_ID)
    fleet_key = _FLEET_KEY
    call_log: list[str] = []
    # exists() returns 0 on every call EXCEPT the SECOND time the
    # account-halt key is queried — which is the post-payload-factory
    # re-check (F3 fix). That second hit returns 1.
    account_hits = {"count": 0}

    async def _exists_with_race(key: str) -> int:
        call_log.append(key)
        if key == account_key:
            account_hits["count"] += 1
            return 0 if account_hits["count"] == 1 else 1
        if key == fleet_key:
            return 0
        return 0

    fake_redis.exists.side_effect = _exists_with_race

    pm = _make_pm(fake_redis)

    # Wire a payload factory so the post-factory re-check runs. The
    # factory itself just returns an empty args tuple — the F3 check
    # fires regardless of payload content.
    async def _factory(
        _row_id: UUID,
        _dep_id: UUID,
        _slug: str,
        _payload: dict[str, object],
    ) -> tuple[object, ...]:
        return ()

    pm._payload_factory = _factory  # type: ignore[assignment]

    fake_proc = MagicMock()
    fake_proc.start = MagicMock(return_value=None)
    fake_proc.pid = 9999
    spawn_ctx_mock = MagicMock()
    spawn_ctx_mock.Process = MagicMock(return_value=fake_proc)
    pm._spawn_ctx = spawn_ctx_mock

    ok = await pm.spawn(**_spawn_kwargs("idem-race"))  # type: ignore[arg-type]

    # F3: ACK so the START doesn't redeliver until the drain clears.
    assert ok is True

    # The subprocess MUST NOT have been started — the post-factory
    # re-check caught the drain before process.start().
    fake_proc.start.assert_not_called()

    # The mark_failed call MUST tag the row ACCOUNT_HALT_ACTIVE, with
    # an explanatory reason mentioning the post-payload-factory recheck.
    mark_failed_calls = pm._test_mark_failed_calls  # type: ignore[attr-defined]
    assert len(mark_failed_calls) == 1
    call = mark_failed_calls[0]
    assert call["failure_kind"] == FailureKind.ACCOUNT_HALT_ACTIVE
    assert "post-payload-factory" in str(call["reason"])
    assert _ACCOUNT_ID in str(call["reason"])

    # Sanity: the account-halt key was queried TWICE (the pre-factory
    # check and the F3 post-factory re-check).
    assert account_hits["count"] == 2


@pytest.mark.asyncio
async def test_phase_a_outcome_sentinels_skip_account_halt(fake_redis: MagicMock) -> None:
    """The account-halt check sits AFTER phase A returns a real
    ``row_id``. Phase A sentinels (idempotent success, no deployment,
    concurrent startup) MUST bypass phase B entirely so no spurious
    account-halt query fires for them."""
    seen_keys: list[str] = []

    async def _exists_recorder(key: str) -> int:
        seen_keys.append(key)
        return 0

    fake_redis.exists.side_effect = _exists_recorder

    pm = _make_pm(fake_redis, account_id=_ACCOUNT_ID)

    async def _phase_a_already_active(**_kwargs: object) -> _PhaseAOutcome:
        return _PhaseAOutcome.ALREADY_ACTIVE

    pm._phase_a_reserve_slot = _phase_a_already_active  # type: ignore[assignment]

    ok = await pm.spawn(**_spawn_kwargs("idem-5"))  # type: ignore[arg-type]

    assert ok is True
    assert _FLEET_KEY not in seen_keys
    assert not any(k.startswith(f"{_FLEET_KEY}:account:") for k in seen_keys)
