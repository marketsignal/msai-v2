"""Thin compatibility facade over :class:`IBAccountSnapshot`.

This module previously owned the per-request IB connection pattern:
every ``GET /api/v1/account/summary`` opened a fresh ``IB()``,
called ``connectAsync`` with a counter-bumped client id, fetched, and
disconnected. That worked for low traffic but accrued two problems:

1. **Connection churn** — concurrent dashboard polls produced
   overlapping connects and surfaced as intermittent
   ``ib_account_summary_failed`` warnings.
2. **Client-id pressure** — an unbounded
   :func:`itertools.count` counter started at 900 and wandered
   indefinitely, increasing collision risk with live-deployment ids.

The fix lives in :mod:`msai.services.ib_account_snapshot`: one
long-lived :class:`ib_async.IB` instance, **static** client id 900,
30-second background refresh tied to the FastAPI lifespan.

This file is kept as a small facade so any in-tree caller importing
``IBAccountService`` continues to work — it just routes through the
snapshot cache. No new ``connectAsync`` happens on the request path.

New code should depend on
:func:`msai.services.ib_account_snapshot.get_snapshot` directly.
"""

from __future__ import annotations

from typing import Any

from msai.core.logging import get_logger
from msai.services.ib_account_snapshot import (
    _PROBE_INTERVAL_S,  # re-exported for tests that previously imported this constant
    IBAccountSnapshot,
    get_snapshot,
)

log = get_logger(__name__)


# Re-export the zero-summary shape for any historical test that imported it.
# The constants now live on :class:`IBAccountSnapshot` but a backward
# compatible alias avoids surprise breakage for in-flight branches.
__all__ = [
    "IBAccountService",
    "_PROBE_INTERVAL_S",
]


class IBAccountService:
    """Deprecated facade — kept for backward compatibility.

    Reads cached values from the process-wide :class:`IBAccountSnapshot`
    rather than opening a fresh IB connection. The ``host`` and ``port``
    constructor arguments are accepted for API compatibility but
    ignored: the snapshot was already created with the right values
    from :class:`msai.core.config.Settings` at FastAPI startup.

    Prefer calling :func:`get_snapshot` directly in new code.
    """

    def __init__(
        self,
        host: str = "ib-gateway",  # noqa: ARG002 - compat only
        port: int = 4002,  # noqa: ARG002 - compat only
    ) -> None:
        # ``host`` / ``port`` are no longer used here: the singleton
        # snapshot is the source of truth and is configured from
        # ``settings.ib_*`` in :func:`get_snapshot`. We accept the
        # parameters so callers built against the old API still work.
        pass

    async def get_summary(self) -> dict[str, float]:
        """Return the latest cached account summary."""
        snapshot: IBAccountSnapshot = get_snapshot()
        return snapshot.get_summary()

    async def get_portfolio(self) -> list[dict[str, Any]]:
        """Return the latest cached portfolio positions."""
        snapshot: IBAccountSnapshot = get_snapshot()
        return snapshot.get_portfolio()
