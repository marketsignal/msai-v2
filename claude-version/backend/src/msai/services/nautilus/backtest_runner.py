"""NautilusTrader-backed backtest runner.

Runs MSAI backtests end-to-end on NautilusTrader's :class:`BacktestNode` --
no hand-rolled Python loops.  The runner lives in the FastAPI / arq worker
process but delegates the actual engine execution to a spawned
subprocess, because:

* NautilusTrader maintains global Rust / Cython state per process and only
  supports **one** ``BacktestEngine`` per process.  Running two backtests
  back-to-back in the same process will poison the state and crash on the
  second run.
* Spawning ensures a clean ``sys.modules`` / event-loop for every run.
* The arq worker process can host many **sequential** runs because each
  one gets its own child process.

IPC uses **file-based pickle** (write result to a tempfile, parent reads
after ``process.join()``) instead of ``multiprocessing.Queue`` because
Queue silently fails when the subprocess writes large DataFrames that
exceed the OS pipe buffer.  The tempfile approach is more robust and
avoids pipe deadlocks.
"""

from __future__ import annotations

import multiprocessing as mp
import pickle
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from msai.services.analytics_math import compute_series_metrics
from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths

if TYPE_CHECKING:
    pass

# NautilusTrader is heavy (pulls in Rust extensions).  We import eagerly
# because this module is only imported inside the backtest worker, which
# always needs Nautilus.  Any import error is captured so the subprocess
# can report it cleanly instead of crashing opaquely.
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
except Exception as exc:  # pragma: no cover - environment-specific
    _NAUTILUS_IMPORT_ERROR = exc


# Phase 2 task 2.9: the backtest runner no longer hard-codes ``SIM``.
# Instead it derives the per-backtest venue list from the canonical
# instrument IDs in the payload (each ``AAPL.NASDAQ``-shape id carries
# its venue suffix). A backtest spanning multiple venues gets one
# ``BacktestVenueConfig`` per unique venue, which matches how
# Nautilus's ``BacktestNode`` wires the engine.
_DEFAULT_STARTING_BALANCE = "1000000 USD"


def _extract_venues_from_instrument_ids(instrument_ids: list[str]) -> list[str]:
    """Derive the unique, deterministically ordered list of venue
    suffixes from a list of canonical Nautilus instrument ids.

    ``"AAPL.NASDAQ"`` -> venue ``"NASDAQ"``. For option ids the
    venue suffix is everything after the FINAL ``.`` (Nautilus's
    simplified symbology puts the venue last --
    ``"C AAPL 20260515 150.SMART"``). A single backtest spanning
    multiple venues (e.g. ``["AAPL.NASDAQ", "ESM5.XCME"]``) returns
    both names so the runner can build one ``BacktestVenueConfig``
    per unique venue.

    Raises ``ValueError`` when ``instrument_ids`` is empty OR any
    id has no venue suffix -- both are programming errors that
    would otherwise surface as an opaque Nautilus runtime crash.
    """
    if not instrument_ids:
        raise ValueError("backtest payload must contain at least one instrument id")
    seen: list[str] = []
    seen_set: set[str] = set()
    for instrument_id in instrument_ids:
        if "." not in instrument_id:
            raise ValueError(
                f"instrument_id {instrument_id!r} has no venue suffix -- "
                "migrate to canonical IDs via SecurityMaster "
                "(Phase 2 task 2.6)",
            )
        venue = instrument_id.rsplit(".", 1)[-1]
        if venue not in seen_set:
            seen_set.add(venue)
            seen.append(venue)
    return seen


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BacktestResult:
    """Structured result handed back to the arq worker.

    Everything here is already materialised as plain pandas / primitive
    objects -- no Nautilus objects leak out of the runner, which would
    not survive the subprocess boundary anyway.
    """

    orders_df: pd.DataFrame
    positions_df: pd.DataFrame
    account_df: pd.DataFrame
    metrics: dict[str, float | int]


