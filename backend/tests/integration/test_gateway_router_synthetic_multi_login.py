"""Synthetic multi-login GatewayRouter test (PR 1 T13).

Council 2026-05-29 blocking objection #14: even though PR 1 ships
single-login (Shape A on ``marin1016test``), the
``_build_production_payload_factory()`` multi-login branch at
``__main__.py:185`` MUST have coverage. Without this, the multi-login
route table is silently undertested until LVP/HVP graduation — and
that's the moment of highest stakes (real money), not the moment to
discover bugs in production-fleet routing.

This test wires a 2-login ``GatewayRouter`` synthetically (NO real IB
Gateway containers), feeds it through the factory with mocked
deployments under each login, and asserts the produced payload's
``ib_host``/``ib_port`` come from the router's resolution — NOT from
the process-wide ``settings.ib_host``/``settings.ib_port``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from msai.services.live.gateway_router import GatewayRouter
from msai.services.nautilus.databento_live_config import ResolvedDatabentoTargets


def _stub_importable_paths() -> MagicMock:
    paths = MagicMock()
    paths.strategy_path = "strategies.example.ema_cross:EMACrossStrategy"
    paths.config_path = "strategies.example.config:EMACrossConfig"
    return paths


def _make_member(symbol: str = "AAPL", order_index: int = 0) -> MagicMock:
    """Mock a ``LivePortfolioRevisionStrategy`` row + its ``Strategy`` row."""
    strategy = MagicMock()
    strategy.id = uuid4()
    strategy.file_path = "strategies/example/ema_cross.py"
    strategy.strategy_class = "EMACrossStrategy"

    member = MagicMock()
    member.strategy_id = strategy.id
    member.order_index = order_index
    member.instruments = [symbol]
    member.config = {
        "instrument_id": f"{symbol}.NASDAQ",
        "bar_type": f"{symbol}.NASDAQ-1-MINUTE-LAST-EXTERNAL",
    }
    member._strategy_row = strategy  # noqa: SLF001 — test plumbing
    return member


def _make_session_factory(deployment: MagicMock, members: list[MagicMock]) -> MagicMock:
    """Mirror the per-account-payload test's session-factory helper."""
    strategy_rows = [m._strategy_row for m in members]  # noqa: SLF001

    deployment_scalar = MagicMock(
        scalar_one_or_none=MagicMock(return_value=deployment),
    )
    members_scalars = MagicMock()
    members_scalars.scalars = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=members)),
    )
    strategy_scalars = MagicMock()
    strategy_scalars.scalars = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=strategy_rows)),
    )

    mock_session = MagicMock()
    mock_session.execute = AsyncMock(
        side_effect=[deployment_scalar, members_scalars, strategy_scalars],
    )
    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=mock_session_ctx)


def _make_deployment(*, ib_login_key: str, account_id: str) -> MagicMock:
    deployment = MagicMock()
    deployment.id = uuid4()
    deployment.deployment_slug = f"slug-{account_id.lower()}"
    deployment.portfolio_revision_id = uuid4()
    deployment.paper_trading = True
    deployment.account_id = account_id
    deployment.ib_login_key = ib_login_key
    return deployment


@pytest.mark.asyncio
async def test_factory_multi_login_uses_router_resolved_host_port_for_marin() -> None:
    """When GatewayRouter has multi-login config + the deployment has
    ``ib_login_key``, the factory's payload carries the ROUTER-resolved
    (host, port), NOT the process-wide settings defaults."""
    from msai.live_supervisor.__main__ import _build_production_payload_factory

    router = GatewayRouter(
        "marin1016test:ib-gateway-paper:4002:accounts=DUP733214|DUP733215,"
        "mslvp000:ib-gateway-lvp:4001:accounts=U1234567"
    )
    assert router.is_multi_login  # synthetic precondition

    deployment = _make_deployment(ib_login_key="marin1016test", account_id="DUP733214")
    members = [_make_member("AAPL")]
    session_factory = _make_session_factory(deployment, members)

    resolved = ResolvedDatabentoTargets(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
    )

    resolved_instrument = MagicMock()
    resolved_instrument.canonical_id = "AAPL.IBKR"
    resolved_instrument.contract_spec = {"symbol": "AAPL"}

    with (
        patch(
            "msai.live_supervisor.__main__.lookup_for_live",
            new=AsyncMock(return_value=[resolved_instrument]),
        ),
        patch(
            "msai.live_supervisor.__main__.resolve_databento_targets",
            new=AsyncMock(return_value=resolved),
        ),
        patch(
            "msai.live_supervisor.__main__.resolve_importable_strategy_paths",
            return_value=_stub_importable_paths(),
        ),
        patch("msai.live_supervisor.__main__.settings") as mock_settings,
    ):
        mock_settings.ib_port = 4002
        mock_settings.ib_host = "127.0.0.1"
        mock_settings.ib_account_id = "DUP733214"
        mock_settings.database_url = ""
        mock_settings.redis_url = ""
        mock_settings.startup_health_timeout_s = 60.0
        mock_settings.strategies_root.joinpath = MagicMock(
            return_value=MagicMock(is_file=MagicMock(return_value=False)),
        )
        factory = _build_production_payload_factory(session_factory, gateway_router=router)
        (payload,) = await factory(
            row_id=uuid4(),
            deployment_id=deployment.id,
            deployment_slug=deployment.deployment_slug,
            payload_dict={},
        )

    # Router-resolved endpoint, NOT settings defaults
    assert payload.ib_host == "ib-gateway-paper"
    assert payload.ib_port == 4002


