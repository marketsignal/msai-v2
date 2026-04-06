"""Periodic health check for IB Gateway connectivity.

The ``IBProbe`` opens a raw TCP connection to the IB Gateway port and
considers the gateway healthy if the connection succeeds within the
configured timeout.  Consecutive failures are tracked; after a
configurable threshold the probe logs an error-level alert.

Designed to run as an ``asyncio`` background task via ``run_periodic()``.
"""

from __future__ import annotations

import asyncio

from msai.core.logging import get_logger

log = get_logger(__name__)

_FAILURE_THRESHOLD = 3


class IBProbe:
    """TCP health check for the IB Gateway.

    Args:
        host: Gateway hostname (default ``"ib-gateway"`` for Docker).
        port: Gateway API port (default ``4002`` for paper trading).
        timeout: Connection timeout in seconds.
    """

    def __init__(
        self,
        host: str = "ib-gateway",
        port: int = 4002,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._consecutive_failures: int = 0
        self._is_healthy: bool = False

    async def check_health(self) -> bool:
        """Perform a single TCP health check.

        Attempts to open a connection to the gateway.  On success the
        failure counter is reset and the probe is marked healthy.  On
        failure the counter increments; after ``_FAILURE_THRESHOLD``
        consecutive failures an error is logged.

        Returns:
            ``True`` if the gateway responded, ``False`` otherwise.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
            writer.close()
            await writer.wait_closed()
            self._consecutive_failures = 0
            self._is_healthy = True
            log.debug("ib_gateway_healthy", host=self.host, port=self.port)
            return True
        except (OSError, asyncio.TimeoutError):
            self._consecutive_failures += 1
            self._is_healthy = False
            if self._consecutive_failures >= _FAILURE_THRESHOLD:
                log.error(
                    "ib_gateway_unhealthy",
                    host=self.host,
                    port=self.port,
                    failures=self._consecutive_failures,
                )
            else:
                log.warning(
                    "ib_gateway_check_failed",
                    host=self.host,
                    port=self.port,
                    failures=self._consecutive_failures,
                )
            return False

    async def run_periodic(self, interval: int = 60) -> None:
        """Run health checks periodically.

        Designed for use as an ``asyncio`` background task::

            task = asyncio.create_task(probe.run_periodic(interval=30))

        Args:
            interval: Seconds between health checks.
        """
        while True:
            await self.check_health()
            await asyncio.sleep(interval)

    @property
    def is_healthy(self) -> bool:
        """Whether the last health check succeeded."""
        return self._is_healthy

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive failed health checks."""
        return self._consecutive_failures
