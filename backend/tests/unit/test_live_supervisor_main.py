"""Unit tests for the live supervisor loop dispatcher (Phase 1 task 1.7).

Covers :func:`handle_command` (pure dispatcher, no async loop) and
:func:`run_forever`'s ACK-on-success-only contract. The heavy-weight
process spawning tests live in ``test_fleet_router.py``; these
tests stub out ``FleetRouter`` entirely to focus on the dispatch
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
    per-account consumer loop needs (``consume`` async iterator + ``ack``
    coroutine + ``publish_router_heartbeat``).

    Yields a pre-baked list of commands once, then blocks on
    ``stop_event`` so the per-account consumer task stays alive until the
    TEST sets ``stop_event`` (PR 2 T4 — ``run_forever`` no longer relies
    on the bus to drive shutdown; the loop owns ``stop_event``).

    ``consume`` accepts the per-account ``stream`` kwarg (T4) and records
    it so tests can assert routing; ``ack`` accepts the matching
    ``stream`` kwarg.
    """

    def __init__(self, commands: list[LiveCommand]) -> None:
        self._commands = commands
        self.acked: list[str] = []
        self.consumed_streams: list[str | None] = []
        self.heartbeats = 0
        self.consumed_accounts_publishes = 0

    def account_stream(self, account_id: str | None) -> str:
        # Mirror LiveCommandBus.account_stream for the default (global-base)
        # bus so the per-account consumer derives the same stream the real bus
        # would (T4 + the iter-21 publish/consume-symmetry change).
        from msai.services.live_command_bus import command_stream_for_account

        return command_stream_for_account(account_id)

    async def consume(
        self,
        consumer_id: str,
        stop_event: asyncio.Event,
        *,
        stream: str | None = None,
    ) -> AsyncIterator[LiveCommand]:
        self.consumed_streams.append(stream)
        # The pre-baked batch belongs to the PER-ACCOUNT stream(s) these tests
        # drive with ``known_accounts=[...]``. ``run_forever`` ALSO starts a
        # base-stream consumer (F1) — it must read NOTHING here (no commands on
        # the base stream in these legacy tests), otherwise every command would
        # be double-consumed/double-ACKed. Yield only on the non-base stream.
        from msai.services.live_command_bus import command_stream_for_account

        base_stream = command_stream_for_account(None)
        if stream != base_stream:
            for cmd in self._commands:
                if stop_event.is_set():
                    return
                yield cmd
        # Drained the pre-baked batch (or this is the base consumer with no
        # commands). Block until the test requests shutdown so the consumer task
        # doesn't tight-loop re-creating the consume generator.
        await stop_event.wait()

    async def ack(self, entry_id: str, *, stream: str | None = None) -> None:
        self.acked.append(entry_id)

    async def publish_router_heartbeat(self) -> None:
        self.heartbeats += 1

    async def publish_consumed_accounts(self, accounts: object) -> None:
        # The heartbeat publisher stamps the consumed-account set right after
        # the router heartbeat (TestConsumedAccountCoverage). Stub records the
        # call count; no test asserts the payload here.
        self.consumed_accounts_publishes += 1


class _StreamRoutingBus:
    """A bus stub that routes pre-baked commands BY stream (unlike ``_StubBus``,
    which yields the same batch to every consumer regardless of stream).

    Each consumer (per-account OR base) calls ``consume(stream=...)`` and only
    receives the commands keyed under exactly that stream — so a test can assert
    which stream a command was consumed from (base vs per-account) and that
    there is no double-consume. ``ack`` records ``(entry_id, stream)`` so the
    test can assert the ACK landed on the same stream the command came from.
    """

    def __init__(self, commands_by_stream: dict[str, list[LiveCommand]]) -> None:
        self._by_stream = commands_by_stream
        self.acked: list[tuple[str, str | None]] = []
        self.consumed_streams: list[str | None] = []
        self.heartbeats = 0
        self.consumed_accounts_publishes = 0

    def account_stream(self, account_id: str | None) -> str:
        from msai.services.live_command_bus import command_stream_for_account

        return command_stream_for_account(account_id)

    async def consume(
        self,
        consumer_id: str,
        stop_event: asyncio.Event,
        *,
        stream: str | None = None,
    ) -> AsyncIterator[LiveCommand]:
        self.consumed_streams.append(stream)
        for cmd in self._by_stream.get(stream or "", []):
            if stop_event.is_set():
                return
            yield cmd
        # Drained this stream's batch — block until shutdown so the consumer
        # task doesn't tight-loop re-creating the consume generator.
        await stop_event.wait()

    async def ack(self, entry_id: str, *, stream: str | None = None) -> None:
        self.acked.append((entry_id, stream))

    async def publish_router_heartbeat(self) -> None:
        self.heartbeats += 1

    async def publish_consumed_accounts(self, accounts: object) -> None:
        self.consumed_accounts_publishes += 1


