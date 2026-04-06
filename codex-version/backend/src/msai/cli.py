from __future__ import annotations

import asyncio
import json

import typer

from msai.core.config import settings
from msai.services.data_ingestion import DataIngestionService
from msai.services.nautilus.backtest_runner import BacktestRunner
from msai.services.nautilus.trading_node import trading_node_manager
from msai.services.parquet_store import ParquetStore
from msai.services.risk_engine import RiskEngine
from msai.services.strategy_registry import StrategyRegistry, file_sha256

app = typer.Typer(help="MSAI CLI")
strategy_app = typer.Typer(help="Strategy commands")
backtest_app = typer.Typer(help="Backtest commands")
live_app = typer.Typer(help="Live trading commands")
app.add_typer(strategy_app, name="strategy")
app.add_typer(backtest_app, name="backtest")
app.add_typer(live_app, name="live")


@app.command("health")
def health() -> None:
    typer.echo("ok")


@app.command("ingest")
def ingest(asset: str, symbols: str, start: str, end: str) -> None:
    symbol_list = [symbol.strip() for symbol in symbols.split(",") if symbol.strip()]

    async def _run() -> dict:
        service = DataIngestionService(ParquetStore(settings.data_root))
        return await service.ingest_historical(asset, symbol_list, start, end)

    result = asyncio.run(_run())
    typer.echo(json.dumps(result, indent=2))


@app.command("ingest-daily")
def ingest_daily(asset: str, symbols: str) -> None:
    symbol_list = [symbol.strip() for symbol in symbols.split(",") if symbol.strip()]

    async def _run() -> dict:
        service = DataIngestionService(ParquetStore(settings.data_root))
        return await service.ingest_daily(asset, symbol_list)

    result = asyncio.run(_run())
    typer.echo(json.dumps(result, indent=2))


@app.command("data-status")
def data_status() -> None:
    service = DataIngestionService(ParquetStore(settings.data_root))
    typer.echo(json.dumps(service.data_status(), indent=2, default=str))


@strategy_app.command("list")
def strategy_list() -> None:
    registry = StrategyRegistry(settings.strategies_root)
    discovered = registry.discover()
    payload = [
        {
            "name": strategy.name,
            "file_path": strategy.file_path.as_posix(),
            "strategy_class": strategy.strategy_class,
        }
        for strategy in discovered
    ]
    typer.echo(json.dumps(payload, indent=2))


@strategy_app.command("validate")
def strategy_validate(name: str) -> None:
    registry = StrategyRegistry(settings.strategies_root)
    discovered = {strategy.name: strategy for strategy in registry.discover()}
    selected = discovered.get(name)
    if selected is None:
        raise typer.BadParameter(f"Unknown strategy: {name}")
    path = registry.root / selected.file_path
    if not path.exists():
        raise typer.BadParameter(f"Strategy file missing: {path}")
    typer.echo(f"ok: {name}")


@backtest_app.command("run")
def backtest_run(
    strategy: str,
    instruments: str,
    start: str,
    end: str,
    config_json: str = "{}",
) -> None:
    registry = StrategyRegistry(settings.strategies_root)
    discovered = {item.name: item for item in registry.discover()}
    selected = discovered.get(strategy)
    if selected is None:
        raise typer.BadParameter(f"Unknown strategy: {strategy}")

    config = json.loads(config_json)
    runner = BacktestRunner()
    result = runner.run(
        strategy_path=str(registry.root / selected.file_path),
        config=config,
        instruments=[value.strip() for value in instruments.split(",") if value.strip()],
        start_date=start,
        end_date=end,
        data_path=settings.parquet_root,
    )
    typer.echo(json.dumps(result.metrics, indent=2))


@live_app.command("start")
def live_start(strategy: str, instruments: str, paper: bool = True, config_json: str = "{}") -> None:
    registry = StrategyRegistry(settings.strategies_root)
    discovered = {item.name: item for item in registry.discover()}
    selected = discovered.get(strategy)
    if selected is None:
        raise typer.BadParameter(f"Unknown strategy: {strategy}")

    config = json.loads(config_json)
    instrument_list = [value.strip() for value in instruments.split(",") if value.strip()]
    if not instrument_list:
        raise typer.BadParameter("At least one instrument is required")

    decision = RiskEngine().validate_start(
        strategy=strategy,
        instrument=instrument_list[0],
        quantity=float(config.get("trade_size", 1.0)),
        current_pnl=0.0,
        portfolio_value=1_000_000.0,
        notional_exposure=10_000.0,
    )
    if not decision.allowed:
        raise typer.BadParameter(f"Blocked by risk engine: {decision.reason}")

    async def _run() -> str:
        return await trading_node_manager.start(
            strategy_id=selected.name,
            strategy_file=str(registry.root / selected.file_path),
            config=config,
            instruments=instrument_list,
            strategy_code_hash=file_sha256(registry.root / selected.file_path),
            strategy_git_sha=None,
            paper_trading=paper,
            started_by=None,
        )

    deployment_id = asyncio.run(_run())
    typer.echo(f"deployment_id={deployment_id}")


@live_app.command("stop")
def live_stop(deployment_id: str) -> None:
    async def _run() -> bool:
        return await trading_node_manager.stop(deployment_id)

    stopped = asyncio.run(_run())
    if not stopped:
        raise typer.BadParameter(f"Deployment not found: {deployment_id}")
    typer.echo("stopped")


@live_app.command("status")
def live_status() -> None:
    async def _run() -> list[dict]:
        return await trading_node_manager.status()

    typer.echo(json.dumps(asyncio.run(_run()), indent=2, default=str))


@live_app.command("kill-all")
def live_kill_all() -> None:
    async def _run() -> int:
        return await trading_node_manager.kill_all()

    count = asyncio.run(_run())
    typer.echo(f"killed={count}")
