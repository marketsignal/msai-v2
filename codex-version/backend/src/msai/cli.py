from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import typer

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.queue import enqueue_portfolio_run, get_redis_pool
from msai.models import Strategy
from msai.services.backtest_analytics import BacktestAnalyticsService
from msai.services.data_ingestion import DataIngestionService
from msai.services.graduation_service import GraduationService
from msai.services.ib_account import ib_account_service
from msai.services.live_runtime import (
    LiveRuntimeUnavailableError,
    live_runtime_client,
)
from msai.services.nautilus.backtest_runner import BacktestRunner
from msai.services.nautilus.catalog_builder import ensure_catalog_data
from msai.services.nautilus.instrument_service import instrument_service
from msai.services.nautilus.strategy_config import (
    prepare_backtest_strategy_config,
    prepare_live_strategy_config,
)
from msai.services.nautilus.trading_node import (
    LiveLiquidationFailedError,
    LiveStartBlockedError,
    LiveStartFailedError,
)
from msai.services.parquet_store import ParquetStore
from msai.services.portfolio_service import (
    PortfolioAllocationInput,
    PortfolioService,
)
from msai.services.research_artifacts import ResearchArtifactService
from msai.services.research_engine import ResearchEngine
from msai.services.research_jobs import ResearchJobService
from msai.services.strategy_registry import StrategyRegistry, file_sha256
from msai.services.strategy_templates import StrategyTemplateService
from msai.services.system_capacity import describe_system_capacity

app = typer.Typer(help="MSAI CLI")
strategy_app = typer.Typer(help="Strategy commands")
backtest_app = typer.Typer(help="Backtest commands")
research_app = typer.Typer(help="Research job commands")
live_app = typer.Typer(help="Live trading commands")
graduation_app = typer.Typer(help="Graduation commands")
portfolio_app = typer.Typer(help="Portfolio commands")
account_app = typer.Typer(help="IB account commands")
app.add_typer(strategy_app, name="strategy")
app.add_typer(backtest_app, name="backtest")
app.add_typer(research_app, name="research")
app.add_typer(live_app, name="live")
app.add_typer(graduation_app, name="graduation")
app.add_typer(portfolio_app, name="portfolio")
app.add_typer(account_app, name="account")


@app.command("health")
def health() -> None:
    typer.echo("ok")


@app.command("ingest")
def ingest(
    asset: str,
    symbols: str,
    start: str,
    end: str,
    provider: str = "auto",
    dataset: str = "",
    schema: str = "",
) -> None:
    symbol_list = [symbol.strip() for symbol in symbols.split(",") if symbol.strip()]

    async def _run() -> dict:
        service = DataIngestionService(ParquetStore(settings.data_root))
        return await service.ingest_historical(
            asset,
            symbol_list,
            start,
            end,
            provider=provider,
            dataset=dataset or None,
            schema=schema or None,
        )

    result = asyncio.run(_run())
    typer.echo(json.dumps(result, indent=2))


@app.command("ingest-daily")
def ingest_daily(
    asset: str,
    symbols: str,
    provider: str = "auto",
    dataset: str = "",
    schema: str = "",
) -> None:
    symbol_list = [symbol.strip() for symbol in symbols.split(",") if symbol.strip()]

    async def _run() -> dict:
        service = DataIngestionService(ParquetStore(settings.data_root))
        return await service.ingest_daily(
            asset,
            symbol_list,
            provider=provider,
            dataset=dataset or None,
            schema=schema or None,
        )

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


@strategy_app.command("templates")
def strategy_templates() -> None:
    service = StrategyTemplateService(settings.strategies_root)
    typer.echo(json.dumps(service.list_templates(), indent=2))


