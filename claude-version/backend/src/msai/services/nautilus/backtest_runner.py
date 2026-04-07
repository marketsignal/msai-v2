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

The subprocess contract is deliberately narrow: the parent hands over a
:class:`_RunPayload` with primitive types, and the child returns a plain
``dict`` over an ``mp.Queue``.  This keeps the pickle surface tiny and
avoids serialising Nautilus objects across process boundaries.
"""

from __future__ import annotations

import multiprocessing as mp
import queue as queue_mod
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths

if TYPE_CHECKING:
    from pathlib import Path

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
    from nautilus_trader.model.identifiers import Venue
    from nautilus_trader.trading.config import ImportableStrategyConfig

    _NAUTILUS_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - environment-specific
    _NAUTILUS_IMPORT_ERROR = exc


# Canonical simulated venue -- MUST match the name used when resolving
# instruments in ``msai.services.nautilus.instruments``.  If these diverge
# NautilusTrader will refuse to run with
# ``Venue '<X>' does not have a BacktestVenueConfig``.
_SIM_VENUE_NAME = "SIM"
_DEFAULT_STARTING_BALANCE = "1000000 USD"


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
    """

    strategy_file: str
    strategy_config: dict[str, Any]
    instrument_ids: list[str]
    start_date: str
    end_date: str
    catalog_path: str


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

        # ``spawn`` is mandatory -- ``fork`` would inherit the parent's
        # Rust/Cython state from any earlier Nautilus imports and crash.
        ctx = mp.get_context("spawn")
        result_queue: Any = ctx.Queue()
        process = ctx.Process(
            target=_run_in_subprocess, args=(payload, result_queue)
        )
        process.start()
        process.join(timeout_seconds)

        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            raise TimeoutError(
                f"Backtest subprocess exceeded timeout of {timeout_seconds}s"
            )

        try:
            raw = cast("dict[str, Any]", result_queue.get(timeout=2))
        except queue_mod.Empty as exc:
            raise RuntimeError("Backtest subprocess exited without a result") from exc

        if not bool(raw.get("ok")):
            raise RuntimeError(str(raw.get("error", "Unknown backtest failure")))

        return BacktestResult(
            orders_df=pd.DataFrame(raw.get("orders", [])),
            positions_df=pd.DataFrame(raw.get("positions", [])),
            account_df=pd.DataFrame(raw.get("account", [])),
            metrics=cast("dict[str, float | int]", raw.get("metrics", _zero_metrics())),
        )


# ---------------------------------------------------------------------------
# Subprocess entry point (must be a module-level function for pickling)
# ---------------------------------------------------------------------------