async def _run_until_acked_then_stop(
    *,
    bus: _StubBus,
    pm: _NoopFleetRouter,
    expected_acks: int | None = None,
    expected_spawns: int = 1,
    known_accounts: list[str] | None = None,
    timeout_s: float = 3.0,
) -> None:
    """Drive ``run_forever`` with a per-account consumer, wait until the
    expected dispatch is observed, then set ``stop_event`` and join.

    ``expected_acks`` waits for that many acks; when ``None`` it waits for
    ``expected_spawns`` spawn calls instead (covers the no-ack paths)."""
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        run_forever(
            bus=bus,  # type: ignore[arg-type]
            process_manager=pm,  # type: ignore[arg-type]
            heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
            stop_event=stop_event,
            known_accounts=known_accounts or ["ACC-1"],
        )
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        while loop.time() < deadline:
            if expected_acks is not None:
                if len(bus.acked) >= expected_acks:
                    break
            elif len(pm.spawn_calls) >= expected_spawns:
                break
            await asyncio.sleep(0.02)
        # Let any (incorrect) extra processing settle before asserting.
        await asyncio.sleep(0.1)
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=timeout_s)


class _NoopHeartbeatMonitor:
    async def run_forever(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()


class _NoopFleetRouter:
    """FleetRouter stub. ``spawn_return`` drives the return value
    of ``spawn`` so tests can assert the ACK path."""

    def __init__(
        self,
        *,
        spawn_return: bool = True,
        raise_on_spawn: bool = False,
        prior_operation: bool = False,
    ) -> None:
        self.spawn_return = spawn_return
        self.raise_on_spawn = raise_on_spawn
        self.spawn_calls: list[UUID] = []
        # Council #4 OPT C Part 1: the periodic reconciling rescan loop calls
        # this on a cadence; count the calls so tests can assert it fires more
        # than the one-shot would.
        self.rescan_calls = 0
        # PR 2 / F3: the heartbeat boot handshake calls this on a None heartbeat
        # to distinguish first-ever boot (False) from expired-after-outage (True).
        self._prior_operation = prior_operation
        self.prior_operation_probe_calls = 0

    async def reap_loop(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    async def watchdog_loop(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    async def rescan_for_restart(self) -> int:
        self.rescan_calls += 1
        return 0

    async def cancel_restart_tasks(self) -> None:
        return None

    async def has_prior_operation_evidence(self) -> bool:
        self.prior_operation_probe_calls += 1
        return self._prior_operation

    async def spawn(
        self,
        *,
        deployment_id: UUID,
        deployment_slug: str,
        payload: dict,
        idempotency_key: str,
        gateway_session_key: str | None = None,  # PR 1 T6: added kwarg
    ) -> bool:
        self.spawn_calls.append(deployment_id)
        # PR 1 T6: capture the propagated key so tests can assert it
        self.last_gateway_session_key: str | None = gateway_session_key
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
        pm = _NoopFleetRouter(spawn_return=True)

        await _run_until_acked_then_stop(bus=bus, pm=pm, expected_acks=1)

        assert bus.acked == [cmd.entry_id]
        assert pm.spawn_calls == [cmd.deployment_id]

    @pytest.mark.asyncio
    async def test_failed_handler_does_not_ack(self) -> None:
        """Decision #13: ACK only on success. A ``False`` return from
        the handler must leave the command in the PEL for retry."""
        cmd = _make_command(payload={"deployment_slug": "abcd1234abcd1234"})
        bus = _StubBus([cmd])
        pm = _NoopFleetRouter(spawn_return=False)

        await _run_until_acked_then_stop(bus=bus, pm=pm, expected_spawns=1)

        assert bus.acked == []  # NOT acked
        assert pm.spawn_calls == [cmd.deployment_id]

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_ack(self) -> None:
        """An exception inside the handler must NOT be converted into
        an ACK by an over-eager finally block (decision #13)."""
        cmd = _make_command(payload={"deployment_slug": "abcd1234abcd1234"})
        bus = _StubBus([cmd])
        pm = _NoopFleetRouter(raise_on_spawn=True)

        # raise_on_spawn means spawn_calls never appends; just let the loop
        # run briefly then stop and assert nothing was ACKed.
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
            )
        )
        await asyncio.sleep(0.2)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)

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
        class _MixedPM(_NoopFleetRouter):
            async def spawn(self, **kwargs):
                self.spawn_calls.append(kwargs["deployment_id"])
                return kwargs["deployment_slug"] != "b" * 16

        pm = _MixedPM()

        await _run_until_acked_then_stop(bus=bus, pm=pm, expected_acks=2)

        assert bus.acked == ["1-0", "3-0"]

    @pytest.mark.asyncio
    async def test_per_account_consumer_consumes_from_account_stream(self) -> None:
        """PR 2 T4: ``run_forever`` consumes from the per-account stream
        (not the global one) for each known account."""
        from msai.services.live_command_bus import command_stream_for_account

        cmd = _make_command(payload={"deployment_slug": "abcd1234abcd1234"})
        bus = _StubBus([cmd])
        pm = _NoopFleetRouter(spawn_return=True)

        await _run_until_acked_then_stop(
            bus=bus, pm=pm, expected_acks=1, known_accounts=["ACC-XYZ"]
        )

        assert command_stream_for_account("ACC-XYZ") in bus.consumed_streams

    @pytest.mark.asyncio
    async def test_account_less_command_on_base_stream_is_consumed_and_acked(self) -> None:
        """F1: an account-less command (published with empty/None ``account_id``,
        the documented legacy/single-account path) lands on the BASE stream.
        ``run_forever`` must start a consumer on the base stream so that command
        is read, dispatched, and ACKed — not stranded with no reader.

        Falsification: pre-fix ``run_forever`` started consumers ONLY for
        non-empty account ids, so the base-stream command was never consumed and
        ``bus.acked`` stayed empty.
        """
        from msai.services.live_command_bus import command_stream_for_account

        base_stream = command_stream_for_account(None)  # the global/base stream
        account_stream = command_stream_for_account("ACC-1")

        cmd = _make_command(entry_id="base-1", payload={"deployment_slug": "a" * 16})
        bus = _StreamRoutingBus({base_stream: [cmd]})
        pm = _NoopFleetRouter(spawn_return=True)

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
                rescan_interval_s=1000.0,
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while loop.time() < deadline and not bus.acked:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)

        # The base-stream command was dispatched and ACKed on the base stream.
        assert pm.spawn_calls == [cmd.deployment_id]
        assert bus.acked == [("base-1", base_stream)]
        # The base stream was consumed exactly once (no double-consume) AND the
        # per-account stream is consumed separately — not the same reader.
        assert bus.consumed_streams.count(base_stream) == 1
        assert account_stream in bus.consumed_streams

    @pytest.mark.asyncio
    async def test_base_stream_consumer_does_not_double_consume_account_commands(self) -> None:
        """F1: the base-stream consumer reads ONLY the base stream; a per-account
        command on ``base:ACC-1`` is consumed by the ACC-1 consumer, NOT the base
        consumer (no double-consume / double-dispatch)."""
        from msai.services.live_command_bus import command_stream_for_account

        account_stream = command_stream_for_account("ACC-1")
        cmd = _make_command(entry_id="acc-1", payload={"deployment_slug": "b" * 16})
        bus = _StreamRoutingBus({account_stream: [cmd]})
        pm = _NoopFleetRouter(spawn_return=True)

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
                rescan_interval_s=1000.0,
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while loop.time() < deadline and not bus.acked:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)

        # Dispatched + ACKed exactly once, on the per-account stream.
        assert pm.spawn_calls == [cmd.deployment_id]
        assert bus.acked == [("acc-1", account_stream)]

    @pytest.mark.asyncio
    async def test_publishes_router_heartbeat(self) -> None:
        """PR 2 T4: the loop stamps ``router_heartbeat`` (the 503 gate's
        liveness signal)."""
        bus = _StubBus([])
        pm = _NoopFleetRouter(spawn_return=True)

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while loop.time() < deadline and bus.heartbeats == 0:
            await asyncio.sleep(0.02)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)

        assert bus.heartbeats >= 1


