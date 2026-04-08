"""Redis-Streams-backed control plane between FastAPI and live-supervisor.

Phase 1 task 1.6. Implements decision #12 (PEL recovery via explicit
XAUTOCLAIM — Codex v3 P0) and decision's follow-on poison-message DLQ
(Codex v4 P2).

Two things this module exists to get right, both of which burned the
previous version of the system:

1. **PEL recovery via XAUTOCLAIM.** Redis Streams consumer groups do
   NOT auto-redeliver unACKed entries the way Kafka does. An entry
   pulled via ``XREADGROUP`` sits in the group's Pending Entries List
   (PEL) until the consumer explicitly XACKs it — or until a PEER
   claims it via ``XCLAIM`` / ``XAUTOCLAIM``. If the supervisor crashes
   mid-handler, the entry is "stuck" in its name until somebody runs
   ``XAUTOCLAIM``. This module runs that reclaim pass on every
   ``consume()`` startup and at ``recovery_interval_s`` in steady state,
   so a restart always picks up the work in flight from the previous
   incarnation.

2. **DLQ for poison messages.** An entry whose handler deterministically
   crashes would bounce through ``XAUTOCLAIM`` forever. We cap the
   per-entry delivery count at ``MAX_DELIVERY_ATTEMPTS`` (5) and move
   any entry over the cap to a sibling stream ``msai:live:commands:dlq``,
   preserving the original payload + diagnostic metadata so an operator
   can replay it after fixing the root cause. We also XACK the poison
   entry on the primary stream so the bounce loop stops.

Explicit ACK semantics
----------------------

Callers MUST pass the ``entry_id`` from each yielded ``LiveCommand``
back to ``ack()`` after the work has been successfully handled AND
observed in the durable store (e.g. the ``live_node_processes`` row
reaching ``'building'``). Decision #13: ACK only on success; never
ACK in a ``finally`` block. The PEL recovery path exists precisely
so un-ACKed entries can be retried — skipping the ACK is the retry
signal.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from redis.exceptions import ResponseError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from redis.asyncio import Redis as AsyncRedis


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire contract constants
# ---------------------------------------------------------------------------

LIVE_COMMAND_STREAM = "msai:live:commands"
"""Primary Redis stream carrying start/stop commands from the API to
the live-supervisor. Part of the wire contract between the two
services — a rename here breaks the supervisor on rollout."""

LIVE_COMMAND_GROUP = "live-supervisor"
"""Redis consumer group name. All supervisor instances join this one
group so each entry is delivered to exactly one pod (Redis Streams
group semantics)."""

LIVE_COMMAND_DLQ_STREAM = "msai:live:commands:dlq"
"""Sibling stream for poison messages. Entries land here with their
original payload plus ``original_entry_id`` / ``delivery_count`` /
``dlq_reason`` / ``moved_at`` diagnostic fields."""

MAX_DELIVERY_ATTEMPTS = 5
"""Delivery-count ceiling. After the 5th XAUTOCLAIM the entry is
considered a poison message and moved to the DLQ. Tuned conservatively
so a normal transient failure (DB connection drop, IB Gateway glitch)
gets plenty of retries, but a deterministic crash can't bounce forever
(Codex v4 P2)."""

_IDEMPOTENCY_KEY_PREFIX = "dep:"
"""Prefix for the default per-deployment idempotency key so it can
never collide with a user-supplied key (which the API layer produces
from the HTTP Idempotency-Key header)."""


class LiveCommandType(StrEnum):
    """Discriminator stored in the Redis entry's ``command_type`` field.

    Kept as a ``StrEnum`` so ``repr``/log output is human-readable and
    the Redis-side value round-trips through ``decode_responses=True``
    without coercion."""

    START = "start"
    STOP = "stop"


class LiveCommand:
    """A decoded command pulled from the stream.

    Intentionally a plain class (not a dataclass) so the ``entry_id``
    field stays non-hashable and the deserialization errors surface
    with stable attribute access rather than a ``frozen=True`` gotcha.
    """

    __slots__ = (
        "command_type",
        "deployment_id",
        "entry_id",
        "idempotency_key",
        "payload",
    )

    def __init__(
        self,
        *,
        entry_id: str,
        command_type: LiveCommandType,
        deployment_id: UUID,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        self.entry_id = entry_id
        self.command_type = command_type
        self.deployment_id = deployment_id
        self.idempotency_key = idempotency_key
        self.payload = payload

    @classmethod
    def from_redis(cls, entry_id: str, fields: dict[str, str]) -> LiveCommand:
        """Build a ``LiveCommand`` from a Redis stream entry.

        Fields the bus always stores:

        - ``command_type`` — ``"start"`` or ``"stop"``
        - ``deployment_id`` — UUID as string
        - ``idempotency_key`` — dedupe key (default or user-supplied)
        - ``payload_*`` — each payload dict entry stored as a flat
          key/value pair so Redis doesn't need to parse JSON out of a
          single opaque field. Values are serialized via ``json.dumps``
          so ``list``/``dict``/``bool``/``int`` round-trip cleanly.
        """
        payload: dict[str, Any] = {}
        for key, value in fields.items():
            if key.startswith("payload_"):
                payload_key = key[len("payload_") :]
                try:
                    payload[payload_key] = json.loads(value)
                except (TypeError, ValueError):
                    # Fall back to the raw string if something wrote a
                    # non-JSON value (e.g. a migration from another
                    # producer). Better than crashing the consumer loop.
                    payload[payload_key] = value

        return cls(
            entry_id=entry_id,
            command_type=LiveCommandType(fields["command_type"]),
            deployment_id=UUID(fields["deployment_id"]),
            idempotency_key=fields["idempotency_key"],
            payload=payload,
        )


def _default_idempotency_key(deployment_id: UUID) -> str:
    """Stable per-deployment dedupe key used when the caller doesn't
    supply one. Two publishes for the same deployment without explicit
    keys collide on purpose so the supervisor dedupes the retry."""
    return f"{_IDEMPOTENCY_KEY_PREFIX}{deployment_id.hex}"


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class LiveCommandBus:
    """Thin async wrapper over Redis Streams for the live control plane.

    Holds a single ``AsyncRedis`` connection; all methods are async. The
    bus is designed to be instantiated once per process (the
    live-supervisor container holds exactly one, the API FastAPI app
    holds exactly one).

    Instances are created with ``decode_responses=True`` clients — this
    class does NOT decode bytes internally.
    """

    def __init__(
        self,
        *,
        redis: AsyncRedis,
        stream: str = LIVE_COMMAND_STREAM,
        group: str = LIVE_COMMAND_GROUP,
        dlq_stream: str = LIVE_COMMAND_DLQ_STREAM,
        max_delivery_attempts: int = MAX_DELIVERY_ATTEMPTS,
        min_idle_ms: int = 30_000,
        recovery_interval_s: int = 30,
        alert_callback: Callable[..., None] | None = None,
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._group = group
        self._dlq_stream = dlq_stream
        self._max_delivery_attempts = max_delivery_attempts
        self._min_idle_ms = min_idle_ms
        self._recovery_interval_s = recovery_interval_s
        self._alert_callback = alert_callback

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish_start(
        self,
        deployment_id: UUID,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        """Publish a start command. Returns the Redis stream entry id.

        ``idempotency_key`` is the HTTP ``Idempotency-Key`` header when
        the caller supplies one (so retries from the same client
        collide); otherwise a stable per-deployment default is used.
        """
        return await self._publish(
            command_type=LiveCommandType.START,
            deployment_id=deployment_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )

    async def publish_stop(
        self,
        deployment_id: UUID,
        reason: str = "user",
        *,
        idempotency_key: str | None = None,
    ) -> str:
        """Publish a stop command. Returns the Redis stream entry id."""
        return await self._publish(
            command_type=LiveCommandType.STOP,
            deployment_id=deployment_id,
            payload={"reason": reason},
            idempotency_key=idempotency_key,
        )

    async def _publish(
        self,
        *,
        command_type: LiveCommandType,
        deployment_id: UUID,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> str:
        fields: dict[str, str] = {
            "command_type": command_type.value,
            "deployment_id": str(deployment_id),
            "idempotency_key": idempotency_key or _default_idempotency_key(deployment_id),
        }
        # Flatten payload into payload_<key> fields so Redis stores
        # structured data as first-class columns (searchable via XRANGE
        # etc.) rather than a single opaque JSON blob.
        for key, value in payload.items():
            fields[f"payload_{key}"] = json.dumps(value)

        entry_id = await self._redis.xadd(self._stream, fields)
        log.info(
            "live_command_published",
            extra={
                "entry_id": entry_id,
                "command_type": command_type.value,
                "deployment_id": str(deployment_id),
            },
        )
        return entry_id

    # ------------------------------------------------------------------
    # Group lifecycle
    # ------------------------------------------------------------------

    async def ensure_group(self) -> None:
        """Idempotently create the consumer group via XGROUP CREATE MKSTREAM.

        ``MKSTREAM`` auto-creates the underlying stream if it doesn't
        exist yet (so the first publish doesn't race with the first
        ensure_group). ``BUSYGROUP`` is swallowed because repeated
        ``ensure_group`` calls across restarts are expected.
        """
        try:
            await self._redis.xgroup_create(
                name=self._stream,
                groupname=self._group,
                id="$",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    # ------------------------------------------------------------------
    # Consume
    # ------------------------------------------------------------------

    async def consume(
        self, consumer_id: str, stop_event: asyncio.Event
    ) -> AsyncIterator[LiveCommand]:
        """Consume commands until ``stop_event`` is set.

        Lifecycle:

        1. ``ensure_group()`` — idempotent XGROUP CREATE MKSTREAM
        2. ``_recover_pending()`` — XAUTOCLAIM stale entries from any
           crashed peer (or our own previous run); yield each one,
           or move to DLQ if the delivery count has hit the ceiling
        3. Steady-state ``XREADGROUP BLOCK`` loop
        4. Every ``recovery_interval_s``, re-run ``_recover_pending()``
           to handle peers crashing in steady state

        Each yielded ``LiveCommand`` has an ``entry_id`` the caller
        MUST pass back to ``ack()`` after successfully handling the
        work (decision #13 — no ACK-in-finally).
        """
        await self.ensure_group()

        # Phase 2: recover any entries stranded in the PEL by a previous
        # run or a peer crash. Yields them through the same interface
        # as fresh reads so the caller doesn't have to care whether an
        # entry is new or recovered.
        async for command in self._recover_pending(consumer_id):
            yield command
            if stop_event.is_set():
                return

        # Phase 3-4: steady state. BLOCK timeout is 5s so a ``stop_event``
        # set mid-block gets honored within 5s of the request. Every
        # recovery_interval_s, re-run _recover_pending to handle peers
        # crashing while we're running.
        last_recovery_s = asyncio.get_event_loop().time()
        block_ms = 5_000
        while not stop_event.is_set():
            now = asyncio.get_event_loop().time()
            if now - last_recovery_s >= self._recovery_interval_s:
                async for command in self._recover_pending(consumer_id):
                    yield command
                    if stop_event.is_set():
                        return
                last_recovery_s = now

            entries = await self._redis.xreadgroup(
                groupname=self._group,
                consumername=consumer_id,
                streams={self._stream: ">"},
                count=16,
                block=block_ms,
            )
            if not entries:
                continue
            # entries shape: [(stream_name, [(entry_id, fields), ...])]
            for _stream_name, stream_entries in entries:
                for entry_id, fields in stream_entries:
                    yield LiveCommand.from_redis(entry_id, fields)
                    if stop_event.is_set():
                        return

    async def _recover_pending(self, consumer_id: str) -> AsyncIterator[LiveCommand]:
        """Reclaim stale entries via XAUTOCLAIM.

        Walks the cursor until ``XAUTOCLAIM`` returns ``"0-0"`` (the
        "end of pending list" sentinel). Each claimed entry has its
        delivery count checked via ``XPENDING``:

        - ``delivery_count >= max_delivery_attempts`` → move to DLQ
          (with diagnostic metadata) and XACK on the primary so it
          stops bouncing
        - otherwise → yield to the caller for another handler attempt
        """
        cursor: str = "0-0"
        while True:
            result = await self._redis.xautoclaim(
                name=self._stream,
                groupname=self._group,
                consumername=consumer_id,
                min_idle_time=self._min_idle_ms,
                start_id=cursor,
                count=100,
                justid=False,
            )
            # redis-py returns (next_cursor, [(entry_id, fields), ...], [deleted_ids])
            next_cursor, claimed, _deleted = result
            for entry_id, fields in claimed:
                pending = await self._redis.xpending_range(
                    name=self._stream,
                    groupname=self._group,
                    min=entry_id,
                    max=entry_id,
                    count=1,
                )
                # If the entry disappeared between xautoclaim and
                # xpending_range, treat as delivery_count=1 so it gets
                # yielded once more before the DLQ ceiling.
                delivery_count = int(pending[0]["times_delivered"]) if pending else 1

                if delivery_count >= self._max_delivery_attempts:
                    await self._move_to_dlq(
                        entry_id=entry_id,
                        fields=fields,
                        delivery_count=delivery_count,
                        reason="max_attempts",
                    )
                    continue
                yield LiveCommand.from_redis(entry_id, fields)

            cursor = next_cursor
            if cursor in ("0-0", b"0-0"):
                break

    # ------------------------------------------------------------------
    # ACK + DLQ
    # ------------------------------------------------------------------

    async def ack(self, entry_id: str) -> None:
        """XACK the entry on the primary stream.

        Safe to call with a stale or already-ACKed entry id — Redis's
        XACK returns 0 in that case, which we don't surface. This lets
        a retry of the ACK path (e.g. after a transient network blip)
        be a no-op instead of crashing the consumer loop.
        """
        await self._redis.xack(self._stream, self._group, entry_id)

    async def _move_to_dlq(
        self,
        *,
        entry_id: str,
        fields: dict[str, str],
        delivery_count: int,
        reason: str,
    ) -> None:
        """Copy the entry to the DLQ stream and XACK it on the primary.

        The DLQ entry preserves every field from the primary entry and
        tacks on diagnostic metadata so an operator can replay or
        triage without reaching into the PEL.
        """
        dlq_fields: dict[str, str] = {
            **fields,
            "original_entry_id": entry_id,
            "delivery_count": str(delivery_count),
            "dlq_reason": reason,
            "moved_at": _utcnow_iso(),
        }
        await self._redis.xadd(self._dlq_stream, dlq_fields)
        await self._redis.xack(self._stream, self._group, entry_id)
        log.error(
            "live_command_moved_to_dlq",
            extra={
                "entry_id": entry_id,
                "delivery_count": delivery_count,
                "reason": reason,
            },
        )
        if self._alert_callback is not None:
            try:
                self._alert_callback(entry_id=entry_id, reason=reason)
            except Exception:  # noqa: BLE001
                # Never let an alerting bug break the consumer loop —
                # log and continue. The DLQ move itself has already
                # completed; the alert is an advisory side effect.
                log.exception("live_command_dlq_alert_failed")
