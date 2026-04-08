"""Unit tests for the live-stream WebSocket handler
(Phase 3 task 3.6).

The handler has three layers worth testing in isolation:

1. ``_authenticate`` — first-message JWT/API key validation
   with a 5 s timeout.
2. ``_send_initial_snapshot`` — calls ``PositionReader`` and
   formats the snapshot JSON the client receives on connect.
3. ``_forward_pubsub_to_websocket`` — pulls messages off the
   subscription and forwards verbatim, dropping malformed
   ones without crashing.

The full end-to-end flow (real Redis + real DB + real
WebSocket) is exercised by the integration test in
``tests/integration/test_websocket_live_events.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import WebSocketDisconnect

from msai.api.websocket import (
    _authenticate,
    _forward_pubsub_to_websocket,
    _heartbeat_loop,
    _send_initial_snapshot,
)
from msai.services.nautilus.projection.events import (
    AccountStateUpdate,
    PositionSnapshot,
)


def _fake_websocket() -> Any:
    """Build an AsyncMock that quacks like a Starlette
    WebSocket — accept(), receive_text(), send_text(),
    send_json(), close()."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.receive_text = AsyncMock()
    ws.send_text = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# _authenticate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_valid_token_returns_claims(monkeypatch: Any) -> None:
    ws = _fake_websocket()
    ws.receive_text.return_value = "valid-token"
    monkeypatch.setattr(
        "msai.api.websocket.validate_token_or_api_key",
        lambda token: {"sub": "user-123"},
    )

    result = await _authenticate(ws)

    assert result == {"sub": "user-123"}
    ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_authenticate_bad_token_closes_socket(monkeypatch: Any) -> None:
    ws = _fake_websocket()
    ws.receive_text.return_value = "bad"

    def boom(token: str) -> dict[str, Any]:
        raise ValueError("bad token")

    monkeypatch.setattr("msai.api.websocket.validate_token_or_api_key", boom)

    result = await _authenticate(ws)

    assert result is None
    ws.close.assert_awaited_once()
    args, kwargs = ws.close.call_args
    assert kwargs.get("code") == 4001


@pytest.mark.asyncio
async def test_authenticate_timeout_closes_socket() -> None:
    ws = _fake_websocket()

    async def hang() -> str:
        await asyncio.sleep(10)
        return "never"

    ws.receive_text.side_effect = hang

    # Patch the timeout to a tiny value so the test doesn't
    # actually wait 5 seconds. We do this by monkeypatching
    # asyncio.wait_for to expire immediately.
    import msai.api.websocket as ws_mod

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(coro: Any, timeout: float) -> Any:
        return await real_wait_for(coro, timeout=0.05)

    ws_mod.asyncio.wait_for = fast_wait_for  # type: ignore[assignment]
    try:
        result = await _authenticate(ws)
    finally:
        ws_mod.asyncio.wait_for = real_wait_for  # type: ignore[assignment]

    assert result is None
    ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_authenticate_disconnect_returns_none() -> None:
    ws = _fake_websocket()
    ws.receive_text.side_effect = WebSocketDisconnect

    result = await _authenticate(ws)

    assert result is None
    # No close call — the socket is already disconnected
    ws.close.assert_not_called()


