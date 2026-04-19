"""Regression tests for the IB probe lifecycle (drill 2026-04-15).

The ``/api/v1/account/health`` endpoint reads ``_ib_probe.is_healthy``,
but the probe's ``run_periodic`` was never started — so the flag stayed
False forever and the endpoint always reported ``gateway_connected=
false`` regardless of whether IB Gateway was actually reachable. Drill
2026-04-15 was misled three times before the gap was found.

These tests pin the lifecycle:

- ``start_ib_probe_task`` creates a background task running the probe
- Calling start twice is idempotent (second call no-ops)
- ``stop_ib_probe_task`` cancels and awaits the task
- The endpoint reflects ``is_healthy`` after the probe runs
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_start_ib_probe_task_creates_background_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """The startup helper must spawn an asyncio task that calls
    ``IBProbe.run_periodic`` so the periodic check_health loop
    actually runs. Before the fix the probe was instantiated but
    its loop was never started."""
    from msai.api import account

    started: list[int] = []

    async def fake_run_periodic(self: object, interval: int = 30) -> None:
        started.append(interval)
        # Block forever so the task stays alive for the assertion.
        await asyncio.sleep(3600)

    monkeypatch.setattr("msai.services.ib_probe.IBProbe.run_periodic", fake_run_periodic)
    # Ensure no pre-existing task leaked from another test.
    await account.stop_ib_probe_task()

    await account.start_ib_probe_task()
    # Yield once so the task has a chance to start executing.
    await asyncio.sleep(0)

    assert account._probe_task is not None  # noqa: SLF001
    assert not account._probe_task.done()  # noqa: SLF001
    assert started == [30]

    await account.stop_ib_probe_task()


@pytest.mark.asyncio
async def test_start_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling ``start_ib_probe_task`` twice must NOT spawn a second
    task. The first task keeps running; the second call returns
    without doing anything."""
    from msai.api import account

    call_count = 0

    async def fake_run_periodic(self: object, interval: int = 30) -> None:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(3600)

    monkeypatch.setattr("msai.services.ib_probe.IBProbe.run_periodic", fake_run_periodic)
    await account.stop_ib_probe_task()

    await account.start_ib_probe_task()
    first_task = account._probe_task  # noqa: SLF001
    await asyncio.sleep(0)

    await account.start_ib_probe_task()
    second_task = account._probe_task  # noqa: SLF001
    await asyncio.sleep(0)

    assert first_task is second_task, "second start must reuse the existing task"
    assert call_count == 1, "run_periodic must only be called once"

    await account.stop_ib_probe_task()


@pytest.mark.asyncio
async def test_stop_cancels_and_clears_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop_ib_probe_task`` must cancel the running task and reset
    the module-level handle to None so a subsequent start cleanly
    creates a new task instead of trying to reuse the cancelled one."""
    from msai.api import account

    async def fake_run_periodic(self: object, interval: int = 30) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr("msai.services.ib_probe.IBProbe.run_periodic", fake_run_periodic)
    await account.stop_ib_probe_task()

    await account.start_ib_probe_task()
    task = account._probe_task  # noqa: SLF001
    assert task is not None
    await asyncio.sleep(0)

    await account.stop_ib_probe_task()

    assert account._probe_task is None  # noqa: SLF001
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_stop_when_no_task_running_is_safe() -> None:
    """Calling ``stop_ib_probe_task`` when nothing is running must
    not raise — supports cleaner shutdown paths and recovery."""
    from msai.api import account

    await account.stop_ib_probe_task()  # idempotent
    await account.stop_ib_probe_task()  # second call also fine
    assert account._probe_task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_stop_clears_cached_probe_state() -> None:
    """Codex review P3 regression: a stop/start cycle in the same
    process must not leak the previous cycle's status. If the old
    probe cached ``is_healthy=True`` then the gateway went down
    before restart, ``/account/health`` would falsely report
    healthy until the next probe tick after restart."""
    from msai.api import account

    # Simulate probe having run and flipped state to healthy.
    account._ib_probe._is_healthy = True  # noqa: SLF001
    account._ib_probe._consecutive_failures = 5  # noqa: SLF001

    await account.stop_ib_probe_task()

    assert account._ib_probe._is_healthy is False  # noqa: SLF001
    assert account._ib_probe._consecutive_failures == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_health_endpoint_reflects_probe_state_after_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end behaviour: once the probe's ``check_health``
    succeeds, the ``/api/v1/account/health`` endpoint must report
    ``gateway_connected=true``. Before the fix the endpoint always
    returned False because no ``check_health`` was ever called."""
    from msai.api import account

    # Force the probe's check_health to flip is_healthy to True
    # immediately, simulating a successful TCP connect to IB Gateway.
    async def fake_check_health(self: object) -> bool:
        self._is_healthy = True  # type: ignore[attr-defined]
        self._consecutive_failures = 0  # type: ignore[attr-defined]
        return True

    async def fake_run_periodic(self: object, interval: int = 30) -> None:
        # Single check then stay alive — mirrors what the real loop
        # does on the first iteration.
        await fake_check_health(self)
        await asyncio.sleep(3600)

    monkeypatch.setattr("msai.services.ib_probe.IBProbe.run_periodic", fake_run_periodic)

    # Reset probe state to the freshly-instantiated baseline.
    account._ib_probe._is_healthy = False  # noqa: SLF001
    account._ib_probe._consecutive_failures = 0  # noqa: SLF001
    await account.stop_ib_probe_task()

    await account.start_ib_probe_task()
    # Yield twice: once for task to start, once for fake_run_periodic
    # to reach the fake_check_health call.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    response = await account.account_health(claims={"sub": "test"})
    assert response["gateway_connected"] is True
    assert response["status"] == "healthy"

    await account.stop_ib_probe_task()
