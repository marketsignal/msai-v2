"""Dual pub/sub fanout (Phase 3 task 3.4 — v5 decision #4).

The :class:`ProjectionConsumer` translates each Nautilus
event into an :class:`InternalEvent` and then publishes to
TWO Redis pub/sub channels per deployment:

1. ``msai:live:state:{deployment_id}`` — state-update channel
   the :class:`StateApplier` task subscribes to via
   ``PSUBSCRIBE msai:live:state:*``. Every uvicorn worker
   gets every state update, regardless of which worker's
   consumer pulled the message from the stream. This is the
   v5 fix for Codex v4 P1 (single-worker state drift on
   multi-worker uvicorn).

2. ``msai:live:events:{deployment_id}`` — WebSocket fan-out
   channel the WebSocket handlers subscribe to and forward
   verbatim to clients.

Both channels carry the SAME serialized
``InternalEvent.model_dump_json()`` payload — the consumer
publishes once per channel. The split is purely about
who's listening: state vs WebSocket clients. The events
channel is the "verbatim feed for the UI"; the state
channel is the "every worker stays current" channel.

The consumer ACKs the Nautilus stream message ONLY after
BOTH publishes succeed. If either fails, the message stays
in the PEL and ``XAUTOCLAIM`` recovers it on the next
recovery pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis

    from msai.services.nautilus.projection.events import InternalEvent


STATE_CHANNEL_PREFIX = "msai:live:state:"
"""Channel prefix for state updates. The :class:`StateApplier`
subscribes via ``PSUBSCRIBE msai:live:state:*``."""

EVENTS_CHANNEL_PREFIX = "msai:live:events:"
"""Channel prefix for WebSocket fan-out. Per-deployment
WebSocket handlers subscribe to the specific channel
``msai:live:events:{deployment_id}``."""


def state_channel_for(deployment_id: str | object) -> str:
    """Build the state channel name for a deployment."""
    return f"{STATE_CHANNEL_PREFIX}{deployment_id}"


def events_channel_for(deployment_id: str | object) -> str:
    """Build the events channel name for a deployment."""
    return f"{EVENTS_CHANNEL_PREFIX}{deployment_id}"


class DualPublisher:
    """Publishes :class:`InternalEvent` instances to BOTH the
    state channel + the events channel. Returns a (state, events)
    tuple of subscriber counts so the consumer can log if a
    publish lands on zero subscribers (a sign that the
    StateApplier or WebSocket handlers haven't started yet).

    The publisher is intentionally a thin wrapper — the only
    logic is the channel-name derivation + the order of the
    two ``PUBLISH`` calls. The consumer owns the retry / ACK
    semantics around it.
    """

    def __init__(self, redis: AsyncRedis) -> None:
        self._redis = redis

    async def publish(self, event: InternalEvent) -> tuple[int, int]:
        """Publish to both channels. Returns
        ``(state_subscribers, events_subscribers)`` —
        Redis's PUBLISH return value is the count of
        clients that received the message.

        Raises if either ``PUBLISH`` raises. The consumer's
        try/except handles the failure by leaving the message
        in the PEL for ``XAUTOCLAIM`` recovery.
        """
        payload = event.model_dump_json().encode("utf-8")
        deployment_id = str(event.deployment_id)

        state_subs = await self._redis.publish(state_channel_for(deployment_id), payload)
        events_subs = await self._redis.publish(events_channel_for(deployment_id), payload)
        return (int(state_subs), int(events_subs))
