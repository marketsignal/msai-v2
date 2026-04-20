"""Guard against Nautilus issue #3176: ``external_order_claims`` creates
duplicate orders on restart.

Nautilus issue #3176 documented that setting ``external_order_claims`` on
the exec engine config causes duplicate orders to be generated during
reconciliation on restart. The fix was to NOT set that field (or set it
to an empty/None value) and instead use the default reconciliation path.

This test verifies that ``build_portfolio_trading_node_config`` does NOT
set ``external_order_claims`` on either the ``LiveExecEngineConfig`` or
any ``InteractiveBrokersExecClientConfig``, preventing the duplicate
order regression from being reintroduced.

Additionally, we verify ``filter_unclaimed_external_orders`` (the correct
Nautilus approach for external-order handling) is left at its default
``False`` — claiming external orders without explicit operator intent
drops legitimate fills from reconciliation.
"""

from __future__ import annotations

from uuid import uuid4

from msai.services.nautilus.live_node_config import (
    IBSettings,
    build_portfolio_trading_node_config,
)
from msai.services.nautilus.security_master.live_resolver import (
    AssetClass,
    ResolvedInstrument,
)
from msai.services.nautilus.trading_node_subprocess import StrategyMemberPayload


def _synth_resolved(symbols: list[str]) -> tuple[ResolvedInstrument, ...]:
    from datetime import date

    return tuple(
        ResolvedInstrument(
            canonical_id=s if "." in s else f"{s}.NASDAQ",
            asset_class=AssetClass.EQUITY,
            contract_spec={
                "secType": "STK",
                "symbol": s.partition(".")[0],
                "exchange": "SMART",
                "primaryExchange": s.partition(".")[2] or "NASDAQ",
                "currency": "USD",
            },
            effective_window=(date(2026, 1, 1), None),
        )
        for s in symbols
    )


def _make_member(
    *,
    instruments: list[str] | None = None,
    strategy_id_full: str = "",
) -> StrategyMemberPayload:
    instruments_val = instruments if instruments is not None else ["AAPL"]
    return StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={},
        strategy_id_full=strategy_id_full,
        instruments=instruments_val,
        resolved_instruments=_synth_resolved(instruments_val),
    )


_DEFAULT_IB = IBSettings(host="127.0.0.1", port=4002, account_id="DU1234567")


def test_portfolio_config_does_not_set_external_order_claims() -> None:
    """The exec engine config must NOT have ``external_order_claims`` set.

    Nautilus issue #3176: setting this field causes duplicate orders on
    restart because the reconciliation loop re-claims orders that were
    already processed. The safe path is to leave it unset (or None/empty)
    and rely on Nautilus's standard reconciliation.
    """
    config = build_portfolio_trading_node_config(
        deployment_slug="abcd1234abcd1234",
        strategy_members=[_make_member()],
        ib_settings=_DEFAULT_IB,
    )

    # Check exec_engine level — the field may not exist on this Nautilus
    # version, which is fine (absence = safe). If it does exist, it must
    # be None or empty.
    claims = getattr(config.exec_engine, "external_order_claims", None)
    assert not claims, (
        f"exec_engine.external_order_claims must NOT be set (issue #3176 "
        f"duplicate-order regression) — got {claims!r}"
    )


def test_portfolio_config_exec_clients_no_external_order_claims() -> None:
    """Per-client ``external_order_claims`` must also be absent.

    The exec client config (``InteractiveBrokersExecClientConfig``) might
    gain an ``external_order_claims`` field in a future Nautilus version.
    Guard against accidentally setting it at the client level too.
    """
    config = build_portfolio_trading_node_config(
        deployment_slug="abcd1234abcd1234",
        strategy_members=[_make_member(instruments=["AAPL", "MSFT"])],
        ib_settings=_DEFAULT_IB,
    )

    for client_name, client_config in config.exec_clients.items():
        claims = getattr(client_config, "external_order_claims", None)
        assert not claims, (
            f"exec_clients[{client_name!r}].external_order_claims must NOT "
            f"be set (issue #3176) — got {claims!r}"
        )


def test_portfolio_config_does_not_filter_unclaimed_external_orders() -> None:
    """``filter_unclaimed_external_orders`` must be False (default).

    Setting it to True would silently drop legitimate fill events from
    other strategies sharing the same IB account — a data-loss risk for
    portfolio deployments where multiple strategies share one exec client.
    """
    config = build_portfolio_trading_node_config(
        deployment_slug="abcd1234abcd1234",
        strategy_members=[
            _make_member(instruments=["AAPL"], strategy_id_full="s1@slug"),
            _make_member(instruments=["MSFT"], strategy_id_full="s2@slug"),
        ],
        ib_settings=_DEFAULT_IB,
    )

    assert config.exec_engine.filter_unclaimed_external_orders is False, (
        "filter_unclaimed_external_orders must be False for portfolio "
        "deployments — True would drop legitimate fills from other "
        "strategies sharing the same IB exec client"
    )
