"""Unit tests for ``_build_results_payload`` — per-strategy enrichment shape.

Verifies the orchestration-level enrichment helper that the H7 results page
consumes via ``walk_forward_payload``:

- Matrices serialise as ``{row_id: {col_id: value}}``.
- Time-series records serialise as flat
  ``[{"timestamp", "strategy_id", value_key}]`` lists per the Task H+
  backend-enrichment spec.
- Drawdown breakdown serialises as ``{strategy_id: {max_drawdown,
  duration_days, recovered}}`` so the H7 page's extractor works unchanged.
- Empty / malformed inputs return ``{}`` (graceful degradation — never
  fails the parent run).
"""

from __future__ import annotations

import pandas as pd
import pytest

from msai.services.portfolio.orchestration import _build_results_payload


def _make_strategy_results(
    *,
    n_strategies: int = 2,
    n_days: int = 30,
) -> list[dict[str, object]]:
    """Build a Quick-mode-shaped ``strategy_results`` list with N strategies.

    Uses deterministic-but-varied returns (mixed signs per strategy with a
    different sinusoidal phase per row) so the equity curves DRAW DOWN
    (non-monotonic), making drawdown correlations a defined value rather
    than NaN.
    """
    import math

    timestamps = pd.date_range("2024-01-01", periods=n_days, freq="B", tz="UTC")
    out: list[dict[str, object]] = []
    for i in range(n_strategies):
        # Phase-shifted sinusoid + small linear drift produces a curve that
        # makes peaks AND troughs across the window, so drawdowns are
        # non-constant and correlation is defined.
        phase = i * (math.pi / 3.0)
        returns = [
            0.002 * math.sin(0.4 * j + phase) + 0.0001 * (-1.0 if i % 2 else 1.0)
            for j in range(n_days)
        ]
        out.append(
            {
                "candidate_id": f"cand-{i}",
                "strategy_id": f"sid-{i}",
                "returns": returns,
                "timestamps": [ts.isoformat() for ts in timestamps],
            }
        )
    return out


def test_build_results_payload_returns_all_five_enrichment_keys() -> None:
    """Quick-mode-shaped input produces the five spec'd top-level keys."""
    results = _make_strategy_results(n_strategies=2, n_days=30)
    payload = _build_results_payload(strategy_results=results, initial_capital=100_000.0)
    assert set(payload.keys()) == {
        "per_strategy_equity",
        "per_strategy_drawdown",
        "return_correlation",
        "drawdown_correlation",
        "drawdown_breakdown",
    }


def test_matrices_serialise_as_row_col_nested_dict() -> None:
    """Correlation matrices must be ``{row_id: {col_id: float}}``."""
    results = _make_strategy_results(n_strategies=2, n_days=30)
    payload = _build_results_payload(strategy_results=results, initial_capital=100_000.0)

    for key in ("return_correlation", "drawdown_correlation"):
        matrix = payload[key]
        assert isinstance(matrix, dict)
        assert set(matrix.keys()) == {"sid-0", "sid-1"}
        for row_id, row in matrix.items():
            assert isinstance(row, dict)
            assert set(row.keys()) == {"sid-0", "sid-1"}
            # Diagonal == 1.0 (modulo float).
            assert row[row_id] == pytest.approx(1.0)
            # Off-diagonal is a finite float.
            for _col_id, value in row.items():
                assert isinstance(value, float)
                assert -1.0 <= value <= 1.0


def test_equity_series_serialise_as_flat_records() -> None:
    """Equity / drawdown series must be flat ``[{timestamp, strategy_id, value}]``."""
    results = _make_strategy_results(n_strategies=2, n_days=10)
    payload = _build_results_payload(strategy_results=results, initial_capital=100_000.0)

    eq_records = payload["per_strategy_equity"]
    assert isinstance(eq_records, list)
    # 2 strategies * 10 days = 20 records.
    assert len(eq_records) == 20
    sample = eq_records[0]
    assert set(sample.keys()) == {"timestamp", "strategy_id", "equity"}
    assert isinstance(sample["timestamp"], str)
    assert isinstance(sample["strategy_id"], str)
    assert isinstance(sample["equity"], float)

    dd_records = payload["per_strategy_drawdown"]
    assert isinstance(dd_records, list)
    sample_dd = dd_records[0]
    assert set(sample_dd.keys()) == {"timestamp", "strategy_id", "drawdown"}
    # Drawdown is non-positive (0 at peaks, more negative in troughs).
    assert sample_dd["drawdown"] <= 0.0


def test_drawdown_breakdown_keyed_by_strategy_id() -> None:
    """Breakdown must be ``{sid: {max_drawdown, duration_days, recovered}}``."""
    results = _make_strategy_results(n_strategies=2, n_days=30)
    payload = _build_results_payload(strategy_results=results, initial_capital=100_000.0)

    bd = payload["drawdown_breakdown"]
    assert isinstance(bd, dict)
    assert set(bd.keys()) == {"sid-0", "sid-1"}
    for row in bd.values():
        assert set(row.keys()) == {"max_drawdown", "duration_days", "recovered"}
        assert isinstance(row["max_drawdown"], float)
        assert isinstance(row["duration_days"], int)
        assert isinstance(row["recovered"], bool)
        assert row["max_drawdown"] <= 0.0


def test_empty_strategy_results_returns_empty_payload() -> None:
    """No strategy results → empty dict (graceful degradation)."""
    assert _build_results_payload(strategy_results=[], initial_capital=100_000.0) == {}


def test_malformed_returns_skipped_gracefully() -> None:
    """A strategy with empty/mismatched returns is skipped, others go through."""
    timestamps = pd.date_range("2024-01-01", periods=10, freq="B", tz="UTC")
    good_returns = [0.001 * i for i in range(10)]
    results = [
        # Good row.
        {
            "candidate_id": "good",
            "strategy_id": "sid-good",
            "returns": good_returns,
            "timestamps": [ts.isoformat() for ts in timestamps],
        },
        # Empty returns — must be skipped.
        {
            "candidate_id": "empty",
            "strategy_id": "sid-empty",
            "returns": [],
            "timestamps": [],
        },
        # Mismatched lengths — must be skipped.
        {
            "candidate_id": "mismatch",
            "strategy_id": "sid-mismatch",
            "returns": [0.001, 0.002],
            "timestamps": [timestamps[0].isoformat()],
        },
    ]
    payload = _build_results_payload(strategy_results=results, initial_capital=100_000.0)
    # Only the good strategy made it through.
    assert set(payload["drawdown_breakdown"].keys()) == {"sid-good"}
    assert set(payload["return_correlation"].keys()) == {"sid-good"}
