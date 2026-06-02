"""Unit tests for the bounded auto-restart guard (PR 2 / Task 3).

The ``RestartPolicy`` decides — purely from the T1 restart-authority
columns on a ``LiveNodeProcess`` row plus the current wall-clock — whether
the reaper should RESTART, wait (BACKOFF), or refuse (PAUSED). It does NOT
touch Redis (no lease) and carries no generation token: the F5 active-live
deploy gate is what prevents two-supervisor overlap, so the policy only
needs the durable per-row counter (survives a container recreate because
it lives on the DB row, not in supervisor memory).

Contract under test:

- ``decide`` is a pure read of the row + ``now`` — it returns the action
  and (for BACKOFF) the remaining delay.
- ``record_failure`` increments the consecutive-failure counter, stamps
  ``last_restart_at``, and at the ceiling sets ``auto_restart_paused=True``
  with a reason. It mutates the row in place (the caller commits).
- ``record_success`` resets the counter, clears the pause, and clears the
  reason.
- The ceiling and backoff schedule honor the module constants, which are
  env-overridable.
- Re-reading the same column values (simulating a fresh supervisor that
  reloaded the row from Postgres) yields the same decision — the counter
  survives a restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from msai.live_supervisor.restart_policy import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    BACKOFF_FACTOR,
    BACKOFF_JITTER_FRAC,
    MAX_RESTART_ATTEMPTS,
    RESTART_WINDOW_S,
    RestartAction,
    RestartDecision,
    RestartPolicy,
)
from msai.models import LiveNodeProcess


def _make_process(
    *,
    auto_restart_paused: bool = False,
    auto_restart_pause_reason: str | None = None,
    consecutive_respawn_failures: int = 0,
    last_restart_at: datetime | None = None,
) -> LiveNodeProcess:
    """Build a transient ``LiveNodeProcess`` carrying only the restart-
    authority columns the policy reads. The row is never persisted — the
    policy is a pure function of these four fields + ``now``."""
    proc = LiveNodeProcess()
    proc.auto_restart_paused = auto_restart_paused
    proc.auto_restart_pause_reason = auto_restart_pause_reason
    proc.consecutive_respawn_failures = consecutive_respawn_failures
    proc.last_restart_at = last_restart_at
    return proc


# --- Concrete-defaults sanity (plan §Task 3) ---------------------------------


def test_module_constants_match_plan_defaults() -> None:
    # Arrange / Act / Assert — the plan pins these exact values.
    assert MAX_RESTART_ATTEMPTS == 5
    assert RESTART_WINDOW_S == 1800
    assert BACKOFF_BASE_S == 10.0
    assert BACKOFF_FACTOR == 2.0
    assert BACKOFF_CAP_S == 300.0
    assert BACKOFF_JITTER_FRAC == 0.25


# --- PAUSED ------------------------------------------------------------------


def test_decide_returns_paused_when_already_paused() -> None:
    # Arrange — operator-or-policy already tripped the latch.
    proc = _make_process(auto_restart_paused=True, auto_restart_pause_reason="ceiling")

    # Act
    decision = RestartPolicy().decide(proc)

    # Assert
    assert decision.action is RestartAction.PAUSED
    assert decision.pause_reason == "ceiling"


def test_n_consecutive_failures_within_window_trip_pause() -> None:
    # Arrange — a healthy row that has just suffered its Nth failure.
    policy = RestartPolicy()
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    proc = _make_process()

    # Act — record_failure MAX_RESTART_ATTEMPTS times, each inside the window.
    for i in range(MAX_RESTART_ATTEMPTS):
        policy.record_failure(proc, now=now + timedelta(seconds=i))

    # Assert — the counter hit the ceiling and the latch is set with a reason.
    assert proc.consecutive_respawn_failures == MAX_RESTART_ATTEMPTS
    assert proc.auto_restart_paused is True
    assert proc.auto_restart_pause_reason  # non-empty operator-facing reason
    assert RestartPolicy().decide(proc, now=now).action is RestartAction.PAUSED


def test_failures_spread_beyond_window_do_not_trip_pause() -> None:
    # Arrange — failures spaced wider than the rolling window each reset the
    # clock, so the counter never accumulates to the ceiling.
    policy = RestartPolicy()
    base = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    proc = _make_process()

    # Act — each failure lands more than RESTART_WINDOW_S after the prior one.
    for i in range(MAX_RESTART_ATTEMPTS + 2):
        policy.record_failure(proc, now=base + timedelta(seconds=(RESTART_WINDOW_S + 1) * i))

    # Assert — never paused; the rolling window kept resetting the count to 1.
    assert proc.auto_restart_paused is False
    assert proc.consecutive_respawn_failures == 1


# --- BACKOFF -----------------------------------------------------------------


def test_backoff_grows_exponentially_within_jitter_bounds() -> None:
    # Arrange — after k failures the nominal backoff is base * factor**(k-1),
    # capped, then jittered by ±BACKOFF_JITTER_FRAC. decide() right after a
    # restart attempt must return BACKOFF with delay inside the jitter band.
    policy = RestartPolicy()
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)

    expected_nominal = [10.0, 20.0, 40.0, 80.0]  # base 10, factor 2
    for k, nominal in enumerate(expected_nominal, start=1):
        proc = _make_process(consecutive_respawn_failures=k, last_restart_at=now)
        # Act — sample many times; jitter must keep delay in [.75x, 1.25x].
        for _ in range(50):
            decision = policy.decide(proc, now=now)
            assert decision.action is RestartAction.BACKOFF
            assert decision.delay_s is not None
            lower = nominal * (1 - BACKOFF_JITTER_FRAC)
            upper = nominal * (1 + BACKOFF_JITTER_FRAC)
            assert lower <= decision.delay_s <= upper


def test_backoff_is_capped() -> None:
    # Arrange — a large failure count whose nominal backoff exceeds the cap.
    policy = RestartPolicy()
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    # MAX_RESTART_ATTEMPTS would PAUSE, so use a count below the ceiling that
    # still exceeds the cap nominally: cap is reached at the 6th attempt
    # (10→20→40→80→160→320>300). With a ceiling of 5 we force the cap by
    # temporarily raising the ceiling via a high-count row that is NOT paused.
    proc = _make_process(
        auto_restart_paused=False,
        consecutive_respawn_failures=MAX_RESTART_ATTEMPTS - 1,  # 4 → nominal 80
        last_restart_at=now,
    )
    # Sanity: 4 failures → nominal 80, under cap; bump to confirm cap clamps a
    # deliberately high count via the public helper.
    capped = policy._nominal_backoff_s(  # noqa: SLF001 — white-box assertion
        consecutive_respawn_failures=20
    )
    # Act / Assert
    assert capped == BACKOFF_CAP_S
    # And a real decide() at count 4 stays within its (uncapped) jitter band.
    decision = policy.decide(proc, now=now)
    assert decision.action is RestartAction.BACKOFF
    assert decision.delay_s is not None
    assert decision.delay_s <= 80.0 * (1 + BACKOFF_JITTER_FRAC)


def test_decide_returns_restart_after_backoff_window_elapsed() -> None:
    # Arrange — enough wall-clock has passed since the last attempt that even
    # the max-jittered backoff has elapsed → RESTART now.
    policy = RestartPolicy()
    last = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    proc = _make_process(consecutive_respawn_failures=2, last_restart_at=last)

    # Act — now is far past the largest possible jittered delay.
    now = last + timedelta(seconds=BACKOFF_CAP_S * 2)
    decision = policy.decide(proc, now=now)

    # Assert
    assert decision.action is RestartAction.RESTART
    assert decision.delay_s is None


def test_decide_restart_on_first_failure_with_no_last_restart() -> None:
    # Arrange — a fresh failure: counter is 0/never restarted. The first
    # auto-restart should fire immediately (no prior attempt to back off from).
    proc = _make_process(consecutive_respawn_failures=0, last_restart_at=None)

    # Act
    decision = RestartPolicy().decide(proc)

    # Assert
    assert decision.action is RestartAction.RESTART


# --- record_success resets ---------------------------------------------------


def test_record_success_resets_counter_and_clears_pause() -> None:
    # Arrange — a row that hit the ceiling and is paused.
    policy = RestartPolicy()
    proc = _make_process(
        auto_restart_paused=True,
        auto_restart_pause_reason="max respawn ceiling reached",
        consecutive_respawn_failures=MAX_RESTART_ATTEMPTS,
        last_restart_at=datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC),
    )

    # Act — the node reached is_running + reconciled.
    policy.record_success(proc)

    # Assert — fully reset so future failures start a fresh window.
    assert proc.consecutive_respawn_failures == 0
    assert proc.auto_restart_paused is False
    assert proc.auto_restart_pause_reason is None
    assert RestartPolicy().decide(proc).action is RestartAction.RESTART


# --- Counter survives a simulated supervisor restart -------------------------


def test_counter_survives_simulated_supervisor_restart() -> None:
    # Arrange — supervisor A records failures, then "crashes". The row is
    # persisted; a fresh supervisor B re-reads the SAME column values from
    # Postgres (simulated here by copying the four fields into a new row).
    policy = RestartPolicy()
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    proc_a = _make_process()
    for i in range(3):
        policy.record_failure(proc_a, now=now + timedelta(seconds=i))

    # Simulate the DB round-trip: B reloads the columns A persisted.
    proc_b = _make_process(
        auto_restart_paused=proc_a.auto_restart_paused,
        auto_restart_pause_reason=proc_a.auto_restart_pause_reason,
        consecutive_respawn_failures=proc_a.consecutive_respawn_failures,
        last_restart_at=proc_a.last_restart_at,
    )

    # Act — B's policy decides off the reloaded state, then records one more
    # failure; the count must continue from where A left off (3 → 4), NOT
    # reset to 1 (which a memory-only counter would do).
    decision = policy.decide(proc_b, now=proc_a.last_restart_at)
    assert decision.action is RestartAction.BACKOFF  # still inside the window
    policy.record_failure(proc_b, now=now + timedelta(seconds=4))

    # Assert — the durable counter carried across the restart.
    assert proc_b.consecutive_respawn_failures == 4


# --- env-overridable constants ----------------------------------------------


def test_constants_are_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    # The drill needs to tune the ceiling/backoff without code edits. The module
    # constants are computed at import via ``_env_int`` / ``_env_float`` (see
    # restart_policy.py), so we verify the override mechanism through those
    # helpers DIRECTLY — NOT via ``importlib.reload(rp)``.
    #
    # Why not reload: reloading restart_policy re-creates the ``RestartAction``
    # enum as a new class object, which poisons cross-module identity. fleet_router
    # imports ``RestartAction`` by value once and never reloads, so its
    # ``decision.action is RestartAction.PAUSED`` check (the auto-restart PAUSED
    # gate) would compare a reloaded enum member against the original and silently
    # fail in any test chained after this one — making a PAUSED deployment appear
    # to "restart". That is a real-money hazard masquerading as a flaky test, so
    # this test must not leak a reloaded module. Testing the helpers exercises the
    # exact code path the constants use, with zero global-state mutation.
    import msai.live_supervisor.restart_policy as rp

    monkeypatch.setenv("MSAI_RESTART_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("MSAI_RESTART_WINDOW_S", "60")
    monkeypatch.setenv("MSAI_RESTART_BACKOFF_BASE_S", "1.5")
    monkeypatch.setenv("MSAI_RESTART_BACKOFF_CAP_S", "9")

    # Override picked up (identical calls to the ones the module constants use).
    assert rp._env_int("MSAI_RESTART_MAX_ATTEMPTS", 5) == 2
    assert rp._env_int("MSAI_RESTART_WINDOW_S", 1800) == 60
    assert rp._env_float("MSAI_RESTART_BACKOFF_BASE_S", 10.0) == 1.5
    assert rp._env_float("MSAI_RESTART_BACKOFF_CAP_S", 300.0) == 9.0

    # A malformed override must fall back to the default — a typo in a drill env
    # var must never wedge the auto-restart brake (must not crash, must not zero).
    monkeypatch.setenv("MSAI_RESTART_MAX_ATTEMPTS", "not-an-int")
    assert rp._env_int("MSAI_RESTART_MAX_ATTEMPTS", 5) == 5


# --- RestartDecision shape ---------------------------------------------------


def test_restart_decision_is_immutable_value() -> None:
    # Arrange / Act
    decision = RestartDecision(action=RestartAction.BACKOFF, delay_s=12.5)

    # Assert — frozen dataclass: a value object the reaper can pass around.
    with pytest.raises((AttributeError, TypeError)):
        decision.delay_s = 99.0  # type: ignore[misc]
