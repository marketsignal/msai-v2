"""StateApplier — per-worker pub/sub subscriber that feeds the
local :class:`ProjectionState` (Phase 3 task 3.4 — v5 fix for
Codex v4 P1).

The :class:`ProjectionConsumer` reads from the Nautilus
message bus stream via consumer groups — exactly ONE worker
processes each message. That solves at-least-once delivery,
but it means the OTHER uvicorn workers never see the state
update and their ``ProjectionState`` drifts out of sync.

The v5 fix: after the consumer publishes the translated event
to the state pub/sub channel, EVERY worker's
:class:`StateApplier` (subscribed via
``PSUBSCRIBE msai:live:state:*``) receives the event and
applies it to its OWN ``ProjectionState`` instance. The
consumer-group's exactly-once semantics still hold for the
stream pull; the pub/sub fanout is what makes the in-memory
state consistent across workers.

Lifecycle:

- Constructed at FastAPI startup with the worker's local
  ``ProjectionState`` and an async Redis client.
- ``run(stop_event)`` loops on ``pubsub.get_message()`` until
  the stop event is set.
- On shutdown, unsubscribes cleanly so Redis doesn't keep a
  stale subscriber registered.

Errors during pub/sub message processing are logged and the
loop continues — a malformed event must NOT crash the
StateApplier or the worker would drift permanently.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter, ValidationError

from msai.services.nautilus.projection.events import InternalEvent
from msai.services.nautilus.projection.fanout import STATE_CHANNEL_PREFIX

if TYPE_CHECKING:
    import asyncio

    from redis.asyncio import Redis as AsyncRedis

    from msai.services.nautilus.projection.projection_state import ProjectionState


log = logging.getLogger(__name__)


_INTERNAL_EVENT_ADAPTER: TypeAdapter[InternalEvent] = TypeAdapter(InternalEvent)
"""Pydantic TypeAdapter for the discriminated InternalEvent
union. Built once at module import time to avoid the per-event
construction cost on the hot pub/sub path."""


class StateApplier:
    """Per-worker background task that subscribes to the state
    pub/sub pattern and feeds events into the local
    :class:`ProjectionState`."""

    def __init__(self, redis: AsyncRedis, projection_state: ProjectionState) -> None:
        self._redis = redis
        self._state = projection_state

    async def run(self, stop_event: asyncio.Event) -> None:
        """Subscribe to ``msai:live:state:*`` and dispatch
        every message into ``self._state.apply``. Loops until
        ``stop_event`` is set; cleans up the pubsub
        subscription on the way out."""
        pubsub = self._redis.pubsub()
        try:
            await pubsub.psubscribe(f"{STATE_CHANNEL_PREFIX}*")
            log.info(
                "state_applier_started",
                extra={"pattern": f"{STATE_CHANNEL_PREFIX}*"},
            )

            while not stop_event.is_set():
                # Block up to 1 second waiting for a message;
                # the timeout lets us re-check ``stop_event``
                # without busy-looping.
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if msg is None:
                    continue

                try:
                    self._dispatch(msg)
                except Exception:  # noqa: BLE001
                    # Single bad message MUST NOT kill the
                    # applier — the worker would drift
                    # permanently. Log + continue.
                    log.exception(
                        "state_applier_dispatch_failed",
                        extra={"message": str(msg)[:500]},
                    )
        finally:
            try:
                await pubsub.punsubscribe(f"{STATE_CHANNEL_PREFIX}*")
                await pubsub.close()
            except Exception:  # noqa: BLE001
                log.exception("state_applier_pubsub_close_failed")

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Validate the pub/sub payload as an
        :class:`InternalEvent` and apply it to the local
        ``ProjectionState``. Pulled into a method so tests
        can drive it directly without standing up a real
        pubsub loop."""
        data = msg.get("data")
        if data is None:
            return
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        try:
            event = _INTERNAL_EVENT_ADAPTER.validate_json(data)
        except ValidationError:
            log.warning(
                "state_applier_event_validation_failed",
                extra={"raw": data[:500]},
            )
            return
        self._state.apply(event)
