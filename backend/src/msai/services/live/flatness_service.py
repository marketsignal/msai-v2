"""API-side flatness coordination for Bug #2 (live-deploy-safety-trio).

Wraps the producer/consumer dance the API does for STOP_AND_REPORT_FLATNESS:

1. ``coalesce_or_publish_stop_with_flatness`` — atomic SET-NX of
   ``inflight_stop:{deployment_id}`` carries the originator's nonce.
   Concurrent stops for the same deployment all converge on the
   originator's nonce (no second SIGTERM, no second flatness ticket).
2. ``poll_stop_report`` — GET-based polling on ``stop_report:{nonce}``
   with exponential backoff and a wall-clock deadline. Returns the
   parsed report dict on hit, ``None`` on timeout. Does NOT delete the
   key (coalesced readers may be polling the same nonce; rely on TTL).

See ``docs/plans/2026-05-13-live-deploy-safety-trio.md`` §Bug #2.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uuid import UUID

    from redis.asyncio import Redis as AsyncRedis

    from msai.services.live_command_bus import LiveCommandBus


_INFLIGHT_TTL_S = 60
"""How long the API will treat a nonce as 'still in flight' for
coalescing. Must be ≥ the API's poll deadline (30 s for /stop, 15 s
for /kill-all) so a second caller arriving JUST before the first hits
its timeout still gets correlated."""

_STOP_REPORT_TTL_S = 120
"""Stop report TTL on the child side. Constant exposed here for tests
that need to assert the consumer's view of the contract."""


async def coalesce_or_publish_stop_with_flatness(
    *,
    redis: AsyncRedis,
    bus: LiveCommandBus,
    deployment_id: UUID,
    member_strategy_id_fulls: list[str],
    reason: str = "user",
    idempotency_key: str | None = None,
) -> tuple[str, bool]:
    """Atomic 'one publish per deployment in flight' primitive.

    Returns ``(stop_nonce, is_originator)``:
        - ``stop_nonce`` — the UUID4 hex the API and caller will poll on.
        - ``is_originator`` — True if THIS call published the command,
          False if a previous in-flight stop is being coalesced.

    Implementation:
        1. Generate a fresh nonce.
        2. ``SET inflight_stop:{deployment_id} <nonce> NX EX 60``.
        3. NX succeeds → publish + return (nonce, True).
        4. NX fails → GET the in-flight nonce, return (existing, False).
    """
    # Local import: keeps the observability layer out of test paths that
    # stub the bus + redis without needing the metric registry.
    from msai.services.observability.trading_metrics import (
        FLATNESS_COALESCED_TOTAL,
        FLATNESS_REQUESTS_TOTAL,
    )

    FLATNESS_REQUESTS_TOTAL.inc()
    nonce = secrets.token_hex(16)
    inflight_key = f"inflight_stop:{deployment_id}"
    acquired = await redis.set(inflight_key, nonce, nx=True, ex=_INFLIGHT_TTL_S)
    if acquired:
        await bus.publish_stop_and_report_flatness(
            deployment_id,
            stop_nonce=nonce,
            member_strategy_id_fulls=member_strategy_id_fulls,
            reason=reason,
            idempotency_key=idempotency_key,
        )
        return nonce, True
    # Lost the race — coalesce onto the existing nonce.
    FLATNESS_COALESCED_TOTAL.inc()
    existing = await redis.get(inflight_key)
    if existing is None:
        # Edge: the key expired between our SET-NX failure and the GET.
        # Recurse once: re-acquire (will almost certainly succeed) and
        # publish freshly. Guarded against infinite recursion by the
        # `nonce` having a fresh value each time.
        return await coalesce_or_publish_stop_with_flatness(
            redis=redis,
            bus=bus,
            deployment_id=deployment_id,
            member_strategy_id_fulls=member_strategy_id_fulls,
            reason=reason,
            idempotency_key=idempotency_key,
        )
    return existing, False


async def poll_stop_report(
    *,
    redis: AsyncRedis,
    stop_nonce: str,
    deadline_s: float,
    initial_interval_s: float = 0.05,
    max_interval_s: float = 1.6,
) -> dict[str, Any] | None:
    """Poll ``stop_report:{stop_nonce}`` until it materializes or
    ``deadline_s`` elapses (wall-clock).

    Exponential backoff: 50 ms → 100 ms → 200 ms → 400 ms → 800 ms →
    1.6 s (capped). Does NOT DEL the key on success — coalesced
    readers may be polling the same nonce (plan §Bug #2 step 4).
    Cleanup is via the 120 s TTL the child set.

    Returns the parsed report dict on hit, ``None`` on timeout.
    """
    from msai.services.observability.trading_metrics import (
        FLATNESS_POLL_TIMEOUT_TOTAL,
        FLATNESS_REPORT_NON_FLAT_TOTAL,
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    interval = initial_interval_s
    key = f"stop_report:{stop_nonce}"
    while True:
        raw = await redis.get(key)
        if raw is not None:
            try:
                report: dict[str, Any] = json.loads(raw)
                if not report.get("broker_flat", True):
                    FLATNESS_REPORT_NON_FLAT_TOTAL.inc()
                return report
            except (ValueError, TypeError):
                # Corrupted payload — treat as if not present. Don't
                # swallow forever; let the deadline drop us out.
                pass
        if loop.time() >= deadline:
            FLATNESS_POLL_TIMEOUT_TOTAL.inc()
            return None
        await asyncio.sleep(min(interval, max(0.0, deadline - loop.time())))
        interval = min(interval * 2, max_interval_s)