# ---------------------------------------------------------------------------
# Internal payloads (kept at module level so they pickle for spawn)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RunPayload:
    """Pickle-friendly bundle sent from parent to child process.

    Attributes:
        strategy_file: Absolute path to the strategy ``.py`` file on disk.
        strategy_config: The config kwargs that will be passed through to
            the Nautilus ``StrategyConfig`` constructor.
        instrument_ids: List of canonical Nautilus instrument IDs the
            backtest should load from the catalog.
        start_date: ISO-8601 start of the backtest window (inclusive).
        end_date: ISO-8601 end of the backtest window (inclusive).
        catalog_path: Filesystem path to the Nautilus ``ParquetDataCatalog``.
        result_path: Tempfile path where the subprocess writes its pickle
            result.  Set by the parent before spawning.
    """

    strategy_file: str
    strategy_config: dict[str, Any]
    instrument_ids: list[str]
    start_date: str
    end_date: str
    catalog_path: str
    result_path: str = ""


# ---------------------------------------------------------------------------
# Runner (parent-side)
# ---------------------------------------------------------------------------


class BacktestRunner:
    """Drive a single backtest run via a spawned ``BacktestNode`` subprocess.

    A new instance is cheap -- just construct it, call :meth:`run`, and
    throw it away.  The class is stateless; all run-specific data lives in
    the :class:`_RunPayload` passed to the subprocess.
    """

    def run(
        self,
        strategy_file: str,
        strategy_config: dict[str, Any],
        instrument_ids: list[str],
        start_date: str,
        end_date: str,
        catalog_path: Path,
        *,
        timeout_seconds: int = 30 * 60,
    ) -> BacktestResult:
        """Execute a backtest and return a :class:`BacktestResult`.

        Args:
            strategy_file: Absolute path to the strategy source file.
            strategy_config: Kwargs for the Nautilus ``StrategyConfig`` --
                must contain ``instrument_id``, ``bar_type``, and any
                user-editable knobs (EMA periods, trade size, ...).
            instrument_ids: Canonical Nautilus instrument IDs the data
                config should load from the catalog.
            start_date: ISO-8601 start of the backtest window.
            end_date: ISO-8601 end of the backtest window.
            catalog_path: Path to the Nautilus ``ParquetDataCatalog`` to
                read bar data from.
            timeout_seconds: Maximum wall-clock time before the subprocess
                is killed.  Defaults to 30 minutes.

        Returns:
            A :class:`BacktestResult` with orders, positions, account
            snapshots and extracted metrics.

        Raises:
            TimeoutError: The subprocess did not finish before
                ``timeout_seconds`` elapsed.
            RuntimeError: The subprocess exited without delivering a
                result, or it reported an error during execution.
        """
        payload = _RunPayload(
            strategy_file=strategy_file,
            strategy_config=strategy_config,
            instrument_ids=instrument_ids,
            start_date=start_date,
            end_date=end_date,
            catalog_path=str(catalog_path),
        )

        # Create a tempfile for the subprocess to write its result into.
        with tempfile.NamedTemporaryFile(
            prefix="msai-backtest-", suffix=".pkl", delete=False,
        ) as tmp:
            result_path = Path(tmp.name)
        payload.result_path = str(result_path)

        # ``spawn`` is mandatory -- ``fork`` would inherit the parent's
        # Rust/Cython state from any earlier Nautilus imports and crash.
        ctx = mp.get_context("spawn")
        process = ctx.Process(target=_run_in_subprocess, args=(payload,))
        try:
            process.start()
            process.join(timeout_seconds)

            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                raise TimeoutError(f"Backtest subprocess exceeded timeout of {timeout_seconds}s")

            if not result_path.exists():
                raise RuntimeError("Backtest subprocess exited without a result")

            with result_path.open("rb") as handle:
                raw = cast("dict[str, Any]", pickle.load(handle))

            if not bool(raw.get("ok")):
                raise RuntimeError(str(raw.get("error", "Unknown backtest failure")))

            return BacktestResult(
                orders_df=pd.DataFrame(raw.get("orders", [])),
                positions_df=pd.DataFrame(raw.get("positions", [])),
                account_df=pd.DataFrame(raw.get("account", [])),
                metrics=cast("dict[str, float | int]", raw.get("metrics", _zero_metrics())),
            )
        finally:
            if result_path.exists():
                result_path.unlink()
            if hasattr(process, "close"):
                process.close()


# ---------------------------------------------------------------------------
# Subprocess entry point (must be a module-level function for pickling)
# ---------------------------------------------------------------------------


