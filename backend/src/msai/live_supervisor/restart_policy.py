"""Bounded auto-restart guard for per-account supervisor processes (PR 2 / T3).

A real-money trading node that crash-loops must NOT be hammered back to
life forever: an infinite respawn loop pounds a recovering shared IB
gateway and masks a genuine, operator-actionable fault. This module is the
durable, DB-backed brake on that loop.

Design (council: *bounded, no infinite loop; no lease*)
-------------------------------------------------------

- **DB-backed counter.** The state lives on the T1 restart-authority
  columns of the ``live_node_processes`` row
  (``auto_restart_paused``, ``auto_restart_pause_reason``,
  ``consecutive_respawn_failures``, ``last_restart_at``) — NOT in
  supervisor memory. So the ceiling and backoff survive a container
  recreate: a freshly-started supervisor reloads the row and continues the
  same streak rather than resetting to zero.

- **No Redis lease, no generation token.** Opt 4 (refined): the F5
  active-live deploy gate prevents two supervisors from overlapping, so
  the policy needs no fencing of its own. It is a pure function of the row
  state plus the current wall-clock.

Decision surface
----------------

``RestartPolicy.decide(process, *, now=...) -> RestartDecision``:

- ``PAUSED``  — ``auto_restart_paused`` is set; the reaper must NOT respawn.
- ``BACKOFF`` — a restart is allowed eventually, but not yet: ``delay_s`` is
  the remaining wait (jittered) before the next attempt.
- ``RESTART`` — respawn now.

Mutators (the caller owns the transaction / commit):

- ``record_failure(process, *, now=...)`` — a respawn attempt failed
  (or the process crashed again). Increments the consecutive-failure
  counter inside the rolling :data:`RESTART_WINDOW_S` window, stamps
  ``last_restart_at``, and — once the streak reaches
  :data:`MAX_RESTART_ATTEMPTS` — sets ``auto_restart_paused=True`` with an
  operator-facing reason.
- ``record_success(process)`` — the node reached ``is_running`` and
  reconciled. Resets the counter and clears the pause + reason.

Concrete defaults (plan §Task 3; resolves PRD §7 open question)
---------------------------------------------------------------

``MAX_RESTART_ATTEMPTS = 5`` consecutive failures within
``RESTART_WINDOW_S = 1800`` (30 min) trips ``PAUSED``. Backoff is
exponential: ``base = 10s``, ``factor = 2``, ``cap = 300s`` (5 min) —
i.e. 10 → 20 → 40 → 80 → 160 → 300 — with ±25% jitter. Conservative on
purpose: the 10s floor sits above ``reconciliation_startup_delay_secs=10``
so a respawn never races a gateway that is still reconciling.

All five tunables are module constants overridable via environment
variables (``MSAI_RESTART_*``) so the operator drill can tighten them
without a code change.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from msai.models import LiveNodeProcess


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int from the environment, falling back to default.

    A malformed value falls back to the default rather than crashing the
    supervisor on startup — a typo in a tuning env var must never wedge the
    auto-restart brake.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to default on error."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --- Tunable policy constants (env-overridable) ------------------------------

MAX_RESTART_ATTEMPTS: int = _env_int("MSAI_RESTART_MAX_ATTEMPTS", 5)
"""Consecutive respawn failures within :data:`RESTART_WINDOW_S` that trip
``auto_restart_paused``. Reaching this count means a human must look."""

RESTART_WINDOW_S: int = _env_int("MSAI_RESTART_WINDOW_S", 1800)
"""Rolling window (seconds). A failure landing more than this many seconds
after the previous attempt breaks the streak and resets the counter to 1."""

BACKOFF_BASE_S: float = _env_float("MSAI_RESTART_BACKOFF_BASE_S", 10.0)
"""Backoff for the first retry. The 10s floor sits above the node's
``reconciliation_startup_delay_secs`` so a respawn never races reconcile."""

BACKOFF_FACTOR: float = _env_float("MSAI_RESTART_BACKOFF_FACTOR", 2.0)
"""Exponential growth factor: delay = base * factor**(attempts - 1)."""

BACKOFF_CAP_S: float = _env_float("MSAI_RESTART_BACKOFF_CAP_S", 300.0)
"""Hard ceiling on the (pre-jitter) backoff so it never grows unbounded."""

BACKOFF_JITTER_FRAC: float = _env_float("MSAI_RESTART_BACKOFF_JITTER_FRAC", 0.25)
"""Symmetric jitter fraction: the actual delay is uniform in
``[nominal*(1-frac), nominal*(1+frac)]`` to de-synchronize fleet-wide
respawns hammering a shared gateway."""

_PAUSE_REASON_CEILING = "max respawn ceiling reached"
"""Operator-facing reason written to ``auto_restart_pause_reason`` when the
consecutive-failure ceiling trips the pause."""


class RestartAction(Enum):
    """What the reaper should do with a failed process."""

    RESTART = "restart"
    """Respawn now — no outstanding backoff and the ceiling is not reached."""

    BACKOFF = "backoff"
    """A restart is allowed, but not yet — wait ``RestartDecision.delay_s``."""

    PAUSED = "paused"
    """Auto-restart is latched off; an operator must intervene."""


@dataclass(frozen=True, slots=True)
class RestartDecision:
    """Immutable verdict returned by :meth:`RestartPolicy.decide`.

    ``delay_s`` is populated only for :attr:`RestartAction.BACKOFF` (the
    remaining wait in seconds). ``pause_reason`` is populated only for
    :attr:`RestartAction.PAUSED`.
    """

    action: RestartAction
    delay_s: float | None = None
    pause_reason: str | None = None


class RestartPolicy:
    """Bounded auto-restart brake driven entirely by the DB row state.

    Stateless aside from the injected ``rng`` (for deterministic jitter in
    tests) — all durable state lives on the ``LiveNodeProcess`` row.
    """

    def __init__(self, *, rng: random.Random | None = None) -> None:
        # A private Random so jitter is injectable/deterministic in tests
        # without perturbing the global random stream.
        self._rng = rng or random.Random()

    # -- read -----------------------------------------------------------------

    def decide(self, process: LiveNodeProcess, *, now: datetime | None = None) -> RestartDecision:
        """Decide whether to RESTART, BACKOFF, or stay PAUSED.

        Pure read of the row's restart-authority columns plus ``now``. Does
        not mutate the row.
        """
        if process.auto_restart_paused:
            return RestartDecision(
                action=RestartAction.PAUSED,
                pause_reason=process.auto_restart_pause_reason,
            )

        now = now or datetime.now(UTC)
        failures = process.consecutive_respawn_failures
        last = process.last_restart_at

        # First-ever failure (or no prior attempt recorded): restart at once;
        # there is no prior attempt to back off from.
        if failures <= 0 or last is None:
            return RestartDecision(action=RestartAction.RESTART)

        elapsed = (now - _as_utc(last)).total_seconds()
        delay = self._jittered_backoff_s(failures)
        if elapsed >= delay:
            return RestartDecision(action=RestartAction.RESTART)
        return RestartDecision(action=RestartAction.BACKOFF, delay_s=delay - elapsed)

    # -- mutators (caller owns the commit) ------------------------------------

    def record_failure(self, process: LiveNodeProcess, *, now: datetime | None = None) -> None:
        """Record a failed respawn attempt on the row.

        Increments the consecutive-failure counter (resetting to 1 if the
        prior attempt fell outside the rolling window), stamps
        ``last_restart_at``, and trips ``auto_restart_paused`` once the
        streak reaches :data:`MAX_RESTART_ATTEMPTS`.
        """
        now = now or datetime.now(UTC)
        last = process.last_restart_at

        within_window = (
            last is not None and (now - _as_utc(last)).total_seconds() <= RESTART_WINDOW_S
        )
        if within_window:
            process.consecutive_respawn_failures += 1
        else:
            # Streak broken (or first failure) — start a fresh window.
            process.consecutive_respawn_failures = 1

        process.last_restart_at = now

        if process.consecutive_respawn_failures >= MAX_RESTART_ATTEMPTS:
            process.auto_restart_paused = True
            process.auto_restart_pause_reason = _PAUSE_REASON_CEILING

    def record_success(self, process: LiveNodeProcess) -> None:
        """Record a healthy restart (node reached ``is_running`` + reconciled).

        Resets the counter and clears the pause latch + reason so a future
        failure starts a fresh rolling window.
        """
        process.consecutive_respawn_failures = 0
        process.auto_restart_paused = False
        process.auto_restart_pause_reason = None

    # -- backoff math ---------------------------------------------------------

    def _nominal_backoff_s(self, *, consecutive_respawn_failures: int) -> float:
        """Pre-jitter backoff for the given failure count, clamped to the cap.

        delay = base * factor**(attempts - 1), capped at
        :data:`BACKOFF_CAP_S`. A count <= 1 yields the base delay.
        """
        attempts = max(consecutive_respawn_failures, 1)
        nominal = BACKOFF_BASE_S * (BACKOFF_FACTOR ** (attempts - 1))
        return min(nominal, BACKOFF_CAP_S)

    def _jittered_backoff_s(self, consecutive_respawn_failures: int) -> float:
        """Nominal backoff with symmetric ±:data:`BACKOFF_JITTER_FRAC` jitter."""
        nominal = self._nominal_backoff_s(consecutive_respawn_failures=consecutive_respawn_failures)
        spread = nominal * BACKOFF_JITTER_FRAC
        return nominal + self._rng.uniform(-spread, spread)


def _as_utc(value: datetime) -> datetime:
    """Coerce a possibly-naive datetime to UTC.

    Postgres ``TIMESTAMPTZ`` round-trips as tz-aware, but a naive value
    (e.g. a test fixture) is treated as UTC so the subtraction never raises
    ``can't subtract offset-naive and offset-aware datetimes``.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