# ---------------------------------------------------------------------------
# Council #4 OPT C Part 1 — the periodic reconciling rescan (the authoritative
# state-driven backstop). ``run_forever`` runs ``rescan_for_restart`` on a
# fixed interval (first pass IMMEDIATE at boot), SUBSUMING the prior one-shot
# startup rescan: there is ONE rescan code path used at boot AND at runtime.
# ---------------------------------------------------------------------------


class TestPeriodicRescan:
    @pytest.mark.asyncio
    async def test_rescan_runs_an_immediate_first_pass_at_boot(self) -> None:
        """Council #4 test 6: the FIRST rescan pass runs IMMEDIATELY at boot
        (subsuming the one-shot startup rescan) — boot recovery does not wait a
        full interval."""
        bus = _StubBus([])
        pm = _NoopFleetRouter(spawn_return=True)

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
                # A LONG interval so only the immediate boot pass can run within
                # the test window — proving the first pass is not interval-gated.
                rescan_interval_s=1000.0,
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while loop.time() < deadline and pm.rescan_calls == 0:
            await asyncio.sleep(0.02)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)

        assert pm.rescan_calls == 1, (
            "the periodic rescan must run an immediate first pass at boot "
            "(subsuming the one-shot startup rescan)"
        )

    @pytest.mark.asyncio
    async def test_rescan_re_drives_on_the_interval_falsification(self) -> None:
        """Council #4 test 2 (FALSIFICATION): a stranded ``failed``+eligible
        deployment is re-driven by the PERIODIC rescan within one interval —
        i.e. ``rescan_for_restart`` fires MORE THAN ONCE over multiple short
        intervals.

        This is the falsifying assertion for OPT C Part 1: the prior ONE-SHOT
        startup rescan would call ``rescan_for_restart`` EXACTLY ONCE and this
        ``>= 2`` assertion would FAIL. The periodic loop makes it pass.
        """
        bus = _StubBus([])
        pm = _NoopFleetRouter(spawn_return=True)

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
                # Tiny interval so several passes land within the test window.
                rescan_interval_s=0.05,
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        # Wait for the SECOND pass — the proof a stranded deployment would be
        # re-driven on a later interval, not just once at boot.
        while loop.time() < deadline and pm.rescan_calls < 2:
            await asyncio.sleep(0.02)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)

        assert pm.rescan_calls >= 2, (
            "the periodic rescan must re-drive on the interval (a one-shot rescan "
            "would fire exactly once — this is the OPT C Part 1 falsification)"
        )

    @pytest.mark.asyncio
    async def test_rescan_loop_drains_on_shutdown(self) -> None:
        """The periodic rescan loop honors ``stop_event`` and the task drains on
        shutdown (no leaked loop after the supervisor starts draining)."""
        bus = _StubBus([])
        pm = _NoopFleetRouter(spawn_return=True)

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
                rescan_interval_s=0.05,
            )
        )
        # Let at least the boot pass run, then request shutdown.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while loop.time() < deadline and pm.rescan_calls == 0:
            await asyncio.sleep(0.02)
        stop_event.set()
        # run_forever must return promptly (the loop wakes on stop_event).
        await asyncio.wait_for(task, timeout=3.0)
        assert task.done()

    @pytest.mark.asyncio
    async def test_rescan_loop_survives_a_raising_pass(self) -> None:
        """A ``rescan_for_restart`` exception on one pass must NOT kill the loop —
        the next interval still fires (exception-guarded log+continue, matching
        the sibling background loops)."""

        class _FlakyRescanRouter(_NoopFleetRouter):
            async def rescan_for_restart(self) -> int:
                self.rescan_calls += 1
                if self.rescan_calls == 1:
                    raise RuntimeError("transient DB blip during rescan")
                return 0

        bus = _StubBus([])
        pm = _FlakyRescanRouter(spawn_return=True)

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
                rescan_interval_s=0.05,
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        # The first pass raises; the loop must survive and fire again.
        while loop.time() < deadline and pm.rescan_calls < 2:
            await asyncio.sleep(0.02)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)

        assert pm.rescan_calls >= 2, "the rescan loop must survive a raising pass and keep going"