@strategy_app.command("scaffold")
def strategy_scaffold(
    template: str = typer.Argument(..., help="Template identifier from `msai strategy templates`."),
    module_name: str = typer.Argument(..., help="Dotted Python module path under the strategies root."),
    description: str = typer.Option("", "--description", help="Optional strategy docstring override."),
    force: bool = typer.Option(False, "--force", help="Overwrite the target file if it already exists."),
) -> None:
    service = StrategyTemplateService(settings.strategies_root)
    scaffolded = service.scaffold(
        template_id=template,
        module_name=module_name,
        description=description or None,
        force=force,
    )
    typer.echo(json.dumps(scaffolded, indent=2))


@strategy_app.command("sync")
def strategy_sync() -> None:
    async def _run() -> list[dict[str, Any]]:
        registry = StrategyRegistry(settings.strategies_root)
        async with async_session_factory() as session:
            synced = await registry.sync(session)
        return [
            {
                "id": strategy.id,
                "name": strategy.name,
                "file_path": strategy.file_path,
                "strategy_class": strategy.strategy_class,
            }
            for strategy in synced
        ]

    typer.echo(json.dumps(asyncio.run(_run()), indent=2))


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
    config = json.loads(config_json)
    registry, selected = _resolve_strategy(strategy)
    requested_instruments = _parse_instruments(instruments)
    instrument_ids = _prepare_catalog_for_backtests(requested_instruments)
    strategy_config = prepare_backtest_strategy_config(config, instrument_ids)
    runner = BacktestRunner()
    result = runner.run(
        strategy_path=str(registry.root / selected.file_path),
        config=strategy_config,
        instruments=instrument_ids,
        start_date=start,
        end_date=end,
        data_path=settings.nautilus_catalog_root,
    )
    typer.echo(json.dumps(result.metrics, indent=2))


@backtest_app.command("sweep")
def backtest_sweep(
    strategy: str,
    instruments: str,
    start: str,
    end: str,
    grid_json: str,
    config_json: str = "{}",
    objective: str = "sharpe",
    search_strategy: str = "auto",
    study_name: str = "",
    stage_fractions_json: str = "",
    reduction_factor: int = 2,
    min_trades: int = 0,
    require_positive_return: bool = False,
    holdout_fraction: float = 0.0,
    holdout_days: int = 0,
    purge_days: int = 5,
    output_path: str = "",
) -> None:
    registry, selected = _resolve_strategy(strategy)
    requested_instruments = _parse_instruments(instruments)
    instrument_ids = _prepare_catalog_for_backtests(requested_instruments)
    engine = ResearchEngine()
    report = engine.run_parameter_sweep(
        strategy_path=str(registry.root / selected.file_path),
        base_config=json.loads(config_json),
        parameter_grid=_parse_parameter_grid(grid_json),
        instruments=instrument_ids,
        start_date=start,
        end_date=end,
        data_path=settings.nautilus_catalog_root,
        objective=objective,
        instruments_prepared=True,
        search_strategy=search_strategy,
        study_key=study_name or None,
        stage_fractions=_parse_stage_fractions(stage_fractions_json),
        reduction_factor=reduction_factor,
        min_trades=min_trades or None,
        require_positive_return=require_positive_return,
        holdout_fraction=holdout_fraction or None,
        holdout_days=holdout_days or None,
        purge_days=purge_days,
    )
    report_path = engine.save_report(report, Path(output_path) if output_path else None)
    typer.echo(
        json.dumps(
            {
                "report_path": str(report_path),
                "summary": report["summary"],
            },
            indent=2,
        )
    )


