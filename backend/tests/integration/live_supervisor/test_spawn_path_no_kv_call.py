"""Regression guard: the supervisor's per-spawn / warm-restart path makes
NO Key Vault (or any broker-credential) call — it reads only the DB.

Council blocking objection (Scalability Hawk, PR "broker-account-spawn-wiring",
narrowed Option A): credential resolution (:meth:`BrokerAccountService.
resolve_for_spawn`, which reads the credentials store — Azure Key Vault in
prod, an env-file store in dev) happens at DEPLOY time on the control plane
(the API), NEVER in the supervisor's spawn path. The supervisor's payload
factory recovers everything it needs (account_id, ib_login_key, strategy
paths, registry-resolved instruments) from the ``live_deployments`` row and
the instrument registry — never from the secret store.

This test LOCKS that invariant. It drives the cleanest unit of the spawn
path — :func:`_build_production_payload_factory`'s returned async factory,
which is exactly what :meth:`FleetRouter.spawn` invokes to build the
``TradingNodePayload`` (the point in the path where a tempting KV call
would live) — to a fully-built payload, with spies on BOTH
:meth:`BrokerAccountService.resolve_for_spawn` AND every credentials-store
``.get(...)`` implementation. It asserts the payload is built AND neither
the broker service nor any store ``.get`` was touched.

It PASSES today (the spawn path was never wired to KV). If it ever FAILS,
some change wrongly added a credential resolution to the supervisor spawn
path — a real finding, not a test bug.

Harness: reuses the lightweight MagicMock session-factory shape from
``tests/unit/live_supervisor/test_payload_factory_databento_resolution.py``
(deployment row -> portfolio members -> strategy rows, in the supervisor's
query order), and the same registry/databento/strategy-path patches, so no
real Nautilus / IB / Databento / Postgres stack is stood up and no real
subprocess launches.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from msai.services.live.broker_account_service import BrokerAccountService
from msai.services.live.broker_credentials_store import (
    AzureKvBrokerCredentialsStore,
    EnvFileBrokerCredentialsStore,
)
from msai.services.nautilus.databento_live_config import ResolvedDatabentoTargets


def _stub_importable_paths() -> MagicMock:
    """Stub return value for ``resolve_importable_strategy_paths``."""
    paths = MagicMock()
    paths.strategy_path = "strategies.example.ema_cross:EMACrossStrategy"
    paths.config_path = "strategies.example.config:EMACrossConfig"
    return paths


def _make_member(symbol: str = "AAPL", order_index: int = 0) -> MagicMock:
    """Mock a ``LivePortfolioRevisionStrategy`` row + its ``Strategy`` row
    in the supervisor's preferred shape (mirrors the payload-factory
    databento-resolution unit test)."""
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
    member._strategy_row = strategy  # noqa: SLF001 — test plumbing
    return member


def _make_session_factory(deployment: MagicMock, members: list[MagicMock]) -> MagicMock:
    """Mock async session whose ``execute`` returns the deployment row,
    then the portfolio members, then the strategy rows — matching the
    supervisor's query order in ``_factory``."""
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


@pytest.mark.asyncio
async def test_spawn_path_builds_payload_without_any_kv_credential_call() -> None:
    """Driving the supervisor's payload factory (the unit
    :meth:`FleetRouter.spawn` calls) to a fully-built ``TradingNodePayload``
    must NOT call :meth:`BrokerAccountService.resolve_for_spawn` nor any
    credentials-store ``.get(...)``. The spawn / warm-restart path reads
    only the DB; credentials are resolved at deploy time on the API.
    """
    from msai.live_supervisor.__main__ import _build_production_payload_factory

    # ---- Arrange: a real-money-shaped paper deployment row + one member ----
    deployment = MagicMock()
    deployment.id = uuid4()
    deployment.portfolio_revision_id = uuid4()
    deployment.account_id = "DUP733214"
    deployment.paper_trading = True
    deployment.strategy_id = uuid4()
    deployment.ib_login_key = ""

    members = [_make_member(symbol="AAPL", order_index=0)]
    session_factory = _make_session_factory(deployment, members)

    resolved_instrument = MagicMock()
    resolved_instrument.canonical_id = "AAPL.NASDAQ"
    resolved_instrument.contract_spec = {"symbol": "AAPL"}

    targets = ResolvedDatabentoTargets(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
    )

    # Spies that PRESERVE the original behavior (wraps=...) so that if the
    # spawn path ever did call them, the call would still work AND be
    # recorded — the assertion below is what fails, pinpointing the
    # regression rather than masking it.
    resolve_spy = MagicMock(wraps=BrokerAccountService.resolve_for_spawn)
    env_get_spy = MagicMock(wraps=EnvFileBrokerCredentialsStore.get)
    azure_get_spy = MagicMock(wraps=AzureKvBrokerCredentialsStore.get)

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
        # Credential-resolution spies — the invariant under test.
        patch.object(BrokerAccountService, "resolve_for_spawn", resolve_spy),
        patch.object(EnvFileBrokerCredentialsStore, "get", env_get_spy),
        patch.object(AzureKvBrokerCredentialsStore, "get", azure_get_spy),
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

        # ---- Act: build the factory and run the full spawn payload build ----
        factory = _build_production_payload_factory(session_factory)
        (payload,) = await factory(
            row_id=uuid4(),
            deployment_id=deployment.id,
            deployment_slug="abcdef0123456789",
            payload_dict={},
        )

    # ---- Assert: the payload was actually built (path was driven past the
    # credential-tempting point) ... ----
    assert payload.ib_account_id == "DUP733214"
    assert payload.strategy_members  # non-empty
    assert payload.use_per_account_topology is True

    # ---- ... AND no credential resolution happened on the spawn path. ----
    resolve_spy.assert_not_called()
    env_get_spy.assert_not_called()
    azure_get_spy.assert_not_called()
