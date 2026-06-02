"""Tests for handle_command's gateway_session_key propagation (PR 1 T6).

Council 2026-05-27 blocking objection #2 + 2026-05-29 blocking objection #12:
the START path MUST pull ``gateway_session_key`` from the command payload and
pass it to ``FleetRouter.spawn(...)``. Without this, per-session startup
serialization silently degrades to global via the DB-row ``ib_login_key`` or
``"default"`` fallback inside ``FleetRouter``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from msai.live_supervisor.main import handle_command
from msai.services.live_command_bus import LiveCommand, LiveCommandType


def _make_command(*, payload: dict) -> LiveCommand:
    return LiveCommand(
        entry_id="0-1",
        command_type=LiveCommandType.START,
        deployment_id=uuid4(),
        idempotency_key="idem-test",
        payload=payload,
    )


@pytest.mark.asyncio
async def test_handle_command_start_passes_gateway_session_key_when_in_payload() -> None:
    pm = MagicMock()
    pm.spawn = AsyncMock(return_value=True)

    command = _make_command(
        payload={
            "deployment_slug": "p-A",
            "gateway_session_key": "marin1016test",
        }
    )

    ack = await handle_command(command, process_manager=pm)

    assert ack is True
    pm.spawn.assert_awaited_once()
    kwargs = pm.spawn.await_args.kwargs
    assert kwargs.get("gateway_session_key") == "marin1016test"


@pytest.mark.asyncio
async def test_handle_command_start_omits_gateway_session_key_when_absent() -> None:
    # Backwards-compat: payload without the key still works (FleetRouter
    # falls back to the deployment row's ib_login_key).
    pm = MagicMock()
    pm.spawn = AsyncMock(return_value=True)

    command = _make_command(payload={"deployment_slug": "p-B"})

    await handle_command(command, process_manager=pm)

    kwargs = pm.spawn.await_args.kwargs
    assert kwargs.get("gateway_session_key") is None
