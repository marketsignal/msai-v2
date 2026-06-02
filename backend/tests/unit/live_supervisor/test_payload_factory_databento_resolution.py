"""Unit tests for the supervisor's per-account Databento resolution path.

PR 1 Task T12 (multi-account-broker-fleet). The supervisor's ASYNC
payload factory pre-resolves three fields that the SYNC subprocess
needs to construct the per-account ``TradingNodeConfig``:

1. ``ibg_client_id`` — derived from ``deployment_slug`` via the shared
   :func:`derive_ibg_client_id` helper (single source of truth).
2. ``native_instrument_ids`` + ``venue_dataset_map`` — output of T10's
   :func:`resolve_databento_targets`.
3. ``canonical_to_native_bar_types`` — built here by zipping each
   strategy's canonical ``.IBKR-...`` bar type with its native
   ``.{native_venue}-...`` equivalent.

These tests exercise the factory with mocked DB + shim so the
end-to-end resolution path can be asserted on the produced
:class:`TradingNodePayload` without standing up real Nautilus / IB /
Databento clients.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from msai.services.nautilus.databento_live_config import ResolvedDatabentoTargets


def _stub_importable_paths() -> MagicMock:
    """Stub return value for ``resolve_importable_strategy_paths``."""
    paths = MagicMock()
    paths.strategy_path = "strategies.example.ema_cross:EMACrossStrategy"
    paths.config_path = "strategies.example.config:EMACrossConfig"
    return paths


def _make_session_factory(deployment: MagicMock, members: list[MagicMock]) -> MagicMock:
    """Build a mock async session whose ``execute`` returns the
    deployment row first, then the portfolio members, then the
    strategy rows. Order matches the supervisor's query order in
    ``_factory``."""
    strategy_rows = [m._strategy_row for m in members]  # noqa: SLF001 — test plumbing

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
    execute_side_effects = [deployment_scalar, members_scalars, strategy_scalars]

    mock_session = MagicMock()
    mock_session.execute = AsyncMock(side_effect=execute_side_effects)

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=mock_session_ctx)


def _make_member(symbol: str = "AAPL", order_index: int = 0) -> MagicMock:
    """Mock a ``LivePortfolioRevisionStrategy`` row + its
    ``Strategy`` row in the supervisor's preferred shape."""
    strategy = MagicMock()
    strategy.id = uuid4()
    strategy.file_path = "strategies/example/ema_cross.py"
    strategy.strategy_class = "EMACrossStrategy"

    member = MagicMock()
    member.strategy_id = strategy.id
    member.order_index = order_index
    member.instruments = [symbol]  # paper symbol
    member.config = {
        "instrument_id": f"{symbol}.NASDAQ",
        "bar_type": f"{symbol}.NASDAQ-1-MINUTE-LAST-EXTERNAL",
    }
    # Stash the strategy row for the session-factory helper.
    member._strategy_row = strategy  # noqa: SLF001 — test plumbing
    return member


