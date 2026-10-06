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

import importlib
import inspect
import math
import multiprocessing as mp
import pickle
import re
import tempfile
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

from msai.services.analytics_math import compute_series_metrics, normalize_daily_returns
from msai.services.nautilus.schema_hooks import validate_strategy_config
from msai.services.nautilus.strategy_loader import resolve_importable_strategy_paths

# NautilusTrader is heavy (pulls in Rust extensions).  We import eagerly
# because this module is only imported inside the backtest worker, which
# always needs Nautilus.  Any import error is captured so the subprocess
# can report it cleanly instead of crashing opaquely.
try:
    from nautilus_trader.backtest import BacktestNode
    from nautilus_trader.config import (
        BacktestDataConfig,
        BacktestEngineConfig,
        BacktestRunConfig,
        BacktestVenueConfig,
        ImportableStrategyConfig,
    )
    from nautilus_trader.execution import DefaultFillModel, FixedFeeModel
    from nautilus_trader.model import (
        BarType,
        Currency,
        InstrumentId,
        Money,
        NautilusDataType,
        Venue,
    )

    _NAUTILUS_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - environment-specific
    _NAUTILUS_IMPORT_ERROR = exc


# Phase 2 task 2.9: the backtest runner no longer hard-codes ``SIM``.
# Instead it derives the per-backtest venue list from the canonical
# instrument IDs in the payload (each ``AAPL.NASDAQ``-shape id carries
# its venue suffix). A backtest spanning multiple venues gets one
# ``BacktestVenueConfig`` per unique venue, which matches how
# Nautilus's ``BacktestNode`` wires the engine.
_CALENDAR_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DAY_NANOSECONDS = 86_400_000_000_000


