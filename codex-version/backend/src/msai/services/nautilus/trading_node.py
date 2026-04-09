from __future__ import annotations

import asyncio
import multiprocessing as mp
import queue
import signal
import time
import traceback
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing.process import BaseProcess
from typing import Any
from urllib.parse import urlparse

import msgspec
from sqlalchemy import select

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models import InstrumentDefinition, LiveDeployment
from msai.services.ib_account import BrokerSnapshot, ib_account_service
from msai.services.live_updates import (
    clear_live_scope,
    load_live_snapshot,
    load_live_snapshots,
    publish_live_update,
)
from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths

logger = get_logger("trading_node")

_ACTIVE_RUNTIME_STATUSES = {"running", "starting", "liquidating"}
_RECONCILE_TARGET_STATUSES = {
    "running",
    "starting",
    "liquidating",
    "unmanaged",
    "stale",
    "reconcile_required",
    "orphaned_exposure",
}

try:
    from nautilus_trader.adapters.interactive_brokers.config import (
        InteractiveBrokersDataClientConfig,
        InteractiveBrokersExecClientConfig,
        InteractiveBrokersInstrumentProviderConfig,
    )
    from nautilus_trader.adapters.interactive_brokers.factories import (
        InteractiveBrokersLiveDataClientFactory,
        InteractiveBrokersLiveExecClientFactory,
    )
    from nautilus_trader.cache.config import CacheConfig
    from nautilus_trader.common.component import LiveClock, MessageBus
    from nautilus_trader.common.config import (
        DatabaseConfig,
        MessageBusConfig,
        msgspec_encoding_hook,
    )
    from nautilus_trader.common.messages import ShutdownSystem
    from nautilus_trader.core import nautilus_pyo3
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.live.config import LiveExecEngineConfig, TradingNodeConfig
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.identifiers import ComponentId, InstrumentId, TraderId
    from nautilus_trader.serialization.serializer import MsgSpecSerializer
    from nautilus_trader.trading.config import ImportableControllerConfig, ImportableStrategyConfig

    _NAUTILUS_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - environment-dependent import
    _NAUTILUS_IMPORT_ERROR = exc


@dataclass(slots=True)
class _TradingNodePayload:
    deployment_id: str
    strategy_id: str
    strategy_name: str
    strategy_code_hash: str
    strategy_path: str
    config_path: str
    config: dict[str, Any]
    ibg_host: str
    ibg_port: int
    data_client_id: int
    exec_client_id: int
    account_id: str | None
    trader_id: str
    paper_trading: bool
    instrument_ids: tuple[str, ...]


@dataclass(slots=True)
class DeploymentStopResult:
    found: bool
    stopped: bool
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class _BrokerExposureView:
    connected: bool | None
    mock_mode: bool
    generated_at: str | None
    open_positions: int
    open_orders: int
    exposure_detected: bool
    reason: str | None = None


class LiveStartError(RuntimeError):
    pass


class LiveStartBlockedError(LiveStartError):
    pass


class LiveStartFailedError(LiveStartError):
    pass


class LiveLiquidationFailedError(RuntimeError):
    pass


