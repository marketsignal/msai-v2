"""Unit tests for ``startup_health`` (Phase 1 task 1.8).

Covers the canonical ``node.kernel.trader.is_running`` signal,
the timeout + diagnosis path, and the defensive error handling inside
``diagnose`` that keeps the subprocess's terminal-status write path
alive even if Nautilus's accessors misbehave.

The ``node`` parameter is intentionally untyped in the production
code, so tests use ``types.SimpleNamespace`` rather than importing
anything from ``nautilus_trader`` — keeps these tests fast and
independent of the IB extras.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from msai.services.nautilus.startup_health import (
    StartupHealthCheckFailed,
    diagnose,
    wait_until_ready,
)


def _make_node(
    *,
    is_running: bool = True,
    data_engine_connected: bool = True,
    exec_engine_connected: bool = True,
    portfolio_initialized: bool = True,
    instruments_count: int = 10,
    exec_clients: dict | None = None,
) -> SimpleNamespace:
    """Build a fake Nautilus ``TradingNode`` stand-in with just the
    attributes :func:`diagnose` and :func:`wait_until_ready` touch."""
    trader = SimpleNamespace(is_running=is_running)

    data_engine = SimpleNamespace(check_connected=lambda: data_engine_connected)
    exec_engine = SimpleNamespace(
        check_connected=lambda: exec_engine_connected,
        _clients=exec_clients or {},
    )
    portfolio = SimpleNamespace(initialized=portfolio_initialized)
    cache = SimpleNamespace(instruments=lambda: list(range(instruments_count)))

    kernel = SimpleNamespace(
        trader=trader,
        data_engine=data_engine,
        exec_engine=exec_engine,
        portfolio=portfolio,
        cache=cache,
    )
    return SimpleNamespace(kernel=kernel)


class _Counter:
    """Callable that returns False the first ``flip_after - 1`` times,
    then True forever. Used to simulate the ``is_running`` FSM flipping
    after a brief scheduling window."""

    def __init__(self, flip_after: int) -> None:
        self.flip_after = flip_after
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls >= self.flip_after


# ---------------------------------------------------------------------------
# wait_until_ready
# ---------------------------------------------------------------------------


class TestWaitUntilReady:
    @pytest.mark.asyncio
    async def test_returns_immediately_when_already_running(self) -> None:
        node = _make_node(is_running=True)
        # Should not raise; should not exhaust any timeout.
        await wait_until_ready(node, timeout_s=5.0, poll_interval_s=0.01)

    @pytest.mark.asyncio
    async def test_returns_after_flip(self) -> None:
        """Simulates the rare window where ``_trader.start()`` is
        queued but the FSM hasn't flipped yet — the poll must wait
        it out without raising."""
        counter = _Counter(flip_after=3)

        class _Trader:
            @property
            def is_running(self) -> bool:
                return counter()

        kernel = SimpleNamespace(trader=_Trader())
        node = SimpleNamespace(kernel=kernel)

        await wait_until_ready(node, timeout_s=5.0, poll_interval_s=0.01)
        assert counter.calls >= 3

    @pytest.mark.asyncio
    async def test_raises_startup_health_check_failed_on_timeout(self) -> None:
        """Never flips to True → must raise with the diagnosis attached."""
        node = _make_node(is_running=False, data_engine_connected=False)

        with pytest.raises(StartupHealthCheckFailed) as exc_info:
            await wait_until_ready(node, timeout_s=0.1, poll_interval_s=0.01)

        msg = str(exc_info.value)
        # The canonical signal is always the first field in the diagnosis
        assert "trader.is_running=False" in msg
        # And the failing sub-step appears too
        assert "data_engine.check_connected()=False" in msg

    @pytest.mark.asyncio
    async def test_canonical_signal_is_node_kernel_trader_is_running(self) -> None:
        """Regression guard: make sure ``wait_until_ready`` is checking
        the canonical ``node.kernel.trader.is_running`` path. A
        lookalike attribute at a different location must NOT satisfy
        the check."""
        # Lookalike attribute at the wrong location — this should NOT
        # be what the health check reads.
        trader = SimpleNamespace(is_running=False, running=True)  # 'running' is the decoy
        kernel = SimpleNamespace(trader=trader)
        node = SimpleNamespace(kernel=kernel, is_running=True)  # another decoy

        with pytest.raises(StartupHealthCheckFailed):
            await wait_until_ready(node, timeout_s=0.1, poll_interval_s=0.01)

    @pytest.mark.asyncio
    async def test_returns_silently_when_shutdown_event_already_set(self) -> None:
        """Codex batch 3 iter3 P2 regression: if ``shutdown_event`` is
        already set before the first poll, ``wait_until_ready`` must
        return silently WITHOUT raising ``StartupHealthCheckFailed``.
        Raising would misclassify a stop-during-wedged-startup as
        ``RECONCILIATION_FAILED`` and stall the operator stop for the
        full startup timeout — the exact bug this fix prevents."""
        node = _make_node(is_running=False)  # never flips to True
        shutdown = asyncio.Event()
        shutdown.set()

        # Would normally raise StartupHealthCheckFailed; with the
        # pre-set shutdown event it must return silently.
        await wait_until_ready(
            node,
            timeout_s=5.0,
            poll_interval_s=0.01,
            shutdown_event=shutdown,
        )

    @pytest.mark.asyncio
    async def test_returns_silently_when_shutdown_event_set_mid_poll(self) -> None:
        """Same P2 regression, but the shutdown signal arrives AFTER
        wait_until_ready has started polling. The poll must notice it
        on the next iteration and exit without raising."""
        counter = _Counter(flip_after=999)  # effectively never flips
        shutdown = asyncio.Event()

        class _Trader:
            @property
            def is_running(self) -> bool:
                count = counter()
                if counter.calls >= 2:
                    # Fire the shutdown on the second poll — simulates
                    # SIGTERM arriving while wait_until_ready is still
                    # waiting for the trader to come up.
                    shutdown.set()
                return count

        kernel = SimpleNamespace(trader=_Trader())
        node = SimpleNamespace(kernel=kernel)

        # Must return silently (not raise) well before the 5s deadline.
        await wait_until_ready(
            node,
            timeout_s=5.0,
            poll_interval_s=0.01,
            shutdown_event=shutdown,
        )
        # Sanity: we didn't wait out the full timeout
        assert counter.calls < 50

    @pytest.mark.asyncio
    async def test_returns_silently_when_shutdown_set_at_deadline_boundary(
        self,
    ) -> None:
        """Codex batch 3 iter4 P3 regression: even if the shutdown
        event becomes set in the narrow window between the last
        post-sleep check and the deadline expiration, the function
        must return silently — NOT raise StartupHealthCheckFailed.

        The boundary check at the very bottom of the function
        guarantees this. We force the boundary by using an extremely
        short timeout and pre-setting the event after the loop has
        certainly exited."""
        node = _make_node(is_running=False)  # never flips
        shutdown = asyncio.Event()

        # Patch monotonic-equivalent timing: use a tiny timeout so the
        # while loop exits immediately on the very first iteration's
        # deadline check. Then the post-loop boundary check is what
        # decides between raise and return. We set the event before
        # calling so the boundary check sees it set.
        shutdown.set()
        await wait_until_ready(
            node,
            timeout_s=0.0,  # deadline = now → loop body never executes
            poll_interval_s=0.001,
            shutdown_event=shutdown,
        )
        # If we reach here without StartupHealthCheckFailed, the
        # boundary check did its job.

    @pytest.mark.asyncio
    async def test_queued_shutdown_callback_observed_at_deadline(self) -> None:
        """Codex batch 3 iter6 P3 regression: the boundary-shutdown
        check must yield to the loop BEFORE the synchronous
        ``is_set()`` so a queued ``loop.add_signal_handler``
        callback (production SIGTERM dispatch) gets a chance to
        flip the flag.

        Without the ``await asyncio.sleep(0)`` in the boundary
        block, this test would still raise — because we use
        ``loop.call_soon`` to enqueue ``shutdown.set()`` instead of
        calling it synchronously. ``call_soon`` matches how
        ``loop.add_signal_handler`` dispatches: the handler is
        registered, but the actual ``set()`` only runs on the
        loop's next iteration. With the yield, the dispatch
        completes during ``await asyncio.sleep(0)`` and the
        post-yield ``is_set()`` returns True.
        """
        node = _make_node(is_running=False)  # never flips
        shutdown = asyncio.Event()

        # Enqueue the set call — it will only execute when the loop
        # gets a chance to dispatch ready callbacks (i.e. on a yield).
        loop = asyncio.get_running_loop()
        loop.call_soon(shutdown.set)

        # If wait_until_ready doesn't yield before its boundary
        # is_set() check, the call_soon callback hasn't fired yet,
        # is_set() returns False, and StartupHealthCheckFailed is
        # raised. With the iter5+iter6 fix, the function yields
        # once before checking and observes the now-set flag.
        await wait_until_ready(
            node,
            timeout_s=0.0,  # force immediate exit from while loop
            poll_interval_s=0.001,
            shutdown_event=shutdown,
        )

    @pytest.mark.asyncio
    async def test_returns_silently_when_trader_becomes_ready_at_deadline(
        self,
    ) -> None:
        """Codex batch 3 iter6 P2 regression: the trader can become
        ready in the very last poll interval (e.g. at 59.9 s with
        a 60 s timeout). The post-loop boundary block must read
        ``is_running`` once more before raising — otherwise a
        successful but slow startup is misclassified as
        ``RECONCILIATION_FAILED``.

        We force the boundary case by:
        - Pre-setting a flag so the trader's first ``is_running``
          read returns False (loop body sees False, exits).
        - Then flipping the flag to True so the post-loop final
          ``is_running`` read returns True.
        """
        polls: list[bool] = []
        ready = False

        class _LateTrader:
            @property
            def is_running(self) -> bool:
                polls.append(True)
                return ready

        kernel = SimpleNamespace(trader=_LateTrader())
        node = SimpleNamespace(kernel=kernel)

        # timeout=0 → while loop doesn't execute. Flip ready BEFORE
        # the call so the post-loop final is_running read returns True.
        ready = True
        await wait_until_ready(
            node,
            timeout_s=0.0,
            poll_interval_s=0.001,
        )
        # The post-loop final is_running read happened
        assert polls, "post-loop is_running read never happened"


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------


class TestDiagnose:
    def test_includes_canonical_signal_first(self) -> None:
        node = _make_node(is_running=False)
        msg = diagnose(node)
        assert msg.startswith("trader.is_running=False")

    def test_includes_data_engine_check_connected_status(self) -> None:
        node = _make_node(data_engine_connected=False)
        msg = diagnose(node)
        assert "data_engine.check_connected()=False" in msg

    def test_includes_exec_engine_check_connected_status(self) -> None:
        node = _make_node(exec_engine_connected=False)
        msg = diagnose(node)
        assert "exec_engine.check_connected()=False" in msg

    def test_includes_each_execution_client_status(self) -> None:
        """Codex v5 P1: ``_clients`` is a dict of client objects we
        reach into directly. Each client's ``reconciliation_active``
        and ``is_connected`` must appear verbatim in the diagnosis."""
        client_a = SimpleNamespace(reconciliation_active=True, is_connected=True)
        client_b = SimpleNamespace(reconciliation_active=False, is_connected=False)
        node = _make_node(exec_clients={"IB-A": client_a, "IB-B": client_b})

        msg = diagnose(node)
        assert "IB-A.reconciliation_active=True,is_connected=True" in msg
        assert "IB-B.reconciliation_active=False,is_connected=False" in msg

    def test_exec_clients_access_error_is_captured(self) -> None:
        """If the private ``_clients`` access raises (Nautilus API
        drifts in a future version, say), ``diagnose`` must capture
        the error and keep producing a report — never crash the
        terminal-status write path it runs in."""

        class _Boom:
            def __getattr__(self, name: str) -> Any:
                if name == "_clients":
                    raise RuntimeError("simulated API drift")
                raise AttributeError(name)

            def check_connected(self) -> bool:
                return True

        trader = SimpleNamespace(is_running=False)
        kernel = SimpleNamespace(
            trader=trader,
            data_engine=SimpleNamespace(check_connected=lambda: True),
            exec_engine=_Boom(),
            portfolio=SimpleNamespace(initialized=True),
            cache=SimpleNamespace(instruments=lambda: []),
        )
        node = SimpleNamespace(kernel=kernel)

        msg = diagnose(node)
        # The error is captured as a string; no exception propagates
        assert "exec_engine._clients=<error:" in msg
        assert "simulated API drift" in msg
        # Other fields still present
        assert "trader.is_running=False" in msg

    def test_portfolio_and_cache_counts_included(self) -> None:
        node = _make_node(portfolio_initialized=True, instruments_count=42)
        msg = diagnose(node)
        assert "portfolio.initialized=True" in msg
        assert "cache.instruments_count=42" in msg

    def test_trader_is_running_error_is_captured(self) -> None:
        """Even the canonical-signal read goes through a try/except so
        a weird Nautilus error here doesn't take down ``diagnose``."""

        class _BadTrader:
            @property
            def is_running(self) -> bool:
                raise RuntimeError("trader exploded")

        kernel = SimpleNamespace(
            trader=_BadTrader(),
            data_engine=SimpleNamespace(check_connected=lambda: True),
            exec_engine=SimpleNamespace(check_connected=lambda: True, _clients={}),
            portfolio=SimpleNamespace(initialized=True),
            cache=SimpleNamespace(instruments=lambda: []),
        )
        node = SimpleNamespace(kernel=kernel)

        msg = diagnose(node)
        assert "trader.is_running=<error:" in msg
        assert "trader exploded" in msg

    def test_node_kernel_accessor_error_is_captured(self) -> None:
        """Codex batch 3 iter2 P2 regression guard: if ``node.kernel``
        itself raises (e.g. ``wait_until_ready`` timed out before
        ``node.build()`` finished), ``diagnose`` must capture the
        error in its return value instead of letting it escape. A
        bare exception would reach the subprocess's generic ``except``
        branch and get classified as ``SPAWN_FAILED_PERMANENT``
        instead of ``RECONCILIATION_FAILED``, losing the structured
        diagnosis this module exists to produce."""

        class _BadNode:
            @property
            def kernel(self) -> Any:
                raise RuntimeError("kernel not built yet")

        # No exception escapes — it's all captured in the return string
        msg = diagnose(_BadNode())
        assert "node.kernel=<error:" in msg
        assert "kernel not built yet" in msg