def _extract_venues_from_instrument_ids(instrument_ids: list[str]) -> list[str]:
    """Derive the unique, deterministically ordered list of venue
    suffixes from a list of canonical Nautilus instrument ids.

    ``"AAPL.NASDAQ"`` -> venue ``"NASDAQ"``. For option ids the
    venue suffix is everything after the FINAL ``.`` (Nautilus's
    simplified symbology puts the venue last --
    ``"C AAPL 20260515 150.SMART"``). A single backtest spanning
    multiple venues (e.g. ``["AAPL.NASDAQ", "ESM5.CME"]``) returns
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
    fills_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    accounting: dict[str, Any] | None = None


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
        start_date: Inclusive UTC calendar date, or an exact ISO-8601 instant.
        end_date: Inclusive UTC calendar date, or an exact ISO-8601 instant.
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
    initial_capital: float = 1_000_000.0
    commission_per_fill: float = 0.0


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
        initial_capital: float = 1_000_000.0,
        commission_per_fill: float = 0.0,
    ) -> BacktestResult:
        """Execute a backtest and return a :class:`BacktestResult`.

        Args:
            strategy_file: Absolute path to the strategy source file.
            strategy_config: Kwargs for the Nautilus ``StrategyConfig`` --
                must contain ``instrument_id``, ``bar_type``, and any
                user-editable knobs (EMA periods, trade size, ...).
            instrument_ids: Canonical Nautilus instrument IDs the data
                config should load from the catalog.
            start_date: Inclusive UTC calendar date, or an exact ISO timestamp.
            end_date: Inclusive UTC calendar date, or an exact ISO timestamp.
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
            initial_capital=initial_capital,
            commission_per_fill=commission_per_fill,
        )

        # Create a tempfile for the subprocess to write its result into.
        with tempfile.NamedTemporaryFile(
            prefix="msai-backtest-",
            suffix=".pkl",
            delete=False,
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

            # An empty result file means the subprocess crashed before
            # the ``except Exception`` handler in ``_run_in_subprocess``
            # could write a pickle — typically a Rust-side panic from
            # Nautilus's identifier validators (e.g. an empty
            # ``order_id_tag`` producing ``StrategyId('SmokeStrategy-')``,
            # rejected by ``strategy_id.rs``).  ``pickle.load`` on an
            # empty file raises ``EOFError: Ran out of input``, which
            # bubbles up as an opaque per-strategy attribution error —
            # surface the real shape here so the operator can find the
            # config field that triggered the panic.
            if result_path.stat().st_size == 0:
                exit_code = getattr(process, "exitcode", None)
                raise RuntimeError(
                    "Backtest subprocess crashed without writing a result "
                    f"(exit_code={exit_code}); the Python-level error handler "
                    "did not fire — this is typically a Rust-side panic from "
                    "Nautilus (invalid identifier, empty order_id_tag, etc.). "
                    "Check the worker logs for a 'thread panicked' line."
                )

            with result_path.open("rb") as handle:
                raw = cast("dict[str, Any]", pickle.load(handle))

            if not bool(raw.get("ok")):
                raise RuntimeError(str(raw.get("error", "Unknown backtest failure")))

            if (
                not isinstance(raw.get("metrics"), dict)
                or not isinstance(raw.get("accounting"), dict)
                or not raw.get("account")
                or any(key not in raw for key in ("orders", "fills", "positions"))
            ):
                raise RuntimeError("Incomplete native backtest result or reports")

            return BacktestResult(
                orders_df=pd.DataFrame(raw.get("orders", [])),
                fills_df=pd.DataFrame(raw.get("fills", [])),
                accounting=raw.get("accounting"),
                positions_df=pd.DataFrame(raw.get("positions", [])),
                account_df=pd.DataFrame(raw.get("account", [])),
                metrics=cast("dict[str, float | int]", raw["metrics"]),
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
        with _capture_strategy_callback_failures(payload.strategy_file) as callback_errors:
            run_config = _build_backtest_run_config(payload)
            node = BacktestNode([run_config])
            try:
                node.build()
                node.add_strategy_from_config(run_config.id, _build_strategy_config(payload))
                results = node.run()
                if callback_errors:
                    raise RuntimeError(callback_errors[0])

                # No results at all -- Nautilus treated the window as empty.
                if len(results) != 1:
                    raise RuntimeError("Native backtest did not produce exactly one result")

                primary = results[0]
                run_config_id = run_config.id
                if int(primary.iterations) == 0:
                    raise RuntimeError("Native backtest processed no bars for the requested window")

                # The venue kwarg is REQUIRED on ``generate_account_report()``
                # (gotcha #2). Phase 2 task 2.9: derive per-venue account
                # reports and concatenate them for multi-venue backtests.
                orders_df = _normalize_native_report(node.generate_orders_report(run_config_id))
                fills_df = _normalize_native_report(node.generate_fills_report(run_config_id))
                if not fills_df.empty:
                    fills_df = fills_df.sort_values("ts_event", kind="stable")
                positions_df = _normalize_native_report(
                    node.generate_positions_report(run_config_id)
                )
                venue_names = _extract_venues_from_instrument_ids(payload.instrument_ids)
                account_frames = []
                opening_balances: dict[str, float] = {}
                for venue_name, venue_config in zip(venue_names, run_config.venues, strict=True):
                    frame = node.generate_account_report(run_config_id, venue=Venue(venue_name))
                    if not isinstance(frame, pd.DataFrame):
                        raise RuntimeError(f"Missing native account report for {venue_name}")
                    if frame.empty or "account_id" not in frame:
                        raise ValueError(f"Missing opening account report for {venue_name}")
                    account_ids = frame["account_id"].dropna().astype(str).unique()
                    if len(account_ids) != 1 or account_ids[0] in opening_balances:
                        raise ValueError("Ambiguous account identity in backtest report")
                    balances = venue_config.starting_balances
                    if len(balances) != 1:
                        raise ValueError("Backtest accounting supports one USD balance per account")
                    amount, currency = str(balances[0]).split()
                    if currency != "USD":
                        raise ValueError("Backtest accounting supports USD only")
                    opening_balances[account_ids[0]] = float(amount)
                    account_frames.append(frame)
                account_df = pd.concat(account_frames) if account_frames else pd.DataFrame()
                account_payload = _compact_account_report(
                    account_df, opening_balances=opening_balances
                )
                accounting = {
                    "version": 1,
                    "basis": "realized_account_balance",
                    "initial_capital": sum(opening_balances.values()),
                    "currency": "USD",
                    "costs": "engine_recorded",
                    "engine_version": __import__("nautilus_trader").__version__,
                    "leverage": 1.0,
                    "fee_model": "FixedFeeModel",
                    "commission_per_fill": float(
                        Money(payload.commission_per_fill, Currency.from_str("USD")).as_decimal()
                    ),
                    "fill_model": "DefaultFillModel",
                    "fill_seed": 42,
                    "slippage_probability": 0.0,
                    "execution_assumptions": (
                        "L1 bar execution; LAST/MID bars synthesize equal bid/ask trade prices "
                        "with OHLC quarter-volume legs; exhausted displayed L1 size can fill "
                        "residual one tick worse independently of random slippage"
                    ),
                }

                success_payload = {
                    "ok": True,
                    "orders": orders_df.to_dict(orient="records"),
                    "fills": fills_df.to_dict(orient="records"),
                    "accounting": accounting,
                    "positions": positions_df.to_dict(orient="records"),
                    "account": account_payload.to_dict(orient="records"),
                    "metrics": _extract_metrics(primary, fills_df, account_payload, positions_df),
                }
            finally:
                # ``dispose`` is not in Nautilus's public type stubs so we
                # cast to ``Any`` to keep mypy happy.
                cast("Any", node).dispose()
            if callback_errors:
                raise RuntimeError(callback_errors[0])
            _write_subprocess_result(payload.result_path, success_payload)
    except Exception:
        _write_subprocess_result(
            payload.result_path,
            {"ok": False, "error": traceback.format_exc()},
        )


@contextmanager
def _capture_strategy_callback_failures(strategy_file: str) -> Iterator[list[str]]:
    """Attribute Python callback failures which RC6's native actor only logs.

    Runs only inside the fresh execution child. Native execution and its original
    exception logging remain intact; failed runs never publish result reports.
    """
    paths = resolve_importable_strategy_paths(strategy_file)
    module_name, class_name = paths.strategy_path.split(":", 1)
    strategy_cls = getattr(importlib.import_module(module_name), class_name)
    failures: list[str] = []
    callbacks = {
        name: callback
        for name, callback in inspect.getmembers(strategy_cls, inspect.isfunction)
        if name.startswith("on_")
    }
    own_names = set(strategy_cls.__dict__)

    def wrap(callback: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(callback)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                return callback(*args, **kwargs)
            except Exception:
                if not failures:
                    failures.append(
                        f"Strategy callback {callback.__name__} failed:\n{traceback.format_exc()}"
                    )
                raise

        return guarded

    try:
        for name, callback in callbacks.items():
            setattr(strategy_cls, name, wrap(callback))
        yield failures
    finally:
        for name, callback in callbacks.items():
            if name in own_names:
                setattr(strategy_cls, name, callback)
            else:
                delattr(strategy_cls, name)


def _write_subprocess_result(result_path: str, payload: dict[str, Any]) -> None:
    """Write the subprocess result to a pickle file at ``result_path``."""
    with Path(result_path).open("wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)


def _normalize_native_report(frame: object) -> pd.DataFrame:
    """Keep native report identities that may be carried by the pandas index."""
    if not isinstance(frame, pd.DataFrame):
        raise RuntimeError("Missing native backtest report")
    if frame.index.name is not None and frame.index.name not in frame.columns:
        return frame.reset_index()
    return frame


# ---------------------------------------------------------------------------
# Config builder (extracted so unit tests can exercise it without spawning)
# ---------------------------------------------------------------------------


def _normalize_backtest_window(start: str, end: str) -> tuple[int, int]:
    """Inclusive UTC calendar dates, or exact timestamp instants, as nanoseconds.

    Both Nautilus's catalog query and engine cutoff are inclusive on ts_init.
    The end of a calendar day is therefore next midnight minus one nanosecond.
    """
    start_is_date = _CALENDAR_DATE.fullmatch(start) is not None
    end_is_date = _CALENDAR_DATE.fullmatch(end) is not None
    if start_is_date and end_is_date and end < start:
        raise ValueError("End date must be on or after start date")

    bounds: list[int] = []
    for value, label, is_date in (
        (start, "start", start_is_date),
        (end, "end", end_is_date),
    ):
        if not is_date and re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ]", value) is None:
            raise ValueError(f"Invalid backtest {label}: use YYYY-MM-DD or an ISO timestamp")
        try:
            stamp = pd.Timestamp(value)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid backtest {label}: {value!r}") from exc
        try:
            stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
            nanos = int(stamp.value)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"Backtest {label} is outside the supported nanosecond range") from exc
        if is_date and label == "end":
            nanos += _DAY_NANOSECONDS - 1
        if not 0 <= nanos <= pd.Timestamp.max.value:
            raise ValueError(f"Backtest {label} is outside the supported nanosecond range")
        bounds.append(nanos)

    if bounds[1] < bounds[0]:
        raise ValueError("End date must be on or after start date")
    return bounds[0], bounds[1]


def _run_bar_type(instrument_id: str) -> BarType:
    """The complete native bar type emitted by this bounded minute-data run."""
    return BarType.from_str(f"{InstrumentId.from_str(instrument_id)}-1-MINUTE-LAST-EXTERNAL")


def _build_strategy_config(payload: _RunPayload) -> ImportableStrategyConfig:
    paths = resolve_importable_strategy_paths(payload.strategy_file)
    module_name, class_name = paths.config_path.split(":", 1)
    config_cls = getattr(importlib.import_module(module_name), class_name)
    if not payload.instrument_ids:
        raise ValueError("Backtest requires at least one instrument")
    instrument_id = InstrumentId.from_str(payload.instrument_ids[0])
    run_bar_type = _run_bar_type(payload.instrument_ids[0])
    prepared = dict(payload.strategy_config)
    # Research trials contain operator parameters only. Supply the identities
    # of this minute-bar run, preserving explicit values for validation/refusal.
    prepared.setdefault("instrument_id", str(instrument_id))
    prepared.setdefault("bar_type", str(run_bar_type))
    native_config = validate_strategy_config(config_cls, prepared)
    if str(native_config.instrument_id) != str(instrument_id):
        raise ValueError("Strategy instrument_id must match the backtest instrument")
    if BarType.from_str(str(native_config.bar_type)) != run_bar_type:
        raise ValueError(f"Strategy bar_type must match the backtest data: {run_bar_type}")
    normalized = {}
    for key in prepared:
        value = getattr(native_config, key)
        normalized[key] = (
            str(value) if isinstance(value, (Decimal, InstrumentId, BarType)) else value
        )
    return ImportableStrategyConfig(
        strategy_path=paths.strategy_path,
        config_path=paths.config_path,
        config=normalized,
    )


def _build_backtest_run_config(payload: _RunPayload) -> BacktestRunConfig:
    """Translate a :class:`_RunPayload` into a Nautilus ``BacktestRunConfig``.

    Kept as a module-level function (rather than a private method on
    :class:`BacktestRunner`) so unit tests can call it directly without
    needing to spin up a subprocess.
    """
    start_ns, end_ns = _normalize_backtest_window(payload.start_date, payload.end_date)
    _build_strategy_config(payload)
    engine_config = BacktestEngineConfig()
    if not math.isfinite(payload.initial_capital) or payload.initial_capital <= 0:
        raise ValueError("Initial capital must be positive and finite")
    if not math.isfinite(payload.commission_per_fill) or payload.commission_per_fill < 0:
        raise ValueError("Commission per fill must be nonnegative and finite")
    usd = Currency.from_str("USD")

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
            starting_balances=[f"{payload.initial_capital} USD"],
            base_currency=usd,
            default_leverage=Decimal(1),
            fee_model=FixedFeeModel(
                Money(payload.commission_per_fill, usd), charge_commission_once=False
            ),
            fill_model=DefaultFillModel(prob_fill_on_limit=1.0, prob_slippage=0.0, random_seed=42),
        )
        for venue_name in venue_names
    ]

    data_config = BacktestDataConfig(
        catalog_path=payload.catalog_path,
        data_type=NautilusDataType.Bar,
        instrument_ids=[InstrumentId.from_str(value) for value in payload.instrument_ids],
        bar_types=[str(_run_bar_type(value)) for value in payload.instrument_ids],
        start_time=start_ns,
        end_time=end_ns,
    )

    return BacktestRunConfig(
        venues=venue_configs,
        data=[data_config],
        engine=engine_config,
        start=start_ns,
        end=end_ns,
        raise_exception=True,
        dispose_on_completion=False,
    )


