"""Unit tests for strategy template scaffolding service and API routes."""

from __future__ import annotations

from pathlib import Path

import pytest

from msai.services.strategy_templates import (
    TEMPLATES,
    StrategyTemplateError,
    StrategyTemplateService,
)

# ---------------------------------------------------------------------------
# Service: list_templates
# ---------------------------------------------------------------------------


class TestListTemplates:
    """Tests for StrategyTemplateService.list_templates."""

    def test_list_templates_returns_three(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        templates = svc.list_templates()
        assert len(templates) == 3

    def test_list_templates_ids(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        ids = {t["id"] for t in svc.list_templates()}
        assert ids == {"mean_reversion_zscore", "ema_cross", "donchian_breakout"}

    def test_list_templates_have_required_keys(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        for t in svc.list_templates():
            assert "id" in t
            assert "label" in t
            assert "description" in t
            assert "default_config" in t


# ---------------------------------------------------------------------------
# Service: scaffold
# ---------------------------------------------------------------------------


class TestScaffold:
    """Tests for StrategyTemplateService.scaffold."""

    @pytest.mark.parametrize("template_id", [t.id for t in TEMPLATES])
    def test_scaffold_creates_file(self, tmp_path: Path, template_id: str) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        result = svc.scaffold(
            template_id=template_id,
            module_name="user.test_strat",
        )
        file_path = tmp_path / result["file_path"]
        assert file_path.exists()
        assert file_path.read_text().strip() != ""

    @pytest.mark.parametrize("template_id", [t.id for t in TEMPLATES])
    def test_scaffold_file_compiles(self, tmp_path: Path, template_id: str) -> None:
        """Generated source must be valid Python (compile check)."""
        svc = StrategyTemplateService(root=tmp_path)
        result = svc.scaffold(
            template_id=template_id,
            module_name=f"user.{template_id}_check",
        )
        source = (tmp_path / result["file_path"]).read_text()
        compile(source, result["file_path"], "exec")

    @pytest.mark.parametrize("template_id", [t.id for t in TEMPLATES])
    def test_scaffold_no_on_stop(self, tmp_path: Path, template_id: str) -> None:
        """Generated strategies must NOT contain on_stop (execution rule #7)."""
        svc = StrategyTemplateService(root=tmp_path)
        result = svc.scaffold(
            template_id=template_id,
            module_name=f"user.{template_id}_stop",
        )
        source = (tmp_path / result["file_path"]).read_text()
        assert "on_stop" not in source

    def test_scaffold_returns_expected_keys(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        result = svc.scaffold(
            template_id="ema_cross",
            module_name="user.my_ema",
        )
        assert result["template_id"] == "ema_cross"
        assert result["name"] == "user.my_ema"
        assert result["file_path"] == "user/my_ema.py"
        assert result["strategy_class"] == "MyEmaStrategy"

    def test_scaffold_creates_init_files(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        svc.scaffold(template_id="ema_cross", module_name="deep.nested.strat")
        assert (tmp_path / "deep" / "__init__.py").exists()
        assert (tmp_path / "deep" / "nested" / "__init__.py").exists()

    def test_scaffold_invalid_template_raises(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        with pytest.raises(StrategyTemplateError, match="Unknown strategy template"):
            svc.scaffold(template_id="nonexistent", module_name="user.x")

    def test_scaffold_empty_module_name_raises(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        with pytest.raises(StrategyTemplateError, match="Module name is required"):
            svc.scaffold(template_id="ema_cross", module_name="")

    def test_scaffold_existing_file_raises_without_force(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        svc.scaffold(template_id="ema_cross", module_name="user.dup")
        with pytest.raises(StrategyTemplateError, match="already exists"):
            svc.scaffold(template_id="ema_cross", module_name="user.dup")

    def test_scaffold_force_overwrites(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        svc.scaffold(template_id="ema_cross", module_name="user.ow")
        result = svc.scaffold(template_id="donchian_breakout", module_name="user.ow", force=True)
        assert result["template_id"] == "donchian_breakout"

    def test_scaffold_custom_description(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        result = svc.scaffold(
            template_id="ema_cross",
            module_name="user.custom",
            description="My custom strategy",
        )
        source = (tmp_path / result["file_path"]).read_text()
        assert "My custom strategy" in source

    def test_scaffold_path_traversal_raises(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        with pytest.raises(StrategyTemplateError):
            svc.scaffold(template_id="ema_cross", module_name="....etc.passwd")

    def test_scaffold_pascal_case(self, tmp_path: Path) -> None:
        svc = StrategyTemplateService(root=tmp_path)
        result = svc.scaffold(template_id="ema_cross", module_name="user.my_cool_strategy")
        assert result["strategy_class"] == "MyCoolStrategyStrategy"

    @pytest.mark.parametrize(
        ("description", "label"),
        [
            (
                'harmless """ + __import__("os").system("rm -rf /") + """ tail',
                "embedded-triple-quote",
            ),
            ('"Fast EMA"', "wrapping-double-quotes"),
            ('description ending in "', "trailing-double-quote"),
            ('" leading double-quote', "leading-double-quote"),
            ('embedded "" doubled quotes', "embedded-doubled-quotes"),
            ("multi\nline\ndescription", "multiline"),
            ("contains\ttabs\\and backslashes", "tabs-and-backslashes"),
        ],
    )
    def test_scaffold_quoting_edge_cases_produce_valid_python(
        self, tmp_path: Path, description: str, label: str
    ) -> None:
        """Scaffold must produce a syntactically valid Python file for every
        quoting edge case. ``repr()``-based escaping handles all of these
        without needing input rejection (Codex iter-2 P2 2026-05-15: input
        rejection of ``\"\"\"`` is insufficient — trailing ``\"`` also
        breaks the docstring).
        """
        svc = StrategyTemplateService(root=tmp_path)
        result = svc.scaffold(
            template_id="ema_cross",
            module_name=f"user.case_{label.replace('-', '_')}",
            description=description,
        )
        source = (tmp_path / result["file_path"]).read_text()
        # Must compile as valid Python — the test that would have caught
        # the original bug.
        compile(source, result["file_path"], "exec")
        # The literal description text must appear in the file (verbatim
        # bytes survive ``repr()`` round-trip even if escaped).
        # Reconstruct what ``repr()`` produces so the assertion handles
        # both single-quoted and double-quoted literal forms.
        assert repr(description) in source


# ---------------------------------------------------------------------------
# API routes registered
# ---------------------------------------------------------------------------


class TestAPIRoutes:
    """Verify the strategy-templates router is registered on the app."""

    def test_strategy_templates_routes_registered(self) -> None:
        from msai.main import app

        paths = {route.path for route in app.routes}
        assert "/api/v1/strategy-templates/" in paths
        assert "/api/v1/strategy-templates/scaffold" in paths
