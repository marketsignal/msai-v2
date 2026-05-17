"""Long-lived IB account snapshot — one connection, 30s refresh loop.

Replaces the per-request ``connectAsync`` pattern in
:mod:`msai.services.ib_account`. The old service opened a fresh IB
connection on every ``GET /api/v1/account/summary`` and
``/api/v1/account/portfolio`` request and incremented a process-wide
``itertools.count(start=900)`` for the client id. Two problems:

1. **Connection churn.** A page that polls both endpoints every 30 s
   opens 2 connections per cycle, each with a new client id. IB
   Gateway logs and rejects duplicates if the previous disconnect
   hasn't fully drained, which surfaced as intermittent
   ``ib_account_summary_failed`` warnings during the 2026-04-15 drill.

2. **Client id pressure.** The counter grows unbounded for the
   lifetime of the process and can collide with reserved ids
   (live deployments derive ids from a deployment-slug hash; this
   service used to start at 900 but was free to wander into any
   range).

The snapshot pattern owns **one** :class:`ib_async.IB` instance, uses
the **static** client id :data:`_STATIC_CLIENT_ID` (900), and refreshes
the cached summary + portfolio every
:data:`_PROBE_INTERVAL_S` seconds in a single background task. Handlers
read from the cache — no I/O on the request path.

Startup is non-blocking: :meth:`IBAccountSnapshot.start` only spawns
the refresh task, it does **not** await the IB connection. If IB
Gateway is unreachable at FastAPI boot, the app comes up cleanly and
:meth:`get_summary` returns the zero-summary shape until a refresh
succeeds. This mirrors :class:`msai.services.ib_probe.IBProbe.run_periodic`
(``ib_probe.py:84-96``) which does I/O **inside** the loop body.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from msai.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ib_async import IB as _IB_TYPE

log = get_logger(__name__)

try:
    from ib_async import IB
except ImportError:  # pragma: no cover - exercised only when extras missing
    IB = None  # type: ignore[assignment,misc]


# Static client id for read-only account queries. Replaces the
# unbounded ``itertools.count(start=900)`` counter in the old
# ``IBAccountService``. The number 900 was chosen because it sits well
# above the deployment-slug-hashed ids used by live trading nodes
# (which are derived from a 16-bit hash of the deployment slug and
# typically land in 1..511) and above the conventional master id of 0.
# Compare the previous behavior in ``ib_account.py:29``::
#
#     _ACCOUNT_CLIENT_COUNTER = _itertools.count(start=900)
#
# Codex iter-1 P1: in production, the backend image starts Uvicorn with
# ``--workers 2``, so this module is imported twice. Without a per-process
# offset, both workers would use the same client id and IB Gateway would
# silently disconnect whichever connected first (Nautilus gotcha #3). The
# base id stays 900 (still the recognizable account-class), but the
# runtime call uses ``_resolve_client_id()`` which adds a stable per-PID
# offset bounded so the id stays under the IB-imposed cap (~1024).
_STATIC_CLIENT_ID: int = 900
_CLIENT_ID_MAX_OFFSET: int = 99


def _resolve_client_id() -> int:
    """Compute the IB clientId for this worker process.

    Each Uvicorn worker is a separate OS process with its own PID; using
    ``os.getpid() % _CLIENT_ID_MAX_OFFSET`` gives a stable per-worker id
    that survives the process lifetime and avoids collisions with
    sibling workers without coordinating shared state.
    """
    return _STATIC_CLIENT_ID + (os.getpid() % _CLIENT_ID_MAX_OFFSET)


# Refresh cadence — aligned with the existing IB Gateway probe in
# :mod:`msai.api.account` (``_PROBE_INTERVAL_S = 30``). 30 s keeps the
# UI showing fresh data without thrashing the gateway.
_PROBE_INTERVAL_S: int = 30

# Connection / fetch budgets. Mirrors the timeouts the old
# ``IBAccountService`` used per request:
# - 5 s for the TCP/handshake step (``connectAsync(..., timeout=5)``).
# - 10 s for each ``accountSummaryAsync`` / ``portfolio`` fetch
#   (``asyncio.wait_for(..., timeout=10)``).
_CONNECT_TIMEOUT_S: float = 5.0
_FETCH_TIMEOUT_S: float = 10.0


_ZERO_SUMMARY: dict[str, float] = {
    "net_liquidation": 0.0,
    "buying_power": 0.0,
    "margin_used": 0.0,
    "available_funds": 0.0,
    "unrealized_pnl": 0.0,
    "realized_pnl": 0.0,
}


_TAG_MAP: dict[str, str] = {
    "NetLiquidation": "net_liquidation",
    "BuyingPower": "buying_power",
    "TotalCashValue": "available_funds",
    "MaintMarginReq": "margin_used",
    "UnrealizedPnL": "unrealized_pnl",
    "RealizedPnL": "realized_pnl",
}


class IBAccountSnapshot:
    """One IB connection, refreshed in the background.

    Module-level singleton — instantiate via :func:`get_snapshot`, not
    directly. Two state fields are observable from outside:

    - :attr:`is_connected` — last-known connection status. Returns
      ``True`` only when the most recent refresh tick connected and
      pulled summary + portfolio without raising.
    - :attr:`refresh_task` — the background :class:`asyncio.Task`, or
      ``None`` if :meth:`start` has not been called yet.

    Args:
        host: IB Gateway hostname. Defaults to the value injected by
            :func:`get_snapshot` from ``settings.ib_host``.
        port: IB Gateway API port. Defaults to the value injected by
            :func:`get_snapshot` from ``settings.ib_port``.
        interval_s: Seconds between refresh ticks. Test-only override;
            production uses :data:`_PROBE_INTERVAL_S`.
    """

    def __init__(
        self,
        *,
        host: str = "ib-gateway",
        port: int = 4002,
        interval_s: int = _PROBE_INTERVAL_S,
    ) -> None:
        self.host: str = host
        self.port: int = port
        self.interval_s: int = interval_s

        # Allow ``IB is None`` (the ib_async optional dep is missing) —
        # the loop will short-circuit on every tick and the snapshot
        # keeps the zero-summary shape. Same fall-back the old
        # ``IBAccountService`` used at ``ib_account.py:58``.
        self._ib: _IB_TYPE | None = IB() if IB is not None else None

        self._summary: dict[str, float] = dict(_ZERO_SUMMARY)
        self._portfolio: list[dict[str, Any]] = []
        self._connected: bool = False
        # SF iter-2 P1: track the timestamp of the last *successful*
        # refresh so callers (and the /account/summary endpoint) can
        # distinguish "snapshot returned $0 because IB is unreachable
        # and we've never connected" from "snapshot returned $0 because
        # IB returned $0 in a real refresh." ``None`` until first success.
        self._last_refresh_success_at: datetime | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the background refresh loop.

        Synchronous on purpose — the IB connection is established
        **inside** the loop body, not awaited here. This means a
        FastAPI lifespan can call ``snapshot.start()`` even when IB
        Gateway is down without blocking startup. Compare the original
        T2 outline which awaited ``connectAsync`` here and crashed
        boot when the gateway was unreachable; Codex F1 caught it.

        Idempotent: calling twice while a task is running leaves the
        original task in place and logs a warning.
        """
        if self._refresh_task is not None and not self._refresh_task.done():
            log.warning("ib_account_snapshot_already_started")
            return
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(),
            name="ib_account_snapshot_refresh",
        )
        log.info(
            "ib_account_snapshot_started",
            interval_s=self.interval_s,
            client_id=_STATIC_CLIENT_ID,
        )

    async def stop(self) -> None:
        """Cancel the refresh task and disconnect the IB client.

        Both steps are best-effort: a missing task is fine, and
        ``IB.disconnect()`` does not raise even if the underlying
        socket is already closed. SF iter-2 P2-C: non-cancel
        exceptions on the awaited task are logged at WARNING rather
        than silently swallowed — leaves a forensic trail when a
        regression bug causes the refresh loop to crash with an
        unexpected error.
        """
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass  # expected — we just cancelled it
            except Exception as exc:  # noqa: BLE001 - log + continue
                log.warning(
                    "ib_account_snapshot_stop_task_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            self._refresh_task = None

        if self._ib is not None and self._connected:
            with suppress(Exception):
                self._ib.disconnect()
        self._connected = False
        log.info("ib_account_snapshot_stopped")

    # ------------------------------------------------------------------
    # Read API — what FastAPI handlers call
    # ------------------------------------------------------------------

    def get_summary(self) -> dict[str, float]:
        """Return the cached account summary.

        Always returns a fresh ``dict`` (caller-mutation safe) with
        exactly the same six keys as the old
        :meth:`IBAccountService.get_summary` so the frontend types do
        not need to change.
        """
        return dict(self._summary)

    def get_portfolio(self) -> list[dict[str, Any]]:
        """Return the cached portfolio positions list.

        Shape matches :meth:`IBAccountService.get_portfolio`
        (``symbol``, ``sec_type``, ``position``, ``market_price``,
        ``market_value``, ``average_cost``, ``unrealized_pnl``,
        ``realized_pnl``). Empty list when the snapshot has never
        successfully fetched (zero-state).
        """
        return list(self._portfolio)

    @property
    def is_connected(self) -> bool:
        """Whether the last refresh tick connected successfully."""
        return self._connected

    @property
    def last_refresh_success_at(self) -> datetime | None:
        """Timestamp of the most recent fully-successful refresh.

        ``None`` until the first successful refresh — used by handlers
        to distinguish "snapshot is in cold-start zero-state because IB
        is unreachable" from "snapshot returned $0 because the account
        is really at $0." SF iter-2 P1.
        """
        return self._last_refresh_success_at

    @property
    def refresh_task(self) -> asyncio.Task[None] | None:
        """The background refresh task, ``None`` before :meth:`start`."""
        return self._refresh_task

    # ------------------------------------------------------------------
    # Refresh loop
    # ------------------------------------------------------------------

    def _drop_connection(self) -> None:
        """Mark disconnected and best-effort tear down the IB socket.

        Shared cleanup body for the three exception arms in
        :meth:`refresh_once` (cancel / timeout / other). ``IB.disconnect``
        is sync and may itself raise on a half-closed socket, so we wrap
        it in :func:`suppress`. The previous cached summary/portfolio is
        deliberately preserved — showing last-known-good is more useful
        than blanking the dashboard on a transient flap.
        """
        self._connected = False
        if self._ib is not None:
            with suppress(Exception):
                self._ib.disconnect()

    async def refresh_once(self) -> None:
        """Execute a single refresh tick.

        Public so tests can drive ticks one at a time without waiting
        ``interval_s`` seconds for the loop. Errors are caught and
        logged at WARNING — the loop body must never raise out (the
        ``while True`` in :meth:`_refresh_loop` would terminate, and
        the snapshot would silently stop refreshing).
        """
        if self._ib is None:
            # ``ib_async`` not installed; remain in zero-state.
            return

        client_id = _resolve_client_id()
        try:
            if not self._connected:
                await asyncio.wait_for(
                    self._ib.connectAsync(
                        self.host,
                        self.port,
                        clientId=client_id,
                    ),
                    timeout=_CONNECT_TIMEOUT_S,
                )
                self._connected = True
                log.info(
                    "ib_account_snapshot_connected",
                    host=self.host,
                    port=self.port,
                    client_id=client_id,
                    pid=os.getpid(),
                )

            new_summary = await asyncio.wait_for(
                self._fetch_summary(),
                timeout=_FETCH_TIMEOUT_S,
            )
            new_portfolio = await asyncio.wait_for(
                self._fetch_portfolio(),
                timeout=_FETCH_TIMEOUT_S,
            )
            self._summary = new_summary
            self._portfolio = new_portfolio
            # SF iter-2 P1: stamp the last-success timestamp so handlers
            # can distinguish "never connected → zero-state lying" from
            # "really connected, account is at $0."
            self._last_refresh_success_at = datetime.now(UTC)
        except asyncio.CancelledError:
            # Cancellation MUST propagate so ``stop()`` can shut the task
            # down cleanly within lifespan. Without this re-raise, the
            # outer ``_refresh_loop`` would continue into asyncio.sleep
            # and ``stop()`` would await the task forever (Codex iter-1
            # P1 + silent-failure-hunter F7).
            self._drop_connection()
            log.info(
                "ib_account_snapshot_refresh_cancelled",
                host=self.host,
                port=self.port,
            )
            raise
        except TimeoutError:
            self._drop_connection()
            log.warning(
                "ib_account_snapshot_refresh_timeout",
                host=self.host,
                port=self.port,
            )
        except Exception as exc:  # noqa: BLE001 - broad on purpose
            # Any failure (ConnectionRefusedError, OSError, ib_async
            # internal exceptions...) puts the snapshot back into the
            # "not connected" mode; previous cached values are preserved.
            self._drop_connection()
            log.warning(
                "ib_account_snapshot_refresh_failed",
                host=self.host,
                port=self.port,
                error=str(exc),
            )

    async def _refresh_loop(self) -> None:
        """Background loop: refresh, sleep, repeat.

        Mirrors :meth:`msai.services.ib_probe.IBProbe.run_periodic` —
        I/O happens **inside** the loop body, never at task creation.
        Exceptions are caught in :meth:`refresh_once`; only
        :exc:`asyncio.CancelledError` (from :meth:`stop`) breaks out.
        """
        while True:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - defensive
                # refresh_once already catches everything; this is a
                # belt-and-braces guard so a programming error in the
                # method body cannot terminate the loop.
                log.error("ib_account_snapshot_loop_error", error=str(exc))
            await asyncio.sleep(self.interval_s)

    # ------------------------------------------------------------------
    # IB fetches (private)
    # ------------------------------------------------------------------

    async def _fetch_summary(self) -> dict[str, float]:
        """Pull account-summary tags from the live IB connection.

        Returns a new dict with the six known keys. Unknown tags are
        ignored; missing tags retain their zero default.
        """
        assert self._ib is not None  # guarded by caller
        tags = await self._ib.accountSummaryAsync()
        result: dict[str, float] = dict(_ZERO_SUMMARY)
        for item in tags:
            key = _TAG_MAP.get(item.tag)
            if key is None:
                continue
            with suppress(ValueError, TypeError):
                result[key] = float(item.value)
        return result

    async def _fetch_portfolio(self) -> list[dict[str, Any]]:
        """Snapshot the current portfolio positions.

        ``IB.portfolio()`` is synchronous (returns the cached list the
        ib_async wrapper has been streaming into); we wrap it in
        ``async`` here so the call site can stay uniform with
        ``_fetch_summary``. No await is actually needed.
        """
        assert self._ib is not None  # guarded by caller
        positions = self._ib.portfolio()
        return [
            {
                "symbol": p.contract.symbol,
                "sec_type": p.contract.secType,
                "position": float(p.position),
                "market_price": float(p.marketPrice),
                "market_value": float(p.marketValue),
                "average_cost": float(p.averageCost),
                "unrealized_pnl": float(p.unrealizedPNL),
                "realized_pnl": float(p.realizedPNL),
            }
            for p in positions
        ]


# ---------------------------------------------------------------------------
# Module-level singleton — lazy created on first access.
# ---------------------------------------------------------------------------

_snapshot: IBAccountSnapshot | None = None


def get_snapshot() -> IBAccountSnapshot:
    """Return the process-wide :class:`IBAccountSnapshot` singleton.

    Lazy-creates on first call using ``settings.ib_host`` /
    ``settings.ib_port``. FastAPI lifespan hooks call this once to
    obtain the instance, then :meth:`IBAccountSnapshot.start` /
    :meth:`stop` to manage its background task.
    """
    global _snapshot  # noqa: PLW0603 — module-level singleton by design
    if _snapshot is None:
        # Imported lazily so that ``import msai.services.ib_account_snapshot``
        # does not eagerly read settings (keeps test isolation clean).
        from msai.core.config import settings

        _snapshot = IBAccountSnapshot(host=settings.ib_host, port=settings.ib_port)
    return _snapshot


def reset_snapshot() -> None:
    """Drop the module-level singleton.

    **Test-only helper.** Used by pytest fixtures to give each test a
    fresh snapshot without lingering background tasks or stale cached
    summaries from a previous test. Never call from production code.
    """
    global _snapshot  # noqa: PLW0603
    _snapshot = None