# ---------------------------------------------------------------------------
# Account report compaction
# ---------------------------------------------------------------------------


def _compact_account_report(
    account_df: pd.DataFrame, *, opening_balances: dict[str, float]
) -> pd.DataFrame:
    """Compact USD account balances, preserving every account's opening capital.

    Dates are observed UTC balance-report dates, not an exchange calendar.
    This is a realized balance series, not marked-to-market NAV.
    """
    if not opening_balances or any(
        not math.isfinite(value) or value <= 0 for value in opening_balances.values()
    ):
        raise ValueError("Missing or invalid opening account balances")
    if account_df.empty:
        raise ValueError("Missing account balance observations")
    frame = account_df.copy()
    if isinstance(frame.index, pd.DatetimeIndex):
        index_name = frame.index.name or "index"
        frame = frame.reset_index().rename(columns={index_name: "timestamp"})
    timestamp_col = _first_present(frame.columns, ("timestamp", "ts_event"))
    if timestamp_col is None or not {"account_id", "currency", "total"}.issubset(frame):
        raise ValueError("Account report lacks timestamp, account, currency or total")
    if frame["currency"].isna().any() or set(frame["currency"].astype(str)) != {"USD"}:
        raise ValueError("Backtest account balances must all be USD")
    if frame["account_id"].isna().any():
        raise ValueError("Missing account identity")
    frame["account_id"] = frame["account_id"].astype(str)
    if set(frame["account_id"]) != set(opening_balances):
        raise ValueError("Every reported account must have a known opening balance")
    frame[timestamp_col] = pd.to_datetime(frame[timestamp_col], utc=True, errors="coerce")
    frame["total"] = pd.to_numeric(frame["total"], errors="coerce")
    if frame[timestamp_col].isna().any() or not frame["total"].map(math.isfinite).all():
        raise ValueError("Invalid account timestamp or balance")
    frame = frame.sort_values(timestamp_col, kind="stable")
    # Each account is a state stream. Seed from configuration, then carry its
    # last state forward at the union of all event times before adding accounts.
    aligned = (
        frame.pivot_table(index=timestamp_col, columns="account_id", values="total", aggfunc="last")
        .sort_index()
        .ffill()
        .fillna(opening_balances)
    )
    balances = aligned.sum(axis=1)
    daily = balances.groupby(balances.index.normalize()).last()
    previous = daily.shift(1)
    previous.iloc[0] = sum(opening_balances.values())
    if (previous <= 0).any():
        raise ValueError("Account returns require a positive prior balance")
    return pd.DataFrame(
        {
            "timestamp": daily.index,
            "equity": daily.values,
            "returns": (daily / previous - 1.0).values,
        }
    )


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
    fills_df: pd.DataFrame,
    account_df: pd.DataFrame | None = None,
    positions_df: pd.DataFrame | None = None,
) -> dict[str, float | int]:
    """Use daily account balance returns for account-level performance.

    Native ``PnL%`` is percentage-valued; API returns are ratios. Native
    position-return Sharpe and position-notional returns cannot substitute
    for daily account metrics. Undefined risk statistics retain the existing
    numeric zero sentinel. ``num_trades`` remains the eligibility input but
    now counts actual fills, also exposed explicitly as ``num_fills``.
    ``num_bars`` is present only when the native consumed-data counter exists.
    """
    stats_pnls = getattr(primary_result, "stats_pnls", None) or {}
    currency_stats: dict[str, object] = {}
    if isinstance(stats_pnls, dict) and stats_pnls:
        if set(stats_pnls) != {"USD"}:
            raise ValueError("Backtest PnL statistics must be USD")
        currency_stats = stats_pnls["USD"]
    native_percent = _find_float(currency_stats, ["pnl% (total)", "pnl%"])
    total_return = (
        native_percent / 100.0
        if math.isfinite(native_percent)
        else _find_float(currency_stats, ["return"])
    )
    win_rate = _find_float(currency_stats, ["win rate"])
    sharpe = sortino = max_drawdown = 0.0
    account_derived = _derive_metrics_from_account(account_df) if account_df is not None else None
    if account_derived is not None:
        max_drawdown = account_derived["max_drawdown"]
        total_return = account_derived["total_return"]
        sharpe = account_derived["sharpe_ratio"]
        sortino = account_derived["sortino_ratio"]
    if not math.isfinite(win_rate) and positions_df is not None:
        positions_derived = _derive_metrics_from_positions(positions_df)
        if positions_derived is not None:
            win_rate = positions_derived["win_rate"]

    metrics: dict[str, float | int] = {
        "sharpe_ratio": _nan_safe(sharpe),
        "sortino_ratio": _nan_safe(sortino),
        "max_drawdown": _nan_safe(max_drawdown),
        "total_return": _nan_safe(total_return),
        "win_rate": _nan_safe(win_rate),
        "num_trades": int(len(fills_df)),
        "num_fills": int(len(fills_df)),
    }
    iterations = getattr(primary_result, "iterations", None)
    if iterations is not None:
        # This runner loads Bar data only; iterations counts consumed data items
        # across all instruments, rather than unique minutes or generated fills.
        metrics["num_bars"] = int(iterations)
    return metrics


