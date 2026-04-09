from __future__ import annotations

import multiprocessing as mp
import pickle
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pandas as pd

from msai.services.analytics_math import compute_series_metrics
from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths

try:
    from nautilus_trader.backtest.config import (
        BacktestDataConfig,
        BacktestEngineConfig,
        BacktestRunConfig,
        BacktestVenueConfig,
    )
    from nautilus_trader.backtest.node import BacktestNode
    from nautilus_trader.model.identifiers import InstrumentId, Venue
    from nautilus_trader.trading.config import ImportableStrategyConfig

    _NAUTILUS_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - environment-dependent import
    _NAUTILUS_IMPORT_ERROR = exc


@dataclass(slots=True)
class BacktestResult:
    orders_df: pd.DataFrame
    positions_df: pd.DataFrame
    account_df: pd.DataFrame
    metrics: dict[str, float | int]


@dataclass(slots=True)
class _RunInput:
    strategy_path: str
    config: dict[str, Any]
    instruments: list[str]
    start_date: str
    end_date: str
    data_path: str
    result_path: str


class BacktestRunner:
    def run(
        self,
        strategy_path: str,
        config: dict[str, Any],
        instruments: list[str],
        start_date: str,
        end_date: str,
        data_path: Path,
        timeout_seconds: int = 30 * 60,
    ) -> BacktestResult:
        payload = _RunInput(
            strategy_path=strategy_path,
            config=config,
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
            data_path=str(data_path),
            result_path="",
        )
        with tempfile.NamedTemporaryFile(prefix="msai-backtest-", suffix=".pkl", delete=False) as tmp:
            result_path = Path(tmp.name)
        payload.result_path = str(result_path)
        ctx = mp.get_context("spawn")
        process = ctx.Process(target=_subprocess_run, args=(payload,))
        try:
            process.start()
            process.join(timeout_seconds)

            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                raise TimeoutError("Backtest subprocess exceeded timeout")

            if not result_path.exists():
                raise RuntimeError("Backtest subprocess exited without result")
            with result_path.open("rb") as handle:
                result = cast("dict[str, Any]", pickle.load(handle))
            if not bool(result.get("ok")):
                raise RuntimeError(str(result.get("error", "Unknown backtest error")))

            return BacktestResult(
                orders_df=pd.DataFrame(result.get("orders", [])),
                positions_df=pd.DataFrame(result.get("positions", [])),
                account_df=pd.DataFrame(result.get("account", [])),
                metrics=cast("dict[str, float | int]", result.get("metrics", {})),
            )
        finally:
            if result_path.exists():
                result_path.unlink()
            if hasattr(process, "close"):
                process.close()


def _subprocess_run(payload: _RunInput) -> None:
    if _NAUTILUS_IMPORT_ERROR is not None:
        _write_subprocess_result(
            payload.result_path,
            {"ok": False, "error": f"Nautilus import failed: {_NAUTILUS_IMPORT_ERROR}"},
        )
        return

    try:
        run_config = _build_backtest_run_config(payload)
        node = BacktestNode([run_config])
        try:
            results = node.run()
            if not results:
                _write_subprocess_result(
                    payload.result_path,
                    {
                        "ok": True,
                        "orders": [],
                        "positions": [],
                        "account": [],
                        "metrics": _zero_metrics(),
                    },
                )
                return

            raw_result = results[0]
            run_config_id = getattr(raw_result, "run_config_id", None)
            engine = node.get_engine(run_config_id) if run_config_id else None
            if engine is None:
                _write_subprocess_result(
                    payload.result_path,
                    {
                        "ok": True,
                        "orders": [],
                        "positions": [],
                        "account": [],
                        "metrics": _extract_metrics(raw_result, pd.DataFrame()),
                    },
                )
                return

            orders_df = engine.trader.generate_orders_report()
            positions_df = engine.trader.generate_positions_report()
            account_frames = [
                engine.trader.generate_account_report(venue=Venue(venue_name))
                for venue_name in _backtest_venues(payload.instruments)
            ]
            account_df = (
                pd.concat(account_frames)
                if account_frames
                else pd.DataFrame()
            )
            account_payload = _compact_account_report(account_df)

            _write_subprocess_result(
                payload.result_path,
                {
                    "ok": True,
                    "orders": orders_df.to_dict(orient="records"),
                    "positions": positions_df.to_dict(orient="records"),
                    "account": account_payload.to_dict(orient="records"),
                    "metrics": _extract_metrics(raw_result, orders_df, account_payload),
                },
            )
        finally:
            cast("Any", node).dispose()
    except Exception:
        _write_subprocess_result(payload.result_path, {"ok": False, "error": traceback.format_exc()})