# ---------------------------------------------------------------------------
# _send_initial_snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_initial_snapshot_with_positions_and_account() -> None:
    ws = _fake_websocket()
    deployment_id = uuid4()
    deployment = MagicMock()
    deployment.id = deployment_id
    deployment.trader_id = "MSAI-test"
    deployment.strategy_id_full = "EMACross-test"
    deployment.account_id = "DU12345"

    pos = PositionSnapshot(
        deployment_id=deployment_id,
        instrument_id="AAPL.NASDAQ",
        qty=Decimal("100"),
        avg_price=Decimal("150"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        ts=datetime.now(UTC),
    )
    acct = AccountStateUpdate(
        deployment_id=deployment_id,
        account_id="DU12345",
        balance=Decimal("100000"),
        margin_used=Decimal("0"),
        margin_available=Decimal("100000"),
        ts=datetime.now(UTC),
    )

    reader = MagicMock()
    reader.get_open_positions = AsyncMock(return_value=[pos])
    reader.get_account = AsyncMock(return_value=acct)

    await _send_initial_snapshot(ws, deployment=deployment, position_reader=reader)

    ws.send_json.assert_awaited_once()
    payload = ws.send_json.call_args.args[0]
    assert payload["type"] == "snapshot"
    assert payload["deployment_id"] == str(deployment_id)
    assert len(payload["positions"]) == 1
    assert payload["positions"][0]["instrument_id"] == "AAPL.NASDAQ"
    assert payload["account"] is not None
    assert payload["account"]["account_id"] == "DU12345"


@pytest.mark.asyncio
async def test_send_initial_snapshot_with_no_account() -> None:
    ws = _fake_websocket()
    deployment_id = uuid4()
    deployment = MagicMock()
    deployment.id = deployment_id
    deployment.trader_id = "MSAI-test"
    deployment.strategy_id_full = "EMACross-test"
    deployment.account_id = "DU12345"

    reader = MagicMock()
    reader.get_open_positions = AsyncMock(return_value=[])
    reader.get_account = AsyncMock(return_value=None)

    await _send_initial_snapshot(ws, deployment=deployment, position_reader=reader)

    payload = ws.send_json.call_args.args[0]
    assert payload["positions"] == []
    assert payload["account"] is None


# ---------------------------------------------------------------------------
# _forward_pubsub_to_websocket
# ---------------------------------------------------------------------------


class FakePubSub:
    """Stub PubSub that pops queued messages off a list and
    returns ``None`` once the queue is exhausted."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = list(messages)
        self.idle_then_done = False

    async def get_message(
        self, *, ignore_subscribe_messages: bool, timeout: float
    ) -> dict[str, Any] | None:
        if self._messages:
            return self._messages.pop(0)
        # Once empty, stop the loop by raising disconnect-equivalent
        raise WebSocketDisconnect


@pytest.mark.asyncio
async def test_forward_valid_json_message_writes_to_socket() -> None:
    ws = _fake_websocket()
    payload = json.dumps({"event_type": "fill", "deployment_id": str(uuid4())})
    pubsub = FakePubSub([{"type": "message", "data": payload.encode("utf-8")}])

    # The forwarder swallows WebSocketDisconnect on get_message
    await _forward_pubsub_to_websocket(ws, pubsub, "msai:live:events:test")

    ws.send_text.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_forward_drops_malformed_payload() -> None:
    ws = _fake_websocket()
    pubsub = FakePubSub(
        [
            {"type": "message", "data": b"not-json-{{{"},
            {"type": "message", "data": json.dumps({"event_type": "fill"}).encode()},
        ]
    )

    await _forward_pubsub_to_websocket(ws, pubsub, "msai:live:events:test")

    # The good message was forwarded; the bad one was dropped
    assert ws.send_text.await_count == 1


@pytest.mark.asyncio
async def test_forward_handles_str_data() -> None:
    ws = _fake_websocket()
    payload = json.dumps({"event_type": "fill", "deployment_id": str(uuid4())})
    # Some Redis client configs return str instead of bytes
    pubsub = FakePubSub([{"type": "message", "data": payload}])

    await _forward_pubsub_to_websocket(ws, pubsub, "msai:live:events:test")

    ws.send_text.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_forward_skips_none_data() -> None:
    ws = _fake_websocket()
    pubsub = FakePubSub(
        [
            {"type": "message", "data": None},
            {"type": "message", "data": json.dumps({"event_type": "fill"}).encode()},
        ]
    )

    await _forward_pubsub_to_websocket(ws, pubsub, "msai:live:events:test")

    assert ws.send_text.await_count == 1


# ---------------------------------------------------------------------------
# _heartbeat_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_loop_sends_periodic_pings(monkeypatch: Any) -> None:
    """Replace the heartbeat interval with something tiny so
    the test runs in milliseconds. Verify the loop sends at
    least one heartbeat before being cancelled."""
    ws = _fake_websocket()
    monkeypatch.setattr("msai.api.websocket._HEARTBEAT_INTERVAL_SECONDS", 0.01)

    task = asyncio.create_task(_heartbeat_loop(ws))
    await asyncio.sleep(0.05)  # Allow several iterations
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert ws.send_json.await_count >= 1
    payload = ws.send_json.call_args.args[0]
    assert payload["type"] == "heartbeat"
    assert "ts" in payload


@pytest.mark.asyncio
async def test_heartbeat_loop_exits_cleanly_on_disconnect() -> None:
    ws = _fake_websocket()
    ws.send_json.side_effect = WebSocketDisconnect
    # Use a tiny interval so the loop reaches send_json fast
    import msai.api.websocket as ws_mod

    real = ws_mod._HEARTBEAT_INTERVAL_SECONDS
    ws_mod._HEARTBEAT_INTERVAL_SECONDS = 0.01  # type: ignore[assignment]
    try:
        await asyncio.wait_for(_heartbeat_loop(ws), timeout=0.5)
    finally:
        ws_mod._HEARTBEAT_INTERVAL_SECONDS = real  # type: ignore[assignment]
    # No exception escaped — the loop returned cleanly