# ---------------------------------------------------------------------------
# Council #4 OPT C Part 3 — SPOF evaluate-stale-before-publish at boot.
# A restart-after-outage must EVALUATE the prior (stale) router_heartbeat —
# firing ``router_spof`` for the gap — BEFORE the heartbeat publisher overwrites
# it with a fresh stamp that would self-mask the outage.
# ---------------------------------------------------------------------------


class _SpofBus:
    """A bus stub modeling a router_heartbeat that is STALE (or ABSENT/None) on
    boot — the prior incarnation's key, left over after (or expired by) an
    outage — and is reset to FRESH the instant the publisher stamps it.
    ``read_router_heartbeat_age_s`` returns the stale age (or ``None`` for an
    expired/never-published key) until the first ``publish_router_heartbeat``
    resets it to ~0. Pass ``stale_age_s=None`` to model the EXPIRED-after-outage
    (or first-ever-boot) case the PR 2 / F3 fix disambiguates via the
    prior-operation probe."""

    def __init__(self, *, stale_age_s: float | None) -> None:
        self._age_s: float | None = stale_age_s
        self.publish_count = 0

    async def consume(
        self,
        consumer_id: str,
        stop_event: asyncio.Event,
        *,
        stream: str | None = None,
    ) -> AsyncIterator[LiveCommand]:
        # No commands; just keep the per-account consumer alive until shutdown.
        if False:  # pragma: no cover — generator with no yields needs a yield stmt
            yield  # type: ignore[unreachable]
        await stop_event.wait()

    async def ack(self, entry_id: str, *, stream: str | None = None) -> None:
        return None

    async def publish_router_heartbeat(self) -> None:
        # Publishing makes the heartbeat FRESH (age ~0) — this is exactly the
        # write that would self-mask the outage if it ran before the first eval.
        self.publish_count += 1
        self._age_s = 0.0

    async def publish_consumed_accounts(self, account_ids: list[str]) -> None:
        return None

    async def read_router_heartbeat_age_s(self) -> float | None:
        return self._age_s


