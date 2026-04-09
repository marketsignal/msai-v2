from __future__ import annotations

import asyncio

from msai.core.logging import get_logger
from msai.services.alerting import AlertingService, alerting_service
from msai.services.ib_account import IBAccountService, ib_account_service

logger = get_logger("ib_probe")


class IBProbe:
    def __init__(self, account_service: IBAccountService, alerting: AlertingService) -> None:
        self._account_service = account_service
        self._alerting = alerting
        self._task: asyncio.Task | None = None
        self._consecutive_failures = 0
        self._status = "unknown"

    async def check_once(self) -> bool:
        try:
            health = await self._account_service.health()
            if not bool(health.get("connected", False)):
                raise RuntimeError("IB not connected")
            summary = await self._account_service.summary()
            if summary.net_liquidation <= 0:
                raise RuntimeError("IB account balance is zero")
            self._status = "healthy"
            if self._consecutive_failures >= 3:
                self._alerting.send_recovery("IB Gateway recovered", "Connectivity restored")
            self._consecutive_failures = 0
            return True
        except Exception as exc:
            self._consecutive_failures += 1
            self._status = "degraded"
            logger.warning("ib_probe_failed", error=str(exc), failures=self._consecutive_failures)
            if self._consecutive_failures >= 3:
                self._alerting.send_alert("error", "IB Gateway unhealthy", str(exc))
            return False

    async def _loop(self) -> None:
        while True:
            await self.check_once()
            await asyncio.sleep(60)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    def health_status(self) -> dict[str, int | str]:
        return {
            "status": self._status,
            "consecutive_failures": self._consecutive_failures,
        }

ib_probe = IBProbe(ib_account_service, alerting_service)
