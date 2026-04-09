from pathlib import Path

import pytest

from msai.services.strategy_templates import StrategyTemplateError, StrategyTemplateService


def test_strategy_template_service_scaffolds_importable_module(tmp_path: Path) -> None:
    root = tmp_path / "strategies"
    service = StrategyTemplateService(root)

    scaffolded = service.scaffold(
        template_id="mean_reversion_zscore",
        module_name="user.my_new_strategy",
        description="Generated in a unit test.",
    )

    created_file = root / "user" / "my_new_strategy.py"
    assert created_file.exists()
    assert (root / "user" / "__init__.py").exists()
    assert scaffolded["name"] == "user.my_new_strategy"
    assert scaffolded["file_path"] == "user/my_new_strategy.py"
    assert scaffolded["strategy_class"] == "MyNewStrategyStrategy"
    assert scaffolded["description"] == "Generated in a unit test."
    assert scaffolded["default_config"]["lookback"] == 20
    assert scaffolded["config_schema"]["$defs"]["MyNewStrategyConfig"]["title"] == "MyNewStrategyConfig"
    assert "class MyNewStrategyStrategy" in created_file.read_text()


def test_strategy_template_service_rejects_invalid_module_names(tmp_path: Path) -> None:
    service = StrategyTemplateService(tmp_path / "strategies")

    with pytest.raises(StrategyTemplateError, match="valid Python identifiers"):
        service.scaffold(
            template_id="mean_reversion_zscore",
            module_name="bad-module-name",
        )
