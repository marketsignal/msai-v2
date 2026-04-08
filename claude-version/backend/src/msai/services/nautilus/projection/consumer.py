"""ProjectionConsumer — Redis Streams consumer + dual fanout +
PEL recovery + DLQ (Phase 3 task 3.4).

Reads the Nautilus message bus stream(s) for every active
deployment via Redis consumer groups, translates each event
to the internal schema, and publishes to the dual pub/sub
fanout. Implements at-least-once delivery via the standard
``XREADGROUP`` + ``XACK`` pattern, plus ``XAUTOCLAIM`` PEL
recovery for crashed workers and a DLQ for poison messages.

This module mirrors the :class:`LiveCommandBus` consumer
pattern from Task 1.6 — same recovery / DLQ approach,
adapted to read from Nautilus's own stream shape rather than
the supervisor's command stream.

Lifecycle:

1. ``run(stop_event)`` runs forever (per uvicorn worker).
2. On startup, scans the registry for active streams and
   joins the consumer group on each via ``XGROUP CREATE
   MKSTREAM`` (idempotent — ignores ``BUSYGROUP``).
3. Every loop iteration:
   - Reads up to ``COUNT`` entries via ``XREADGROUP BLOCK 5000``
   - For each entry: deserialize → translate → publish
     dual fanout → ``XACK``
   - Periodically (every ``recovery_interval_s``) runs
     ``XAUTOCLAIM`` to reclaim entries idle longer than
     ``min_idle_ms``
   - Entries reaching ``max_delivery_attempts`` are moved
     to the DLQ stream and ``XACK``ed on the primary
4. On shutdown, ``XGROUP DELCONSUMER`` so the consumer name
   is freed for the next worker startup.

Each uvicorn worker uses a UNIQUE consumer name within the
shared group so the same entry is delivered to exactly one
worker.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import TYPE_CHECKING, Any

from msgspec import msgpack

from msai.services.nautilus.projection.translator import translate

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

    from redis.asyncio import Redis as AsyncRedis

    from msai.services.nautilus.projection.fanout import DualPublisher
    from msai.services.nautilus.projection.registry import StreamRegistry


log = logging.getLogger(__name__)


CONSUMER_GROUP = "msai-projection"
"""Single consumer group across all uvicorn workers — Redis
delivers each entry to exactly one worker."""

DLQ_STREAM_PREFIX = "msai:live:events:dlq:"
"""DLQ stream key per deployment. Poison messages land here
with the original payload + a ``dlq_reason`` field."""

DEFAULT_BLOCK_MS = 5000
"""``XREADGROUP BLOCK`` timeout — short enough to re-check
``stop_event`` and re-scan the registry."""

DEFAULT_COUNT = 100
"""Max entries per ``XREADGROUP`` call."""

DEFAULT_MIN_IDLE_MS = 30_000
"""Entries idle longer than this are reclaimed via
``XAUTOCLAIM`` (Codex v3 P0)."""

DEFAULT_RECOVERY_INTERVAL_S = 30.0
"""How often to run the ``XAUTOCLAIM`` recovery sweep."""

DEFAULT_MAX_DELIVERY_ATTEMPTS = 5
"""Entries reaching this delivery count are routed to the DLQ
(Codex v4 P2 — same threshold as the LiveCommandBus DLQ)."""


def default_consumer_name() -> str:
    """Build a unique consumer name for this uvicorn worker
    so the same entry is delivered to exactly one worker.
    Format: ``projection-{hostname}-{pid}``."""
    return f"projection-{socket.gethostname()}-{os.getpid()}"


class ProjectionConsumer:
    """Reads Nautilus message bus streams via consumer groups
    and fans out to the dual pub/sub channels."""

    def __init__(
        self,
        *,
        redis: AsyncRedis,
        registry: StreamRegistry,
        publisher: DualPublisher,
        consumer_name: str | None = None,
        block_ms: int = DEFAULT_BLOCK_MS,
        count: int = DEFAULT_COUNT,
        min_idle_ms: int = DEFAULT_MIN_IDLE_MS,
        recovery_interval_s: float = DEFAULT_RECOVERY_INTERVAL_S,
        max_delivery_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
        on_alert: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._redis = redis
        self._registry = registry
        self._publisher = publisher
        self._consumer_name = consumer_name or default_consumer_name()
        self._block_ms = block_ms
        self._count = count
        self._min_idle_ms = min_idle_ms
        self._recovery_interval_s = recovery_interval_s
        self._max_delivery_attempts = max_delivery_attempts
        self._on_alert = on_alert
        self._known_streams: set[str] = set()
        self._last_recovery: float = 0.0

    # ------------------------------------------------------------------
    # Group management
    # ------------------------------------------------------------------

    async def ensure_group(self, stream: str) -> None:
        """Idempotently create the consumer group on
        ``stream``. Swallows ``BUSYGROUP`` if the group
        already exists."""
        try:
            await self._redis.xgroup_create(
                name=stream,
                groupname=CONSUMER_GROUP,
                id="0",
                mkstream=True,
            )
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" in str(exc):
                return
            raise

    async def _refresh_known_streams(self) -> list[str]:
        """Walk the registry and ensure we have a consumer
        group on every active stream. Returns the current
        active list so the read loop can iterate it."""
        active = list(self._registry.active_streams().values())
        for stream in active:
            if stream not in self._known_streams:
                await self.ensure_group(stream)
                self._known_streams.add(stream)
                log.info("projection_stream_joined", extra={"stream": stream})
        return active

    # ------------------------------------------------------------------
    # Read + dispatch
    # ------------------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop. Runs until ``stop_event`` is set."""
        while not stop_event.is_set():
            try:
                streams = await self._refresh_known_streams()
                if not streams:
                    await asyncio.sleep(1.0)
                    continue

                await self._read_once(streams)
                await self._maybe_recover()
            except Exception:  # noqa: BLE001
                log.exception("projection_consumer_loop_iteration_failed")
                await asyncio.sleep(1.0)

    async def _read_once(self, streams: list[str]) -> None:
        """One ``XREADGROUP`` call across every active stream.
        Dispatches each entry to :meth:`_dispatch_entry`."""
        # XREADGROUP needs ``streams={stream: ">"}`` for
        # "new entries since last consumer position".
        stream_map: dict[bytes | str | memoryview[int], int | bytes | str | memoryview[int]] = (
            dict.fromkeys(streams, ">")
        )
        try:
            response = await self._redis.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=self._consumer_name,
                streams=stream_map,
                count=self._count,
                block=self._block_ms,
            )
        except Exception:  # noqa: BLE001
            log.exception("projection_consumer_xreadgroup_failed")
            await asyncio.sleep(1.0)
            return

        if not response:
            return

        for stream_name, entries in response:
            stream_str = stream_name.decode() if isinstance(stream_name, bytes) else stream_name
            for entry_id, fields in entries:
                entry_id_str = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                await self._dispatch_entry(stream_str, entry_id_str, fields)

    async def _dispatch_entry(self, stream: str, entry_id: str, fields: dict[Any, Any]) -> None:
        """Translate one Nautilus message bus entry, publish to
        the dual fanout, and ACK on success. On failure,
        leave the entry in the PEL — recovery will retry it,
        and the DLQ catches poison messages at the
        ``max_delivery_attempts`` threshold."""
        try:
            payload = self._extract_payload(fields)
            topic = self._extract_topic(fields)
            event_dict = msgpack.decode(payload) if isinstance(payload, bytes) else payload
            if not isinstance(event_dict, dict):
                log.warning(
                    "projection_event_payload_not_dict",
                    extra={"stream": stream, "entry_id": entry_id},
                )
                await self._ack(stream, entry_id)
                return

            deployment_id = self._resolve_deployment_id_from_stream(stream)
            if deployment_id is None:
                log.warning(
                    "projection_no_deployment_for_stream",
                    extra={"stream": stream, "entry_id": entry_id},
                )
                await self._ack(stream, entry_id)
                return

            internal = translate(
                topic=topic,
                event_dict=event_dict,
                deployment_id=deployment_id,
            )
            if internal is None:
                # Unrouted topic — ACK and move on so the
                # PEL doesn't accumulate.
                await self._ack(stream, entry_id)
                return

            await self._publisher.publish(internal)
            await self._ack(stream, entry_id)
        except Exception:  # noqa: BLE001
            log.exception(
                "projection_dispatch_failed",
                extra={"stream": stream, "entry_id": entry_id},
            )
            # NO ACK — message stays in PEL for recovery / DLQ.

    @staticmethod
    def _extract_payload(fields: dict[Any, Any]) -> bytes | dict[Any, Any]:
        """Pull the message-bus payload bytes out of the entry
        fields. Nautilus's ``MessageBusConfig`` writes the
        message under the ``payload`` field."""
        payload = fields.get(b"payload") or fields.get("payload")
        return payload  # type: ignore[return-value]

    @staticmethod
    def _extract_topic(fields: dict[Any, Any]) -> str:
        topic = fields.get(b"topic") or fields.get("topic", b"")
        if isinstance(topic, bytes):
            topic = topic.decode("utf-8")
        return str(topic)

    def _resolve_deployment_id_from_stream(self, stream: str) -> UUID | None:
        """Streams are named ``trader-MSAI-{slug}-stream``
        (Nautilus's ``MessageBusConfig`` shape). Pull the
        slug, look up the deployment_id."""
        # Strip the leading "trader-MSAI-" and trailing "-stream"
        if not stream.startswith("trader-MSAI-") or not stream.endswith("-stream"):
            return None
        slug = stream[len("trader-MSAI-") : -len("-stream")]
        return self._registry.deployment_id_for_slug(slug)

    async def _ack(self, stream: str, entry_id: str) -> None:
        try:
            await self._redis.xack(stream, CONSUMER_GROUP, entry_id)
        except Exception:  # noqa: BLE001
            log.exception(
                "projection_xack_failed",
                extra={"stream": stream, "entry_id": entry_id},
            )

    # ------------------------------------------------------------------
    # PEL recovery + DLQ
    # ------------------------------------------------------------------

    async def _maybe_recover(self) -> None:
        """Run ``XAUTOCLAIM`` on every active stream if it's
        been longer than ``recovery_interval_s`` since the
        last sweep."""
        loop = asyncio.get_event_loop()
        now = loop.time()
        if now - self._last_recovery < self._recovery_interval_s:
            return
        self._last_recovery = now

        for stream in list(self._known_streams):
            try:
                await self._recover_pending_for_stream(stream)
            except Exception:  # noqa: BLE001
                log.exception(
                    "projection_recovery_failed",
                    extra={"stream": stream},
                )

    async def _recover_pending_for_stream(self, stream: str) -> None:
        """Reclaim idle entries via ``XAUTOCLAIM`` and either
        re-process them or route to the DLQ if they've hit
        ``max_delivery_attempts``."""
        cursor = "0-0"
        while True:
            try:
                response = await self._redis.xautoclaim(
                    name=stream,
                    groupname=CONSUMER_GROUP,
                    consumername=self._consumer_name,
                    min_idle_time=self._min_idle_ms,
                    start_id=cursor,
                    count=100,
                )
            except Exception:  # noqa: BLE001
                log.exception("projection_xautoclaim_failed", extra={"stream": stream})
                return

            if not response:
                return
            next_cursor, claimed = response[0], response[1]
            cursor_str = next_cursor.decode() if isinstance(next_cursor, bytes) else next_cursor
            if not claimed:
                return

            for entry_id, fields in claimed:
                entry_id_str = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                # Check delivery count via XPENDING
                attempts = await self._delivery_attempts(stream, entry_id_str)
                if attempts >= self._max_delivery_attempts:
                    await self._move_to_dlq(stream, entry_id_str, fields)
                else:
                    await self._dispatch_entry(stream, entry_id_str, fields)

            if cursor_str == "0-0":
                return
            cursor = cursor_str

    async def _delivery_attempts(self, stream: str, entry_id: str) -> int:
        try:
            pending = await self._redis.xpending_range(
                name=stream,
                groupname=CONSUMER_GROUP,
                min=entry_id,
                max=entry_id,
                count=1,
            )
            if not pending:
                return 0
            entry = pending[0]
            # ``xpending_range`` returns a list of dicts with
            # ``times_delivered`` (or a tuple in older
            # redis-py — handle both).
            if isinstance(entry, dict):
                return int(entry.get("times_delivered", 0))
            return int(entry[3])  # legacy tuple shape
        except Exception:  # noqa: BLE001
            log.exception("projection_xpending_failed", extra={"stream": stream})
            return 0

    async def _move_to_dlq(self, stream: str, entry_id: str, fields: dict[Any, Any]) -> None:
        """Route a poison message to the DLQ stream and ACK
        on the primary so it stops being redelivered."""
        # Pull the deployment_id out of the stream name to
        # build the DLQ stream key.
        deployment_id = self._resolve_deployment_id_from_stream(stream)
        dlq_stream = f"{DLQ_STREAM_PREFIX}{deployment_id}"

        # Redis xadd expects str-or-bytes-or-numeric values; coerce
        # any non-supported types to their str() form so the xadd
        # call type-checks under mypy --strict.
        dlq_fields: dict[bytes | str, bytes | str] = {}
        for k, v in fields.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            value = v if isinstance(v, (bytes, str)) else str(v)
            dlq_fields[key] = value
        dlq_fields["dlq_reason"] = "max_delivery_attempts_exceeded"
        dlq_fields["original_entry_id"] = entry_id
        dlq_fields["original_stream"] = stream

        try:
            await self._redis.xadd(dlq_stream, dlq_fields)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            log.exception("projection_dlq_xadd_failed", extra={"stream": stream})
            return

        await self._ack(stream, entry_id)
        log.warning(
            "projection_message_moved_to_dlq",
            extra={
                "stream": stream,
                "entry_id": entry_id,
                "dlq_stream": dlq_stream,
            },
        )
        if self._on_alert is not None:
            try:
                await self._on_alert(
                    "projection_dlq",
                    {
                        "stream": stream,
                        "entry_id": entry_id,
                        "dlq_stream": dlq_stream,
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception("projection_alert_callback_failed")
