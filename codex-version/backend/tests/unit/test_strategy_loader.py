from pathlib import Path

from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths


def test_resolve_importable_strategy_paths() -> None:
    strategy_file = (
        Path(__file__).resolve().parents[3] / "strategies" / "example" / "ema_cross.py"
    )
    resolved = resolve_importable_strategy_paths(str(strategy_file))
    assert resolved.strategy_path.endswith(":EMACrossStrategy")
    assert resolved.config_path.endswith(":EMACrossConfig")


def test_resolve_importable_strategy_paths_for_mean_reversion() -> None:
    strategy_file = (
        Path(__file__).resolve().parents[3] / "strategies" / "example" / "mean_reversion.py"
    )
    resolved = resolve_importable_strategy_paths(str(strategy_file))
    assert resolved.strategy_path.endswith(":MeanReversionZScoreStrategy")
    assert resolved.config_path.endswith(":MeanReversionZScoreConfig")


def test_resolve_importable_strategy_paths_for_donchian_breakout() -> None:
    strategy_file = (
        Path(__file__).resolve().parents[3] / "strategies" / "example" / "donchian_breakout.py"
    )
    resolved = resolve_importable_strategy_paths(str(strategy_file))
    assert resolved.strategy_path.endswith(":DonchianBreakoutStrategy")
    assert resolved.config_path.endswith(":DonchianBreakoutConfig")
