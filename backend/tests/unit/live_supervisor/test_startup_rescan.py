"""Unit tests for the supervisor startup / periodic re-scan (PR 2 / T6 + F2).

A node can die while the supervisor itself is down (container recreate, OOM
of the whole supervisor, a crash before the reaper observed the exit). When a
fresh supervisor boots its :attr:`FleetRouter.node_handle_cache` is EMPTY — it
holds no handle for the dead child — so the reaper's ``_on_child_exit`` path
never fires for it. Without a re-scan that account would stay flat and
unmonitored across a supervisor restart.

:meth:`FleetRouter.rescan_for_restart` closes that gap: on supervisor start
(and on a fixed cadence after) it scans this fleet's ``failed`` / stale active
deployments that are NOT ``auto_restart_paused`` and re-evaluates each through
the SAME halt-gate + bounded policy used by the reaper (``_maybe_auto_restart``).
The duplicate case — the re-scan AND per-account PEL recovery both trying to
restart the same dead deployment — is serialised by the existing
partial-unique-index inside ``spawn`` (Phase A ``ALREADY_ACTIVE`` path): exactly
one restart wins, the other ACKs idempotently and does NOT double-count an
attempt.

PR 2 F2 (review P2): the re-scan now passes ``skip_backoff=True`` so a candidate
in its ``RestartPolicy`` BACKOFF window is SKIPPED (no inline sleep) rather than
blocking evaluation of the LATER candidates. Previously the rescan awaited the
cancellable backoff (up to 300s) IN-BAND for each candidate, so one
crash-looping account left every unrelated account flat-and-unmonitored for up
to the backoff cap.

These tests stub the candidate-loading seam (``_load_rescan_candidates``) and
``_maybe_auto_restart`` so the re-scan orchestration is unit-tested without a
real Postgres / Redis.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from msai.live_supervisor.fleet_router import FleetRouter
from msai.live_supervisor.restart_policy import RestartPolicy


def _make_pm() -> FleetRouter:
    policy = MagicMock(spec=RestartPolicy)
    return FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )


@pytest.mark.asyncio
async def test_rescan_restarts_a_node_that_died_while_supervisor_was_down() -> None:
    """The re-scan finds a ``failed`` active deployment whose handle is gone
    (supervisor was down when it died) and routes it through the SAME
    halt-gated restart decision the reaper uses."""
    dep_id = uuid4()
    pm = _make_pm()

    async def _candidates() -> list[UUID]:
        return [dep_id]

    seen: list[UUID] = []

    async def _maybe(dep: UUID, *, skip_backoff: bool = False) -> bool:
        seen.append(dep)
        return True

    pm._load_rescan_candidates = _candidates  # type: ignore[assignment]
    pm._maybe_auto_restart = _maybe  # type: ignore[assignment]

    restarted = await pm.rescan_for_restart()

    assert restarted == 1
    assert seen == [dep_id], "re-scan must re-evaluate the dead deployment"


@pytest.mark.asyncio
async def test_rescan_passes_skip_backoff_to_the_restart_decision() -> None:
    """PR 2 F2 — the re-scan invokes ``_maybe_auto_restart`` with
    ``skip_backoff=True`` so a BACKOFF candidate is skipped (no inline sleep)
    rather than blocking the loop."""
    dep_id = uuid4()
    pm = _make_pm()

    async def _candidates() -> list[UUID]:
        return [dep_id]

    skip_flags: list[bool] = []

    async def _maybe(dep: UUID, *, skip_backoff: bool = False) -> bool:
        skip_flags.append(skip_backoff)
        return True

    pm._load_rescan_candidates = _candidates  # type: ignore[assignment]
    pm._maybe_auto_restart = _maybe  # type: ignore[assignment]

    await pm.rescan_for_restart()

    assert skip_flags == [True], "the re-scan must request skip_backoff so it never sleeps inline"


@pytest.mark.asyncio
async def test_rescan_does_not_block_on_a_candidate_in_backoff() -> None:
    """PR 2 F2 (review P2) — the headline fix: a candidate in a LONG backoff
    window must NOT delay the EVALUATION of the OTHER candidates.

    With ``skip_backoff=True`` a BACKOFF candidate returns immediately (the real
    ``_attempt_auto_restart`` returns SUPPRESSED without sleeping). We model the
    OLD bug by having the FIRST candidate's decision sleep a long time when
    ``skip_backoff`` is NOT honoured; the rescan must finish PROMPTLY and still
    evaluate the second candidate. Because the production rescan passes
    ``skip_backoff=True``, the modelled long sleep never runs.
    """
    backoff_dep = uuid4()
    other_dep = uuid4()
    pm = _make_pm()

    async def _candidates() -> list[UUID]:
        return [backoff_dep, other_dep]

    evaluated: list[UUID] = []

    async def _maybe(dep: UUID, *, skip_backoff: bool = False) -> bool:
        evaluated.append(dep)
        if dep == backoff_dep and not skip_backoff:
            # The OLD inline-backoff behaviour: block for a long time. The
            # production rescan passes skip_backoff=True, so this never runs —
            # if it does, the wait_for below times out and the test fails.
            await asyncio.sleep(300)
            return True
        # skip_backoff path (or the non-backoff candidate): return at once.
        return dep != backoff_dep  # the backoff candidate is "skipped" (no restart)

    pm._load_rescan_candidates = _candidates  # type: ignore[assignment]
    pm._maybe_auto_restart = _maybe  # type: ignore[assignment]

    # The rescan must finish promptly — never blocking on the backoff candidate.
    restarted = await asyncio.wait_for(pm.rescan_for_restart(), timeout=2.0)

    assert evaluated == [backoff_dep, other_dep], (
        "the later candidate was evaluated WITHOUT waiting for the earlier "
        "candidate's backoff (the F2 isolation fix)"
    )
    assert restarted == 1, "the backoff candidate is skipped; the other restarts this pass"


@pytest.mark.asyncio
async def test_rescan_routes_every_candidate_through_the_same_halt_gate() -> None:
    """Each candidate is sent through ``_maybe_auto_restart`` (the SAME
    halt-gate + policy as the reaper) — the re-scan does NOT bypass the gate
    with its own restart path."""
    deps = [uuid4(), uuid4(), uuid4()]
    pm = _make_pm()

    async def _candidates() -> list[UUID]:
        return list(deps)

    # Halt-gate suppresses the middle one (modeled as _maybe returning False).
    async def _maybe(dep: UUID, *, skip_backoff: bool = False) -> bool:
        return dep != deps[1]

    pm._load_rescan_candidates = _candidates  # type: ignore[assignment]
    pm._maybe_auto_restart = _maybe  # type: ignore[assignment]

    restarted = await pm.rescan_for_restart()

    assert restarted == 2, "two restarted, the halted one suppressed by the shared gate"


@pytest.mark.asyncio
async def test_rescan_continues_when_one_candidate_raises() -> None:
    """A single candidate raising must not abort the whole re-scan — the
    remaining dead nodes still get their restart evaluation (best-effort,
    one bad row can't strand the fleet)."""
    good = uuid4()
    bad = uuid4()
    pm = _make_pm()

    async def _candidates() -> list[UUID]:
        return [bad, good]

    seen: list[UUID] = []

    async def _maybe(dep: UUID, *, skip_backoff: bool = False) -> bool:
        if dep == bad:
            raise RuntimeError("transient DB blip on this row")
        seen.append(dep)
        return True

    pm._load_rescan_candidates = _candidates  # type: ignore[assignment]
    pm._maybe_auto_restart = _maybe  # type: ignore[assignment]

    restarted = await pm.rescan_for_restart()

    assert restarted == 1
    assert seen == [good], "the good candidate is still evaluated after the bad one raised"


@pytest.mark.asyncio
async def test_rescan_and_pel_recovery_race_yields_exactly_one_restart() -> None:
    """Re-scan + per-account PEL recovery racing the SAME deployment → exactly
    one restart, no double-count.

    Both paths call ``spawn``; the first reserves the active-row slot, the
    second hits the partial unique index and gets ``ALREADY_ACTIVE`` (ACK,
    idempotent — no new process, no extra attempt counted). We model that by
    having ``spawn`` succeed once and then report idempotent-no-op, and assert
    only ONE real respawn (and one counted attempt) results.
    """
    from msai.live_supervisor.fleet_router import _RestartContext

    dep_id = uuid4()
    proc = MagicMock()
    proc.id = uuid4()
    proc.auto_restart_paused = False
    proc.auto_restart_pause_reason = None
    proc.consecutive_respawn_failures = 0
    proc.last_restart_at = None
    ctx = _RestartContext(
        deployment_id=dep_id,
        deployment_slug="abcdef0123456789",
        account_id="DUP733214",
        gateway_session_key="sess-1",
        process=proc,
    )

    policy = MagicMock(spec=RestartPolicy)
    from msai.live_supervisor.restart_policy import RestartAction, RestartDecision

    policy.decide = MagicMock(return_value=RestartDecision(action=RestartAction.RESTART))
    policy.record_failure = MagicMock(return_value=None)

    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
        restart_policy=policy,
    )

    async def _load_ctx(_dep_id: UUID) -> _RestartContext | None:
        return ctx

    async def _halt(_account_id: str | None) -> bool:
        return False

    # The unique index serialises: first spawn reserves+spawns+counts
    # (process_started=True), the second is the idempotent ALREADY_ACTIVE
    # no-op (ACKed but process_started=False — no extra attempt counted).
    spawn_calls = {"n": 0}

    async def _spawn(**_kwargs: object) -> tuple[bool, bool]:
        spawn_calls["n"] += 1
        # First call genuinely starts a process; the second loses the unique-
        # index race → ALREADY_ACTIVE → ACK without a process (the count is
        # NOT incremented for the loser — prior-review P2 double-count fix).
        started = spawn_calls["n"] == 1
        return True, started

    pm._load_restart_context = _load_ctx  # type: ignore[assignment]
    pm._halt_active = _halt  # type: ignore[assignment]
    # FINDING 1 (P1): stub the operator-stop DB seam (default: no
    # stop requested) so _attempt_auto_restart doesn't hit the MagicMock db.
    async def _no_stop(_dep_id: object) -> bool:
        return False

    pm._operator_stop_requested = _no_stop  # type: ignore[assignment]
    pm.spawn_with_outcome = _spawn  # type: ignore[method-assign]

    # Both racing paths call the SAME decision seam.
    r1 = await pm._maybe_auto_restart(dep_id)
    r2 = await pm._maybe_auto_restart(dep_id)

    # The serialisation point is the unique index inside Phase A — the
    # decision seam itself doesn't add a distributed lock. The contract this
    # test pins: both attempts route through ``spawn_with_outcome`` (so the
    # index can serialise them), but EXACTLY ONE reports a genuine restart
    # (process_started); the idempotent loser does not (no double-count).
    assert r1 is True, "the winner reports a restart"
    assert r2 is False, "the idempotent loser does NOT report a restart (ALREADY_ACTIVE)"
    assert spawn_calls["n"] == 2, "both attempts route through spawn; the index dedupes downstream"


@pytest.mark.asyncio
async def test_rescan_noop_with_no_candidates() -> None:
    """An empty candidate set is a clean no-op (steady-state boot)."""
    pm = _make_pm()

    async def _candidates() -> list[UUID]:
        return []

    pm._load_rescan_candidates = _candidates  # type: ignore[assignment]
    pm._maybe_auto_restart = AsyncMock(return_value=True)  # type: ignore[attr-defined]

    restarted = await pm.rescan_for_restart()

    assert restarted == 0
    pm._maybe_auto_restart.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_rescan_off_when_no_policy_injected() -> None:
    """A FleetRouter built without a ``restart_policy`` does not re-scan —
    backward-compatible with the legacy/test wiring."""
    pm = FleetRouter(
        db=MagicMock(),
        redis=MagicMock(),
        spawn_target=lambda: None,
    )
    # No policy → re-scan is a no-op (returns 0), never touching the DB.
    restarted = await pm.rescan_for_restart()
    assert restarted == 0