@backtest_app.command("walk-forward")
def backtest_walk_forward(
    strategy: str,
    instruments: str,
    start: str,
    end: str,
    grid_json: str,
    train_days: int,
    test_days: int,
    step_days: int = 0,
    mode: str = "rolling",
    config_json: str = "{}",
    objective: str = "sharpe",
    search_strategy: str = "auto",
    study_name: str = "",
    stage_fractions_json: str = "",
    reduction_factor: int = 2,
    min_trades: int = 0,
    require_positive_return: bool = False,
    holdout_fraction: float = 0.0,
    holdout_days: int = 0,
    purge_days: int = 5,
    output_path: str = "",
) -> None:
    registry, selected = _resolve_strategy(strategy)
    requested_instruments = _parse_instruments(instruments)
    instrument_ids = _prepare_catalog_for_backtests(requested_instruments)
    engine = ResearchEngine()
    report = engine.run_walk_forward(
        strategy_path=str(registry.root / selected.file_path),
        base_config=json.loads(config_json),
        parameter_grid=_parse_parameter_grid(grid_json),
        instruments=instrument_ids,
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
        train_days=train_days,
        test_days=test_days,
        step_days=step_days or None,
        mode=mode,
        data_path=settings.nautilus_catalog_root,
        objective=objective,
        instruments_prepared=True,
        search_strategy=search_strategy,
        study_key=study_name or None,
        stage_fractions=_parse_stage_fractions(stage_fractions_json),
        reduction_factor=reduction_factor,
        min_trades=min_trades or None,
        require_positive_return=require_positive_return,
        holdout_fraction=holdout_fraction or None,
        holdout_days=holdout_days or None,
        purge_days=purge_days,
    )
    report_path = engine.save_report(report, Path(output_path) if output_path else None)
    typer.echo(
        json.dumps(
            {
                "report_path": str(report_path),
                "summary": report["summary"],
            },
            indent=2,
        )
    )


@backtest_app.command("analytics")
def backtest_analytics(job_id: str) -> None:
    service = BacktestAnalyticsService(settings.backtest_analytics_root)
    typer.echo(json.dumps(service.load(job_id), indent=2))


@research_app.command("list")
def research_list() -> None:
    typer.echo(json.dumps(ResearchJobService().list_jobs(), indent=2))


@research_app.command("show")
def research_show(job_id: str) -> None:
    typer.echo(json.dumps(ResearchJobService().load_job(job_id), indent=2))


@research_app.command("cancel")
def research_cancel(job_id: str) -> None:
    service = ResearchJobService()
    job = service.request_cancel(job_id)
    queue_job_id = job.get("queue_job_id")
    queue_name = str(job.get("queue_name") or settings.research_queue_name)
    status_value = str(job.get("status") or "pending")

    async def _cancel() -> None:
        if status_value == "pending" and isinstance(queue_job_id, str):
            pool = await get_redis_pool()
            from msai.core.queue import remove_queued_job

            await remove_queued_job(pool, queue_name=queue_name, queue_job_id=queue_job_id)
            service.mark_cancelled(job_id)

    asyncio.run(_cancel())
    typer.echo(json.dumps(service.load_job(job_id), indent=2))


@research_app.command("retry")
def research_retry(job_id: str) -> None:
    service = ResearchJobService()
    job = service.load_job(job_id)
    status_value = str(job.get("status") or "")
    if status_value not in {"failed", "cancelled"}:
        raise typer.BadParameter("Only failed or cancelled jobs can be retried")

    async def _retry() -> None:
        pool = await get_redis_pool()
        queue_job_id = await enqueue_research_job(
            pool,
            job_id,
            str(job["job_type"]),
            dict(job["request"]),
        )
        service.mark_enqueued(
            job_id,
            queue_name=settings.research_queue_name,
            queue_job_id=queue_job_id or job_id,
        )

    from msai.core.queue import enqueue_research_job

    asyncio.run(_retry())
    typer.echo(json.dumps(service.load_job(job_id), indent=2))


@research_app.command("capacity")
def research_capacity() -> None:
    async def _capacity() -> dict[str, Any]:
        pool = await get_redis_pool()
        return await describe_system_capacity(pool)

    typer.echo(json.dumps(asyncio.run(_capacity()), indent=2))


