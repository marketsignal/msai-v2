"""Unit tests for StreamRegistry — the slug → deployment_id
resolver the projection consumer uses (Phase 3 task 3.4)."""

from __future__ import annotations

from uuid import uuid4

from msai.services.nautilus.projection.registry import StreamRegistry


def test_register_then_lookup_by_slug() -> None:
    registry = StreamRegistry()
    deployment_id = uuid4()

    registry.register(
        deployment_id=deployment_id,
        deployment_slug="ema-cross-aapl",
        stream_name="trader-MSAI-ema-cross-aapl-stream",
    )

    assert registry.deployment_id_for_slug("ema-cross-aapl") == deployment_id


def test_unknown_slug_returns_none() -> None:
    registry = StreamRegistry()
    assert registry.deployment_id_for_slug("nonexistent") is None


def test_register_replaces_existing_slug() -> None:
    registry = StreamRegistry()
    first = uuid4()
    second = uuid4()

    registry.register(
        deployment_id=first,
        deployment_slug="strat-x",
        stream_name="trader-MSAI-strat-x-stream",
    )
    registry.register(
        deployment_id=second,
        deployment_slug="strat-x",
        stream_name="trader-MSAI-strat-x-stream",
    )

    assert registry.deployment_id_for_slug("strat-x") == second


def test_unregister_clears_slug_and_stream() -> None:
    registry = StreamRegistry()
    deployment_id = uuid4()
    registry.register(
        deployment_id=deployment_id,
        deployment_slug="strat-y",
        stream_name="trader-MSAI-strat-y-stream",
    )

    registry.unregister(deployment_id)

    assert registry.deployment_id_for_slug("strat-y") is None
    assert registry.stream_name_for(deployment_id) is None
    assert registry.has_deployment(deployment_id) is False


def test_deployment_id_for_trader_id_strips_prefix() -> None:
    registry = StreamRegistry()
    deployment_id = uuid4()
    registry.register(
        deployment_id=deployment_id,
        deployment_slug="ema-cross",
        stream_name="trader-MSAI-ema-cross-stream",
    )

    assert registry.deployment_id_for_trader_id("MSAI-ema-cross") == deployment_id


def test_deployment_id_for_trader_id_without_prefix_returns_none() -> None:
    registry = StreamRegistry()
    assert registry.deployment_id_for_trader_id("ema-cross") is None


def test_active_streams_returns_snapshot_copy() -> None:
    registry = StreamRegistry()
    a, b = uuid4(), uuid4()
    registry.register(deployment_id=a, deployment_slug="x", stream_name="trader-MSAI-x-stream")
    registry.register(deployment_id=b, deployment_slug="y", stream_name="trader-MSAI-y-stream")

    snapshot = registry.active_streams()
    assert snapshot == {a: "trader-MSAI-x-stream", b: "trader-MSAI-y-stream"}

    # Mutating the snapshot must not affect internal state
    snapshot.clear()
    assert len(registry) == 2


def test_known_slugs_lists_all_registered() -> None:
    registry = StreamRegistry()
    registry.register(
        deployment_id=uuid4(), deployment_slug="a", stream_name="trader-MSAI-a-stream"
    )
    registry.register(
        deployment_id=uuid4(), deployment_slug="b", stream_name="trader-MSAI-b-stream"
    )

    assert set(registry.known_slugs()) == {"a", "b"}


def test_has_deployment_after_register() -> None:
    registry = StreamRegistry()
    deployment_id = uuid4()
    registry.register(
        deployment_id=deployment_id,
        deployment_slug="z",
        stream_name="trader-MSAI-z-stream",
    )
    assert registry.has_deployment(deployment_id) is True