@pytest.mark.asyncio
async def test_factory_multi_login_uses_router_resolved_host_port_for_mslvp() -> None:
    """Companion of the marin case: same router, different login key
    → different (host, port) resolution. Asserts the multi-login
    dispatch is keyed by ``ib_login_key``, not falling through to a
    single fixed endpoint."""
    from msai.live_supervisor.__main__ import _build_production_payload_factory

    router = GatewayRouter(
        "marin1016test:ib-gateway-paper:4002:accounts=DUP733214|DUP733215,"
        "mslvp000:ib-gateway-lvp:4001:accounts=U1234567"
    )

    # mslvp is a LIVE account (no DU prefix) — paper_trading=False to satisfy
    # the IB-port validator. Using the LIVE port 4001 from the router.
    deployment = _make_deployment(ib_login_key="mslvp000", account_id="U1234567")
    deployment.paper_trading = False
    members = [_make_member("AAPL")]
    session_factory = _make_session_factory(deployment, members)

    resolved = ResolvedDatabentoTargets(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
    )

    resolved_instrument = MagicMock()
    resolved_instrument.canonical_id = "AAPL.IBKR"
    resolved_instrument.contract_spec = {"symbol": "AAPL"}

    with (
        patch(
            "msai.live_supervisor.__main__.lookup_for_live",
            new=AsyncMock(return_value=[resolved_instrument]),
        ),
        patch(
            "msai.live_supervisor.__main__.resolve_databento_targets",
            new=AsyncMock(return_value=resolved),
        ),
        patch(
            "msai.live_supervisor.__main__.resolve_importable_strategy_paths",
            return_value=_stub_importable_paths(),
        ),
        patch("msai.live_supervisor.__main__.settings") as mock_settings,
    ):
        mock_settings.ib_port = 4002
        mock_settings.ib_host = "127.0.0.1"
        mock_settings.ib_account_id = "DUP733214"
        mock_settings.database_url = ""
        mock_settings.redis_url = ""
        mock_settings.startup_health_timeout_s = 60.0
        mock_settings.strategies_root.joinpath = MagicMock(
            return_value=MagicMock(is_file=MagicMock(return_value=False)),
        )
        factory = _build_production_payload_factory(session_factory, gateway_router=router)
        (payload,) = await factory(
            row_id=uuid4(),
            deployment_id=deployment.id,
            deployment_slug=deployment.deployment_slug,
            payload_dict={},
        )

    assert payload.ib_host == "ib-gateway-lvp"
    assert payload.ib_port == 4001


def test_synthetic_router_correctness_floor() -> None:
    """Smoke-floor for the synthetic-only invariant: even in PR 1's
    single-login Shape A, a router parser bug in the multi-login
    grammar would silently break LVP/HVP graduation. This test
    asserts the parser handles the multi-login form deterministically."""
    router = GatewayRouter(
        "marin1016test:ib-gateway-paper:4002:accounts=DUP733214|DUP733215,"
        "mslvp000:ib-gateway-lvp:4001:accounts=U1234567"
    )
    marin = router.resolve("marin1016test")
    mslvp = router.resolve("mslvp000")
    assert marin.host == "ib-gateway-paper"
    assert marin.port == 4002
    assert mslvp.host == "ib-gateway-lvp"
    assert mslvp.port == 4001
    # Account lists per login, pipe-separated grammar
    assert router.accounts_for("marin1016test") == ["DUP733214", "DUP733215"]
    assert router.accounts_for("mslvp000") == ["U1234567"]
    # Fail-closed: duplicate login raises (council 2026-05-29 obj #13)
    with pytest.raises(ValueError, match="duplicate ib_login_key"):
        GatewayRouter("marin1016test:host-a:4002,marin1016test:host-b:4001")
