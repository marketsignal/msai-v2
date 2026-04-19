"""Post-start health check for Nautilus ``TradingNode`` (Phase 1 task 1.8).

Decision #14 / plan v7: after ``node.start_async()`` returns, the
subprocess verifies the trader actually started by polling
:attr:`nautilus_trader.trading.trader.Trader.is_running`. That
property only flips to ``True`` inside the LAST line of
``kernel.start_async`` (``self._trader.start()`` at
``nautilus_trader/system/kernel.py:1037``), which is reached only on
full success of every internal await — engine connect, reconciliation,
portfolio init, strategy start.

Nautilus's engines silently early-return on failure (the kernel
swallows connect-time errors and keeps the node "started" from its
own perspective). Without this health check a broken live node would
reach the ``running`` row state without any IB data actually flowing.

Verified against ``nautilus_trader 1.223.0``.
"""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any


class StartupHealthCheckFailed(Exception):  # noqa: N818 — plan-prescribed name
    """Raised when :func:`wait_until_ready` times out.

    The message is the structured diagnosis from :func:`diagnose`
    listing the values of every relevant Nautilus accessor at timeout,
    so log triage can pinpoint which step failed (engine connect,
    reconciliation, portfolio init, instrument loading).
    """


async def wait_until_ready(
    node: Any,  # TradingNode — kept untyped so the module doesn't pull in nautilus at import time
    timeout_s: float = 60.0,
    poll_interval_s: float = 0.5,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Poll ``node.kernel.trader.is_running`` until it flips to True or
    ``timeout_s`` elapses.

    Canonical signal: ``node.kernel.trader.is_running`` — a property
    on ``Trader``/``Component`` (at
    ``nautilus_trader/common/component.pyx:1768-1779``). The trader
    FSM transitions to RUNNING only inside the LAST line of
    ``kernel.start_async`` (``self._trader.start()`` at
    ``kernel.py:1037``), which is reached only on full success of
    every internal await.

    A brief poll handles the rare async-task scheduling window where
    ``_trader.start()`` has been queued but the FSM hasn't flipped
    yet.

    **Accessor errors are caught** (Codex batch 3 P2 fix). Nautilus
    can raise on a partially-initialized kernel (e.g. ``trader`` is
    ``None`` for an instant between ``start_async`` enqueueing the
    trader-start task and the task actually running). If we let
    those exceptions propagate, the subprocess would classify the
    outcome as ``SPAWN_FAILED_PERMANENT`` instead of
    ``RECONCILIATION_FAILED``, losing the structured diagnosis this
    module exists to produce. We treat any accessor exception as
    "not ready yet" and keep polling; on timeout,
    :func:`diagnose` captures the final state (including any
    accessor error) in the raised
    :class:`StartupHealthCheckFailed`.

    **Shutdown-aware polling** (Codex batch 3 iter3 P2 fix). When
    ``shutdown_event`` is provided and becomes set mid-poll, the
    function returns silently WITHOUT raising
    :class:`StartupHealthCheckFailed`. Returning silently is the
    right behavior: the caller already has a shutdown checkpoint
    immediately after ``wait_until_ready`` that converts the signal
    into a clean ``stopped`` termination. Raising
    ``StartupHealthCheckFailed`` here would misclassify an operator
    stop as ``RECONCILIATION_FAILED`` and delay the stop by up to
    the full ``timeout_s`` — the exact misbehavior this fix exists
    to prevent.

    Args:
        node: The ``TradingNode``-like object to poll.
        timeout_s: Maximum time to wait for readiness.
        poll_interval_s: Sleep between polls.
        shutdown_event: Optional external shutdown signal. If set,
            the function returns silently and the caller is
            responsible for observing the same flag and taking the
            shutdown path.

    Raises:
        StartupHealthCheckFailed: if ``is_running`` is still ``False``
            (or repeatedly errors) when the deadline passes and
            ``shutdown_event`` is not set. The exception message is
            the structured diagnosis from :func:`diagnose` — attach
            it to the terminal ``error_message`` column for log
            triage.
    """
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        # Shutdown check BEFORE the accessor read so an already-set
        # event exits immediately, avoiding one extra accessor call
        # on a kernel that might already be torn down.
        if shutdown_event is not None and shutdown_event.is_set():
            return
        try:
            if node.kernel.trader.is_running:
                return
        except Exception:  # noqa: BLE001
            # Partially-initialized kernel / transient accessor drift.
            # Keep polling — on final timeout, diagnose() will capture
            # the error context for the StartupHealthCheckFailed message.
            pass
        # Re-check the shutdown flag AFTER the sleep so a signal
        # delivered during the sleep is observed on the next iteration
        # without an extra accessor poll.
        await asyncio.sleep(poll_interval_s)
        if shutdown_event is not None and shutdown_event.is_set():
            return
    # Final shutdown re-check on the deadline boundary (Codex batch 3
    # iter5 P3 fix). In production, ``shutdown_event`` is flipped by
    # the ``loop.add_signal_handler`` callback inside
    # ``run_subprocess_async`` — that callback only runs when control
    # returns to the event loop. Without an explicit ``await`` here,
    # a SIGTERM that arrived between the last post-sleep check and
    # this re-check would still be queued (its callback hasn't been
    # dispatched yet), the synchronous ``is_set()`` would return
    # False, and we'd misclassify an operator stop as
    # ``RECONCILIATION_FAILED``. Yielding once via ``asyncio.sleep(0)``
    # gives the loop a chance to dispatch any pending signal callback
    # so the re-check sees the updated flag.
    if shutdown_event is not None:
        await asyncio.sleep(0)
        if shutdown_event.is_set():
            return

    # Final ``is_running`` re-check on the deadline boundary (Codex
    # batch 3 iter6 P2 fix). The trader can become ready in the very
    # last poll interval — for example at 59.9 s with a 60 s timeout
    # and the default 0.5 s poll. Without this re-read we'd raise
    # ``StartupHealthCheckFailed`` on a node that just came up,
    # misclassifying a successful (slow) startup as
    # ``RECONCILIATION_FAILED``.
    try:
        if node.kernel.trader.is_running:
            return
    except Exception:  # noqa: BLE001
        pass

    raise StartupHealthCheckFailed(diagnose(node))


def diagnose(node: Any) -> str:
    """Structured failure-reason string using the real Nautilus accessors.

    Verified against ``nautilus_trader 1.223.0``:

    - ``kernel.trader.is_running`` — property on Trader/Component
    - ``data_engine.check_connected()`` — METHOD
      (``nautilus_trader/data/engine.pyx:296``)
    - ``exec_engine.check_connected()`` — METHOD
      (``nautilus_trader/execution/engine.pyx:269``)
    - per-execution-client ``.reconciliation_active`` flag
      (``live/execution_client.py:136``)
    - ``portfolio.initialized`` attribute (``portfolio.pyx:218``)
    - ``len(cache.instruments())`` — current instrument count

    Note on ``exec_engine._clients``: Nautilus's public
    ``registered_clients`` returns a ``list[ClientId]`` — just ids,
    not client objects
    (``nautilus_trader/execution/engine.pyx:204-214``). The dict of
    actual ``LiveExecutionClient`` instances is the private
    ``_clients`` attribute. We access it directly here (Codex v5 P1)
    because this function runs inside the subprocess that built the
    kernel — same Python interpreter, no abstraction boundary
    crossed. Any ``AttributeError`` or runtime error on that access
    is caught and reported in the diagnosis so ``diagnose`` itself
    can never crash the terminal-status write path.
    """
    parts: list[str] = []

    # Read ``node.kernel`` defensively. Nautilus exposes ``kernel`` as a
    # property that raises if the kernel isn't constructed yet (e.g.
    # ``wait_until_ready`` timed out before ``build`` finished). Without
    # this guard ``diagnose`` propagates the AttributeError, the
    # subprocess catches it in the generic ``except Exception`` path,
    # and the outcome is classified ``SPAWN_FAILED_PERMANENT`` instead
    # of ``RECONCILIATION_FAILED`` — losing the structured diagnosis
    # this module exists to produce (Codex batch 3 iter2 P2 fix).
    try:
        kernel = node.kernel
    except Exception as exc:  # noqa: BLE001
        return f"node.kernel=<error: {exc}>"

    # Canonical signal first so it's always the leading field in the
    # triage output.
    try:
        parts.append(f"trader.is_running={kernel.trader.is_running}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"trader.is_running=<error: {exc}>")

    try:
        parts.append(f"data_engine.check_connected()={kernel.data_engine.check_connected()}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"data_engine.check_connected()=<error: {exc}>")

    try:
        parts.append(f"exec_engine.check_connected()={kernel.exec_engine.check_connected()}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"exec_engine.check_connected()=<error: {exc}>")

    # Per-execution-client reconciliation + connection state via the
    # private _clients dict. See the note above for why this is OK.
    try:
        clients_dict = getattr(kernel.exec_engine, "_clients", {}) or {}
        for client_id, client in clients_dict.items():
            recon = getattr(client, "reconciliation_active", None)
            connected = getattr(client, "is_connected", None)
            parts.append(f"{client_id}.reconciliation_active={recon},is_connected={connected}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"exec_engine._clients=<error: {exc}>")

    try:
        parts.append(f"portfolio.initialized={getattr(kernel.portfolio, 'initialized', None)}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"portfolio.initialized=<error: {exc}>")

    try:
        parts.append(f"cache.instruments_count={len(kernel.cache.instruments())}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"cache.instruments_count=<error: {exc}>")

    return "; ".join(parts)
