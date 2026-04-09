from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.models import Strategy


@dataclass(slots=True)
class StrategyFile:
    name: str
    file_path: Path
    strategy_class: str
    description: str | None
    config_schema: dict[str, Any] | None
    default_config: dict[str, Any] | None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StrategyRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root

    def discover(self) -> list[StrategyFile]:
        if not self.root.exists():
            return []
        discovered: list[StrategyFile] = []
        for file_path in sorted(self.root.rglob("*.py")):
            if file_path.name == "__init__.py":
                continue
            strategy_file = self._scan_file(file_path)
            if strategy_file is not None:
                discovered.append(strategy_file)
        return discovered

    def resolve_path(self, strategy: Strategy) -> Path:
        return self.root / strategy.file_path

    def _scan_file(self, file_path: Path) -> StrategyFile | None:
        try:
            module = self._load_module(file_path)
        except Exception:
            return None

        strategy_class = None
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__:
                continue
            if cls.__name__.lower().endswith("strategy") and cls.__name__.lower() != "strategy":
                strategy_class = cls
                break
        if strategy_class is None:
            return None

        config_schema: dict[str, Any] | None = None
        default_config: dict[str, Any] | None = None
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__name__.lower().endswith("config") and (
                hasattr(cls, "model_json_schema") or hasattr(cls, "json_schema")
            ):
                try:
                    config_schema = self._extract_schema(cls)
                    default_config = self._extract_default_config(cls)
                except Exception:
                    config_schema = None
                    default_config = None
                break

        rel_path = file_path.relative_to(self.root)
        return StrategyFile(
            name=rel_path.with_suffix("").as_posix().replace("/", "."),
            file_path=rel_path,
            strategy_class=strategy_class.__name__,
            description=inspect.getdoc(strategy_class),
            config_schema=config_schema,
            default_config=default_config,
        )

    def _load_module(self, file_path: Path) -> ModuleType:
        root_parent = self.root.resolve().parent
        root_parent_str = str(root_parent)
        if root_parent_str not in sys.path:
            sys.path.insert(0, root_parent_str)

        relative_path = file_path.resolve().relative_to(root_parent).with_suffix("")
        module_name = ".".join(relative_path.parts)
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load strategy module from {file_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _extract_schema(self, config_class: type) -> dict[str, Any] | None:
        if hasattr(config_class, "model_json_schema"):
            return config_class.model_json_schema()
        if hasattr(config_class, "json_schema"):
            return config_class.json_schema()
        return None

    def _extract_default_config(self, config_class: type) -> dict[str, Any] | None:
        model_fields = getattr(config_class, "model_fields", None)
        if model_fields is not None:
            defaults: dict[str, Any] = {}
            for name, field in model_fields.items():
                default = getattr(field, "default", None)
                if default is None:
                    continue
                if type(default).__name__ == "PydanticUndefinedType":
                    continue
                defaults[name] = default

            schema_defaults = self._extract_schema_defaults(config_class)
            for key, value in schema_defaults.items():
                defaults.setdefault(key, value)
            return defaults

        schema_defaults = self._extract_schema_defaults(config_class)
        return schema_defaults or None

    def _extract_schema_defaults(self, config_class: type) -> dict[str, Any]:
        schema = self._extract_schema(config_class)
        if schema is None:
            return {}

        schema_defaults: dict[str, Any] = {}
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, value in properties.items():
                if isinstance(value, dict) and "default" in value:
                    schema_defaults[key] = value["default"]

        defs = schema.get("$defs")
        if isinstance(defs, dict):
            for definition in defs.values():
                if not isinstance(definition, dict):
                    continue
                definition_properties = definition.get("properties")
                if not isinstance(definition_properties, dict):
                    continue
                for key, value in definition_properties.items():
                    if isinstance(value, dict) and "default" in value:
                        schema_defaults.setdefault(key, value["default"])

        return schema_defaults

    async def sync(self, session: AsyncSession) -> list[Strategy]:
        existing = {
            strategy.file_path: strategy
            for strategy in (await session.execute(select(Strategy))).scalars().all()
        }

        for discovered in self.discover():
            file_key = discovered.file_path.as_posix()
            if file_key in existing:
                row = existing[file_key]
                row.name = discovered.name
                row.description = discovered.description
                row.strategy_class = discovered.strategy_class
                row.config_schema = discovered.config_schema
                if row.default_config is None:
                    row.default_config = discovered.default_config
                continue
            session.add(
                Strategy(
                    name=discovered.name,
                    description=discovered.description,
                    file_path=file_key,
                    strategy_class=discovered.strategy_class,
                    config_schema=discovered.config_schema,
                    default_config=discovered.default_config,
                )
            )

        await session.commit()
        result = await session.execute(select(Strategy).order_by(Strategy.name))
        return list(result.scalars().all())

    async def validate(self, strategy: Strategy, config: dict[str, Any]) -> tuple[bool, str]:
        file_path = self.resolve_path(strategy)
        if not file_path.exists():
            return False, f"Strategy file missing: {file_path}"
        try:
            module = self._load_module(file_path)
        except Exception as exc:
            return False, f"Failed to import strategy module: {exc}"

        strategy_cls = getattr(module, strategy.strategy_class, None)
        if strategy_cls is None:
            return False, f"Class {strategy.strategy_class} not found in module"

        config_class = None
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__name__.lower().endswith("config") and (
                hasattr(cls, "model_validate") or hasattr(cls, "parse")
            ):
                config_class = cls
                break

        if config_class is not None:
            try:
                if hasattr(config_class, "model_validate"):
                    config_class.model_validate(config)
                elif hasattr(config_class, "parse"):
                    config_class.parse(json.dumps(config))
            except Exception as exc:
                return False, f"Config validation failed: {exc}"

        return True, "Strategy validation succeeded"