def _run_in_subprocess(payload: _RunPayload) -> None:
    """Execute the backtest inside the spawned child process.

    This function runs in a completely fresh Python interpreter, so it
    has to re-import ``nautilus_trader`` itself.  Any error -- import
    failure, engine crash, user strategy exception -- is caught and
    packaged into the result pickle so the parent can raise a
    meaningful ``RuntimeError``.
    """
    if _NAUTILUS_IMPORT_ERROR is not None:
        _write_subprocess_result(
            payload.result_path,
            {
                "ok": False,
                "error": f"NautilusTrader import failed: {_NAUTILUS_IMPORT_ERROR}",
            },
        )
        return

    try:
        run_config = _build_backtest_run_config(payload)
        node = BacktestNode([run_config])
        try:
            results = node.run()

            # No results at all -- Nautilus treated the window as empty.
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

            primary = results[0]
            run_config_id = getattr(primary, "run_config_id", None)
            engine = node.get_engine(run_config_id) if run_config_id else None

            if engine is None:
                # Backtest completed but we can't fish out the engine --
                # return the stats we have without trade-level detail.
                _write_subprocess_result(
                    payload.result_path,
                    {
                        "ok": True,
                        "orders": [],
                        "positions": [],
                        "account": [],
                        "metrics": _extract_metrics(primary, pd.DataFrame(), pd.DataFrame()),
                    },
                )
                return

            # The venue kwarg is REQUIRED on ``generate_account_report()``
            # (gotcha #2). Phase 2 task 2.9: derive per-venue account
            # reports and concatenate them for multi-venue backtests.
            orders_df = engine.trader.generate_orders_report()
            positions_df = engine.trader.generate_positions_report()
            venue_names = _extract_venues_from_instrument_ids(payload.instrument_ids)
            account_frames = [
                engine.trader.generate_account_report(venue=Venue(v))
                for v in venue_names
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
                    "metrics": _extract_metrics(primary, orders_df, account_payload),
                },
            )
        finally:
            # ``dispose`` is not in Nautilus's public type stubs so we
            # cast to ``Any`` to keep mypy happy.
            cast("Any", node).dispose()
    except Exception:
        _write_subprocess_result(
            payload.result_path,
            {"ok": False, "error": traceback.format_exc()},
        )


