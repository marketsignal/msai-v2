from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from msai.core.config import settings
from msai.core.database import get_db
from msai.main import app
from msai.models import Strategy
from msai.services.strategy_registry import StrategyRegistry
from msai.services.strategy_templates import StrategyTemplateService


@pytest.fixture(autouse=True)
def _reset_strategy_app_state() -> None:
    app.dependency_overrides.clear()


def test_strategy_registry_endpoints_support_list_patch_and_validate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")
    monkeypatch.setattr(settings, "strategies_root", tmp_path / "strategies")

    strategy = Strategy(
        id="strategy-1",
        name="example.mean_reversion",
        description="Mean reversion baseline",
        file_path="example/mean_reversion.py",
        strategy_class="MeanReversionStrategy",
        config_schema={"type": "object"},
        default_config={"lookback": 20, "zscore_threshold": 1.5},
    )

    class _FakeSession:
        async def get(self, model, identifier: str):  # noqa: ANN001
            return strategy if model is Strategy and identifier == strategy.id else None

        async def commit(self) -> None:
            return None

        async def refresh(self, instance: Strategy) -> None:
            return None

        async def delete(self, instance: Strategy) -> None:
            return None

    async def _override_db():
        yield _FakeSession()

    async def _fake_sync(self, session):  # noqa: ANN001
        return [strategy]

    async def _fake_validate(self, db_strategy: Strategy, config: dict[str, object]):
        assert db_strategy.id == "strategy-1"
        assert config["lookback"] == 30
        return True, "Strategy validation succeeded"

    app.dependency_overrides[get_db] = _override_db
    monkeypatch.setattr(StrategyRegistry, "sync", _fake_sync)
    monkeypatch.setattr(StrategyRegistry, "validate", _fake_validate)

    with TestClient(app) as client:
        headers = {"X-API-Key": "msai-test-key"}

        list_response = client.get("/api/v1/strategies/", headers=headers)
        assert list_response.status_code == 200
        assert list_response.json()[0]["name"] == "example.mean_reversion"

        detail_response = client.get("/api/v1/strategies/strategy-1", headers=headers)
        assert detail_response.status_code == 200
        assert detail_response.json()["default_config"] == {"lookback": 20, "zscore_threshold": 1.5}

        patch_response = client.patch(
            "/api/v1/strategies/strategy-1",
            headers=headers,
            json={"default_config": {"lookback": 30, "zscore_threshold": 1.2}},
        )
        assert patch_response.status_code == 200
        assert patch_response.json()["default_config"] == {"lookback": 30, "zscore_threshold": 1.2}

        validate_response = client.post(
            "/api/v1/strategies/strategy-1/validate",
            headers=headers,
            json={"config": {"lookback": 30, "zscore_threshold": 1.2}},
        )
        assert validate_response.status_code == 200
        assert validate_response.json() == {
            "valid": True,
            "message": "Strategy validation succeeded",
        }


def test_strategy_template_endpoints_support_listing_scaffolding_and_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")
    monkeypatch.setattr(settings, "strategies_root", tmp_path / "strategies")

    scaffolded_strategy = Strategy(
        id="strategy-scaffolded",
        name="user.my_new_strategy",
        description="Scaffolded strategy",
        file_path="user/my_new_strategy.py",
        strategy_class="MyNewStrategyStrategy",
        config_schema={"type": "object"},
        default_config={"lookback": 20},
    )

    class _FakeSession:
        async def get(self, model, identifier: str):  # noqa: ANN001
            return scaffolded_strategy if model is Strategy and identifier == scaffolded_strategy.id else None

        async def commit(self) -> None:
            return None

        async def refresh(self, instance: Strategy) -> None:
            return None

        async def delete(self, instance: Strategy) -> None:
            return None

    async def _override_db():
        yield _FakeSession()

    async def _fake_sync(self, session):  # noqa: ANN001
        return [scaffolded_strategy]

    def _fake_scaffold(self, *, template_id: str, module_name: str, description: str | None, force: bool):  # noqa: ANN001
        assert template_id == "mean_reversion_zscore"
        assert module_name == "user.my_new_strategy"
        assert description == "Scaffolded strategy"
        assert force is False
        return {
            "strategy_id": None,
            "template_id": template_id,
            "name": module_name,
            "description": description,
            "file_path": "user/my_new_strategy.py",
            "strategy_class": "MyNewStrategyStrategy",
            "config_schema": {"type": "object"},
            "default_config": {"lookback": 20},
        }

    app.dependency_overrides[get_db] = _override_db
    monkeypatch.setattr(StrategyRegistry, "sync", _fake_sync)
    monkeypatch.setattr(StrategyTemplateService, "scaffold", _fake_scaffold)

    with TestClient(app) as client:
        headers = {"X-API-Key": "msai-test-key"}

        templates_response = client.get("/api/v1/strategy-templates", headers=headers)
        assert templates_response.status_code == 200
        assert any(item["id"] == "mean_reversion_zscore" for item in templates_response.json())

        scaffold_response = client.post(
            "/api/v1/strategy-templates/scaffold",
            headers=headers,
            json={
                "template_id": "mean_reversion_zscore",
                "module_name": "user.my_new_strategy",
                "description": "Scaffolded strategy",
                "force": False,
            },
        )
        assert scaffold_response.status_code == 200
        assert scaffold_response.json()["strategy_id"] == "strategy-scaffolded"
        assert scaffold_response.json()["name"] == "user.my_new_strategy"

        sync_response = client.post("/api/v1/strategies/sync", headers=headers)
        assert sync_response.status_code == 200
        assert sync_response.json() == [
            {
                "id": "strategy-scaffolded",
                "name": "user.my_new_strategy",
                "description": "Scaffolded strategy",
                "file_path": "user/my_new_strategy.py",
                "strategy_class": "MyNewStrategyStrategy",
            }
        ]
