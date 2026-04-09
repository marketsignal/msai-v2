from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from msai.core.logging import get_logger
from msai.services.nautilus.trading_node import (
    DeploymentStopResult,
    LiveLiquidationFailedError,
    LiveStartBlockedError,
    LiveStartFailedError,
    trading_node_manager,
)

logger = get_logger("workers.live_runtime")


async def run_live_start(ctx: dict, **kwargs: object) -> dict[str, Any]:
    _ = ctx
    try:
        deployment_id = await trading_node_manager.start(**kwargs)
    except LiveStartBlockedError as exc:
        return _error_payload("blocked", str(exc))
    except LiveStartFailedError as exc:
        return _error_payload("failed", str(exc))
    except Exception as exc:
        logger.exception("live_runtime_start_failed", error=str(exc))
        return _error_payload("runtime", str(exc))
    return {"ok": True, "deployment_id": deployment_id}


async def run_live_stop(
    ctx: dict,
    deployment_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    _ = ctx
    try:
        result = await trading_node_manager.liquidate_and_stop(
            deployment_id,
            reason=reason,
        )
    except LiveLiquidationFailedError as exc:
        return _error_payload("liquidation_failed", str(exc))
    except Exception as exc:
        logger.exception("live_runtime_stop_failed", deployment_id=deployment_id, error=str(exc))
        return _error_payload("runtime", str(exc))
    return {"ok": True, "result": _stop_result_payload(result)}


async def run_live_status(ctx: dict) -> dict[str, Any]:
    _ = ctx
    try:
        rows = await trading_node_manager.status()
    except Exception as exc:
        logger.exception("live_runtime_status_failed", error=str(exc))
        return _error_payload("runtime", str(exc))
    return {"ok": True, "rows": _jsonable(rows)}


async def run_live_kill_all(ctx: dict) -> dict[str, Any]:
    _ = ctx
    try:
        stopped = await trading_node_manager.kill_all()
    except LiveLiquidationFailedError as exc:
        return _error_payload("liquidation_failed", str(exc))
    except Exception as exc:
        logger.exception("live_runtime_kill_all_failed", error=str(exc))
        return _error_payload("runtime", str(exc))
    return {"ok": True, "stopped": stopped}


def _stop_result_payload(result: DeploymentStopResult) -> dict[str, Any]:
    return {
        "found": result.found,
        "stopped": result.stopped,
        "reason": result.reason,
    }


def _error_payload(error_type: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": error_type,
        "detail": detail,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _jsonable(value: object) -> Any:
    return json.loads(json.dumps(value, default=_json_default))


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC).isoformat()
        return value.isoformat()
    if isinstance(value, Mapping):
        return json.dumps(dict(value))
    return str(value)
