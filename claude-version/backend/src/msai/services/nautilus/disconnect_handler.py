"""IB disconnect handler with halt-on-extended-disconnect
(Phase 4 task 4.2).

A background task that runs INSIDE the live trading subprocess
and watches Nautilus's connection state. The contract:

1. On the first disconnect, start a timer.
2. If the broker reconnects within ``disconnect_grace_seconds``
   (default 120 s), cancel the timer and log "transient
   disconnect" — no halt.
3. If the grace window expires while still disconnected,
   trigger the kill switch by setting ``msai:risk:halt`` in
   Redis with ``reason='ib_disconnect'``. The supervisor's
   layer 3 (push-based stop, Phase 3 task 3.9) will see the
   flag and tear down running deployments. The strategy's
   ``manage_stop=True`` flatten loop runs as part of the
   normal stop sequence.
4. Stay halted until an operator manually calls
   ``/api/v1/live/resume`` — there is **NO** auto-resume on
   reconnect, even after a clean reconnect to IB. This
   matches Codex's "remain paused until warm" wording in the
   v9 plan: a long IB outage may have left the broker side
   in an inconsistent state and needs human verification
   before re-deploying.

Why this lives in a separate module: it's a pure async loop
with one I/O dependency (Redis) and one input (a
``ConnectionStateProvider`` callable that returns ``True`` if
IB is currently connected). Both are injected so unit tests
can drive the loop deterministically without standing up a
real IB Gateway or a Nautilus runtime.

Rationale for grace seconds:
- IB Gateway routinely emits brief disconnects during the
  daily reset window (~ 23:45 ET) that auto-recover within
  30 seconds.
- A real network outage that lasts longer than 2 minutes is
  almost always a sign that orders aren't getting through.

The 120 s default is the same value used by the LiveCommandBus
PEL recovery threshold, so a single number governs both
"slow" recovery paths.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from redis.asyncio import Redis as AsyncRedis


log = logging.getLogger(__name__)


_HALT_KEY = "msai:risk:halt"
"""Same key the API's ``/kill-all`` writes (api/live.py:96).
Setting it from inside the trading subprocess triggers the
exact same downstream behavior as a manual kill-all: the
supervisor blocks new starts, running deployments get a stop
command, and the strategy mixin's defense-in-depth check
refuses any new orders."""

_HALT_REASON_KEY = "msai:risk:halt:reason"
_HALT_SOURCE_KEY = "msai:risk:halt:source"

DEFAULT_GRACE_SECONDS = 120.0
"""Wait this long for the broker to reconnect before halting.
2 minutes covers the IB Gateway nightly reset window
(typically 30-60s). Anything longer is treated as a real
outage that warrants stopping trading."""

DEFAULT_POLL_INTERVAL_S = 1.0
"""How often the loop checks the connection state. 1 s is
fast enough that we react inside the grace window even at
the boundary, slow enough that the loop is essentially
free."""

_HALT_TTL_SECONDS = 86400
"""Same 24h TTL the API's /kill-all uses, so the disconnect
halt has the same expiry as a manual kill switch — operators
get the same recovery window either way."""

_HALT_SET_MAX_ATTEMPTS = 5
"""Codex batch 10 P2: how many times ``_fire_halt`` retries
the Redis SET sequence before giving up. With exponential
backoff starting at 100ms and doubling, the total wait is
~3.1 seconds — long enough to ride out a transient Redis
blip without delaying the halt past the point where the
strategy could send another order."""

_HALT_SET_BACKOFF_S = 0.1
"""Initial backoff between halt-set retry attempts. Doubles
on each attempt: 100ms, 200ms, 400ms, 800ms, 1.6s."""


class IBDisconnectHandler:
    """Background task that watches IB connection state and
    triggers the kill switch when an outage exceeds the grace
    window.

    Lifecycle:

    - Constructed at subprocess startup with the deployment's
      Redis client and a connection-state provider (callable
      returning ``True`` if connected).
    - ``run(stop_event)`` loops until ``stop_event`` is set
      (clean shutdown) or until the loop triggers a halt.
    - Halt is one-shot: once the loop fires the kill switch
      it returns. The supervisor's stop command will then
      cancel ``stop_event`` from the outside.
    """

    def __init__(
        self,
        *,
        redis: AsyncRedis,
        is_connected: Callable[[], bool],
        deployment_slug: str,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        on_halt: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._redis = redis
        self._is_connected = is_connected
        self._deployment_slug = deployment_slug
        self._grace_seconds = grace_seconds
        self._poll_interval_s = poll_interval_s
        self._on_halt = on_halt
        self._disconnected_since: float | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop. Runs until ``stop_event`` is set or until
        the handler fires a halt and exits."""
        loop = asyncio.get_event_loop()
        while not stop_event.is_set():
            connected = self._safe_check_connection()
            now = loop.time()

            if connected:
                if self._disconnected_since is not None:
                    elapsed = now - self._disconnected_since
                    log.info(
                        "ib_reconnect_within_grace",
                        extra={
                            "deployment_slug": self._deployment_slug,
                            "elapsed_s": elapsed,
                        },
                    )
                    self._disconnected_since = None
            else:
                if self._disconnected_since is None:
                    self._disconnected_since = now
                    log.warning(
                        "ib_disconnect_observed",
                        extra={"deployment_slug": self._deployment_slug},
                    )
                elif now - self._disconnected_since >= self._grace_seconds:
                    elapsed = now - self._disconnected_since
                    log.critical(
                        "ib_disconnect_grace_exceeded",
                        extra={
                            "deployment_slug": self._deployment_slug,
                            "elapsed_s": elapsed,
                            "grace_s": self._grace_seconds,
                        },
                    )
                    from msai.services.observability.trading_metrics import IB_DISCONNECTS
                    IB_DISCONNECTS.inc()
                    await self._fire_halt()
                    return  # one-shot

            await self._sleep_or_stop(stop_event)

    def _safe_check_connection(self) -> bool:
        """Wrap the caller-provided connection check in a try
        so a transient probe error doesn't crash the loop.
        Treat exceptions as "still disconnected" — fail
        closed."""
        try:
            return bool(self._is_connected())
        except Exception:  # noqa: BLE001
            log.exception("ib_connection_check_failed")
            return False

    async def _sleep_or_stop(self, stop_event: asyncio.Event) -> None:
        """Sleep up to ``poll_interval_s`` but wake up early
        if ``stop_event`` is set so a clean shutdown isn't
        delayed by the poll cadence."""
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_s)
        except TimeoutError:
            return

    async def _fire_halt(self) -> None:
        """Set the persistent halt flag in Redis and call the
        optional ``on_halt`` callback. The flag is the same
        one ``/api/v1/live/kill-all`` sets, so the supervisor
        and the strategy mixin both react to it identically.

        Codex batch 10 P2 fix: previously this swallowed
        Redis write errors and exited one-shot, leaving the
        platform in a fail-OPEN state where an extended IB
        outage produced NO halt signal. The new behavior:
        retry with exponential backoff up to
        ``_HALT_SET_MAX_ATTEMPTS``, log critical on every
        failure, and the on_halt callback still fires even
        if the Redis writes never succeed (so a flatten hook
        runs regardless of Redis health).
        """
        success = False
        last_exc: Exception | None = None
        for attempt in range(_HALT_SET_MAX_ATTEMPTS):
            try:
                await self._redis.set(_HALT_KEY, "true", ex=_HALT_TTL_SECONDS)
                await self._redis.set(_HALT_REASON_KEY, "ib_disconnect", ex=_HALT_TTL_SECONDS)
                await self._redis.set(
                    _HALT_SOURCE_KEY,
                    f"ib_disconnect_handler:{self._deployment_slug}",
                    ex=_HALT_TTL_SECONDS,
                )
                success = True
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.critical(
                    "ib_disconnect_halt_set_failed",
                    extra={
                        "attempt": attempt + 1,
                        "max_attempts": _HALT_SET_MAX_ATTEMPTS,
                        "deployment_slug": self._deployment_slug,
                    },
                    exc_info=exc,
                )
                if attempt + 1 < _HALT_SET_MAX_ATTEMPTS:
                    backoff = _HALT_SET_BACKOFF_S * (2**attempt)
                    await asyncio.sleep(backoff)

        if not success:
            log.critical(
                "ib_disconnect_halt_set_exhausted",
                extra={
                    "deployment_slug": self._deployment_slug,
                    "last_error": str(last_exc),
                },
            )

        # Best-effort email alert for extended IB disconnects.
        try:
            from msai.services.alerting import AlertService
            await AlertService().alert_ib_disconnect()
        except Exception:  # noqa: BLE001
            log.debug("ib_disconnect_alert_failed")

        if self._on_halt is not None:
            try:
                await self._on_halt()
            except Exception:  # noqa: BLE001
                log.exception("ib_disconnect_on_halt_callback_failed")