def _write_subprocess_result(result_path: str, payload: dict[str, Any]) -> None:
    with Path(result_path).open("wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)


def _build_backtest_run_config(payload: _RunInput) -> BacktestRunConfig:
    import_paths = resolve_importable_strategy_paths(payload.strategy_path)

    strategy_config = ImportableStrategyConfig(
        strategy_path=import_paths.strategy_path,
        config_path=import_paths.config_path,
        config=payload.config,
    )
    engine_config = BacktestEngineConfig(strategies=[strategy_config])

    data_config = BacktestDataConfig(
        catalog_path=payload.data_path,
        data_cls="nautilus_trader.model.data:Bar",
        instrument_ids=payload.instruments,
        start_time=payload.start_date,
        end_time=payload.end_date,
    )

    return BacktestRunConfig(
        venues=_build_backtest_venue_configs(payload.instruments),
        data=[data_config],
        engine=engine_config,
        start=payload.start_date,
        end=payload.end_date,
        raise_exception=True,
        dispose_on_completion=False,
    )


def _build_backtest_venue_configs(instruments: list[str]) -> list[BacktestVenueConfig]:
    return [
        BacktestVenueConfig(
            name=venue_name,
            oms_type="NETTING",
            account_type="MARGIN",
            starting_balances=["1000000 USD"],
            base_currency="USD",
        )
        for venue_name in _backtest_venues(instruments)
    ]


def _backtest_venues(instruments: list[str]) -> list[str]:
    venues: list[str] = []
    for instrument_id in instruments:
        venue_name = InstrumentId.from_str(instrument_id).venue.value
        if venue_name not in venues:
            venues.append(venue_name)
    return venues


def _extract_metrics(
    raw_result: object,
    orders_df: pd.DataFrame,
    account_df: pd.DataFrame,
) -> dict[str, float | int]:
    """Pull normalized metrics from NautilusTrader's result dicts.

    Nautilus exposes two relevant dicts:
    - ``stats_returns`` (flat): keys like "Sharpe Ratio (252 days)", "Sortino Ratio (252 days)"
    - ``stats_pnls`` (nested by currency): {"USD": {"PnL% (total)": ..., "Win Rate": ...}}
    """
    import math

    def _nan_safe(v: float) -> float:
        return 0.0 if not math.isfinite(v) else v

    returns_stats = getattr(raw_result, "stats_returns", None) or {}
    pnls_stats = getattr(raw_result, "stats_pnls", None) or {}

    sharpe = _find_stat(returns_stats, ["sharpe ratio", "sharpe"])
    sortino = _find_stat(returns_stats, ["sortino ratio", "sortino"])
    max_drawdown = _find_stat(returns_stats, ["max drawdown", "maximum drawdown"])

    currency_stats: dict[str, object] = {}
    if isinstance(pnls_stats, dict) and pnls_stats:
        first = next(iter(pnls_stats.values()))
        if isinstance(first, dict):
            currency_stats = first

    total_return = _find_stat(currency_stats, ["pnl% (total)", "pnl%", "return"])
    win_rate = _find_stat(currency_stats, ["win rate"])
    derived = _derive_metrics_from_account(account_df)

    if abs(max_drawdown) <= 1e-12 and derived is not None:
        max_drawdown = derived["max_drawdown"]
    if abs(total_return) <= 1e-12 and derived is not None:
        total_return = derived["total_return"]

    return {
        "sharpe": _nan_safe(sharpe),
        "sortino": _nan_safe(sortino),
        "max_drawdown": _nan_safe(max_drawdown),
        "total_return": _nan_safe(total_return),
        "win_rate": _nan_safe(win_rate),
        "num_trades": int(len(orders_df)),
    }


def _derive_metrics_from_account(account_df: pd.DataFrame) -> dict[str, float] | None:
    if account_df.empty or "returns" not in account_df.columns:
        return None
    frame = account_df.copy()
    timestamp_col = _first_present(
        frame.columns,
        ("timestamp", "ts_last", "ts_event", "ts_init", "datetime", "date"),
    )
    if timestamp_col is None:
        return None
    frame[timestamp_col] = pd.to_datetime(frame[timestamp_col], utc=True, errors="coerce")
    frame = frame.dropna(subset=[timestamp_col]).sort_values(timestamp_col)
    if frame.empty:
        return None
    returns = pd.Series(
        pd.to_numeric(frame["returns"], errors="coerce").fillna(0.0).values,
        index=pd.DatetimeIndex(frame[timestamp_col]),
    )
    derived = compute_series_metrics(returns)
    return {
        "max_drawdown": float(derived.max_drawdown),
        "total_return": float(derived.total_return),
    }


def _find_stat(stats: dict[str, object] | object, prefixes: list[str]) -> float:
    """Case-insensitive prefix lookup for Nautilus stats keys."""
    if not isinstance(stats, dict):
        return 0.0
    for key, value in stats.items():
        key_lower = str(key).lower()
        for prefix in prefixes:
            if key_lower.startswith(prefix):
                try:
                    return float(value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    return 0.0
    return 0.0


def _zero_metrics() -> dict[str, float | int]:
    return {
        "sharpe": 0.0,
        "sortino": 0.0,
        "max_drawdown": 0.0,
        "total_return": 0.0,
        "win_rate": 0.0,
        "num_trades": 0,
    }


def _compact_account_report(account_df: pd.DataFrame) -> pd.DataFrame:
    if account_df.empty:
        return pd.DataFrame(columns=["timestamp", "returns", "equity"])

    frame = account_df.copy()
    if isinstance(frame.index, pd.DatetimeIndex):
        frame = frame.reset_index().rename(columns={frame.index.name or "index": "timestamp"})
    timestamp_col = _first_present(
        frame.columns,
        ("timestamp", "ts_last", "ts_event", "ts_init", "datetime", "date"),
    )
    if timestamp_col is None:
        return frame

    frame[timestamp_col] = pd.to_datetime(frame[timestamp_col], utc=True, errors="coerce")
    frame = frame.dropna(subset=[timestamp_col]).sort_values(timestamp_col)
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "returns", "equity"])

    equity_col = _first_present(
        frame.columns,
        ("equity", "equity_total", "balance_total", "total", "balance", "net_liquidation"),
    )
    returns_col = _first_present(frame.columns, ("returns", "return", "pnl_pct", "pnl_percent"))

    if equity_col is not None:
        frame[equity_col] = pd.to_numeric(frame[equity_col], errors="coerce")
        frame = frame.dropna(subset=[equity_col])
        if frame.empty:
            return pd.DataFrame(columns=["timestamp", "returns", "equity"])
        account_id_col = _first_present(frame.columns, ("account_id",))
        if account_id_col is not None:
            deduped = (
                frame.groupby([timestamp_col, account_id_col], as_index=False)[equity_col]
                .last()
            )
            intraday_equity = (
                deduped.groupby(timestamp_col, as_index=True)[equity_col].sum().sort_index()
            )
        else:
            intraday_equity = (
                frame.groupby(timestamp_col, as_index=True)[equity_col].last().sort_index()
            )
        grouped = intraday_equity.groupby(intraday_equity.index.normalize(), as_index=True).last()
        compact = pd.DataFrame(
            {
                "timestamp": grouped.index,
                "equity": grouped.values,
                "returns": grouped.pct_change().fillna(0.0).values,
            }
        )
        return compact

    if returns_col is not None:
        grouped_returns = (
            frame.groupby(frame[timestamp_col].dt.normalize(), as_index=True)[returns_col]
            .apply(lambda values: (1.0 + pd.to_numeric(values, errors="coerce").fillna(0.0)).prod() - 1.0)
            .sort_index()
        )
        return pd.DataFrame(
            {
                "timestamp": grouped_returns.index,
                "returns": grouped_returns.values,
            }
        )

    return frame


def _first_present(columns: pd.Index, names: tuple[str, ...]) -> str | None:
    lowered = {str(column).lower(): str(column) for column in columns}
    for name in names:
        match = lowered.get(name.lower())
        if match is not None:
            return match
    return None
