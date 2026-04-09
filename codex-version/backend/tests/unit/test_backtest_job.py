import pandas as pd

from msai.workers.backtest_job import _account_returns_series


def test_account_returns_series_uses_timestamp_column_as_index() -> None:
    account_df = pd.DataFrame(
        {
            "timestamp": ["2026-04-07T00:00:00Z", "2026-04-08T00:00:00Z"],
            "returns": ["0.01", "-0.02"],
        }
    )

    series = _account_returns_series(account_df)

    assert isinstance(series.index, pd.DatetimeIndex)
    assert list(series.values) == [0.01, -0.02]
    assert list(series.index.strftime("%Y-%m-%d")) == ["2026-04-07", "2026-04-08"]
