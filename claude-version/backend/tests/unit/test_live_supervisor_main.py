"""Unit tests for the live supervisor loop dispatcher (Phase 1 task 1.7).

Covers :func:`handle_command` (pure dispatcher, no async loop) and
:func:`run_forever`'s ACK-on-success-only contract. The heavy-weight
process spawning tests live in ``test_process_manager.py``; these
tests stub out ``ProcessManager`` entirely to focus on the dispatch
+ ACK semantics.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from msai.live_supervisor.main import handle_command, run_forever
from msai.services.live_command_bus import LiveCommand, LiveCommandType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _make_command(
    *,
    entry_id: str = "1-0",
    command_type: LiveCommandType = LiveCommandType.START,
    deployment_id: UUID | None = None,
    payload: dict | None = None,
) -> LiveCommand:
    return LiveCommand(
        entry_id=entry_id,
        command_type=command_type,
        deployment_id=deployment_id or uuid4(),
        idempotency_key="test-key",
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# handle_command dispatcher
# ---------------------------------------------------------------------------


class TestHandleCommand:
    @pytest.mark.asyncio
    async def test_start_dispatches_to_spawn(self) -> None:
        pm = MagicMock()
        pm.spawn = AsyncMock(return_value=True)
        dep_id = uuid4()
        cmd = _make_command(
            command_type=LiveCommandType.START,
            deployment_id=dep_id,
            payload={"deployment_slug": "abcd1234abcd1234"},
        )

        ok = await handle_command(cmd, process_manager=pm)

        assert ok is True
        pm.spawn.assert_awaited_once()
        call = pm.spawn.await_args
        assert call.kwargs["deployment_id"] == dep_id
        assert call.kwargs["deployment_slug"] == "abcd1234abcd1234"
        assert call.kwargs["idempotency_key"] == "test-key"

    @pytest.mark.asyncio
    async def test_stop_dispatches_to_stop(self) -> None:
        pm = MagicMock()
        pm.stop = AsyncMock(return_value=True)
        dep_id = uuid4()
        cmd = _make_command(
            command_type=LiveCommandType.STOP,
            deployment_id=dep_id,
            payload={"reason": "user"},
        )

        ok = await handle_command(cmd, process_manager=pm)

        assert ok is True
        pm.stop.assert_awaited_once_with(dep_id, reason="user")

    @pytest.mark.asyncio
    async def test_start_returns_false_when_spawn_returns_false(self) -> None:
        """Spawn's False return (hard failure or busy-stopping) must
        propagate so the caller skips the ACK."""
        pm = MagicMock()
        pm.spawn = AsyncMock(return_value=False)
        cmd = _make_command(
            command_type=LiveCommandType.START,
            payload={"deployment_slug": "abcd1234abcd1234"},
        )

        ok = await handle_command(cmd, process_manager=pm)
        assert ok is False


# ---------------------------------------------------------------------------
# run_forever ACK semantics
# ---------------------------------------------------------------------------


class _StubBus:
    """Minimal stub that mimics the :class:`LiveCommandBus` surface the
    loop needs (``consume`` async iterator + ``ack`` coroutine).

    Yields a pre-baked list of commands once, then honors ``stop_event``
    so ``run_forever`` can exit cleanly.
    """

    def __init__(self, commands: list[LiveCommand]) -> None:
        self._commands = commands
        self.acked: list[str] = []

    async def consume(
        self, consumer_id: str, stop_event: asyncio.Event
    ) -> AsyncIterator[LiveCommand]:
        for cmd in self._commands:
            if stop_event.is_set():
                return
            yield cmd
        # Stop after draining so the loop exits without blocking.
        stop_event.set()

    async def ack(self, entry_id: str) -> None:
        self.acked.append(entry_id)


class _NoopHeartbeatMonitor:
    async def run_forever(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()


class _NoopProcessManager:
    """Process manager stub. ``spawn_return`` drives the return value
    of ``spawn`` so tests can assert the ACK path."""

    def __init__(
        self,
        *,
        spawn_return: bool = True,
        raise_on_spawn: bool = False,
    ) -> None:
        self.spawn_return = spawn_return
        self.raise_on_spawn = raise_on_spawn
        self.spawn_calls: list[UUID] = []

    async def reap_loop(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    async def watchdog_loop(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    async def spawn(
        self,
        *,
        deployment_id: UUID,
        deployment_slug: str,
        payload: dict,
        idempotency_key: str,
    ) -> bool:
        self.spawn_calls.append(deployment_id)
        if self.raise_on_spawn:
            raise RuntimeError("boom")
        return self.spawn_return

    async def stop(self, deployment_id: UUID, *, reason: str = "user") -> bool:
        return True


class TestRunForeverAckSemantics:
    @pytest.mark.asyncio
    async def test_successful_handler_acks(self) -> None:
        cmd = _make_command(payload={"deployment_slug": "abcd1234abcd1234"})
        bus = _StubBus([cmd])
        pm = _NoopProcessManager(spawn_return=True)
        stop_event = asyncio.Event()

        await run_forever(
            bus=bus,  # type: ignore[arg-type]
            process_manager=pm,  # type: ignore[arg-type]
            heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
            stop_event=stop_event,
        )

        assert bus.acked == [cmd.entry_id]
        assert pm.spawn_calls == [cmd.deployment_id]

    @pytest.mark.asyncio
    async def test_failed_handler_does_not_ack(self) -> None:
        """Decision #13: ACK only on success. A ``False`` return from
        the handler must leave the command in the PEL for retry."""
        cmd = _make_command(payload={"deployment_slug": "abcd1234abcd1234"})
        bus = _StubBus([cmd])
        pm = _NoopProcessManager(spawn_return=False)
        stop_event = asyncio.Event()

        await run_forever(
            bus=bus,  # type: ignore[arg-type]
            process_manager=pm,  # type: ignore[arg-type]
            heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
            stop_event=stop_event,
        )

        assert bus.acked == []  # NOT acked
        assert pm.spawn_calls == [cmd.deployment_id]

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_ack(self) -> None:
        """An exception inside the handler must NOT be converted into
        an ACK by an over-eager finally block (decision #13)."""
        cmd = _make_command(payload={"deployment_slug": "abcd1234abcd1234"})
        bus = _StubBus([cmd])
        pm = _NoopProcessManager(raise_on_spawn=True)
        stop_event = asyncio.Event()

        await run_forever(
            bus=bus,  # type: ignore[arg-type]
            process_manager=pm,  # type: ignore[arg-type]
            heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
            stop_event=stop_event,
        )

        assert bus.acked == []

    @pytest.mark.asyncio
    async def test_multiple_commands_each_acked_independently(self) -> None:
        """A mixed batch (success, failure, success) must ACK the two
        successes and leave the failure in the PEL."""
        cmds = [
            _make_command(entry_id="1-0", payload={"deployment_slug": "a" * 16}),
            _make_command(entry_id="2-0", payload={"deployment_slug": "b" * 16}),
            _make_command(entry_id="3-0", payload={"deployment_slug": "c" * 16}),
        ]
        bus = _StubBus(cmds)

        # Custom PM that fails the middle command only.
        class _MixedPM(_NoopProcessManager):
            async def spawn(self, **kwargs):
                self.spawn_calls.append(kwargs["deployment_id"])
                return kwargs["deployment_slug"] != "b" * 16

        pm = _MixedPM()
        stop_event = asyncio.Event()

        await run_forever(
            bus=bus,  # type: ignore[arg-type]
            process_manager=pm,  # type: ignore[arg-type]
            heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
            stop_event=stop_event,
        )

        assert bus.acked == ["1-0", "3-0"]