def _run_in_subprocess(payload: _RunPayload, result_queue: Any) -> None:
    """Execute the backtest inside the spawned child process.

    This function runs in a completely fresh Python interpreter, so it
    has to re-import ``nautilus_trader`` itself.  Any error -- import
    failure, engine crash, user strategy exception -- is caught and
    packaged into the ``result_queue`` so the parent can raise a
    meaningful ``RuntimeError``.
    """
    if _NAUTILUS_IMPORT_ERROR is not None:
        result_queue.put(
            {
                "ok": False,
                "error": f"NautilusTrader import failed: {_NAUTILUS_IMPORT_ERROR}",
            }
        )
        return

    try:
        run_config = _build_backtest_run_config(payload)
        node = BacktestNode([run_config])
        try:
            results = node.run()

            # No results at all -- Nautilus treated the window as empty.
            if not results:
                result_queue.put(
                    {
                        "ok": True,
                        "orders": [],
                        "positions": [],
                        "account": [],
                        "metrics": _zero_metrics(),
                    }
                )
                return

            primary = results[0]
            run_config_id = getattr(primary, "run_config_id", None)
            engine = node.get_engine(run_config_id) if run_config_id else None

            if engine is None:
                # Backtest completed but we can't fish out the engine --
                # return the stats we have without trade-level detail.
                result_queue.put(
                    {
                        "ok": True,
                        "orders": [],
                        "positions": [],
                        "account": [],
                        "metrics": _extract_metrics(primary, pd.DataFrame()),
                    }
                )
                return

            # The venue kwarg is REQUIRED -- calling
            # ``generate_account_report()`` without it raises on current
            # Nautilus versions.  This cost us an entire debugging session
            # before; do not remove the ``venue=`` argument.
            orders_df = engine.trader.generate_orders_report()
            positions_df = engine.trader.generate_positions_report()
            account_df = engine.trader.generate_account_report(
                venue=Venue(_SIM_VENUE_NAME)
            )

            result_queue.put(
                {
                    "ok": True,
                    "orders": orders_df.to_dict(orient="records"),
                    "positions": positions_df.to_dict(orient="records"),
                    "account": account_df.to_dict(orient="records"),
                    "metrics": _extract_metrics(primary, orders_df),
                }
            )
        finally:
            # ``dispose`` is not in Nautilus's public type stubs so we
            # cast to ``Any`` to keep mypy happy.
            cast("Any", node).dispose()
    except Exception:
        result_queue.put({"ok": False, "error": traceback.format_exc()})


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

    venue_config = BacktestVenueConfig(
        name=_SIM_VENUE_NAME,
        oms_type="NETTING",
        account_type="MARGIN",
        starting_balances=[_DEFAULT_STARTING_BALANCE],
        base_currency="USD",
    )

    data_config = BacktestDataConfig(
        catalog_path=payload.catalog_path,
        data_cls="nautilus_trader.model.data:Bar",
        instrument_ids=payload.instrument_ids,
        start_time=payload.start_date,
        end_time=payload.end_date,
    )

    return BacktestRunConfig(
        venues=[venue_config],
        data=[data_config],
        engine=engine_config,
        start=payload.start_date,
        end=payload.end_date,
        raise_exception=True,
        dispose_on_completion=False,
    )


# ---------------------------------------------------------------------------
# Metrics extraction
# ---------------------------------------------------------------------------


def _extract_metrics(primary_result: object, orders_df: pd.DataFrame) -> dict[str, float | int]:
    """Pull a normalised metrics dict out of the Nautilus result object.

    NautilusTrader exposes two relevant stat dicts with human-readable keys:

    * ``stats_returns`` -- flat dict: ``"Sharpe Ratio (252 days)"``,
      ``"Sortino Ratio (252 days)"``, ``"Profit Factor"``, etc.
    * ``stats_pnls`` -- nested dict keyed by currency:
      ``{"USD": {"PnL% (total)": 0.14, "Win Rate": 0.38, ...}}``

    We look up each metric by a small list of candidate key prefixes so
    minor version changes ("Sharpe Ratio (252 days)" vs "Sharpe Ratio")
    don't silently zero-out the dashboard.
    """
    stats_returns = getattr(primary_result, "stats_returns", None) or {}
    stats_pnls = getattr(primary_result, "stats_pnls", None) or {}

    # Flat returns stats — sharpe / sortino / drawdown
    sharpe = _find_float(stats_returns, ["sharpe ratio", "sharpe"])
    sortino = _find_float(stats_returns, ["sortino ratio", "sortino"])
    max_drawdown = _find_float(stats_returns, ["max drawdown", "maximum drawdown"])

    # PnL stats live under a currency key (usually "USD"). Pick the first one.
    currency_stats: dict[str, object] = {}
    if isinstance(stats_pnls, dict) and stats_pnls:
        first = next(iter(stats_pnls.values()))
        if isinstance(first, dict):
            currency_stats = first

    # "PnL% (total)" is the fraction return as a percentage (e.g. 0.138 == 0.138%).
    # Nautilus returns it as an absolute percent — leave it as-is.
    total_return = _find_float(currency_stats, ["pnl% (total)", "pnl%", "return"])
    win_rate = _find_float(currency_stats, ["win rate"])

    return {
        "sharpe_ratio": _nan_safe(sharpe),
        "sortino_ratio": _nan_safe(sortino),
        "max_drawdown": _nan_safe(max_drawdown),
        "total_return": _nan_safe(total_return),
        "win_rate": _nan_safe(win_rate),
        "num_trades": int(len(orders_df)),
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


# Silence "imported but unused" warning for ``field`` -- reserved for
# possible future payload extensions.
_ = field