def _write_subprocess_result(result_path: str, payload: dict[str, Any]) -> None:
    """Write the subprocess result to a pickle file at ``result_path``."""
    with Path(result_path).open("wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)


# ---------------------------------------------------------------------------
# Config builder (extracted so unit tests can exercise it without spawning)
# ---------------------------------------------------------------------------


def _build_backtest_run_config(payload: _RunPayload) -> BacktestRunConfig:
    """Translate a :class:`_RunPayload` into a Nautilus ``BacktestRunConfig``.

    Kept as a module-level function (rather than a private method on
    :class:`BacktestRunner`) so unit tests can call it directly without
    needing to spin up a subprocess.
    """
    paths = resolve_importable_strategy_paths(payload.strategy_file)

    strategy_config = ImportableStrategyConfig(
        strategy_path=paths.strategy_path,
        config_path=paths.config_path,
        config=payload.strategy_config,
    )
    engine_config = BacktestEngineConfig(strategies=[strategy_config])

    # Phase 2 task 2.9: one BacktestVenueConfig per unique venue
    # in the instruments list. A single-venue equity backtest
    # produces one config; a multi-venue (e.g. equities + futures)
    # backtest produces one per venue. If any venue is missing a
    # config Nautilus refuses to run with
    # ``Venue '<X>' does not have a BacktestVenueConfig`` -- gotcha
    # #4 in the Nautilus reference.
    venue_names = _extract_venues_from_instrument_ids(payload.instrument_ids)
    venue_configs = [
        BacktestVenueConfig(
            name=venue_name,
            oms_type="NETTING",
            account_type="MARGIN",
            starting_balances=[_DEFAULT_STARTING_BALANCE],
            base_currency="USD",
        )
        for venue_name in venue_names
    ]

    data_config = BacktestDataConfig(
        catalog_path=payload.catalog_path,
        data_cls="nautilus_trader.model.data:Bar",
        instrument_ids=payload.instrument_ids,
        start_time=payload.start_date,
        end_time=payload.end_date,
    )

    return BacktestRunConfig(
        venues=venue_configs,
        data=[data_config],
        engine=engine_config,
        start=payload.start_date,
        end=payload.end_date,
        raise_exception=True,
        dispose_on_completion=False,
    )


# ---------------------------------------------------------------------------
# Account report compaction
# ---------------------------------------------------------------------------


def _compact_account_report(account_df: pd.DataFrame) -> pd.DataFrame:
    """Reduce the raw Nautilus account report to a daily equity/returns series.

    The raw report can have thousands of intraday rows.  We compact it to
    one row per day with ``timestamp``, ``equity``, and ``returns`` columns
    so that downstream analytics and the QuantStats tearsheet generator
    get a clean daily time series.
    """
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
    """Return the first column name from ``names`` that exists in ``columns``."""
    lowered = {str(column).lower(): str(column) for column in columns}
    for name in names:
        match = lowered.get(name.lower())
        if match is not None:
            return match
    return None


# ---------------------------------------------------------------------------
# Metrics extraction
# ---------------------------------------------------------------------------


def _extract_metrics(
    primary_result: object,
    orders_df: pd.DataFrame,
    account_df: pd.DataFrame | None = None,
) -> dict[str, float | int]:
    """Pull a normalised metrics dict out of the Nautilus result object.

    NautilusTrader exposes two relevant stat dicts with human-readable keys:

    * ``stats_returns`` -- flat dict: ``"Sharpe Ratio (252 days)"``,
      ``"Sortino Ratio (252 days)"``, ``"Profit Factor"``, etc.
    * ``stats_pnls`` -- nested dict keyed by currency:
      ``{"USD": {"PnL% (total)": 0.14, "Win Rate": 0.38, ...}}``

    We look up each metric by a small list of candidate key prefixes so
    minor version changes ("Sharpe Ratio (252 days)" vs "Sharpe Ratio")
    don't silently zero-out the dashboard.

    When Nautilus's built-in stats produce zeros (e.g. for short backtests),
    we fall back to deriving metrics from the compacted account report
    using ``compute_series_metrics``.
    """
    stats_returns = getattr(primary_result, "stats_returns", None) or {}
    stats_pnls = getattr(primary_result, "stats_pnls", None) or {}

    # Flat returns stats -- sharpe / sortino / drawdown
    sharpe = _find_float(stats_returns, ["sharpe ratio", "sharpe"])
    sortino = _find_float(stats_returns, ["sortino ratio", "sortino"])
    max_drawdown = _find_float(stats_returns, ["max drawdown", "maximum drawdown"])

    # PnL stats live under a currency key (usually "USD"). Pick the first one.
    currency_stats: dict[str, object] = {}
    if isinstance(stats_pnls, dict) and stats_pnls:
        first = next(iter(stats_pnls.values()))
        if isinstance(first, dict):
            currency_stats = first

    total_return = _find_float(currency_stats, ["pnl% (total)", "pnl%", "return"])
    win_rate = _find_float(currency_stats, ["win rate"])

    # Fall back to account-derived metrics when Nautilus stats are zero.
    if account_df is not None:
        derived = _derive_metrics_from_account(account_df)
        if derived is not None:
            if abs(max_drawdown) <= 1e-12:
                max_drawdown = derived["max_drawdown"]
            if abs(total_return) <= 1e-12:
                total_return = derived["total_return"]

    return {
        "sharpe_ratio": _nan_safe(sharpe),
        "sortino_ratio": _nan_safe(sortino),
        "max_drawdown": _nan_safe(max_drawdown),
        "total_return": _nan_safe(total_return),
        "win_rate": _nan_safe(win_rate),
        "num_trades": int(len(orders_df)),
    }


def _derive_metrics_from_account(account_df: pd.DataFrame) -> dict[str, float] | None:
    """Derive total_return and max_drawdown from a compacted account report.

    Uses :func:`compute_series_metrics` from ``analytics_math`` so the
    calculation is consistent with any other place we compute metrics
    from a returns series.
    """
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


def _find_float(stats: dict[str, object] | object, prefixes: list[str]) -> float:
    """Look up a float metric by case-insensitive key prefix match."""
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


def _nan_safe(value: float) -> float:
    """Replace NaN/Inf with 0.0 so the metrics JSON is safe to persist."""
    import math

    if not math.isfinite(value):
        return 0.0
    return value


def _zero_metrics() -> dict[str, float | int]:
    """Return a fresh zero-valued metrics dict.

    Used when a backtest completes successfully but produced zero bars
    or zero trades -- callers should still see every expected metric key.
    """
    return {
        "sharpe_ratio": 0.0,
        "sortino_ratio": 0.0,
        "max_drawdown": 0.0,
        "total_return": 0.0,
        "win_rate": 0.0,
        "num_trades": 0,
    }