class _RecordingAlertService:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str]] = []  # (subject, level)

    async def send_alert(self, subject: str, body: str, *, level: str = "warning") -> None:
        self.alerts.append((subject, level))


class TestSpofEvaluateStaleBeforePublish:
    @pytest.mark.asyncio
    async def test_router_spof_evaluates_stale_heartbeat_before_first_publish(self) -> None:
        """Council #4 test 7: on a restart-after-outage the fleet-alert loop's
        FIRST evaluation reads the PRIOR (stale) ``router_heartbeat`` and fires
        ``router_spof`` for the gap — BEFORE the heartbeat publisher overwrites it
        with a fresh stamp.

        Falsification shape: if the heartbeat publisher ran first (the pre-fix
        ordering), the first evaluation would read a FRESH age (~0) and NO
        ``router_spof`` alert would fire. The deterministic gate makes the eval
        observe the stale age, so the SPOF alert fires.
        """
        from msai.services.fleet_alerts import (
            ROUTER_HEARTBEAT_SPOF_THRESHOLD_S,
            FleetHealthSnapshot,
        )

        # Prior heartbeat is well past the SPOF threshold (the outage gap).
        bus = _SpofBus(stale_age_s=ROUTER_HEARTBEAT_SPOF_THRESHOLD_S + 120.0)
        pm = _NoopFleetRouter(spawn_return=True)
        alerts = _RecordingAlertService()

        observed_ages: list[float | None] = []

        async def _provider(consumed_accounts: list[str]) -> FleetHealthSnapshot:
            age = await bus.read_router_heartbeat_age_s()
            observed_ages.append(age)
            return FleetHealthSnapshot(
                router_heartbeat_age_s=age,
                deployments=[],
                accounts_with_active_deployments=[],
                consumed_accounts=consumed_accounts,
            )

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
                alert_service=alerts,  # type: ignore[arg-type]
                fleet_health_provider=_provider,  # type: ignore[arg-type]
                fleet_alert_interval_s=0.05,
                rescan_interval_s=1000.0,  # keep the rescan out of the way
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while loop.time() < deadline and not observed_ages:
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)

        assert observed_ages, "the fleet-alert loop must have evaluated at least once"
        # The FIRST evaluation observed the STALE prior heartbeat (NOT the fresh
        # post-publish ~0) — i.e. it ran BEFORE the first publish.
        assert observed_ages[0] is not None
        assert observed_ages[0] > ROUTER_HEARTBEAT_SPOF_THRESHOLD_S, (
            "the first fleet-alert evaluation must read the STALE prior heartbeat, "
            "not a fresh post-publish stamp (evaluate-stale-before-publish)"
        )
        # And the SPOF alert fired for the gap.
        assert any(level == "critical" for _subject, level in alerts.alerts), (
            "router_spof must fire for the outage gap before the fresh stamp masks it"
        )

    @pytest.mark.asyncio
    async def test_heartbeat_still_publishes_when_no_alert_loop_wired(self) -> None:
        """Degraded wiring (no alert loop) must NOT wedge the heartbeat: with no
        ``fleet_health_provider``/``alert_service`` the first-publish gate is set
        immediately so liveness still publishes (no deadlock)."""
        bus = _SpofBus(stale_age_s=999.0)
        pm = _NoopFleetRouter(spawn_return=True)

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
                rescan_interval_s=1000.0,
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while loop.time() < deadline and bus.publish_count == 0:
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)

        assert bus.publish_count >= 1, (
            "with no alert loop wired the heartbeat must publish immediately (gate not deadlocked)"
        )

    # -----------------------------------------------------------------------
    # PR 2 / F3 — expired-after-outage (None heartbeat) must NOT mask an outage.
    # The boot handshake distinguishes a genuine first-ever boot (no prior-
    # operation evidence) from an expired-after-outage boot (evidence present)
    # via ``has_prior_operation_evidence``, and fires SPOF on the first eval
    # BEFORE publishing fresh in the outage case.
    # -----------------------------------------------------------------------

    async def _run_and_capture_first_age(
        self, *, bus: _SpofBus, pm: _NoopFleetRouter
    ) -> tuple[list[float | None], _RecordingAlertService]:
        from msai.services.fleet_alerts import FleetHealthSnapshot

        alerts = _RecordingAlertService()
        observed_ages: list[float | None] = []

        async def _provider(consumed_accounts: list[str]) -> FleetHealthSnapshot:
            age = await bus.read_router_heartbeat_age_s()
            observed_ages.append(age)
            return FleetHealthSnapshot(
                router_heartbeat_age_s=age,
                deployments=[],
                accounts_with_active_deployments=[],
                consumed_accounts=consumed_accounts,
            )

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
                alert_service=alerts,  # type: ignore[arg-type]
                fleet_health_provider=_provider,  # type: ignore[arg-type]
                fleet_alert_interval_s=0.05,
                rescan_interval_s=1000.0,
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while loop.time() < deadline and not observed_ages:
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)
        return observed_ages, alerts

    @pytest.mark.asyncio
    async def test_expired_heartbeat_with_prior_operation_fires_spof_before_publish(
        self,
    ) -> None:
        """F3: the prior ``router_heartbeat`` key has EXPIRED (None) after an
        outage longer than its TTL, AND there is durable prior-operation
        evidence. The boot handshake must treat this as an OUTAGE — the
        fleet-alert loop's FIRST eval reads the absent (None) heartbeat and
        fires ``router_spof`` BEFORE the publisher stamps a fresh value that
        would mask the gap.

        Falsification: before the fix, a None heartbeat took the clean-boot
        branch (publish FIRST), so the first eval would read a fresh ~0 age and
        no SPOF would fire — masking the outage."""
        bus = _SpofBus(stale_age_s=None)  # expired-after-outage
        pm = _NoopFleetRouter(spawn_return=True, prior_operation=True)

        observed_ages, alerts = await self._run_and_capture_first_age(bus=bus, pm=pm)

        assert pm.prior_operation_probe_calls >= 1, (
            "a None heartbeat at boot must consult the prior-operation probe"
        )
        assert observed_ages, "the fleet-alert loop must have evaluated at least once"
        assert observed_ages[0] is None, (
            "the first eval must read the ABSENT prior heartbeat (None), not a fresh "
            "post-publish stamp — evaluate-before-publish on an expired-after-outage boot"
        )
        assert any(level == "critical" for _subject, level in alerts.alerts), (
            "router_spof (critical) must fire for the expired-after-outage gap before "
            "the fresh stamp masks it"
        )

    @pytest.mark.asyncio
    async def test_first_ever_boot_no_evidence_does_not_fire_spurious_spof(self) -> None:
        """F3 (the safety half): a genuine FIRST-EVER boot — the prior key is
        absent (None) AND there is NO prior-operation evidence (brand-new
        install) — takes the clean-boot branch: publish the fresh stamp FIRST so
        the first eval reads a healthy heartbeat and NO spurious ``router_spof``
        fires."""
        bus = _SpofBus(stale_age_s=None)  # absent key, brand-new install
        pm = _NoopFleetRouter(spawn_return=True, prior_operation=False)

        observed_ages, alerts = await self._run_and_capture_first_age(bus=bus, pm=pm)

        assert pm.prior_operation_probe_calls >= 1, "the probe distinguishes first-boot from outage"
        assert observed_ages, "the fleet-alert loop must have evaluated at least once"
        # Clean-boot branch published FIRST → the first eval reads the fresh stamp.
        assert observed_ages[0] == 0.0, (
            "on a genuine first-ever boot the publisher stamps FIRST, so the first eval "
            "reads a fresh heartbeat (no outage gap to surface)"
        )
        assert not any(level == "critical" for _subject, level in alerts.alerts), (
            "a genuine first-ever boot must NOT fire a spurious router_spof"
        )