@pytest.mark.asyncio
async def test_factory_embeds_per_account_databento_fields() -> None:
    """When the deployment has ``account_id``, the produced payload
    carries all three pre-resolved Databento fields PLUS a non-zero
    ``ibg_client_id``. The downstream subprocess gates on these via
    :pyattr:`TradingNodePayload.use_per_account_topology`."""
    from msai.live_supervisor.__main__ import _build_production_payload_factory

    deployment = MagicMock()
    deployment.id = uuid4()
    deployment.portfolio_revision_id = uuid4()
    deployment.account_id = "DUP733214"
    deployment.paper_trading = True
    deployment.strategy_id = uuid4()
    deployment.ib_login_key = ""

    members = [_make_member(symbol="AAPL", order_index=0)]
    session_factory = _make_session_factory(deployment, members)

    # ``lookup_for_live`` must return a ResolvedInstrument whose
    # ``canonical_id`` carries the listing venue. The supervisor
    # builds the .IBKR-suffixed list by partitioning on '.' so the
    # exact venue doesn't matter (only the root).
    resolved_instrument = MagicMock()
    resolved_instrument.canonical_id = "AAPL.NASDAQ"
    resolved_instrument.contract_spec = {"symbol": "AAPL"}

    targets = ResolvedDatabentoTargets(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
    )

    with (
        patch(
            "msai.live_supervisor.__main__.lookup_for_live",
            new=AsyncMock(return_value=[resolved_instrument]),
        ),
        patch(
            "msai.live_supervisor.__main__.resolve_databento_targets",
            new=AsyncMock(return_value=targets),
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

        factory = _build_production_payload_factory(session_factory)
        (payload,) = await factory(
            row_id=uuid4(),
            deployment_id=deployment.id,
            deployment_slug="abcdef0123456789",
            payload_dict={},
        )

    # ---- account_id (already wired via ib_account_id pre-T12) ----
    assert payload.ib_account_id == "DUP733214"

    # ---- ibg_client_id ----
    # Derived from deployment_slug via the shared helper; non-zero,
    # 31-bit positive int.
    assert payload.ibg_client_id > 0
    assert payload.ibg_client_id < 2**31

    # ---- native_instrument_ids ----
    assert payload.native_instrument_ids == ["AAPL.XNAS"]

    # ---- venue_dataset_map ----
    assert payload.venue_dataset_map == {"XNAS": "EQUS.MINI"}

    # ---- canonical_to_native_bar_types ----
    # Canonical key carries .IBKR; value carries the native venue.
    # The bar-type spec tail is preserved verbatim.
    assert payload.canonical_to_native_bar_types == {
        "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
    }

    # ---- routing predicate ----
    assert payload.use_per_account_topology is True


@pytest.mark.asyncio
async def test_factory_ibg_client_id_is_deterministic_from_slug() -> None:
    """Two spawns of the SAME deployment_slug MUST produce the SAME
    ``ibg_client_id``. Otherwise IB Gateway sees a "new" connection
    on restart and the old client's open orders + subscriptions get
    stranded (gotcha #3 + the determinism contract in the helper)."""
    from msai.live_supervisor.__main__ import _build_production_payload_factory
    from msai.services.nautilus.ibg_client_id import ROLE_EXEC, derive_ibg_client_id

    deployment = MagicMock()
    deployment.id = uuid4()
    deployment.portfolio_revision_id = uuid4()
    deployment.account_id = "DUP733214"
    deployment.paper_trading = True
    deployment.strategy_id = uuid4()
    deployment.ib_login_key = ""

    members = [_make_member()]
    session_factory = _make_session_factory(deployment, members)

    resolved_instrument = MagicMock()
    resolved_instrument.canonical_id = "AAPL.NASDAQ"
    resolved_instrument.contract_spec = {"symbol": "AAPL"}

    targets = ResolvedDatabentoTargets(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
    )

    slug = "abcdef0123456789"

    with (
        patch(
            "msai.live_supervisor.__main__.lookup_for_live",
            new=AsyncMock(return_value=[resolved_instrument]),
        ),
        patch(
            "msai.live_supervisor.__main__.resolve_databento_targets",
            new=AsyncMock(return_value=targets),
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

        factory = _build_production_payload_factory(session_factory)
        (payload,) = await factory(
            row_id=uuid4(),
            deployment_id=deployment.id,
            deployment_slug=slug,
            payload_dict={},
        )

    assert payload.ibg_client_id == derive_ibg_client_id(slug, ROLE_EXEC)


@pytest.mark.asyncio
async def test_factory_skips_per_account_path_on_non_equity() -> None:
    """PR 1 scope is equities-only. When ``resolve_databento_targets``
    raises ``NotImplementedError`` (futures / options / fx), the
    factory MUST fall back: the payload carries the legacy fields
    but the three new fields are empty so the subprocess routes to
    the legacy builder."""
    from msai.live_supervisor.__main__ import _build_production_payload_factory

    deployment = MagicMock()
    deployment.id = uuid4()
    deployment.portfolio_revision_id = uuid4()
    deployment.account_id = "DUP733214"
    deployment.paper_trading = True
    deployment.strategy_id = uuid4()
    deployment.ib_login_key = ""

    members = [_make_member(symbol="ES")]
    session_factory = _make_session_factory(deployment, members)

    resolved_instrument = MagicMock()
    resolved_instrument.canonical_id = "ES.GLOBEX"
    resolved_instrument.contract_spec = {"symbol": "ES"}

    async def _raise_futures(**_kwargs: object) -> None:
        raise NotImplementedError(
            "resolve_for_databento PR 1 scope is equities only; got asset_class='FUTURES'",
        )

    with (
        patch(
            "msai.live_supervisor.__main__.lookup_for_live",
            new=AsyncMock(return_value=[resolved_instrument]),
        ),
        patch(
            "msai.live_supervisor.__main__.resolve_databento_targets",
            new=AsyncMock(side_effect=_raise_futures),
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

        factory = _build_production_payload_factory(session_factory)
        (payload,) = await factory(
            row_id=uuid4(),
            deployment_id=deployment.id,
            deployment_slug="abcdef0123456789",
            payload_dict={},
        )

    # Per-account topology fields are EMPTY so subprocess routes to
    # the legacy builder.
    assert payload.native_instrument_ids == []
    assert payload.venue_dataset_map == {}
    assert payload.canonical_to_native_bar_types == {}
    assert payload.ibg_client_id == 0
    assert payload.use_per_account_topology is False

    # But the legacy fields are still populated so the subprocess
    # can spawn through the legacy portfolio builder.
    assert payload.ib_account_id == "DUP733214"
    assert payload.strategy_members  # non-empty


@pytest.mark.asyncio
async def test_factory_falls_back_on_resolver_miss_for_non_equity() -> None:
    """Codex iter 17 P1: when the shim raises ``RegistryMissError``
    (a ``LiveResolverError`` subclass) — e.g. supervisor strips
    ``.CME`` off ``ESM6.CME`` and the bare symbol ``ESM6`` doesn't
    match any raw_symbol (futures are keyed by ``ES``+alias) — the
    factory MUST fall back to the legacy builder instead of marking
    the deployment permanently failed. ``NotImplementedError`` is no
    longer the only fallback trigger."""
    from msai.live_supervisor.__main__ import _build_production_payload_factory
    from msai.services.nautilus.security_master.live_resolver import (
        RegistryMissError,
    )

    deployment = MagicMock()
    deployment.id = uuid4()
    deployment.portfolio_revision_id = uuid4()
    deployment.account_id = "DUP733214"
    deployment.paper_trading = True
    deployment.strategy_id = uuid4()
    deployment.ib_login_key = ""

    members = [_make_member(symbol="ESM6")]
    session_factory = _make_session_factory(deployment, members)

    resolved_instrument = MagicMock()
    resolved_instrument.canonical_id = "ESM6.CME"
    resolved_instrument.contract_spec = {"symbol": "ESM6"}

    async def _raise_miss(**_kwargs: object) -> None:
        raise RegistryMissError(symbols=["ESM6"], as_of_date=__import__("datetime").date.today())

    with (
        patch(
            "msai.live_supervisor.__main__.lookup_for_live",
            new=AsyncMock(return_value=[resolved_instrument]),
        ),
        patch(
            "msai.live_supervisor.__main__.resolve_databento_targets",
            new=AsyncMock(side_effect=_raise_miss),
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

        factory = _build_production_payload_factory(session_factory)
        (payload,) = await factory(
            row_id=uuid4(),
            deployment_id=deployment.id,
            deployment_slug="abcdef0123456789",
            payload_dict={},
        )

    # Same fallback shape as the NotImplementedError path — per-account
    # fields empty, legacy fields populated.
    assert payload.native_instrument_ids == []
    assert payload.venue_dataset_map == {}
    assert payload.canonical_to_native_bar_types == {}
    assert payload.ibg_client_id == 0
    assert payload.use_per_account_topology is False
    assert payload.ib_account_id == "DUP733214"


@pytest.mark.asyncio
async def test_factory_raises_when_ib_login_key_unknown_to_router() -> None:
    """F5 fix (Codex iter 2 P1 / silent-failure-hunter F2): when the
    deployment has a non-empty ``ib_login_key`` and a router is wired,
    the factory MUST call ``gateway_router.resolve(...)`` UNCONDITIONALLY
    (no ``is_multi_login`` gate). An unknown key MUST raise ``ValueError``
    (which the fleet_router's permanent-catch translates to
    ``SPAWN_FAILED_PERMANENT``) — NOT silently fall through to
    ``settings.ib_host/port``.

    Without this fix, a misconfigured prod env would route orders for
    account A on login key X to whatever the process-wide IB env vars
    point at — potentially the wrong broker container.
    """
    from msai.live_supervisor.__main__ import _build_production_payload_factory
    from msai.services.live.gateway_router import GatewayRouter

    # Synthetic single-login router with ``marin1016test``. The
    # deployment requests ``unknown_key`` — must NOT resolve.
    router = GatewayRouter("marin1016test:ib-gateway-paper:4002")

    deployment = MagicMock()
    deployment.id = uuid4()
    deployment.portfolio_revision_id = uuid4()
    deployment.account_id = "DUP733214"
    deployment.paper_trading = True
    deployment.strategy_id = uuid4()
    deployment.ib_login_key = "unknown_key"

    members = [_make_member(symbol="AAPL")]
    session_factory = _make_session_factory(deployment, members)

    with (
        patch("msai.live_supervisor.__main__.settings") as mock_settings,
    ):
        mock_settings.ib_port = 4004
        mock_settings.ib_host = "127.0.0.1"
        mock_settings.ib_account_id = "DUP733214"
        mock_settings.database_url = ""
        mock_settings.redis_url = ""
        mock_settings.startup_health_timeout_s = 60.0
        mock_settings.strategies_root.joinpath = MagicMock(
            return_value=MagicMock(is_file=MagicMock(return_value=False)),
        )

        factory = _build_production_payload_factory(
            session_factory,
            gateway_router=router,
        )

        # F5 (Codex iter 2 P1): the factory must reach
        # ``gateway_router.resolve("unknown_key")`` and let the ValueError
        # propagate. The fleet_router's permanent-failure catch then
        # translates this into SPAWN_FAILED_PERMANENT — no silent
        # fallback to settings.ib_host/port.
        with pytest.raises(ValueError, match="No gateway configured"):
            await factory(
                row_id=uuid4(),
                deployment_id=deployment.id,
                deployment_slug="abcdef0123456789",
                payload_dict={},
            )


@pytest.mark.asyncio
async def test_factory_resolves_single_login_router_when_ib_login_key_set() -> None:
    """F5 follow-up: a single-login router (``is_multi_login = False``)
    MUST still resolve when the deployment sets ``ib_login_key`` to the
    known key. Pre-fix, the gate ``is_multi_login`` skipped resolution
    for single-login routers and fell through to ``settings.ib_host/port``
    — silently ignoring an explicit login_key. Post-fix, single-login
    routers participate in resolution; only ``ib_login_key`` empty
    falls through to settings."""
    from msai.live_supervisor.__main__ import _build_production_payload_factory
    from msai.services.live.gateway_router import GatewayRouter
    from msai.services.nautilus.databento_live_config import ResolvedDatabentoTargets

    router = GatewayRouter("marin1016test:ib-gateway-paper:4002")
    assert not router.is_multi_login  # single-login

    deployment = MagicMock()
    deployment.id = uuid4()
    deployment.portfolio_revision_id = uuid4()
    deployment.account_id = "DUP733214"
    deployment.paper_trading = True
    deployment.strategy_id = uuid4()
    deployment.ib_login_key = "marin1016test"  # matches router

    members = [_make_member(symbol="AAPL")]
    session_factory = _make_session_factory(deployment, members)

    resolved_instrument = MagicMock()
    resolved_instrument.canonical_id = "AAPL.NASDAQ"
    resolved_instrument.contract_spec = {"symbol": "AAPL"}

    targets = ResolvedDatabentoTargets(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
    )

    with (
        patch(
            "msai.live_supervisor.__main__.lookup_for_live",
            new=AsyncMock(return_value=[resolved_instrument]),
        ),
        patch(
            "msai.live_supervisor.__main__.resolve_databento_targets",
            new=AsyncMock(return_value=targets),
        ),
        patch(
            "msai.live_supervisor.__main__.resolve_importable_strategy_paths",
            return_value=_stub_importable_paths(),
        ),
        patch("msai.live_supervisor.__main__.settings") as mock_settings,
    ):
        # settings says "different host" — so we can detect whether the
        # factory used the router or fell through to settings.
        mock_settings.ib_port = 9999
        mock_settings.ib_host = "WRONG-HOST"
        mock_settings.ib_account_id = "DUP733214"
        mock_settings.database_url = ""
        mock_settings.redis_url = ""
        mock_settings.startup_health_timeout_s = 60.0
        mock_settings.strategies_root.joinpath = MagicMock(
            return_value=MagicMock(is_file=MagicMock(return_value=False)),
        )

        factory = _build_production_payload_factory(
            session_factory,
            gateway_router=router,
        )
        (payload,) = await factory(
            row_id=uuid4(),
            deployment_id=deployment.id,
            deployment_slug="abcdef0123456789",
            payload_dict={},
        )

    # Single-login router participated — payload carries router endpoint,
    # NOT settings fallback.
    assert payload.ib_host == "ib-gateway-paper"
    assert payload.ib_port == 4002
