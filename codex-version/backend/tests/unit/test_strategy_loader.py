from pathlib import Path

from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths


def test_resolve_importable_strategy_paths() -> None:
    strategy_file = (
        Path(__file__).resolve().parents[3] / "strategies" / "example" / "ema_cross.py"
    )
    resolved = resolve_importable_strategy_paths(str(strategy_file))
    assert resolved.strategy_path.endswith(":EMACrossStrategy")
    assert resolved.config_path.endswith(":EMACrossConfig")
