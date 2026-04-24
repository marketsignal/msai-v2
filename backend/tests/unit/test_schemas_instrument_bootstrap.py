import pytest
from pydantic import ValidationError

from msai.schemas.instrument_bootstrap import (
    BootstrapRequest,
    BootstrapResultItem,
    CandidateInfo,
    build_bootstrap_response,
)


def test_bootstrap_request_valid():
    req = BootstrapRequest(provider="databento", symbols=["AAPL", "SPY"])
    assert req.max_concurrent == 3


def test_bootstrap_request_empty_symbols_rejected():
    with pytest.raises(ValidationError):
        BootstrapRequest(provider="databento", symbols=[])


def test_bootstrap_request_too_many_symbols_rejected():
    with pytest.raises(ValidationError):
        BootstrapRequest(provider="databento", symbols=["X"] * 51)


def test_bootstrap_request_max_concurrent_capped_at_3():
    with pytest.raises(ValidationError):
        BootstrapRequest(provider="databento", symbols=["X"], max_concurrent=4)


def test_bootstrap_request_unsupported_provider_rejected():
    with pytest.raises(ValidationError):
        BootstrapRequest(provider="polygon", symbols=["X"])


def test_bootstrap_request_asset_class_override_taxonomy():
    """Registry DB taxonomy is equity|futures|fx|option|crypto. 'etf' and 'future'
    (singular) must be rejected."""
    # Valid
    BootstrapRequest(provider="databento", symbols=["X"], asset_class_override="equity")
    BootstrapRequest(provider="databento", symbols=["X"], asset_class_override="futures")
    BootstrapRequest(provider="databento", symbols=["X"], asset_class_override="fx")
    BootstrapRequest(provider="databento", symbols=["X"], asset_class_override="option")
    # Invalid
    with pytest.raises(ValidationError):
        BootstrapRequest(provider="databento", symbols=["X"], asset_class_override="etf")
    with pytest.raises(ValidationError):
        BootstrapRequest(
            provider="databento", symbols=["X"], asset_class_override="future"
        )  # singular


def test_bootstrap_request_exact_ids_must_subset_symbols():
    with pytest.raises(ValidationError):
        BootstrapRequest(
            provider="databento",
            symbols=["AAPL"],
            exact_ids={"MSFT": "MSFT.XNAS"},  # MSFT not in symbols
        )


def test_bootstrap_request_symbol_regex():
    # Valid shapes
    BootstrapRequest(
        provider="databento", symbols=["AAPL", "ES.n.0", "EUR/USD", "BRK.B", "FOO-BAR"]
    )
    # Invalid: length > 32 chars OR bad chars
    with pytest.raises(ValidationError):
        BootstrapRequest(provider="databento", symbols=["SPACE NOT ALLOWED"])


def test_bootstrap_response_item_has_all_readiness_flags():
    item = BootstrapResultItem(
        symbol="AAPL",
        outcome="created",
        registered=True,
        backtest_data_available=False,
        live_qualified=False,
        canonical_id="AAPL.NASDAQ",
        dataset="XNAS.ITCH",
        asset_class="equity",
    )
    assert item.registered is True
    assert item.live_qualified is False


def test_build_bootstrap_response_computes_summary():
    items = [
        BootstrapResultItem(
            symbol="A",
            outcome="created",
            registered=True,
            backtest_data_available=False,
            live_qualified=False,
        ),
        BootstrapResultItem(
            symbol="B",
            outcome="ambiguous",
            registered=False,
            backtest_data_available=False,
            live_qualified=False,
            candidates=[
                CandidateInfo(
                    alias_string="B.XNYS", raw_symbol="B", asset_class="equity", dataset="XNYS"
                ),
                CandidateInfo(
                    alias_string="B.XNAS", raw_symbol="B", asset_class="equity", dataset="XNAS"
                ),
            ],
        ),
        BootstrapResultItem(
            symbol="C",
            outcome="noop",
            registered=True,
            backtest_data_available=False,
            live_qualified=False,
        ),
    ]
    resp = build_bootstrap_response(items)
    assert resp.summary.total == 3
    assert resp.summary.created == 1
    assert resp.summary.noop == 1
    assert resp.summary.failed == 1  # ambiguous
