"""Manages NautilusTrader TradingNode processes for live/paper trading.

Each strategy deployment runs as a managed subprocess.  The
``TradingNodeManager`` validates deployments against the ``RiskEngine``
before starting them and provides lifecycle management (start, stop,
stop-all) and status queries.

Phase 1 tracks processes in-memory without actually spawning subprocesses.
Phase 2 will spawn real NautilusTrader ``TradingNode`` processes.
"""

from __future__ import annotations

import subprocess
from typing import Any

from msai.core.logging import get_logger
from msai.services.risk_engine import RiskEngine

log = get_logger(__name__)


class TradingNodeManager:
    """Manages TradingNode lifecycle.

    Each deployment is identified by a string ``deployment_id`` and
    tracked in an internal dictionary.  The ``RiskEngine`` is consulted
    before every new deployment.

    Args:
        risk_engine: The shared risk engine instance for pre-deployment
            validation and emergency halt propagation.
    """

    def __init__(self, risk_engine: RiskEngine) -> None:
        self.risk_engine = risk_engine
        self._processes: dict[str, subprocess.Popen[bytes] | None] = {}

    async def start(
        self,
        deployment_id: str,
        strategy_path: str,
        config: dict[str, Any],
        instruments: list[str],
    ) -> bool:
        """Start a TradingNode for a deployment.

        Validates with the risk engine first.  If rejected, logs a warning
        and returns ``False``.

        Args:
            deployment_id: Unique identifier for this deployment.
            strategy_path: Filesystem path to the strategy module.
            config: Strategy configuration parameters.
            instruments: List of instrument identifiers to subscribe to.

        Returns:
            ``True`` if the node was started (or marked as started),
            ``False`` if the risk engine rejected it.
        """
        allowed, reason = self.risk_engine.validate_deployment(config, len(self._processes))
        if not allowed:
            log.warning(
                "deployment_rejected",
                deployment_id=deployment_id,
                reason=reason,
            )
            return False

        log.info(
            "trading_node_start",
            deployment_id=deployment_id,
            strategy_path=strategy_path,
            instruments=instruments,
        )
        # TODO: Actually spawn NautilusTrader TradingNode subprocess in Phase 2
        self._processes[deployment_id] = None  # Placeholder
        return True

    async def stop(self, deployment_id: str) -> bool:
        """Stop a TradingNode gracefully.

        Args:
            deployment_id: Identifier of the deployment to stop.

        Returns:
            ``True`` if stopped, ``False`` if the deployment was not found.
        """
        if deployment_id not in self._processes:
            log.warning("trading_node_not_found", deployment_id=deployment_id)
            return False

        proc = self._processes[deployment_id]
        if proc is not None:
            proc.terminate()
            log.info("trading_node_process_terminated", deployment_id=deployment_id)

        log.info("trading_node_stop", deployment_id=deployment_id)
        del self._processes[deployment_id]
        return True

    async def stop_all(self) -> int:
        """Emergency stop all running nodes.

        Triggers ``kill_all()`` on the risk engine and then stops every
        managed process.

        Returns:
            The number of nodes that were stopped.
        """
        count = len(self._processes)
        self.risk_engine.kill_all()
        for did in list(self._processes.keys()):
            await self.stop(did)
        log.critical("all_trading_nodes_stopped", count=count)
        return count

    def status(self) -> dict[str, str]:
        """Return the status of all managed nodes.

        Returns:
            Dictionary mapping deployment IDs to their status string.
        """
        return {did: "running" for did in self._processes}

    @property
    def active_count(self) -> int:
        """Number of currently active deployments."""
        return len(self._processes)
