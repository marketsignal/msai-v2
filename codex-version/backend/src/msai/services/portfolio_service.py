from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.services.analytics_math import (
    build_series_from_returns,
    combine_weighted_returns,
    compute_series_metrics,
    dataframe_to_series_payload,
    normalize_weights,
)
from msai.services.backtest_analytics import BacktestAnalyticsService
from msai.services.graduation_service import GraduationService
from msai.services.market_data_query import MarketDataQuery
from msai.services.nautilus.backtest_runner import BacktestRunner
from msai.services.nautilus.instrument_service import instrument_service
from msai.services.nautilus.strategy_config import prepare_backtest_strategy_config
from msai.services.report_generator import ReportGenerator

PORTFOLIO_OBJECTIVES = {"equal_weight", "maximize_profit", "maximize_sharpe", "maximize_sortino", "manual"}


class PortfolioDefinitionNotFoundError(FileNotFoundError):
    """Raised when a portfolio definition cannot be found."""


class PortfolioRunNotFoundError(FileNotFoundError):
    """Raised when a portfolio run cannot be found."""


class PortfolioDefinitionError(ValueError):
    """Raised when a portfolio definition is invalid."""


@dataclass(frozen=True, slots=True)
class PortfolioAllocationInput:
    candidate_id: str
    weight: float | None = None


