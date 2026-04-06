from __future__ import annotations

import multiprocessing as mp
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing.process import BaseProcess
from typing import Any

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models import LiveDeployment
from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths

logger = get_logger("trading_node")

try:
    from nautilus_trader.adapters.interactive_brokers.config import (
        InteractiveBrokersDataClientConfig,
        InteractiveBrokersExecClientConfig,
    )
    from nautilus_trader.adapters.interactive_brokers.factories import (
        InteractiveBrokersLiveDataClientFactory,
        InteractiveBrokersLiveExecClientFactory,
    )
    from nautilus_trader.live.config import TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.trading.config import ImportableStrategyConfig

    _NAUTILUS_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - environment-dependent import
    _NAUTILUS_IMPORT_ERROR = exc


@dataclass(slots=True)
class _TradingNodePayload:
    strategy_path: str
    config_path: str
    config: dict[str, Any]
    ibg_host: str
    ibg_port: int
    data_client_id: int
    exec_client_id: int
    account_id: str | None
    trader_id: str


class TradingNodeManager:
    def __init__(self) -> None:
        self._processes: dict[str, BaseProcess] = {}

    async def start(
        self,
        strategy_id: str,
        strategy_file: str,
        config: dict[str, Any],
        instruments: list[str],
        strategy_code_hash: str,
        strategy_git_sha: str | None,
        paper_trading: bool,
        started_by: str | None,
    ) -> str:
        _ = instruments
        if _NAUTILUS_IMPORT_ERROR is not None:
            raise RuntimeError(f"Nautilus live imports unavailable: {_NAUTILUS_IMPORT_ERROR}")

        import_paths = resolve_importable_strategy_paths(strategy_file)
        payload = _TradingNodePayload(
            strategy_path=import_paths.strategy_path,
            config_path=import_paths.config_path,
            config=config,
            ibg_host=settings.ib_gateway_host,
            ibg_port=settings.ib_gateway_port_paper if paper_trading else settings.ib_gateway_port_live,
            data_client_id=settings.ib_data_client_id,
            exec_client_id=settings.ib_exec_client_id,
            account_id=settings.ib_account_id,
            trader_id=settings.nautilus_trader_id,
        )

        process_ctx = mp.get_context("spawn")
        process = process_ctx.Process(target=_run_trading_node_process, args=(payload,), daemon=True)
        process.start()

        async with async_session_factory() as session:
            deployment = LiveDeployment(
                strategy_id=strategy_id,
                strategy_code_hash=strategy_code_hash,
                strategy_git_sha=strategy_git_sha,
                config=config,
                instruments=instruments,
                status="running" if process.is_alive() else "error",
                paper_trading=paper_trading,
                started_at=datetime.now(UTC),
                started_by=started_by,
            )
            session.add(deployment)
            await session.commit()
            await session.refresh(deployment)

        self._processes[deployment.id] = process
        deployment_id = deployment.id
        if not isinstance(deployment_id, str):
            raise RuntimeError("Unexpected deployment ID type")
        return deployment_id

    async def stop(self, deployment_id: str) -> bool:
        process = self._processes.get(deployment_id)
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=10)

        async with async_session_factory() as session:
            deployment = await session.get(LiveDeployment, deployment_id)
            if deployment is None:
                return False
            deployment.status = "stopped"
            deployment.stopped_at = datetime.now(UTC)
            await session.commit()

        self._processes.pop(deployment_id, None)
        return True

    async def kill_all(self) -> int:
        active = list(self._processes.keys())
        for deployment_id in active:
            await self.stop(deployment_id)
        return len(active)

    async def status(self) -> list[dict[str, Any]]:
        async with async_session_factory() as session:
            rows = (await session.execute(LiveDeployment.__table__.select())).mappings().all()

        status_rows: list[dict[str, Any]] = []
        for row in rows:
            deployment_id = row["id"]
            process = self._processes.get(deployment_id)
            process_alive = process.is_alive() if process is not None else False
            computed_status = row["status"]
            if row["status"] == "running" and not process_alive:
                computed_status = "error"
            status_rows.append({**dict(row), "process_alive": process_alive, "status": computed_status})
        return status_rows


def _run_trading_node_process(payload: _TradingNodePayload) -> None:
    if _NAUTILUS_IMPORT_ERROR is not None:
        raise RuntimeError(f"Nautilus live imports unavailable: {_NAUTILUS_IMPORT_ERROR}")

    node_cfg = build_trading_node_config(payload)
    node = TradingNode(config=node_cfg)
    node.add_data_client_factory("IB", InteractiveBrokersLiveDataClientFactory)
    node.add_exec_client_factory("IB", InteractiveBrokersLiveExecClientFactory)

    def _shutdown_handler(signum: int, frame: Any) -> None:
        _ = signum, frame
        try:
            node.stop()
            node.dispose()
        finally:
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    try:
        node.build()
        node.run(raise_exception=True)
    finally:
        try:
            if node.is_running():
                node.stop()
        finally:
            node.dispose()


def build_trading_node_config(payload: _TradingNodePayload) -> TradingNodeConfig:
    if _NAUTILUS_IMPORT_ERROR is not None:
        raise RuntimeError(f"Nautilus live imports unavailable: {_NAUTILUS_IMPORT_ERROR}")

    strategy_cfg = ImportableStrategyConfig(
        strategy_path=payload.strategy_path,
        config_path=payload.config_path,
        config=payload.config,
    )
    data_cfg = InteractiveBrokersDataClientConfig(
        ibg_host=payload.ibg_host,
        ibg_port=payload.ibg_port,
        ibg_client_id=payload.data_client_id,
    )
    exec_cfg = InteractiveBrokersExecClientConfig(
        ibg_host=payload.ibg_host,
        ibg_port=payload.ibg_port,
        ibg_client_id=payload.exec_client_id,
        account_id=payload.account_id,
    )

    return TradingNodeConfig(
        trader_id=payload.trader_id,
        strategies=[strategy_cfg],
        data_clients={"IB": data_cfg},
        exec_clients={"IB": exec_cfg},
    )


trading_node_manager = TradingNodeManager()
