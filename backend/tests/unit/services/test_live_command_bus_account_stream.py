"""Tests for the per-account Redis stream helper (PR 1 T7)."""

from __future__ import annotations

from msai.services.live_command_bus import (
    LIVE_COMMAND_STREAM,
    command_stream_for_account,
)


def test_none_account_id_returns_global_stream() -> None:
    assert command_stream_for_account(None) == LIVE_COMMAND_STREAM
    assert command_stream_for_account(None) == "msai:live:commands"


def test_empty_account_id_returns_global_stream() -> None:
    # Empty string is treated the same as None — falsy.
    assert command_stream_for_account("") == LIVE_COMMAND_STREAM


def test_explicit_account_id_namespaces_stream() -> None:
    assert command_stream_for_account("DUP733214") == "msai:live:commands:DUP733214"
    assert command_stream_for_account("DUP733215") == "msai:live:commands:DUP733215"