class PortfolioService:
    def __init__(
        self,
        root: Path | None = None,
        *,
        graduation_service: GraduationService | None = None,
        analytics_service: BacktestAnalyticsService | None = None,
    ) -> None:
        self.root = root or settings.portfolio_root
        self.definitions_root = self.root / "definitions"
        self.runs_root = self.root / "runs"
        self.graduation_service = graduation_service or GraduationService()
        self.analytics_service = analytics_service or BacktestAnalyticsService()
        self.market_data_query = MarketDataQuery(settings.data_root)

    def list_definitions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not self.definitions_root.exists():
            return []
        rows = [self._read_json(path) for path in sorted(self.definitions_root.glob("*.json"))]
        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def load_definition(self, portfolio_id: str) -> dict[str, Any]:
        path = self._definition_path(portfolio_id)
        if not path.exists():
            raise PortfolioDefinitionNotFoundError(f"Portfolio definition not found: {portfolio_id}")
        return self._read_json(path)

    def create_definition(
        self,
        *,
        name: str,
        allocations: list[PortfolioAllocationInput],
        created_by: str | None,
        objective: str,
        base_capital: float,
        requested_leverage: float,
        downside_target: float | None = None,
        benchmark_symbol: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        if objective not in PORTFOLIO_OBJECTIVES:
            raise PortfolioDefinitionError(f"Unsupported portfolio objective: {objective}")
        if not allocations:
            raise PortfolioDefinitionError("A portfolio requires at least one allocation")

        normalized_rows = self._resolve_allocations(allocations, objective=objective)
        portfolio_id = str(uuid4())
        now = _now_iso()
        payload = {
            "id": portfolio_id,
            "name": name,
            "description": description,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
            "objective": objective,
            "base_capital": float(base_capital),
            "requested_leverage": float(requested_leverage),
            "downside_target": float(downside_target) if downside_target is not None else None,
            "benchmark_symbol": benchmark_symbol,
            "allocations": normalized_rows,
        }
        self._write_json(self._definition_path(portfolio_id), payload)
        return payload

    def list_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not self.runs_root.exists():
            return []
        rows = [self._read_json(path) for path in sorted(self.runs_root.glob("*.json"))]
        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def load_run(self, run_id: str) -> dict[str, Any]:
        path = self._run_path(run_id)
        if not path.exists():
            raise PortfolioRunNotFoundError(f"Portfolio run not found: {run_id}")
        return self._read_json(path)

    def create_run(
        self,
        *,
        portfolio_id: str,
        start_date: str,
        end_date: str,
        created_by: str | None,
        max_parallelism: int | None = None,
    ) -> dict[str, Any]:
        definition = self.load_definition(portfolio_id)
        run_id = str(uuid4())
        payload = {
            "id": run_id,
            "portfolio_id": portfolio_id,
            "portfolio_name": definition["name"],
            "created_by": created_by,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": "pending",
            "start_date": start_date,
            "end_date": end_date,
            "max_parallelism": max_parallelism,
            "error_message": None,
            "metrics": None,
            "series": [],
            "allocations": definition["allocations"],
            "report_path": None,
            "queue_name": None,
            "queue_job_id": None,
            "worker_id": None,
            "attempt": 0,
            "heartbeat_at": None,
        }
        self._write_json(self._run_path(run_id), payload)
        return payload

    def mark_run_enqueued(self, run_id: str, *, queue_name: str, queue_job_id: str | None) -> dict[str, Any]:
        return self._update_run(
            run_id,
            updated_at=_now_iso(),
            queue_name=queue_name,
            queue_job_id=queue_job_id or run_id,
        )

    def mark_run_running(self, run_id: str, *, worker_id: str | None = None) -> dict[str, Any]:
        current = self.load_run(run_id)
        return self._update_run(
            run_id,
            status="running",
            updated_at=_now_iso(),
            error_message=None,
            worker_id=worker_id,
            heartbeat_at=_now_iso(),
            attempt=int(current.get("attempt") or 0) + 1,
        )

    def heartbeat_run(self, run_id: str, *, worker_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"updated_at": _now_iso(), "heartbeat_at": _now_iso()}
        if worker_id:
            payload["worker_id"] = worker_id
        return self._update_run(run_id, **payload)

    def mark_run_failed(self, run_id: str, *, error_message: str) -> dict[str, Any]:
        return self._update_run(
            run_id,
            status="failed",
            updated_at=_now_iso(),
            error_message=error_message,
            heartbeat_at=_now_iso(),
        )

    async def run_portfolio_backtest(self, run_id: str) -> dict[str, Any]:
        run = self.load_run(run_id)
        definition = self.load_definition(str(run["portfolio_id"]))
        allocations = list(definition.get("allocations") or [])
        if not allocations:
            raise PortfolioDefinitionError("Portfolio definition has no allocations")

        strategy_runs = await self._execute_candidate_backtests(
            allocations=allocations,
            start_date=str(run["start_date"]),
            end_date=str(run["end_date"]),
            max_parallelism=run.get("max_parallelism"),
        )

        weighted_series = [
            (
                str(item["candidate_id"]),
                float(item["weight"]),
                pd.Series(item["returns"], index=pd.to_datetime(item["timestamps"], utc=True)),
            )
            for item in strategy_runs
        ]

        effective_leverage = _effective_leverage(
            weighted_series=weighted_series,
            requested_leverage=float(definition["requested_leverage"]),
            downside_target=definition.get("downside_target"),
        )
        combined_returns = combine_weighted_returns(weighted_series, leverage=effective_leverage)
        benchmark_returns = self._load_benchmark_returns(
            definition.get("benchmark_symbol"),
            start_date=str(run["start_date"]),
            end_date=str(run["end_date"]),
        )
        metrics = compute_series_metrics(combined_returns, benchmark_returns=benchmark_returns).as_dict()
        metrics["num_strategies"] = len(strategy_runs)
        metrics["effective_leverage"] = effective_leverage
        series_frame = build_series_from_returns(combined_returns, base_value=float(definition["base_capital"]))

        report_generator = ReportGenerator(settings.reports_root / "portfolios")
        benchmark_series = None
        if benchmark_returns is not None:
            benchmark_series = (1.0 + benchmark_returns).cumprod() - 1.0
        html = report_generator.generate_tearsheet(combined_returns, benchmark=benchmark_series)
        report_path = report_generator.save_report(html, run_id)

        completed = self._update_run(
            run_id,
            status="completed",
            updated_at=_now_iso(),
            completed_at=_now_iso(),
            heartbeat_at=_now_iso(),
            metrics=metrics,
            series=dataframe_to_series_payload(series_frame),
            allocations=strategy_runs,
            report_path=str(report_path),
        )
        return completed

    def _resolve_allocations(
        self,
        allocations: list[PortfolioAllocationInput],
        *,
        objective: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for allocation in allocations:
            candidate = self.graduation_service.load_candidate(allocation.candidate_id)
            rows.append(
                {
                    "candidate_id": candidate["id"],
                    "strategy_id": candidate["strategy_id"],
                    "strategy_name": candidate["strategy_name"],
                    "strategy_path": candidate["strategy_path"],
                    "instruments": list(candidate.get("instruments") or []),
                    "config": dict(candidate.get("config") or {}),
                    "selection": dict(candidate.get("selection") or {}),
                    "weight": float(allocation.weight) if allocation.weight is not None else _heuristic_weight(candidate, objective),
                }
            )
        return normalize_weights(rows)

    async def _execute_candidate_backtests(
        self,
        *,
        allocations: list[dict[str, Any]],
        start_date: str,
        end_date: str,
        max_parallelism: int | None,
    ) -> list[dict[str, Any]]:
        worker_count = max(1, min(len(allocations), int(max_parallelism or settings.research_max_parallelism or 1)))
        if worker_count <= 1:
            return [await self._run_candidate_backtest(allocation, start_date=start_date, end_date=end_date) for allocation in allocations]

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            loop = asyncio.get_running_loop()
            tasks = [
                loop.run_in_executor(
                    executor,
                    lambda row=allocation: asyncio_run_sync(
                        self._run_candidate_backtest(row, start_date=start_date, end_date=end_date)
                    ),
                )
                for allocation in allocations
            ]
            return list(await asyncio.gather(*tasks))

    async def _run_candidate_backtest(
        self,
        allocation: dict[str, Any],
        *,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        from msai.services.nautilus.catalog_builder import ensure_catalog_data

        instruments = list(allocation["instruments"])
        if not instruments:
            raise PortfolioDefinitionError(
                f"Portfolio allocation {allocation['candidate_id']} has no instruments"
            )

        async with async_session_factory() as session:
            definitions = await instrument_service.ensure_backtest_definitions(session, instruments)
            await session.commit()

        instrument_ids = ensure_catalog_data(
            definitions=definitions,
            raw_parquet_root=settings.parquet_root,
            catalog_root=settings.nautilus_catalog_root,
        )
        config = prepare_backtest_strategy_config(dict(allocation["config"]), instrument_ids)
        result = BacktestRunner().run(
            strategy_path=str(allocation["strategy_path"]),
            config=config,
            instruments=instrument_ids,
            start_date=start_date,
            end_date=end_date,
            data_path=settings.nautilus_catalog_root,
            timeout_seconds=settings.backtest_timeout_seconds,
        )
        frame = self.analytics_service.build_payload(
            backtest_id=f"candidate-{allocation['candidate_id']}",
            account_df=result.account_df,
            metrics=result.metrics,
            report_path=None,
        )
        returns = [float(point["returns"]) for point in frame["series"]]
        timestamps = [str(point["timestamp"]) for point in frame["series"]]
        return {
            "candidate_id": allocation["candidate_id"],
            "strategy_name": allocation["strategy_name"],
            "instruments": list(instrument_ids),
            "weight": float(allocation["weight"]),
            "metrics": dict(result.metrics),
            "series": frame["series"],
            "returns": returns,
            "timestamps": timestamps,
        }

    def _load_benchmark_returns(
        self,
        benchmark_symbol: object,
        *,
        start_date: str,
        end_date: str,
    ) -> pd.Series | None:
        symbol = str(benchmark_symbol or "").strip()
        if not symbol:
            return None
        raw_symbol = symbol.split(".", 1)[0]
        bars = self.market_data_query.get_bars(raw_symbol, start_date, end_date, interval="1m")
        rows = list(bars.get("bars") or [])
        if not rows:
            return None
        frame = pd.DataFrame(rows)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        close = frame.dropna(subset=["timestamp", "close"]).set_index("timestamp")["close"]
        if close.empty:
            return None
        returns = close.pct_change().fillna(0.0)
        returns.name = "benchmark_returns"
        return returns

    def _definition_path(self, portfolio_id: str) -> Path:
        return self.definitions_root / f"{portfolio_id}.json"

    def _run_path(self, run_id: str) -> Path:
        return self.runs_root / f"{run_id}.json"

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        temp_path.replace(path)

    def _read_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text())

    def _update_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        payload = self.load_run(run_id)
        payload.update(fields)
        self._write_json(self._run_path(run_id), payload)
        return payload


def _heuristic_weight(candidate: dict[str, Any], objective: str) -> float:
    metrics = dict(candidate.get("selection", {}).get("metrics") or {})
    if objective == "maximize_profit":
        return max(float(metrics.get("total_return") or 0.0), 0.0) or 1.0
    if objective == "maximize_sortino":
        return max(float(metrics.get("sortino") or 0.0), 0.0) or 1.0
    if objective == "maximize_sharpe":
        return max(float(metrics.get("sharpe") or 0.0), 0.0) or 1.0
    return float(candidate.get("weight") or 1.0)


def _effective_leverage(
    *,
    weighted_series: list[tuple[str, float, pd.Series]],
    requested_leverage: float,
    downside_target: object,
) -> float:
    leverage = max(0.0, float(requested_leverage))
    if leverage <= 0.0 or downside_target in (None, ""):
        return leverage or 1.0

    combined = combine_weighted_returns(weighted_series, leverage=1.0)
    metrics = compute_series_metrics(combined)
    downside_risk = float(metrics.downside_risk)
    target = float(downside_target)
    if downside_risk <= 0.0:
        return leverage
    scale = min(1.0, target / downside_risk)
    return max(0.1, leverage * scale)


def asyncio_run_sync(coro):
    import asyncio

    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        if loop.is_running():
            new_loop = asyncio.new_event_loop()
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