@account_app.command("summary")
def account_summary(
    paper: bool = typer.Option(True, "--paper/--live", help="Query the paper or live IB account."),
) -> None:
    async def _run() -> dict[str, float]:
        summary = await ib_account_service.summary(paper_trading=paper)
        return {
            "net_liquidation": summary.net_liquidation,
            "equity_with_loan_value": summary.equity_with_loan_value,
            "buying_power": summary.buying_power,
            "margin_used": summary.margin_used,
            "initial_margin_requirement": summary.initial_margin_requirement,
            "maintenance_margin_requirement": summary.maintenance_margin_requirement,
            "available_funds": summary.available_funds,
            "excess_liquidity": summary.excess_liquidity,
            "sma": summary.sma,
            "gross_position_value": summary.gross_position_value,
            "cushion": summary.cushion,
            "unrealized_pnl": summary.unrealized_pnl,
        }

    typer.echo(json.dumps(asyncio.run(_run()), indent=2))


@account_app.command("portfolio")
def account_portfolio(
    paper: bool = typer.Option(True, "--paper/--live", help="Query the paper or live IB account."),
) -> None:
    async def _run() -> list[dict[str, float | str]]:
        return await ib_account_service.portfolio(paper_trading=paper)

    typer.echo(json.dumps(asyncio.run(_run()), indent=2))


@account_app.command("snapshot")
def account_snapshot(
    paper: bool = typer.Option(True, "--paper/--live", help="Query the paper or live IB account."),
) -> None:
    async def _run() -> dict[str, Any]:
        snapshot = await ib_account_service.reconciliation_snapshot(paper_trading=paper)
        return {
            "connected": snapshot.connected,
            "mock_mode": snapshot.mock_mode,
            "generated_at": snapshot.generated_at,
            "positions": snapshot.positions,
            "open_orders": snapshot.open_orders,
        }

    typer.echo(json.dumps(asyncio.run(_run()), indent=2))


@account_app.command("health")
def account_health(
    paper: bool = typer.Option(True, "--paper/--live", help="Query the paper or live IB account."),
) -> None:
    async def _run() -> dict[str, str | bool]:
        return await ib_account_service.health(paper_trading=paper)

    typer.echo(json.dumps(asyncio.run(_run()), indent=2))