class TestFleetAlertLoopDedupe:
    """The fleet-alert loop must own ONE deduper across its evaluation ticks so
    a PERSISTING condition pages the operator ONCE — not on every ~10s tick.

    Without this wiring the loop re-sends the same critical alert every pass,
    flooding SMTP and evicting other entries from the bounded alert history
    (Codex P2). The falsification shape: if the loop constructed a fresh deduper
    each tick (or passed none), a condition that persists across N ticks would
    send N times instead of once."""

    @pytest.mark.asyncio
    async def test_persisting_flat_condition_pages_once_across_many_ticks(self) -> None:
        from msai.services.fleet_alerts import (
            STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S,
            DeploymentHealth,
            FleetHealthSnapshot,
        )

        # A FRESH router heartbeat (no SPOF) keeps the boot handshake on the
        # clean path; the persisting condition under test is a single
        # flat-and-unmonitored account that the provider reports EVERY tick.
        bus = _SpofBus(stale_age_s=0.0)
        pm = _NoopFleetRouter(spawn_return=True, prior_operation=False)
        alerts = _RecordingAlertService()

        eval_count = 0

        async def _provider(consumed_accounts: list[str]) -> FleetHealthSnapshot:
            nonlocal eval_count
            eval_count += 1
            return FleetHealthSnapshot(
                router_heartbeat_age_s=0.0,
                deployments=[
                    DeploymentHealth(
                        deployment_id="dep-flat",
                        account_id="DU999",
                        status="failed",
                        last_heartbeat_age_s=STALE_DEPLOYMENT_HEARTBEAT_THRESHOLD_S + 30.0,
                        respawned_successfully=False,
                    )
                ],
                accounts_with_active_deployments=[],
                consumed_accounts=consumed_accounts,
            )

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            run_forever(
                bus=bus,  # type: ignore[arg-type]
                process_manager=pm,  # type: ignore[arg-type]
                heartbeat_monitor=_NoopHeartbeatMonitor(),  # type: ignore[arg-type]
                stop_event=stop_event,
                known_accounts=["ACC-1"],
                alert_service=alerts,  # type: ignore[arg-type]
                fleet_health_provider=_provider,  # type: ignore[arg-type]
                fleet_alert_interval_s=0.02,  # many ticks in the window below
                rescan_interval_s=1000.0,
            )
        )
        # Let the loop run MANY evaluation ticks against the persisting condition.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.0
        while loop.time() < deadline and eval_count < 5:
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)

        # The condition persisted across several ticks (the default 30-min
        # cooldown never elapses in this <1s window), so the flat alert must
        # have been sent EXACTLY ONCE despite many evaluations.
        assert eval_count >= 2, "the loop must have evaluated the persisting condition many times"
        flat_sends = [s for s, _level in alerts.alerts if "FLAT AND UNMONITORED" in s]
        assert len(flat_sends) == 1, (
            "a persisting flat-and-unmonitored condition must page ONCE across many ticks, "
            f"not once per tick (got {len(flat_sends)} sends across {eval_count} evals)"
        )