def _derive_metrics_from_positions(positions_df: pd.DataFrame) -> dict[str, float] | None:
    """Closed-position win rate only; position notional is not account capital."""
    if "realized_pnl" not in positions_df.columns or positions_df.empty:
        return None

    closed = positions_df
    if "side" in positions_df.columns:
        closed = positions_df[positions_df["side"].astype(str) == "FLAT"]
    if closed.empty:
        return None

    raw_pairs = [_money_to_float_with_currency(v) for v in closed["realized_pnl"].tolist()]
    if any(pair is None or pair[1] != "USD" or not math.isfinite(pair[0]) for pair in raw_pairs):
        raise ValueError("Closed position PnL must be finite USD amounts")
    values = [pair[0] for pair in raw_pairs if pair is not None]
    return {"win_rate": sum(value > 0 for value in values) / len(values)}


def _money_to_float(value: object) -> float | None:
    """Coerce a Nautilus money-shaped value to a Python float.

    Nautilus's positions report renders ``Money`` as the string
    ``"0.11 USD"``. ``str(value).split()[0]`` strips the currency
    suffix; the same path also handles plain ints/floats and Decimal
    via ``float()``. Returns ``None`` for empty/unparseable values so
    the caller can drop them from aggregations rather than poisoning
    the sum with a 0.0 that masquerades as a real fill.
    """
    pair = _money_to_float_with_currency(value)
    return pair[0] if pair is not None else None


