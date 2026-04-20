from datetime import date

import pytest

from msai.services.nautilus.security_master.live_resolver import (
    AssetClass,
    RegistryIncompleteError,
    RegistryMissError,
    ResolvedInstrument,
    UnsupportedAssetClassError,
)


def test_asset_class_enum_covers_required_classes():
    assert AssetClass.EQUITY.value == "equity"
    assert AssetClass.FUTURES.value == "futures"
    assert AssetClass.FX.value == "fx"
    assert AssetClass.OPTION.value == "option"
    assert AssetClass.CRYPTO.value == "crypto"


def test_resolved_instrument_is_frozen_dataclass():
    ri = ResolvedInstrument(
        canonical_id="AAPL.NASDAQ",
        asset_class=AssetClass.EQUITY,
        contract_spec={"secType": "STK", "symbol": "AAPL"},
        effective_window=(date(2026, 1, 1), None),
    )
    with pytest.raises((AttributeError, TypeError)):
        ri.canonical_id = "other"  # type: ignore[misc]


def test_registry_miss_error_lists_symbols():
    err = RegistryMissError(symbols=["GBP/USD", "NQ"], as_of_date=date(2026, 4, 20))
    assert "GBP/USD" in str(err)
    assert "NQ" in str(err)
    assert "msai instruments refresh" in str(err)


def test_registry_incomplete_error_names_missing_field():
    err = RegistryIncompleteError(symbol="NVDA", missing_field="listing_venue")
    assert "NVDA" in str(err)
    assert "listing_venue" in str(err)


def test_unsupported_asset_class_error_names_class():
    err = UnsupportedAssetClassError(symbol="SPY_CALL_500", asset_class=AssetClass.OPTION)
    assert "option" in str(err).lower()
    assert "SPY_CALL_500" in str(err)


def test_live_resolver_error_is_value_error_subclass():
    """Critical: supervisor's ProcessManager.spawn() permanent-catch only
    fires for ValueError/ImportError/etc. Resolver errors MUST inherit
    ValueError via LiveResolverError, or they land in the transient-
    retry branch."""
    from msai.services.nautilus.security_master.live_resolver import (
        AmbiguousRegistryError,
        LiveResolverError,
    )
    assert issubclass(LiveResolverError, ValueError)
    assert issubclass(RegistryMissError, LiveResolverError)
    assert issubclass(RegistryIncompleteError, LiveResolverError)
    assert issubclass(UnsupportedAssetClassError, LiveResolverError)
    assert issubclass(AmbiguousRegistryError, LiveResolverError)


def test_to_error_message_round_trips_to_structured_dict():
    """Each error class emits a JSON envelope the API layer can parse
    back to {code, message, details}."""
    import json

    from msai.services.nautilus.security_master.live_resolver import (
        AmbiguousRegistryError,
    )

    miss = RegistryMissError(symbols=["QQQ"], as_of_date=date(2026, 4, 20))
    envelope = json.loads(miss.to_error_message())
    assert envelope["code"] == "REGISTRY_MISS"
    assert envelope["details"]["missing_symbols"] == ["QQQ"]
    assert envelope["details"]["as_of_date"] == "2026-04-20"
    assert "msai instruments refresh" in envelope["message"]

    inc = RegistryIncompleteError(symbol="NVDA", missing_field="listing_venue")
    envelope = json.loads(inc.to_error_message())
    assert envelope["code"] == "REGISTRY_INCOMPLETE"
    assert envelope["details"] == {"symbol": "NVDA", "missing_field": "listing_venue"}

    unsup = UnsupportedAssetClassError(symbol="SPY_C", asset_class=AssetClass.OPTION)
    envelope = json.loads(unsup.to_error_message())
    assert envelope["code"] == "UNSUPPORTED_ASSET_CLASS"
    assert envelope["details"] == {"symbol": "SPY_C", "asset_class": "option"}

    amb_cross = AmbiguousRegistryError(
        symbol="SPY",
        conflicts=["equity", "option"],
        reason=AmbiguousRegistryError.REASON_CROSS_ASSET_CLASS,
    )
    envelope = json.loads(amb_cross.to_error_message())
    assert envelope["code"] == "AMBIGUOUS_REGISTRY"
    assert envelope["details"]["reason"] == "cross_asset_class"
    assert envelope["details"]["conflicts"] == ["equity", "option"]
    assert envelope["details"]["symbol"] == "SPY"

    amb_day = AmbiguousRegistryError(
        symbol="ES",
        conflicts=["ESM6.CME", "ESU6.CME"],
        reason=AmbiguousRegistryError.REASON_SAME_DAY_OVERLAP,
    )
    envelope = json.loads(amb_day.to_error_message())
    assert envelope["details"]["reason"] == "same_day_overlap"


def test_ambiguous_registry_error_sorts_conflicts_deterministically():
    from msai.services.nautilus.security_master.live_resolver import (
        AmbiguousRegistryError,
    )
    # Inputs in non-sorted order — err.conflicts must be sorted
    err = AmbiguousRegistryError(
        symbol="ES",
        conflicts=["ESU6.CME", "ESM6.CME"],
        reason=AmbiguousRegistryError.REASON_SAME_DAY_OVERLAP,
    )
    assert err.conflicts == ["ESM6.CME", "ESU6.CME"]  # sorted