class TradingNodeManager:
    def __init__(self) -> None:
        self._processes: dict[str, BaseProcess] = {}

    async def start(
        self,
        strategy_id: str,
        strategy_name: str,
        strategy_file: str,
        config: dict[str, Any],
        instruments: list[str],
        strategy_code_hash: str,
        strategy_git_sha: str | None,
        paper_trading: bool,
        started_by: str | None,
    ) -> str:
        if _NAUTILUS_IMPORT_ERROR is not None:
            raise RuntimeError(f"Nautilus live imports unavailable: {_NAUTILUS_IMPORT_ERROR}")

        await self._assert_no_live_overlap(
            instruments=instruments,
            paper_trading=paper_trading,
        )

        async with async_session_factory() as session:
            data_client_id, exec_client_id = await self._allocate_ib_client_ids(
                session,
                paper_trading=paper_trading,
            )
            deployment = LiveDeployment(
                strategy_id=strategy_id,
                strategy_code_hash=strategy_code_hash,
                strategy_git_sha=strategy_git_sha,
                config=config,
                instruments=instruments,
                status="starting",
                paper_trading=paper_trading,
                ib_data_client_id=data_client_id,
                ib_exec_client_id=exec_client_id,
                process_pid=None,
                started_at=datetime.now(UTC),
                started_by=started_by,
            )
            session.add(deployment)
            await session.commit()
            await session.refresh(deployment)

        deployment_id = deployment.id
        if not isinstance(deployment_id, str):
            raise RuntimeError("Unexpected deployment ID type")

        import_paths = resolve_importable_strategy_paths(strategy_file)
        payload = _TradingNodePayload(
            deployment_id=deployment_id,
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            strategy_code_hash=strategy_code_hash,
            strategy_path=import_paths.strategy_path,
            config_path=import_paths.config_path,
            config=config,
            ibg_host=settings.ib_gateway_host,
            ibg_port=settings.ib_gateway_port_paper if paper_trading else settings.ib_gateway_port_live,
            data_client_id=data_client_id,
            exec_client_id=exec_client_id,
            account_id=settings.ib_account_id,
            trader_id=_deployment_trader_id(deployment_id),
            paper_trading=paper_trading,
            instrument_ids=tuple(instruments),
        )

        process_ctx = mp.get_context("spawn")
        startup_queue: Any = process_ctx.Queue()
        process = process_ctx.Process(
            name=f"msai-live-{deployment_id}",
            target=_run_trading_node_process,
            args=(payload, startup_queue),
            daemon=False,
        )

        try:
            process.start()
            startup_status = await self._await_startup_status(
                deployment_id=deployment_id,
                process=process,
                startup_queue=startup_queue,
            )

            async with async_session_factory() as session:
                deployment = await session.get(LiveDeployment, deployment_id)
                if deployment is None:
                    raise RuntimeError("Live deployment record disappeared during startup")
                deployment.status = str(startup_status.get("status", "running"))
                deployment.process_pid = process.pid
                await session.commit()

            self._processes[deployment_id] = process
        except LiveStartBlockedError:
            await self._update_deployment_state(deployment_id, "blocked")
            _terminate_managed_process(process)
            raise
        except LiveStartFailedError:
            await self._update_deployment_state(deployment_id, "error")
            _terminate_managed_process(process)
            raise
        except Exception:
            await self._update_deployment_state(deployment_id, "error")
            _terminate_managed_process(process)
            raise
        finally:
            startup_queue.close()

        await _publish_live_update_safely(
            "deployment.started",
            {
                "deployment_id": deployment_id,
                "strategy_id": strategy_id,
                "paper_trading": paper_trading,
                "process_pid": process.pid,
                "ib_data_client_id": data_client_id,
                "ib_exec_client_id": exec_client_id,
                "instruments": instruments,
            },
        )
        return deployment_id

    async def stop(self, deployment_id: str) -> DeploymentStopResult:
        remote_stop = False
        remote_state: str | None = None
        async with async_session_factory() as session:
            deployment = await session.get(LiveDeployment, deployment_id)
            if deployment is None:
                return DeploymentStopResult(found=False, stopped=False, reason="not_found")

            process = self._processes.get(deployment_id)
            if process is None:
                remote_status = await self._load_remote_status(deployment_id)
                remote_state = str(remote_status.get("status")) if remote_status is not None else None
                if remote_state == "stopped":
                    deployment.status = "stopped"
                    deployment.process_pid = None
                    deployment.stopped_at = datetime.now(UTC)
                    await session.commit()
                    await _clear_live_scope_safely(deployment_id)
                    return DeploymentStopResult(found=True, stopped=True, reason="already_stopped")
                if remote_state in {"blocked", "error"}:
                    deployment.status = remote_state
                    deployment.process_pid = None
                    deployment.stopped_at = datetime.now(UTC)
                    await session.commit()
                    return DeploymentStopResult(
                        found=True,
                        stopped=False,
                        reason=f"Deployment is already {remote_state}",
                    )
                if remote_status is None or remote_state not in {"running", "starting", "liquidating"}:
                    if deployment.status in {"running", "starting", "liquidating"}:
                        deployment.status = "unmanaged"
                        await session.commit()
                    return DeploymentStopResult(
                        found=True,
                        stopped=False,
                        reason="Deployment is not managed by this API instance",
                    )
                remote_stop = True
            else:
                process_pid = process.pid
                _terminate_managed_process(process)
                deployment.status = "stopped"
                deployment.process_pid = None
                deployment.stopped_at = datetime.now(UTC)
                await session.commit()

        if remote_stop:
            if remote_state != "liquidating":
                await _publish_shutdown_command(
                    trader_id=_deployment_trader_id(deployment_id),
                    reason=f"API stop requested for deployment {deployment_id}",
                )
            try:
                await self._await_remote_shutdown(deployment_id)
            except LiveStartFailedError:
                return DeploymentStopResult(
                    found=True,
                    stopped=False,
                    reason="Timed out waiting for remote deployment shutdown",
                )
            async with async_session_factory() as session:
                deployment = await session.get(LiveDeployment, deployment_id)
                if deployment is not None:
                    deployment.status = "stopped"
                    deployment.process_pid = None
                    deployment.stopped_at = datetime.now(UTC)
                    await session.commit()
            await _clear_live_scope_safely(deployment_id)
            await _publish_live_update_safely(
                "deployment.stopped",
                {"deployment_id": deployment_id, "process_pid": None},
            )
            return DeploymentStopResult(found=True, stopped=True, reason="stopped")

        self._processes.pop(deployment_id, None)
        await _clear_live_scope_safely(deployment_id)
        await _publish_live_update_safely(
            "deployment.stopped",
            {"deployment_id": deployment_id, "process_pid": process_pid},
        )
        return DeploymentStopResult(found=True, stopped=True, reason="stopped")

    async def liquidate_and_stop(self, deployment_id: str, *, reason: str) -> DeploymentStopResult:
        process = self._processes.get(deployment_id)
        async with async_session_factory() as session:
            deployment = await session.get(LiveDeployment, deployment_id)
            if deployment is None:
                return DeploymentStopResult(found=False, stopped=False, reason="not_found")

            remote_status = await self._load_remote_status(deployment_id)
            remote_state = str(remote_status.get("status")) if remote_status is not None else None

            if remote_state == "stopped":
                deployment.status = "stopped"
                deployment.process_pid = None
                deployment.stopped_at = datetime.now(UTC)
                await session.commit()
                await _clear_live_scope_safely(deployment_id)
                return DeploymentStopResult(found=True, stopped=True, reason="already_stopped")

            if remote_state in {"blocked", "error"}:
                deployment.status = remote_state
                deployment.process_pid = None
                deployment.stopped_at = datetime.now(UTC)
                await session.commit()
                return DeploymentStopResult(
                    found=True,
                    stopped=False,
                    reason=f"Deployment is already {remote_state}",
                )

            if process is not None and _runtime_status_is_flat(remote_status):
                logger.info(
                    "deployment_flat_local_stop",
                    deployment_id=deployment_id,
                    reason=reason,
                )
                return await self.stop(deployment_id)

            if process is not None and await _deployment_is_broker_flat(
                {
                    "id": deployment.id,
                    "paper_trading": deployment.paper_trading,
                    "instruments": list(deployment.instruments or []),
                    "ib_exec_client_id": deployment.ib_exec_client_id,
                }
            ):
                logger.info(
                    "deployment_broker_flat_local_stop",
                    deployment_id=deployment_id,
                    reason=reason,
                )
                return await self.stop(deployment_id)

            is_controllable = process is not None or remote_state in {"running", "starting", "liquidating"}
            if not is_controllable:
                if deployment.status in {"running", "starting", "liquidating"}:
                    deployment.status = "unmanaged"
                    await session.commit()
                return DeploymentStopResult(
                    found=True,
                    stopped=False,
                    reason="Deployment is not reachable for liquidation",
                )

        try:
            await _publish_liquidation_command(
                trader_id=_deployment_trader_id(deployment_id),
                deployment_id=deployment_id,
                reason=reason,
                shutdown_after_flat=True,
            )
        except Exception as exc:
            return DeploymentStopResult(
                found=True,
                stopped=False,
                reason=f"Failed to publish liquidation command: {exc}",
            )
        try:
            await self._await_remote_shutdown(deployment_id)
        except LiveStartFailedError as exc:
            return DeploymentStopResult(found=True, stopped=False, reason=str(exc))

        process = self._processes.get(deployment_id)
        if process is not None and not _join_managed_process(process):
            return DeploymentStopResult(
                found=True,
                stopped=False,
                reason="Managed trading node process did not exit after liquidation",
            )

        async with async_session_factory() as session:
            deployment = await session.get(LiveDeployment, deployment_id)
            if deployment is not None:
                deployment.status = "stopped"
                deployment.process_pid = None
                deployment.stopped_at = datetime.now(UTC)
                await session.commit()

        self._processes.pop(deployment_id, None)
        await _clear_live_scope_safely(deployment_id)
        await _publish_live_update_safely(
            "deployment.stopped",
            {"deployment_id": deployment_id, "process_pid": None},
        )
        return DeploymentStopResult(found=True, stopped=True, reason="stopped")

    async def kill_all(self) -> int:
        rows = await self.status()
        failures: list[str] = []
        stopped = 0
        for row in rows:
            deployment_id = str(row["id"])
            status = str(row["status"])
            if status == "unmanaged" and not bool(row.get("broker_exposure_detected")):
                await self._finalize_flat_unmanaged_deployment(deployment_id)
                stopped += 1
                continue
            if status in {"blocked", "error", "stopped", "unmanaged"}:
                continue
            result = await self.liquidate_and_stop(
                deployment_id,
                reason=f"Global kill-all liquidation requested for deployment {deployment_id}",
            )
            if result.stopped:
                stopped += 1
                continue
            failures.append(f"{deployment_id}: {result.reason or 'liquidation failed'}")

        if failures:
            raise LiveLiquidationFailedError("; ".join(failures))
        return stopped

    async def _assert_no_live_overlap(
        self,
        *,
        instruments: list[str],
        paper_trading: bool,
    ) -> None:
        conflicts = _overlapping_live_deployments(
            await self.status(),
            instruments=instruments,
            paper_trading=paper_trading,
        )
        if not conflicts:
            return

        details = ", ".join(
            f"{deployment_id} ({', '.join(overlap)})"
            for deployment_id, overlap in conflicts
        )
        mode = "paper" if paper_trading else "live"
        raise LiveStartBlockedError(
            f"Cannot start overlapping {mode} deployment; instruments already controlled by {details}"
        )

    async def _allocate_ib_client_ids(
        self,
        session: Any,
        *,
        paper_trading: bool,
    ) -> tuple[int, int]:
        rows = (
            await session.execute(
                select(
                    LiveDeployment.status,
                    LiveDeployment.ib_data_client_id,
                    LiveDeployment.ib_exec_client_id,
                ).where(LiveDeployment.paper_trading.is_(paper_trading))
            )
        ).mappings().all()
        return _next_ib_client_id_pair(rows)

    async def status(self) -> list[dict[str, Any]]:
        async with async_session_factory() as session:
            rows = (await session.execute(LiveDeployment.__table__.select())).mappings().all()
        runtime_by_id = await _load_status_snapshots_by_scope()
        now = datetime.now(UTC)
        broker_views = await self._load_broker_views(rows, runtime_by_id, now)

        status_rows: list[dict[str, Any]] = []
        for row in rows:
            deployment_id = str(row["id"])
            process = self._processes.get(deployment_id)
            process_alive = process.is_alive() if process is not None else False
            computed_status = str(row["status"])
            runtime = runtime_by_id.get(deployment_id)
            runtime_fresh = _snapshot_is_fresh(runtime, now=now)
            runtime_status = _snapshot_status(runtime)
            broker_view = broker_views.get(deployment_id)
            control_mode = "local" if process is not None and process_alive else "none"
            reason = _snapshot_reason(runtime) if runtime_fresh else None

            if runtime_fresh and runtime_status is not None:
                computed_status = runtime_status
                if process is None and runtime_status in _ACTIVE_RUNTIME_STATUSES:
                    control_mode = "remote"
            elif process is not None and process_alive:
                computed_status = "stale"
                control_mode = "local"
                reason = "Local Nautilus process is alive but runtime snapshots are stale"
            elif broker_view is not None and broker_view.reason is not None:
                computed_status = "reconcile_required"
                control_mode = "broker"
                reason = broker_view.reason
            elif broker_view is not None and broker_view.exposure_detected:
                computed_status = "orphaned_exposure"
                control_mode = "broker"
                if process is not None and not process_alive:
                    reason = "Managed Nautilus process exited while broker exposure remains"
                else:
                    reason = "Broker exposure remains without a fresh Nautilus runtime snapshot"
            elif computed_status in _RECONCILE_TARGET_STATUSES:
                computed_status = "error" if process is not None and not process_alive else "unmanaged"
                control_mode = "broker" if broker_view is not None else "none"
                reason = "Broker reports the account is flat, but the deployment is no longer broadcasting state"

            status_rows.append(
                {
                    **dict(row),
                    "process_alive": process_alive,
                    "status": computed_status,
                    "control_mode": control_mode,
                    "runtime_fresh": runtime_fresh,
                    "reason": reason,
                    "broker_connected": broker_view.connected if broker_view is not None else None,
                    "broker_mock_mode": broker_view.mock_mode if broker_view is not None else False,
                    "broker_updated_at": broker_view.generated_at if broker_view is not None else None,
                    "broker_open_positions": broker_view.open_positions if broker_view is not None else 0,
                    "broker_open_orders": broker_view.open_orders if broker_view is not None else 0,
                    "broker_exposure_detected": (
                        broker_view.exposure_detected if broker_view is not None else False
                    ),
                }
            )
        return status_rows

    async def _update_deployment_state(
        self,
        deployment_id: str,
        status: str,
        *,
        process_pid: int | None = None,
    ) -> None:
        async with async_session_factory() as session:
            deployment = await session.get(LiveDeployment, deployment_id)
            if deployment is None:
                return
            deployment.status = status
            deployment.process_pid = process_pid
            if status in {"blocked", "error", "stopped"}:
                deployment.stopped_at = datetime.now(UTC)
            await session.commit()
        if status == "error":
            await _clear_live_scope_safely(deployment_id)

    async def _finalize_flat_unmanaged_deployment(self, deployment_id: str) -> None:
        async with async_session_factory() as session:
            deployment = await session.get(LiveDeployment, deployment_id)
            if deployment is None:
                return
            deployment.status = "stopped"
            deployment.process_pid = None
            deployment.stopped_at = datetime.now(UTC)
            await session.commit()

        self._processes.pop(deployment_id, None)
        await _clear_live_scope_safely(deployment_id)
        await _publish_live_update_safely(
            "deployment.stopped",
            {"deployment_id": deployment_id, "process_pid": None},
        )

    async def _await_startup_status(
        self,
        *,
        deployment_id: str,
        process: BaseProcess,
        startup_queue: Any,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + settings.live_node_startup_timeout_seconds
        queue_error: str | None = None

        while time.monotonic() < deadline:
            startup_snapshot = await load_live_snapshot("status", scope=deployment_id)
            if startup_snapshot is not None and isinstance(startup_snapshot.get("data"), dict):
                status_payload = dict(startup_snapshot["data"])
                status_value = str(status_payload.get("status", ""))
                if status_value == "running":
                    return status_payload
                if status_value == "blocked":
                    reason = str(status_payload.get("reason") or "Trading node startup blocked")
                    raise LiveStartBlockedError(reason)
                if status_value == "error":
                    reason = str(status_payload.get("reason") or "Trading node startup failed")
                    raise LiveStartFailedError(reason)

            try:
                while True:
                    startup_result = startup_queue.get_nowait()
                    if startup_result.get("status") == "error":
                        queue_error = _summarize_startup_error(
                            startup_result.get("error", "Trading node failed during startup")
                        )
                        break
            except queue.Empty:
                pass

            if queue_error is not None:
                raise LiveStartFailedError(queue_error)

            if not process.is_alive():
                raise LiveStartFailedError("Trading node exited during startup")

            await asyncio.sleep(0.25)

        raise LiveStartFailedError("Timed out waiting for trading node startup")

    async def _load_remote_status(self, deployment_id: str) -> dict[str, Any] | None:
        snapshot = await load_live_snapshot("status", scope=deployment_id)
        if snapshot is None:
            return None
        data = snapshot.get("data")
        if not isinstance(data, dict):
            return None
        if not _snapshot_is_fresh(snapshot, now=datetime.now(UTC)):
            return None
        return dict(data)

    async def _load_broker_views(
        self,
        rows: list[Mapping[str, Any]],
        runtime_by_id: Mapping[str, dict[str, Any]],
        now: datetime,
    ) -> dict[str, _BrokerExposureView]:
        reconcile_rows = [
            row
            for row in rows
            if str(row.get("status")) in _RECONCILE_TARGET_STATUSES
            and not _snapshot_is_fresh(runtime_by_id.get(str(row["id"])), now=now)
        ]
        if not reconcile_rows:
            return {}

        instrument_ids = {
            str(instrument_id)
            for row in reconcile_rows
            for instrument_id in row.get("instruments", [])
            if instrument_id
        }
        definitions_by_id = await _load_instrument_definitions(instrument_ids)

        broker_snapshots: dict[bool, BrokerSnapshot | Exception] = {}
        for paper_trading in {bool(row.get("paper_trading", True)) for row in reconcile_rows}:
            try:
                broker_snapshots[paper_trading] = await ib_account_service.reconciliation_snapshot(
                    paper_trading=paper_trading
                )
            except Exception as exc:
                logger.warning(
                    "broker_reconciliation_failed",
                    paper_trading=paper_trading,
                    error=str(exc),
                )
                broker_snapshots[paper_trading] = exc

        return {
            str(row["id"]): _deployment_broker_view(
                row,
                definitions_by_id=definitions_by_id,
                broker_state=broker_snapshots.get(bool(row.get("paper_trading", True))),
            )
            for row in reconcile_rows
        }

    async def _await_remote_shutdown(self, deployment_id: str) -> None:
        deadline = time.monotonic() + settings.live_node_startup_timeout_seconds
        while time.monotonic() < deadline:
            snapshot = await load_live_snapshot("status", scope=deployment_id)
            if snapshot is not None and isinstance(snapshot.get("data"), dict):
                status_value = str(snapshot["data"].get("status", ""))
                if status_value == "stopped":
                    return
                if status_value == "error":
                    raise LiveStartFailedError(
                        str(snapshot["data"].get("reason") or "Remote deployment failed during shutdown")
                    )
            await asyncio.sleep(0.25)
        raise LiveStartFailedError("Timed out waiting for remote deployment shutdown")


def _run_trading_node_process(payload: _TradingNodePayload, startup_queue: Any) -> None:
    if _NAUTILUS_IMPORT_ERROR is not None:
        raise RuntimeError(f"Nautilus live imports unavailable: {_NAUTILUS_IMPORT_ERROR}")

    node: TradingNode | None = None
    try:
        node_cfg = build_trading_node_config(payload)
        node = TradingNode(config=node_cfg)
        node.kernel.msgbus.add_streaming_type(ShutdownSystem)
        node.kernel.msgbus.add_streaming_type(dict)
        node.kernel.msgbus.add_streaming_type(str)
        node.add_data_client_factory("IB", InteractiveBrokersLiveDataClientFactory)
        node.add_exec_client_factory("IB", InteractiveBrokersLiveExecClientFactory)

        def _shutdown_handler(signum: int, frame: Any) -> None:
            _ = signum, frame
            try:
                if node is not None:
                    node.stop()
                    node.dispose()
            finally:
                raise SystemExit(0)

        signal.signal(signal.SIGTERM, _shutdown_handler)
        signal.signal(signal.SIGINT, _shutdown_handler)

        node.build()
        node.run(raise_exception=True)
    except Exception:
        with suppress(Exception):
            startup_queue.put({"status": "error", "error": traceback.format_exc()})
        raise
    finally:
        try:
            if node is not None and node.is_running():
                node.stop()
        finally:
            if node is not None:
                node.dispose()


def build_trading_node_config(payload: _TradingNodePayload) -> TradingNodeConfig:
    if _NAUTILUS_IMPORT_ERROR is not None:
        raise RuntimeError(f"Nautilus live imports unavailable: {_NAUTILUS_IMPORT_ERROR}")

    instrument_provider = _build_instrument_provider_config(payload.instrument_ids)
    strategy_cfg = ImportableStrategyConfig(
        strategy_path=payload.strategy_path,
        config_path=payload.config_path,
        config=payload.config,
    )
    data_cfg = InteractiveBrokersDataClientConfig(
        instrument_provider=instrument_provider,
        ibg_host=payload.ibg_host,
        ibg_port=payload.ibg_port,
        ibg_client_id=payload.data_client_id,
    )
    exec_cfg = InteractiveBrokersExecClientConfig(
        instrument_provider=instrument_provider,
        ibg_host=payload.ibg_host,
        ibg_port=payload.ibg_port,
        ibg_client_id=payload.exec_client_id,
        account_id=payload.account_id,
    )
    redis_database = _redis_database_config()
    reconciliation_instrument_ids = [
        InstrumentId.from_str(instrument_id) for instrument_id in payload.instrument_ids
    ] or None

    return TradingNodeConfig(
        trader_id=payload.trader_id,
        cache=CacheConfig(database=redis_database),
        message_bus=MessageBusConfig(
            database=redis_database,
            external_streams=[
                _deployment_shutdown_stream(payload.trader_id),
                _deployment_liquidation_stream(payload.trader_id, payload.deployment_id),
            ],
        ),
        load_state=True,
        save_state=True,
        controller=ImportableControllerConfig(
            controller_path="msai.services.nautilus.live_state:LiveStateController",
            config_path="msai.services.nautilus.live_state:LiveStateControllerConfig",
            config={
                "deployment_id": payload.deployment_id,
                "strategy_db_id": payload.strategy_id,
                "strategy_name": payload.strategy_name,
                "strategy_code_hash": payload.strategy_code_hash,
                "instrument_ids": list(payload.instrument_ids),
                "startup_instrument_id": str(payload.config["instrument_id"]),
                "startup_quantity": float(payload.config.get("trade_size", 1.0)),
                "account_id": payload.account_id,
                "paper_trading": payload.paper_trading,
                "snapshot_interval_secs": settings.live_state_snapshot_interval_seconds,
                "liquidation_topic": _deployment_liquidation_topic(payload.deployment_id),
            },
        ),
        exec_engine=LiveExecEngineConfig(
            snapshot_orders=True,
            snapshot_positions=True,
            snapshot_positions_interval_secs=5.0,
            reconciliation=True,
            reconciliation_instrument_ids=reconciliation_instrument_ids,
        ),
        strategies=[strategy_cfg],
        data_clients={"IB": data_cfg},
        exec_clients={"IB": exec_cfg},
    )


trading_node_manager = TradingNodeManager()


def _redis_database_config() -> DatabaseConfig:
    parsed = urlparse(settings.redis_url)
    return DatabaseConfig(
        type="redis",
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        username=parsed.username or None,
        password=parsed.password or None,
        ssl=parsed.scheme == "rediss",
    )


def _build_instrument_provider_config(
    instrument_ids: Iterable[str],
) -> InteractiveBrokersInstrumentProviderConfig:
    parsed_ids = frozenset(InstrumentId.from_str(instrument_id) for instrument_id in instrument_ids)
    return InteractiveBrokersInstrumentProviderConfig(load_ids=parsed_ids or None)


def _terminate_managed_process(process: BaseProcess | None) -> None:
    if process is None:
        return
    try:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
    except Exception:
        logger.warning("process_termination_failed", pid=getattr(process, "pid", None))


def _join_managed_process(process: BaseProcess | None, timeout: float = 10.0) -> bool:
    if process is None:
        return True
    try:
        process.join(timeout=timeout)
        if process.is_alive():
            logger.warning("process_join_timed_out", pid=getattr(process, "pid", None))
            process.terminate()
            process.join(timeout=5)
        return not process.is_alive()
    except Exception:
        logger.warning("process_join_failed", pid=getattr(process, "pid", None))
        return False


async def _publish_live_update_safely(event_type: str, data: dict[str, Any]) -> None:
    try:
        await publish_live_update(event_type, data)
    except Exception as exc:
        logger.warning("live_update_publish_failed", event_type=event_type, error=str(exc))


async def _clear_live_scope_safely(scope: str) -> None:
    try:
        await clear_live_scope(scope)
    except Exception as exc:
        logger.warning("live_scope_clear_failed", scope=scope, error=str(exc))


def _summarize_startup_error(error: object) -> str:
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    if not lines:
        return "Trading node failed during startup"
    return lines[-1]


def _deployment_trader_id(deployment_id: str) -> str:
    normalized = deployment_id.replace("-", "").upper()
    return f"{settings.nautilus_trader_id}-{normalized[:12]}"


def _deployment_shutdown_stream(trader_id: str) -> str:
    return f"trader-{trader_id}:stream:commands.system.shutdown"


def _deployment_liquidation_topic(deployment_id: str) -> str:
    return f"commands.msai.deployment.{deployment_id}.liquidate"


def _deployment_liquidation_stream(trader_id: str, deployment_id: str) -> str:
    return f"trader-{trader_id}:stream:{_deployment_liquidation_topic(deployment_id)}"


async def _load_status_snapshots_by_scope() -> dict[str, dict[str, Any]]:
    snapshots = await load_live_snapshots("status")
    return {
        str(snapshot.get("scope")): snapshot
        for snapshot in snapshots
        if snapshot.get("scope")
    }


async def _load_instrument_definitions(
    instrument_ids: set[str],
) -> dict[str, InstrumentDefinition]:
    if not instrument_ids:
        return {}

    async with async_session_factory() as session:
        result = await session.execute(
            select(InstrumentDefinition).where(InstrumentDefinition.instrument_id.in_(instrument_ids))
        )
        models = result.scalars().all()
    return {row.instrument_id: row for row in models}


def _deployment_broker_view(
    row: Mapping[str, Any],
    *,
    definitions_by_id: Mapping[str, InstrumentDefinition],
    broker_state: BrokerSnapshot | Exception | None,
) -> _BrokerExposureView:
    if broker_state is None:
        return _BrokerExposureView(
            connected=None,
            mock_mode=False,
            generated_at=None,
            open_positions=0,
            open_orders=0,
            exposure_detected=False,
            reason="Broker reconciliation snapshot is unavailable",
        )

    if isinstance(broker_state, Exception):
        return _BrokerExposureView(
            connected=None,
            mock_mode=False,
            generated_at=None,
            open_positions=0,
            open_orders=0,
            exposure_detected=False,
            reason=f"Broker reconciliation failed: {broker_state}",
        )

    if broker_state.mock_mode:
        return _BrokerExposureView(
            connected=False,
            mock_mode=True,
            generated_at=broker_state.generated_at,
            open_positions=0,
            open_orders=0,
            exposure_detected=False,
            reason="Broker reconciliation is running in mock mode",
        )

    deployment_instruments = [str(value) for value in row.get("instruments", []) if value]
    matchers = [
        _instrument_matcher(instrument_id, definitions_by_id.get(instrument_id))
        for instrument_id in deployment_instruments
    ]
    exec_client_id = _optional_int(row.get("ib_exec_client_id"))
    matched_positions = [
        position
        for position in broker_state.positions
        if any(_broker_row_matches_instrument(position, matcher) for matcher in matchers)
    ]
    if exec_client_id is not None:
        matched_orders = [
            order
            for order in broker_state.open_orders
            if _optional_int(order.get("client_id")) == exec_client_id
        ]
    else:
        matched_orders = [
            order
            for order in broker_state.open_orders
            if any(_broker_row_matches_instrument(order, matcher) for matcher in matchers)
        ]

    return _BrokerExposureView(
        connected=broker_state.connected,
        mock_mode=broker_state.mock_mode,
        generated_at=broker_state.generated_at,
        open_positions=len(matched_positions),
        open_orders=len(matched_orders),
        exposure_detected=bool(matched_positions or matched_orders),
        reason=None,
    )


def _instrument_matcher(
    instrument_id: str,
    definition: InstrumentDefinition | None,
) -> dict[str, Any]:
    aliases = {instrument_id.upper()}
    base_value = instrument_id.rsplit(".", 1)[0].upper()
    aliases.add(base_value)
    con_ids: set[int] = set()

    if definition is not None:
        aliases.add(str(definition.raw_symbol).upper())
        contract = definition.contract_details or {}
        contract_payload = contract.get("contract") if isinstance(contract, dict) else None
        if isinstance(contract_payload, dict):
            for key in ("localSymbol", "symbol", "tradingClass"):
                value = contract_payload.get(key)
                if value:
                    aliases.add(str(value).upper())
            con_id = _optional_int(contract_payload.get("conId"))
            if con_id is not None:
                con_ids.add(con_id)

    return {"aliases": aliases, "con_ids": con_ids}


def _broker_row_matches_instrument(
    row: Mapping[str, Any],
    matcher: Mapping[str, Any],
) -> bool:
    con_id = _optional_int(row.get("con_id"))
    matcher_con_ids = matcher.get("con_ids", set())
    if con_id is not None and con_id in matcher_con_ids:
        return True

    aliases = {
        str(value).upper()
        for value in (
            row.get("instrument"),
            row.get("symbol"),
            row.get("local_symbol"),
        )
        if value
    }
    return bool(aliases.intersection(matcher.get("aliases", set())))


def _snapshot_status(snapshot: dict[str, Any] | None) -> str | None:
    if snapshot is None:
        return None
    data = snapshot.get("data")
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    return str(status) if status is not None else None


def _snapshot_reason(snapshot: dict[str, Any] | None) -> str | None:
    if snapshot is None:
        return None
    data = snapshot.get("data")
    if not isinstance(data, dict):
        return None
    reason = data.get("reason")
    return str(reason) if reason is not None else None


def _optional_int(value: object | None) -> int | None:
    if value in (None, "", 0):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _next_ib_client_id_pair(rows: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
    used_ids = {
        client_id
        for row in rows
        if str(row.get("status")) not in {"blocked", "error", "stopped", "unmanaged"}
        for client_id in (
            _optional_int(row.get("ib_data_client_id")),
            _optional_int(row.get("ib_exec_client_id")),
        )
        if client_id is not None
    }

    for offset in range(10_000):
        data_client_id = settings.ib_data_client_id + (offset * 2)
        exec_client_id = settings.ib_exec_client_id + (offset * 2)
        if data_client_id == exec_client_id:
            continue
        if data_client_id not in used_ids and exec_client_id not in used_ids:
            return data_client_id, exec_client_id

    raise RuntimeError("Unable to allocate unique IB client IDs for live deployment")


def _overlapping_live_deployments(
    rows: Iterable[Mapping[str, Any]],
    *,
    instruments: Iterable[str],
    paper_trading: bool,
) -> list[tuple[str, list[str]]]:
    requested = {str(instrument_id) for instrument_id in instruments if instrument_id}
    if not requested:
        return []

    conflicts: list[tuple[str, list[str]]] = []
    for row in rows:
        if bool(row.get("paper_trading")) != paper_trading:
            continue
        if str(row.get("status")) in {"blocked", "error", "stopped", "unmanaged"}:
            continue
        overlap = sorted(requested.intersection(str(value) for value in row.get("instruments", []) if value))
        if overlap:
            conflicts.append((str(row.get("id")), overlap))
    return conflicts


def _snapshot_is_fresh(snapshot: dict[str, Any] | None, *, now: datetime) -> bool:
    if snapshot is None:
        return False
    data = snapshot.get("data")
    if not isinstance(data, dict):
        return False
    updated_at = data.get("updated_at") or snapshot.get("generated_at")
    if not isinstance(updated_at, str):
        return False
    try:
        updated_dt = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    if updated_dt.tzinfo is None:
        updated_dt = updated_dt.replace(tzinfo=UTC)
    max_age = max(settings.live_state_snapshot_interval_seconds * 3.0, 15.0)
    return (now - updated_dt).total_seconds() <= max_age


async def _deployment_is_broker_flat(row: Mapping[str, Any]) -> bool:
    instrument_ids = {
        str(instrument_id)
        for instrument_id in row.get("instruments", [])
        if instrument_id
    }
    definitions_by_id = await _load_instrument_definitions(instrument_ids)
    try:
        broker_state = await ib_account_service.reconciliation_snapshot(
            paper_trading=bool(row.get("paper_trading", True))
        )
    except Exception as exc:
        logger.warning(
            "broker_flat_check_failed",
            deployment_id=row.get("id"),
            error=str(exc),
        )
        return False

    broker_view = _deployment_broker_view(
        row,
        definitions_by_id=definitions_by_id,
        broker_state=broker_state,
    )
    return _broker_view_is_flat(broker_view)


def _broker_view_is_flat(view: _BrokerExposureView) -> bool:
    return view.connected is True and view.exposure_detected is False


def _runtime_status_is_flat(snapshot: Mapping[str, Any] | None) -> bool:
    if snapshot is None:
        return False

    try:
        open_positions = int(snapshot.get("open_positions", 0) or 0)
        open_orders = int(snapshot.get("open_orders", 0) or 0)
    except (TypeError, ValueError):
        return False
    return open_positions == 0 and open_orders == 0


async def _publish_shutdown_command(trader_id: str, reason: str) -> None:
    if _NAUTILUS_IMPORT_ERROR is not None:
        raise RuntimeError(f"Nautilus live imports unavailable: {_NAUTILUS_IMPORT_ERROR}")

    redis_database = _redis_database_config()
    message_bus_config = MessageBusConfig(database=redis_database)
    clock = LiveClock()
    msgbus_database = nautilus_pyo3.RedisMessageBusDatabase(
        trader_id=nautilus_pyo3.TraderId(trader_id),
        instance_id=nautilus_pyo3.UUID4.from_str(UUID4().value),
        config_json=msgspec.json.encode(message_bus_config, enc_hook=msgspec_encoding_hook),
    )
    bus = MessageBus(
        trader_id=TraderId(trader_id),
        clock=clock,
        serializer=MsgSpecSerializer(encoding=msgspec.msgpack),
        database=msgbus_database,
        config=message_bus_config,
    )
    try:
        bus.publish(
            "commands.system.shutdown",
            ShutdownSystem(
                trader_id=TraderId(trader_id),
                component_id=ComponentId("msai-control"),
                command_id=UUID4(),
                ts_init=clock.timestamp_ns(),
                reason=reason,
            ),
        )
    finally:
        bus.dispose()


async def _publish_liquidation_command(
    trader_id: str,
    deployment_id: str,
    *,
    reason: str,
    shutdown_after_flat: bool,
) -> None:
    if _NAUTILUS_IMPORT_ERROR is not None:
        raise RuntimeError(f"Nautilus live imports unavailable: {_NAUTILUS_IMPORT_ERROR}")

    redis_database = _redis_database_config()
    message_bus_config = MessageBusConfig(database=redis_database)
    clock = LiveClock()
    msgbus_database = nautilus_pyo3.RedisMessageBusDatabase(
        trader_id=nautilus_pyo3.TraderId(trader_id),
        instance_id=nautilus_pyo3.UUID4.from_str(UUID4().value),
        config_json=msgspec.json.encode(message_bus_config, enc_hook=msgspec_encoding_hook),
    )
    bus = MessageBus(
        trader_id=TraderId(trader_id),
        clock=clock,
        serializer=MsgSpecSerializer(encoding=msgspec.msgpack),
        database=msgbus_database,
        config=message_bus_config,
    )
    try:
        bus.publish(
            _deployment_liquidation_topic(deployment_id),
            {
                "action": "liquidate",
                "reason": reason,
                "shutdown_after_flat": shutdown_after_flat,
            },
        )
    finally:
        bus.dispose()