def _money_to_float_with_currency(value: object) -> tuple[float, str] | None:
    """Parse amount/currency without silently converting unsupported currencies."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return (float(value), "")
    text = str(value).strip()
    if not text:
        return None
    parts = text.split()
    head = parts[0]
    currency = parts[1] if len(parts) > 1 else ""
    try:
        return (float(head), currency)
    except (TypeError, ValueError):
        return None


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
    derived = compute_series_metrics(normalize_daily_returns(returns))
    return {
        "max_drawdown": float(derived.max_drawdown),
        "total_return": float(derived.total_return),
        "sharpe_ratio": float(derived.sharpe),
        "sortino_ratio": float(derived.sortino),
    }


def _find_float(stats: dict[str, object] | object, prefixes: list[str]) -> float:
    """Look up a float metric by case-insensitive key prefix match."""
    if not isinstance(stats, dict):
        return float("nan")
    for key, value in stats.items():
        key_lower = str(key).lower()
        for prefix in prefixes:
            if key_lower.startswith(prefix):
                try:
                    return float(value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    return float("nan")
    return float("nan")


def _nan_safe(value: float) -> float:
    """Replace NaN/Inf with 0.0 so the metrics JSON is safe to persist."""
    import math

    if not math.isfinite(value):
        return 0.0
    return value
