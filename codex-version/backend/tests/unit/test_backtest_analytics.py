import pandas as pd

from msai.services.backtest_analytics import BacktestAnalyticsService


def test_build_payload_handles_datetime_index_account_reports() -> None:
    account_df = pd.DataFrame(
        {
            "account_id": ["EQUS-001", "EQUS-001", "EQUS-001", "EQUS-001"],
            "total": ["100.0", "101.0", "101.0", "103.0"],
        },
        index=pd.to_datetime(
            [
                "2026-04-07T14:30:00Z",
                "2026-04-07T14:30:00Z",
                "2026-04-07T14:31:00Z",
                "2026-04-08T14:30:00Z",
            ],
            utc=True,
        ),
    )

    payload = BacktestAnalyticsService().build_payload(
        backtest_id="bt-123",
        account_df=account_df,
        metrics={"sharpe": 1.0},
        report_path=None,
    )

    assert payload["id"] == "bt-123"
    assert payload["metrics"]["sharpe"] == 1.0
    assert len(payload["series"]) == 3
    assert payload["series"][0]["equity"] == 101.0
    assert payload["series"][2]["equity"] == 103.0
    assert payload["series"][0]["drawdown"] == 0.0
