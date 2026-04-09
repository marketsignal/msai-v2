from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from msai.core.config import settings
from msai.core.logging import get_logger
from msai.core.queue import enqueue_live_runtime, get_redis_pool
from msai.services.nautilus.trading_node import (
    DeploymentStopResult,
    LiveLiquidationFailedError,
    LiveStartBlockedError,
    LiveStartFailedError,
)

logger = get_logger("services.live_runtime")


class LiveRuntimeUnavailableError(RuntimeError):
    """Raised when the dedicated live runtime service cannot be reached."""


@dataclass
class LiveRuntimeClient:
    async def start(self, **kwargs: object) -> str:
        payload = await self._call(
            "run_live_start",
            timeout=max(settings.live_runtime_request_timeout_seconds, settings.live_node_startup_timeout_seconds + 20.0),
            **kwargs,
        )
        if payload.get("ok"):
            deployment_id = payload.get("deployment_id")
            if isinstance(deployment_id, str):
                return deployment_id
            raise LiveRuntimeUnavailableError("Live runtime returned an invalid deployment ID")
        self._raise(payload)
        raise LiveRuntimeUnavailableError("Live runtime returned an invalid start payload")

    async def stop(self, deployment_id: str, *, reason: str) -> DeploymentStopResult:
        payload = await self._call(
            "run_live_stop",
            timeout=max(settings.live_runtime_request_timeout_seconds, 120.0),
            deployment_id=deployment_id,
            reason=reason,
        )
        if payload.get("ok"):
            result = payload.get("result")
            if isinstance(result, dict):
                return DeploymentStopResult(
                    found=bool(result.get("found", False)),
                    stopped=bool(result.get("stopped", False)),
                    reason=str(result.get("reason")) if result.get("reason") is not None else None,
                )
            raise LiveRuntimeUnavailableError("Live runtime returned an invalid stop payload")
        self._raise(payload)
        raise LiveRuntimeUnavailableError("Live runtime returned an invalid stop response")

    async def status(self) -> list[dict[str, Any]]:
        payload = await self._call("run_live_status", timeout=15.0)
        if payload.get("ok"):
            rows = payload.get("rows")
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, dict)]
            raise LiveRuntimeUnavailableError("Live runtime returned an invalid status payload")
        self._raise(payload)
        raise LiveRuntimeUnavailableError("Live runtime returned an invalid status response")

    async def kill_all(self) -> int:
        payload = await self._call(
            "run_live_kill_all",
            timeout=max(settings.live_runtime_request_timeout_seconds, 120.0),
        )
        if payload.get("ok"):
            return int(payload.get("stopped", 0) or 0)
        self._raise(payload)
        raise LiveRuntimeUnavailableError("Live runtime returned an invalid kill-all response")

    async def _call(self, function: str, *, timeout: float, **kwargs: object) -> dict[str, Any]:
        pool = await get_redis_pool()
        job = await enqueue_live_runtime(pool, function, **kwargs)
        if job is None:
            raise LiveRuntimeUnavailableError(f"Failed to enqueue live runtime job {function}")
        try:
            result = await job.result(timeout=timeout, poll_delay=0.25)
        except TimeoutError as exc:
            raise LiveRuntimeUnavailableError(
                f"Timed out waiting for live runtime job {function}"
            ) from exc
        except Exception as exc:
            logger.warning("live_runtime_job_failed", function=function, error=str(exc))
            raise LiveRuntimeUnavailableError(
                f"Live runtime job {function} failed: {exc}"
            ) from exc

        if not isinstance(result, dict):
            raise LiveRuntimeUnavailableError(f"Live runtime job {function} returned an invalid payload")
        return result

    @staticmethod
    def _raise(payload: dict[str, Any]) -> None:
        detail = str(payload.get("detail") or "Live runtime request failed")
        error_type = str(payload.get("error_type") or "runtime")
        if error_type == "blocked":
            raise LiveStartBlockedError(detail)
        if error_type == "failed":
            raise LiveStartFailedError(detail)
        if error_type == "liquidation_failed":
            raise LiveLiquidationFailedError(detail)
        raise LiveRuntimeUnavailableError(detail)


live_runtime_client = LiveRuntimeClient()