@live_app.command("start")
def live_start(strategy: str, instruments: str, paper: bool = True, config_json: str = "{}") -> None:
    registry = StrategyRegistry(settings.strategies_root)
    config = json.loads(config_json)
    instrument_list = [value.strip() for value in instruments.split(",") if value.strip()]
    if not instrument_list:
        raise typer.BadParameter("At least one instrument is required")

    async def _run() -> str:
        async with async_session_factory() as session:
            canonical_instruments = await instrument_service.canonicalize_instruments(
                session,
                instrument_list,
                paper_trading=paper,
            )
            strategies = await registry.sync(session)
            selected = next((item for item in strategies if item.name == strategy), None)
            if selected is None:
                raise typer.BadParameter(f"Unknown strategy: {strategy}")
            await session.commit()

        strategy_config = prepare_live_strategy_config(
            config,
            canonical_instruments,
        )
        try:
            return await live_runtime_client.start(
                strategy_id=selected.id,
                strategy_name=selected.name,
                strategy_file=str(registry.root / selected.file_path),
                config=strategy_config,
                instruments=list(
                    dict.fromkeys(
                        [
                            str(strategy_config["instrument_id"]),
                            *canonical_instruments,
                        ]
                    )
                ),
                strategy_code_hash=file_sha256(registry.root / selected.file_path),
                strategy_git_sha=None,
                paper_trading=paper,
                started_by=None,
            )
        except LiveStartBlockedError as exc:
            raise typer.BadParameter(str(exc)) from exc
        except LiveStartFailedError as exc:
            typer.echo(f"live start failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        except LiveRuntimeUnavailableError as exc:
            typer.echo(f"live runtime unavailable: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    deployment_id = asyncio.run(_run())
    typer.echo(f"deployment_id={deployment_id}")


@live_app.command("stop")
def live_stop(deployment_id: str) -> None:
    async def _run():
        return await live_runtime_client.stop(
            deployment_id,
            reason=f"Operator requested graceful stop for deployment {deployment_id}",
        )

    try:
        result = asyncio.run(_run())
    except LiveLiquidationFailedError as exc:
        typer.echo(f"stop failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except LiveRuntimeUnavailableError as exc:
        typer.echo(f"live runtime unavailable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not result.found:
        raise typer.BadParameter(f"Deployment not found: {deployment_id}")
    if not result.stopped:
        raise typer.BadParameter(result.reason or "Deployment could not be stopped")
    typer.echo("stopped")


@live_app.command("status")
def live_status() -> None:
    async def _run() -> list[dict]:
        return await live_runtime_client.status()

    try:
        typer.echo(json.dumps(asyncio.run(_run()), indent=2, default=str))
    except LiveRuntimeUnavailableError as exc:
        typer.echo(f"live runtime unavailable: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@live_app.command("kill-all")
def live_kill_all() -> None:
    async def _run() -> int:
        return await live_runtime_client.kill_all()

    try:
        count = asyncio.run(_run())
    except LiveLiquidationFailedError as exc:
        typer.echo(f"kill-all failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except LiveRuntimeUnavailableError as exc:
        typer.echo(f"live runtime unavailable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"killed={count}")


@graduation_app.command("list")
def graduation_list() -> None:
    typer.echo(json.dumps(GraduationService().list_candidates(), indent=2))


@graduation_app.command("show")
def graduation_show(candidate_id: str) -> None:
    typer.echo(json.dumps(GraduationService().load_candidate(candidate_id), indent=2))


@graduation_app.command("create")
def graduation_create(promotion_id: str, notes: str = "") -> None:
    artifact_service = ResearchArtifactService()
    graduation_service = GraduationService()

    async def _run() -> dict[str, Any]:
        promotion = artifact_service.load_promotion(promotion_id)
        async with async_session_factory() as session:
            registry = StrategyRegistry(settings.strategies_root)
            await registry.sync(session)
            strategy = await session.get(Strategy, str(promotion["strategy_id"]))
            if strategy is None:
                raise typer.BadParameter(f"Strategy not found for promotion {promotion_id}")
            candidate = graduation_service.create_candidate(
                promotion=promotion,
                strategy_path=str(registry.resolve_path(strategy)),
                created_by=None,
                notes=notes or None,
            )
            await session.commit()
            return candidate

    typer.echo(json.dumps(asyncio.run(_run()), indent=2))


@graduation_app.command("stage")
def graduation_stage(candidate_id: str, stage: str, notes: str = "") -> None:
    candidate = GraduationService().update_stage(candidate_id, stage=stage, notes=notes or None)
    typer.echo(json.dumps(candidate, indent=2))


@portfolio_app.command("list")
def portfolio_list() -> None:
    typer.echo(json.dumps(PortfolioService().list_definitions(), indent=2))


@portfolio_app.command("runs")
def portfolio_runs() -> None:
    typer.echo(json.dumps(PortfolioService().list_runs(), indent=2))


@portfolio_app.command("show")
def portfolio_show(portfolio_id: str) -> None:
    typer.echo(json.dumps(PortfolioService().load_definition(portfolio_id), indent=2))


@portfolio_app.command("show-run")
def portfolio_show_run(run_id: str) -> None:
    typer.echo(json.dumps(PortfolioService().load_run(run_id), indent=2))


@portfolio_app.command("create")
def portfolio_create(spec_json: str) -> None:
    payload = _parse_json_object(spec_json)
    allocations_payload = payload.get("allocations")
    if not isinstance(allocations_payload, list) or not allocations_payload:
        raise typer.BadParameter("spec_json.allocations must be a non-empty JSON list")

    allocations = [
        PortfolioAllocationInput(
            candidate_id=str(item["candidate_id"]),
            weight=float(item["weight"]) if "weight" in item and item["weight"] is not None else None,
        )
        for item in allocations_payload
        if isinstance(item, dict) and "candidate_id" in item
    ]
    if not allocations:
        raise typer.BadParameter("No valid portfolio allocations were provided")

    definition = PortfolioService().create_definition(
        name=str(payload.get("name") or "Portfolio"),
        description=str(payload.get("description")) if payload.get("description") is not None else None,
        allocations=allocations,
        created_by=None,
        objective=str(payload.get("objective") or "equal_weight"),
        base_capital=float(payload.get("base_capital") or 1_000_000),
        requested_leverage=float(payload.get("requested_leverage") or 1.0),
        downside_target=float(payload["downside_target"]) if payload.get("downside_target") is not None else None,
        benchmark_symbol=str(payload.get("benchmark_symbol")) if payload.get("benchmark_symbol") is not None else None,
    )
    typer.echo(json.dumps(definition, indent=2))


@portfolio_app.command("run")
def portfolio_run(portfolio_id: str, start: str, end: str, max_parallelism: int = 0) -> None:
    service = PortfolioService()
    run = service.create_run(
        portfolio_id=portfolio_id,
        start_date=start,
        end_date=end,
        created_by=None,
        max_parallelism=max_parallelism or None,
    )

    async def _enqueue() -> None:
        pool = await get_redis_pool()
        queue_job_id = await enqueue_portfolio_run(pool, run["id"])
        service.mark_run_enqueued(
            run["id"],
            queue_name=settings.portfolio_queue_name,
            queue_job_id=queue_job_id or run["id"],
        )

    asyncio.run(_enqueue())
    typer.echo(json.dumps(service.load_run(run["id"]), indent=2))


def _resolve_strategy(strategy: str):
    registry = StrategyRegistry(settings.strategies_root)
    discovered = {item.name: item for item in registry.discover()}
    selected = discovered.get(strategy)
    if selected is None:
        raise typer.BadParameter(f"Unknown strategy: {strategy}")
    return registry, selected


def _parse_instruments(instruments: str) -> list[str]:
    requested_instruments = [value.strip() for value in instruments.split(",") if value.strip()]
    if not requested_instruments:
        raise typer.BadParameter("At least one instrument is required")
    return requested_instruments


def _prepare_catalog_for_backtests(requested_instruments: list[str]) -> list[str]:
    async def _prepare() -> list:
        async with async_session_factory() as session:
            definitions = await instrument_service.ensure_backtest_definitions(
                session,
                requested_instruments,
            )
            await session.commit()
        return definitions

    instrument_definitions = asyncio.run(_prepare())
    return ensure_catalog_data(
        definitions=instrument_definitions,
        raw_parquet_root=settings.parquet_root,
        catalog_root=settings.nautilus_catalog_root,
    )


def _parse_parameter_grid(grid_json: str) -> dict[str, list[Any]]:
    try:
        payload = json.loads(grid_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid grid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise typer.BadParameter("grid_json must be a JSON object of parameter -> candidate list")

    normalized: dict[str, list[Any]] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise typer.BadParameter("Parameter grid keys must be strings")
        if not isinstance(value, list):
            raise typer.BadParameter(f"Parameter grid value for {key!r} must be a JSON list")
        normalized[key] = value
    return normalized


def _parse_json_object(payload: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter("Expected a JSON object")
    return parsed


def _parse_stage_fractions(payload: str) -> list[float] | None:
    if not payload.strip():
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid stage fractions JSON: {exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise typer.BadParameter("stage_fractions_json must be a non-empty JSON list")
    fractions: list[float] = []
    for value in parsed:
        try:
            fraction = float(value)
        except (TypeError, ValueError) as exc:
            raise typer.BadParameter("Stage fractions must be numeric") from exc
        if fraction <= 0 or fraction > 1:
            raise typer.BadParameter("Stage fractions must be in the range (0, 1]")
        fractions.append(fraction)
    return fractions
