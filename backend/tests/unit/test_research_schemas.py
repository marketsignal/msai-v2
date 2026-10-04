"""Research split validation keeps explicitly requested diagnostics intact."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from msai.schemas.research import ResearchSweepRequest, ResearchWalkForwardRequest


@pytest.mark.parametrize(
    "split",
    [
        {"holdout_days": 2, "purge_days": 2},
        {"holdout_days": 4, "purge_days": 0},
        {"holdout_fraction": 0.5, "purge_days": 0},
    ],
)
def test_requested_split_without_training_refuses_with_actionable_message(
    split: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="training.*Reduce holdout or purge"):
        ResearchSweepRequest(
            strategy_id=uuid4(),
            instruments=["AAPL.XNAS"],
            start_date="2025-01-22",
            end_date="2025-01-25",
            parameter_grid={"fast_period": [5, 10]},
            **split,
        )


def test_walk_forward_requested_holdout_must_fit_first_training_slice() -> None:
    with pytest.raises(ValidationError, match="training.*Reduce holdout or purge"):
        ResearchWalkForwardRequest(
            strategy_id=uuid4(),
            instruments=["AAPL.XNAS"],
            start_date="2025-01-22",
            end_date="2025-01-25",
            parameter_grid={"fast_period": [5, 10]},
            train_days=2,
            test_days=2,
            holdout_days=1,
            purge_days=1,
        )


def test_four_day_sweep_accepts_two_day_holdout_without_purge() -> None:
    request = ResearchSweepRequest(
        strategy_id=uuid4(),
        instruments=["AAPL.XNAS"],
        start_date="2025-01-22",
        end_date="2025-01-25",
        parameter_grid={"fast_period": [5, 10]},
        holdout_days=2,
        purge_days=0,
    )
    assert request.holdout_days == 2
