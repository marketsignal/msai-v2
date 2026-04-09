from __future__ import annotations

import pandas as pd
import pytest

from msai.services.report_generator import _normalize_report_returns


def test_normalize_report_returns_compounds_intraday_to_daily() -> None:
    index = pd.to_datetime(
        [
            "2026-04-07T14:30:00Z",
            "2026-04-07T14:31:00Z",
            "2026-04-08T14:30:00Z",
        ],
        utc=True,
    )
    returns = pd.Series([0.01, -0.02, 0.03], index=index)

    normalized = _normalize_report_returns(returns)

    assert list(normalized.index.strftime("%Y-%m-%d")) == ["2026-04-07", "2026-04-08"]
    assert normalized.iloc[0] == pytest.approx((1.01 * 0.98) - 1.0)
    assert normalized.iloc[1] == pytest.approx(0.03)


def test_normalize_report_returns_handles_non_datetime_index() -> None:
    returns = pd.Series([0.01, -0.02, 0.03], index=[1, 2, 3])

    normalized = _normalize_report_returns(returns)

    assert list(normalized.tolist()) == [0.01, -0.02, 0.03]
