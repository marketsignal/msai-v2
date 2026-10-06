"""Schema/validation for explicit native V2 user config constructors.

Native StrategyConfig accepts extra kwargs and has no user JSON schema.
Pydantic covers declared constructor fields; native IDs use native parsing.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, cast, get_args, get_origin, get_type_hints

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    PlainSerializer,
    WithJsonSchema,
    create_model,
)


class ConfigSchemaStatus(StrEnum):
    READY = "ready"
    UNSUPPORTED = "unsupported"
    EXTRACTION_FAILED = "extraction_failed"
    NO_CONFIG_CLASS = "no_config_class"


def nautilus_schema_hook(t: type) -> dict[str, Any]:
    from nautilus_trader.model import (
        AccountId,
        BarType,
        ClientId,
        ComponentId,
        InstrumentId,
        OrderListId,
        PositionId,
        StrategyId,
        Symbol,
        TraderId,
        Venue,
    )

    if t is InstrumentId:
        return {
            "type": "string",
            "title": "Instrument ID",
            "x-format": "instrument-id",
            "description": "SYMBOL.VENUE",
            "examples": ["AAPL.NASDAQ", "EUR/USD.IDEALPRO"],
        }
    if t is BarType:
        return {
            "type": "string",
            "title": "Bar Type",
            "x-format": "bar-type",
            "description": "INSTRUMENT_ID-STEP-AGGREGATION-PRICE_TYPE-SOURCE",
            "examples": ["AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"],
        }
    if t in (
        StrategyId,
        ComponentId,
        Venue,
        Symbol,
        AccountId,
        ClientId,
        OrderListId,
        PositionId,
        TraderId,
    ):
        return {"type": "string", "title": t.__name__}
    raise NotImplementedError(f"no schema hook for {t!r}")


def _field_type(annotation: Any) -> Any:
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        return Annotated[(_field_type(args[0]), *args[1:])]
    if annotation is Decimal:
        return Annotated[
            Decimal, WithJsonSchema({"type": "string", "format": "decimal"}, mode="serialization")
        ]
    if isinstance(annotation, type) and annotation.__module__.startswith("nautilus_trader"):
        schema = nautilus_schema_hook(annotation)

        def parse(value: Any) -> Any:
            if isinstance(value, annotation):
                return value
            if not isinstance(value, str):
                raise ValueError(f"{annotation.__name__} must be a string")
            try:
                factory = getattr(annotation, "from_str", annotation)
                return factory(value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"invalid {annotation.__name__}: {exc}") from exc

        return Annotated[
            annotation,
            BeforeValidator(parse),
            PlainSerializer(str, return_type=str),
            WithJsonSchema(schema),
        ]
    return annotation


def _user_model(config_cls: type) -> type[BaseModel]:
    constructor = inspect.getattr_static(config_cls, "__init__")
    signature = inspect.signature(constructor)
    hints = get_type_hints(constructor, include_extras=True)
    fields: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if name == "self" or parameter.kind in (parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL):
            continue
        if name not in hints:
            raise NotImplementedError(f"config constructor field {name} needs a type annotation")
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[name] = (_field_type(hints[name]), default)
    return cast(
        "type[BaseModel]",
        create_model(
            config_cls.__name__ + "Fields",
            __config__=ConfigDict(
                extra="forbid", arbitrary_types_allowed=True, validate_default=True
            ),
            **fields,
        ),
    )


def validate_strategy_config(config_cls: type, payload: dict[str, Any]) -> Any:
    """Reject unknown/bad fields and retain typed values for native creation."""
    model = _user_model(config_cls).model_validate(payload)
    return config_cls(**{name: getattr(model, name) for name in type(model).model_fields})


def build_user_schema(
    config_cls: type | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, ConfigSchemaStatus]:
    if config_cls is None:
        return None, None, ConfigSchemaStatus.NO_CONFIG_CLASS
    try:
        schema = _user_model(config_cls).model_json_schema(mode="serialization")
    except NotImplementedError:
        return None, None, ConfigSchemaStatus.UNSUPPORTED
    except Exception:  # noqa: BLE001 — isolate one unsupported strategy's schema
        return None, None, ConfigSchemaStatus.EXTRACTION_FAILED
    schema["properties"].pop("order_id_tag", None)
    schema["required"] = [name for name in schema.get("required", []) if name != "order_id_tag"]
    defaults = {
        name: field["default"] for name, field in schema["properties"].items() if "default" in field
    }
    return schema, defaults, ConfigSchemaStatus.READY
