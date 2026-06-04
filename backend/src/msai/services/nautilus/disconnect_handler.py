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
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from msai.core.halt_keys import (
    HALT_SET_BACKOFF_S,
    HALT_SET_MAX_ATTEMPTS,
    HALT_TTL_SECONDS,
    HALT_WRITE_LUA,
    HaltCause,
    fleet_halt_write_args,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from redis.asyncio import Redis as AsyncRedis

# Same latch the API's /kill-all writes — setting it from inside the trading
# subprocess triggers the same downstream behavior: supervisor blocks new
# starts, running deployments get a stop command, and the strategy mixin's
# defense-in-depth check refuses any new orders. ``_fire_halt`` writes it (plus
# the canonical cause) via ``HALT_WRITE_LUA``; see msai.core.halt_keys for the
# canonical helpers (fleet_halt_key, account_halt_key, halt_cause_key).


log = logging.getLogger(__name__)


# DEPRECATED (PR 1b T5): these ad-hoc keys are NO LONGER written. ``_fire_halt``
# now writes the canonical fleet cause via ``HALT_WRITE_LUA`` (same path the
# data-stale monitor uses). The constants are retained ONLY so Task 6's
# ``/resume`` transition-compat clear can delete any residue left by a pre-T5
# node still running across a deploy. Remove once that compat window closes.
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

    def _build_cause(self, detected_at: str) -> dict[str, object | None]:
        """Canonical fleet halt-cause JSON for an IB-disconnect halt.

        Mirrors the data-stale monitor's cause shape (see the
        :data:`~msai.core.halt_keys.HALT_WRITE_LUA` docstring) so an operator
        reads ONE consistent cause representation regardless of which safety
        layer latched the halt. ``account_id`` / ``node_id`` are ``None`` here —
        the disconnect handler is constructed with only the ``deployment_slug``
        (it watches one node's IB connection, not a per-account feed), so the
        slug carries the deployment identity and the ``source`` field records
        the handler that fired it.
        """
        return {
            "reason": HaltCause.IB_DISCONNECT.value,
            "account_id": None,
            "node_id": None,
            "deployment_id": self._deployment_slug,
            "detected_at": detected_at,
            "source": f"ib_disconnect_handler:{self._deployment_slug}",
        }

    async def _fire_halt(self) -> None:
        """Latch the fleet halt via the atomic Lua script and call the optional
        ``on_halt`` callback. The latch is the same one
        ``/api/v1/live/kill-all`` sets, so the supervisor and the strategy mixin
        both react to it identically.

        PR 1b T5: writes the CANONICAL fleet cause (:func:`halt_cause_key`) via
        the SHARED :data:`~msai.core.halt_keys.HALT_WRITE_LUA` script the
        data-stale monitor uses — one atomic round-trip writes the latch +
        ``:set_by`` / ``:set_at`` companions, sets the cause ONLY-IF-ABSENT
        (preserving a pre-existing manual ``/kill-all`` or data-stale cause), and
        LPUSH/LTRIMs the cause onto the capped history list. This replaces the
        prior ad-hoc ``:reason`` / ``:source`` SETs, which diverged from the
        canonical representation and were never cleared by ``/resume``.

        Codex batch 10 P2 fix (preserved): previously the write swallowed Redis
        errors and exited one-shot, leaving the platform fail-OPEN. The current
        behavior retries with exponential backoff up to
        ``_HALT_SET_MAX_ATTEMPTS``, logs critical on every failure, and fires the
        ``on_halt`` callback even if the writes never succeed (so a flatten hook
        runs regardless of Redis health).
        """
        detected_at = datetime.now(UTC).isoformat()
        cause_json = json.dumps(self._build_cause(detected_at))
        set_by = f"ib_disconnect_handler:{self._deployment_slug}"

        keys, argv = fleet_halt_write_args(
            set_by=set_by,
            set_at=detected_at,
            cause_json=cause_json,
            ttl_s=HALT_TTL_SECONDS,
        )

        success = False
        last_exc: Exception | None = None
        for attempt in range(HALT_SET_MAX_ATTEMPTS):
            try:
                await self._redis.eval(HALT_WRITE_LUA, len(keys), *keys, *argv)  # type: ignore[misc]
                success = True
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.critical(
                    "ib_disconnect_halt_set_failed",
                    extra={
                        "attempt": attempt + 1,
                        "max_attempts": HALT_SET_MAX_ATTEMPTS,
                        "deployment_slug": self._deployment_slug,
                    },
                    exc_info=exc,
                )
                if attempt + 1 < HALT_SET_MAX_ATTEMPTS:
                    backoff = HALT_SET_BACKOFF_S * (2**attempt)
                    await asyncio.sleep(backoff)

        if not success:
            log.critical(
                "ib_disconnect_halt_set_exhausted",
                extra={
                    "deployment_slug": self._deployment_slug,
                    "last_error": str(last_exc),
                },
            )

        # on_halt FIRST — this is the fail-closed local shutdown path.
        # Must fire before the email alert because SMTP can be slow/hang
        # and we must not delay the node shutdown. Codex review P1 fix.
        if self._on_halt is not None:
            try:
                await self._on_halt()
            except Exception:  # noqa: BLE001
                log.exception("ib_disconnect_on_halt_callback_failed")

        # Best-effort email alert — after the node is already stopping.
        try:
            from msai.services.alerting import AlertService

            await AlertService().alert_ib_disconnect()
        except Exception:  # noqa: BLE001
            log.debug("ib_disconnect_alert_failed")
