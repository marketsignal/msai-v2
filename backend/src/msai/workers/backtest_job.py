"""arq worker function that drives a full NautilusTrader backtest.

Lifecycle for a single job:

1. Fetch the :class:`Backtest` row by ID and flip its status to ``running``.
2. Ensure the Nautilus ``ParquetDataCatalog`` has bars for every requested
   symbol -- lazy conversion from raw OHLCV Parquet files happens here.
3. Hand the canonical instrument IDs + strategy file + date range to
   :class:`BacktestRunner`, which spawns a subprocess and runs NautilusTrader
   end-to-end.
4. Pull returns out of the account report, generate a QuantStats HTML
   tearsheet, and persist it under ``{reports_root}/{backtest_id}.html``.
5. Write the metrics dict, report path, completion timestamp, and every
   order row back to the database.  Orders become :class:`Trade` rows
   linked to the backtest.
6. On any failure, mark the backtest ``failed`` with a readable error
   message so the UI can display it.

The function is intentionally verbose about error handling -- a silent
backtest failure is one of the worst user experiences imaginable because
it tends to look like "the job is still running" until someone checks the
logs.

Task B8 wires the missing-data path into a bounded retry-once loop via
:func:`msai.services.backtests.auto_heal.run_auto_heal`: the first
``FileNotFoundError`` from ``ensure_catalog_data`` triggers one
auto-heal cycle, and on ``AutoHealOutcome.SUCCESS`` the execution body
re-enters with the same backtest snapshot. ``_start_backtest`` runs
exactly once so the ``attempt`` counter does not double-increment.
Non-SUCCESS outcomes are translated to typed exceptions via
:data:`_OUTCOME_TO_EXC` so the classifier continues to produce the
right :class:`FailureCode`.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import date

import pandas as pd

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models.backtest import Backtest
from msai.models.trade import Trade
from msai.services.backtests.auto_heal import AutoHealOutcome, run_auto_heal
from msai.services.nautilus.backtest_runner import BacktestResult, BacktestRunner
from msai.services.nautilus.catalog_builder import describe_catalog, ensure_catalog_data
from msai.services.report_generator import ReportGenerator

log = get_logger(__name__)

_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_HEARTBEAT_INTERVAL_S = 15


# Map non-SUCCESS auto-heal outcomes to exception types the
# ``classify_worker_failure`` branch will re-tag correctly. The
# classifier branches on ``isinstance(exc, ...)`` so the exception type
# is what picks the ``FailureCode``:
#
# * ``FileNotFoundError`` → ``FailureCode.MISSING_DATA`` — used for
#   ``GUARDRAIL_REJECTED`` and ``COVERAGE_STILL_MISSING``. Both stay as
#   MISSING_DATA because the remediation is still "get more data" even
#   when auto-heal declined to fetch it. The retry-once cap prevents a
#   second heal attempt from firing.
# * ``TimeoutError`` → ``FailureCode.TIMEOUT``
# * ``RuntimeError`` → ``FailureCode.ENGINE_CRASH``
_OUTCOME_TO_EXC: dict[AutoHealOutcome, type[BaseException]] = {
    AutoHealOutcome.GUARDRAIL_REJECTED: FileNotFoundError,
    AutoHealOutcome.COVERAGE_STILL_MISSING: FileNotFoundError,
    AutoHealOutcome.TIMEOUT: TimeoutError,
    AutoHealOutcome.INGEST_FAILED: RuntimeError,
}


async def run_backtest_job(
    ctx: dict[str, Any],
    backtest_id: str,
    strategy_path: str,
    config: dict[str, Any],
) -> None:
    """Run a backtest end-to-end and persist its results.

    This is the function the arq worker dispatches when it picks up a
    ``run_backtest`` job off the Redis queue.  It never raises -- all
    failures are captured and written to the backtest row so the API can
    surface them to the user.

    Args:
        ctx: arq worker context. ``ctx["redis"]`` is the ``ArqRedis``
            pool the auto-heal orchestrator uses to enqueue the ingest
            job on the dedicated queue and poll its status.
        backtest_id: UUID string of the :class:`Backtest` row to execute.
        strategy_path: Absolute path to the strategy source file on disk.
            The backtest runner will import this file in a spawned
            subprocess and instantiate its Nautilus ``Strategy`` subclass.
        config: Raw config dict enqueued by the API.  We forward it
            directly to the Nautilus ``StrategyConfig`` so it must match
            the strategy's expected schema (instrument_id, bar_type,
            period knobs, trade size, ...).
    """
    log.info(
        "backtest_job_started",
        backtest_id=backtest_id,
        strategy_path=strategy_path,
    )

    # --- 1. Mark running ---
    # Called ONCE — snapshot reused on retry-after-heal so attempt doesn't double-increment.
    backtest_row = await _start_backtest(backtest_id)
    if backtest_row is None:
        log.error("backtest_not_found", backtest_id=backtest_id)
        return

    symbols: list[str] = list(backtest_row["instruments"])
    start_iso: str = backtest_row["start_date"].isoformat()
    end_iso: str = backtest_row["end_date"].isoformat()
    strategy_id = backtest_row["strategy_id"]
    strategy_code_hash = backtest_row["strategy_code_hash"]
    # Asset class is passed through the config dict so the worker can
    # look up raw Parquet data under the correct subdirectory
    # (stocks, futures, crypto, ...). Defaults to stocks for backwards
    # compatibility with older payloads.
    asset_class: str = str(config.get("asset_class", "stocks"))

    # --- Heartbeat task (spawned once at the outer level so it keeps
    # firing through both attempts and through the auto-heal poll loop
    # in between). Cancelled in the outer finally.
    stop_heartbeat = asyncio.Event()

    async def _refresh_heartbeat() -> None:
        while not stop_heartbeat.is_set():
            try:
                async with async_session_factory() as hb_session:
                    row = await hb_session.get(Backtest, backtest_id)
                    if row is not None:
                        row.heartbeat_at = datetime.now(UTC)
                        await hb_session.commit()
            except Exception:  # noqa: BLE001 — best-effort heartbeat
                log.warning(
                    "backtest_heartbeat_refresh_failed",
                    backtest_id=backtest_id,
                    exc_info=True,
                )
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)

    heartbeat_task = asyncio.create_task(_refresh_heartbeat())

    try:
        # --- 2. Retry-once loop (Task B8) -----------------------------------
        # Attempt 1: run the full execution body. If it raises
        # FileNotFoundError (ensure_catalog_data couldn't find raw
        # Parquet) we trigger one auto-heal cycle. On SUCCESS we loop
        # back into attempt 2 with the SAME snapshot (no second
        # _start_backtest, no double attempt-counter increment).
        # On any non-SUCCESS outcome or on any non-FNF exception we
        # break out to the terminal-failure handler below.
        attempt = 0
        terminal_exc: BaseException | None = None
        while attempt < 2:
            attempt += 1
            try:
                await _execute_backtest(
                    backtest_row=backtest_row,
                    backtest_id=backtest_id,
                    strategy_path=strategy_path,
                    config=config,
                    symbols=symbols,
                    asset_class=asset_class,
                    start_iso=start_iso,
                    end_iso=end_iso,
                    strategy_id=strategy_id,
                    strategy_code_hash=strategy_code_hash,
                )
                return  # happy path — execution succeeded
            except FileNotFoundError as exc:
                if attempt == 1:
                    # Wrap run_auto_heal so any exception it raises (e.g.,
                    # Redis connection errors that AutoHealLock intentionally
                    # propagates) becomes the terminal_exc. Without this guard
                    # an exception raised INSIDE this except-handler escapes
                    # the while-loop without ever reaching
                    # _handle_terminal_failure, leaving the backtest row
                    # stuck as "running". Codex review P1 2026-04-21.
                    try:
                        result = await run_auto_heal(
                            backtest_id=backtest_id,
                            instruments=symbols,
                            start=backtest_row["start_date"],
                            end=backtest_row["end_date"],
                            catalog_root=settings.nautilus_catalog_root,
                            caller_asset_class_hint=asset_class,
                            pool=ctx["redis"],
                        )
                    except Exception as heal_exc:  # noqa: BLE001 — never let heal kill the backtest silently
                        log.exception(
                            "backtest_auto_heal_orchestrator_raised",
                            backtest_id=backtest_id,
                        )
                        terminal_exc = heal_exc
                        break
                    if result.outcome == AutoHealOutcome.SUCCESS:
                        # Re-enter the execution body with healed data.
                        continue
                    exc_cls = _OUTCOME_TO_EXC.get(result.outcome, FileNotFoundError)
                    terminal_exc = exc_cls(
                        result.reason_human or result.outcome.value,
                    )
                    break
                # Second-attempt FNF — heal didn't stick; surface raw.
                terminal_exc = exc
                break
            except Exception as exc:  # noqa: BLE001 — terminal handler classifies
                terminal_exc = exc
                break

        if terminal_exc is not None:
            await _handle_terminal_failure(
                backtest_id=backtest_id,
                symbols=symbols,
                asset_class=asset_class,
                backtest_row=backtest_row,
                exc=terminal_exc,
            )
    finally:
        stop_heartbeat.set()
        heartbeat_task.cancel()


async def _execute_backtest(
    *,
    backtest_row: dict[str, Any],
    backtest_id: str,
    strategy_path: str,
    config: dict[str, Any],
    symbols: list[str],
    asset_class: str,
    start_iso: str,
    end_iso: str,
    strategy_id: Any,
    strategy_code_hash: str,
) -> None:
    """Run the catalog-build + subprocess-spawn + finalize pipeline.

    Extracted from ``run_backtest_job`` so the retry-once loop can
    re-enter with the same backtest snapshot on a successful heal
    without calling ``_start_backtest`` a second time. Any exception
    propagates to the retry loop which decides whether to invoke
    auto-heal or hand off to ``_handle_terminal_failure``.
    """
    # --- Build / refresh the Nautilus catalog -------------------------------
    instrument_ids = ensure_catalog_data(
        symbols=symbols,
        raw_parquet_root=settings.parquet_root,
        catalog_root=settings.nautilus_catalog_root,
        asset_class=asset_class,
    )
    log.info(
        "backtest_catalog_ready",
        backtest_id=backtest_id,
        instrument_ids=instrument_ids,
    )

    # --- Capture data lineage snapshot --------------------------------------
    lineage_snapshot = describe_catalog(
        instruments=instrument_ids,
        data_path=str(settings.parquet_root),
    )
    python_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    try:
        import nautilus_trader  # noqa: WPS433

        nautilus_ver: str | None = nautilus_trader.__version__
    except Exception:  # noqa: BLE001 — best-effort version capture
        log.warning("nautilus_version_capture_failed", exc_info=True)
        nautilus_ver = None

    await _persist_lineage(
        backtest_id=backtest_id,
        nautilus_version=nautilus_ver,
        python_version=python_ver,
        data_snapshot=lineage_snapshot,
    )

    # --- Build the strategy config with the resolved instrument -------------
    strategy_config = _prepare_strategy_config(config, instrument_ids)

    # --- Run the backtest ---------------------------------------------------
    runner = BacktestRunner()
    result: BacktestResult = await asyncio.to_thread(
        runner.run,
        strategy_file=strategy_path,
        strategy_config=strategy_config,
        instrument_ids=instrument_ids,
        start_date=start_iso,
        end_date=end_iso,
        catalog_path=settings.nautilus_catalog_root,
        timeout_seconds=settings.backtest_timeout_seconds,
    )

    # --- Generate QuantStats report -----------------------------------------
    returns_series = _extract_returns_series(result.account_df)
    report_generator = ReportGenerator()
    html = report_generator.generate_tearsheet(
        returns=returns_series,
        title=f"Backtest {backtest_id}",
    )
    report_path = report_generator.save_report(
        html=html,
        backtest_id=backtest_id,
        data_root=str(settings.data_root),
    )

    # --- Persist results + trade rows ---------------------------------------
    await _finalize_backtest(
        backtest_id=backtest_id,
        metrics=result.metrics,
        report_path=report_path,
        orders_df=result.orders_df,
        strategy_id=strategy_id,
        strategy_code_hash=strategy_code_hash,
    )

    log.info(
        "backtest_job_completed",
        backtest_id=backtest_id,
        num_trades=result.metrics.get("num_trades", 0),
        total_return=result.metrics.get("total_return", 0.0),
    )


async def _handle_terminal_failure(
    *,
    backtest_id: str,
    symbols: list[str],
    asset_class: str,
    backtest_row: dict[str, Any],
    exc: BaseException,
) -> None:
    """Log + persist a terminal backtest failure.

    Emits the same structured log events operators grep on
    (``backtest_missing_data`` / ``backtest_timeout`` /
    ``backtest_job_failed``) and delegates envelope construction to
    :func:`_mark_backtest_failed`, which calls the shared classifier.
    """
    if isinstance(exc, FileNotFoundError):
        log.error("backtest_missing_data", backtest_id=backtest_id, error=str(exc))
    elif isinstance(exc, TimeoutError):
        log.error("backtest_timeout", backtest_id=backtest_id, error=str(exc))
    else:
        log.exception(
            "backtest_job_failed",
            backtest_id=backtest_id,
            error=str(exc),
            exc_type=exc.__class__.__name__,
        )
    await _mark_backtest_failed(
        backtest_id=backtest_id,
        exc=exc,
        # ``symbols`` is the user-submitted list — exactly what the
        # remediation command needs to echo back. The canonicalized
        # ``instrument_ids`` list is intentionally NOT used here
        # because it's unbound when the exception fires inside
        # ``ensure_catalog_data``.
        instruments=list(symbols),
        asset_class=asset_class,
        start_date=backtest_row["start_date"],
        end_date=backtest_row["end_date"],
    )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


async def _start_backtest(backtest_id: str) -> dict[str, Any] | None:
    """Flip the backtest row to ``running`` and return its primitive fields.

    We snapshot the fields we need while the row is live so the rest of
    the job can operate without holding an async session open the entire
    time the Nautilus subprocess runs.

    Args:
        backtest_id: UUID string of the backtest row.

    Returns:
        A dict with the fields we need later, or ``None`` if the row
        was missing.
    """
    async with async_session_factory() as session:
        backtest = await session.get(Backtest, backtest_id)
        if backtest is None:
            return None
        backtest.status = "running"
        backtest.progress = 10
        backtest.started_at = datetime.now(UTC)
        backtest.worker_id = _WORKER_ID
        backtest.heartbeat_at = datetime.now(UTC)
        backtest.attempt = (backtest.attempt or 0) + 1
        await session.commit()
        return {
            "instruments": list(backtest.instruments),
            "start_date": backtest.start_date,
            "end_date": backtest.end_date,
            "strategy_id": backtest.strategy_id,
            "strategy_code_hash": backtest.strategy_code_hash,
        }


async def _persist_lineage(
    *,
    backtest_id: str,
    nautilus_version: str | None,
    python_version: str,
    data_snapshot: dict[str, Any],
) -> None:
    """Write data lineage fields onto the backtest row.

    Called right after the catalog is confirmed ready but before the
    actual backtest subprocess starts.  This means even failed backtests
    will carry their lineage info, which is important for debugging
    data-related failures.
    """
    try:
        async with async_session_factory() as session:
            row = await session.get(Backtest, backtest_id)
            if row is None:
                return
            row.nautilus_version = nautilus_version
            row.python_version = python_version
            row.data_snapshot = data_snapshot
            await session.commit()
    except Exception:  # noqa: BLE001 — best-effort lineage persist
        log.warning("backtest_lineage_persist_failed", backtest_id=backtest_id, exc_info=True)


async def _finalize_backtest(
    *,
    backtest_id: str,
    metrics: dict[str, float | int],
    report_path: str,
    orders_df: pd.DataFrame,
    strategy_id: Any,
    strategy_code_hash: str,
) -> None:
    """Persist metrics, report path, and trade rows for a completed backtest.

    Runs inside a single DB transaction so the backtest row and its
    child ``trades`` are committed atomically.

    Args:
        backtest_id: UUID string of the backtest row to update.
        metrics: Metrics dict produced by :class:`BacktestRunner`.
        report_path: Absolute path to the generated HTML report.
        orders_df: DataFrame of executed orders from the Nautilus trader
            report.  Each row becomes a :class:`Trade` record.
        strategy_id: FK to the :class:`Strategy` row -- denormalised onto
            every trade for easy provenance lookups.
        strategy_code_hash: SHA256 of the strategy source at run time --
            stored on each trade for reproducibility.
    """
    async with async_session_factory() as session:
        row = await session.get(Backtest, backtest_id)
        if row is None:
            return

        row.status = "completed"
        row.progress = 100
        row.metrics = dict(metrics)
        row.report_path = report_path
        row.completed_at = datetime.now(UTC)

        for order in orders_df.to_dict(orient="records"):
            trade = _order_row_to_trade(
                order=order,
                backtest_id=row.id,
                strategy_id=strategy_id,
                strategy_code_hash=strategy_code_hash,
            )
            if trade is not None:
                session.add(trade)

        await session.commit()


async def _mark_backtest_failed(
    *,
    backtest_id: str,
    exc: BaseException,
    instruments: list[str],
    start_date: date,
    end_date: date,
    asset_class: str | None = None,
) -> None:
    """Update a backtest row to ``failed`` with structured classification.

    Classifies ``exc`` via :func:`classify_worker_failure` and persists
    the envelope fields alongside the raw ``error_message`` (kept for
    operators who want the full unsanitized exception).

    ``asset_class`` is the caller-known classification hint (worker has
    it from the config) — passed through so the classifier can build a
    precise `msai ingest <asset_class> ...` remediation command even
    when `settings.parquet_root` doesn't match the hard-coded
    `/app/data/parquet/...` regex used to recover it from the message.

    Swallows all exceptions from the update itself -- if we can't even
    reach the database there's nothing more we can do, and we don't want
    to override the original failure with a DB error.
    """
    from msai.services.backtests.classifier import classify_worker_failure

    classification = classify_worker_failure(
        exc,
        instruments=instruments,
        start_date=start_date,
        end_date=end_date,
        asset_class=asset_class,
    )
    remediation_json = (
        classification.remediation.model_dump(mode="json")
        if classification.remediation is not None
        else None
    )

    try:
        async with async_session_factory() as session:
            row = await session.get(Backtest, backtest_id)
            if row is None:
                return
            row.status = "failed"
            row.error_message = str(exc) or exc.__class__.__name__  # raw
            row.error_code = classification.code.value
            row.error_public_message = classification.public_message
            row.error_suggested_action = classification.suggested_action
            row.error_remediation = remediation_json
            row.completed_at = datetime.now(UTC)
            await session.commit()
    except Exception:  # noqa: BLE001 — already in error path
        log.exception("backtest_status_update_failed", backtest_id=backtest_id)


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _prepare_strategy_config(
    config: dict[str, Any],
    instrument_ids: list[str],
) -> dict[str, Any]:
    """Inject a default ``instrument_id`` / ``bar_type`` if the caller omitted them.

    Nautilus ``StrategyConfig`` subclasses typically require both fields.
    The API layer already exposes them but older payloads and CLI calls
    may not, so we default to the first resolved instrument.

    Args:
        config: Raw strategy config dict from the API.
        instrument_ids: Canonical Nautilus instrument IDs for the backtest.

    Returns:
        A shallow-copied dict safe to hand to
        :class:`ImportableStrategyConfig`.
    """
    prepared = dict(config)
    if "instrument_id" not in prepared and instrument_ids:
        prepared["instrument_id"] = instrument_ids[0]
    if "bar_type" not in prepared and instrument_ids:
        prepared["bar_type"] = f"{instrument_ids[0]}-1-MINUTE-LAST-EXTERNAL"
    return prepared


def _extract_returns_series(account_df: pd.DataFrame) -> pd.Series:  # type: ignore[type-arg]
    """Pull a time-indexed returns series out of the Nautilus account report.

    Nautilus's :func:`generate_account_report` does not always include a
    ``returns`` column (it depends on how the account evolved over the
    run).  We handle that gracefully by returning an empty series so the
    QuantStats fallback report still renders.

    The series MUST have a ``DatetimeIndex`` -- QuantStats uses the index
    to compute annualisation, monthly heatmaps, and chart x-axes.  If we
    return a bare ``RangeIndex`` the tearsheet ends up dated 1970 and
    every time-based statistic becomes meaningless.

    Args:
        account_df: DataFrame returned by
            ``engine.trader.generate_account_report(venue=...)``.

    Returns:
        A pandas Series of period-over-period returns with a UTC
        ``DatetimeIndex``, or an empty float series if the column is
        missing.
    """
    if account_df.empty or "returns" not in account_df.columns:
        return pd.Series(dtype=float)

    returns = account_df["returns"].astype(float)

    # Find an appropriate timestamp column; Nautilus reports use
    # ``ts_init``/``ts_last`` (nanosecond ints) or a plain ``timestamp``
    # column depending on the version. Fall back to the DataFrame index
    # if it already carries datetimes.
    for ts_col in ("ts_last", "ts_init", "timestamp"):
        if ts_col in account_df.columns:
            index = pd.to_datetime(account_df[ts_col], utc=True, errors="coerce")
            if not index.isna().all():
                returns = returns.copy()
                returns.index = index
                return returns

    if isinstance(account_df.index, pd.DatetimeIndex):
        returns = returns.copy()
        returns.index = account_df.index
        return returns

    return returns


def _order_row_to_trade(
    *,
    order: dict[str, Any],
    backtest_id: Any,
    strategy_id: Any,
    strategy_code_hash: str,
) -> Trade | None:
    """Translate a Nautilus order report row into a :class:`Trade` model.

    The Nautilus report schema is not perfectly stable across versions,
    so we look at a few candidate field names for each attribute and
    degrade gracefully if something is missing.

    Args:
        order: A single row from ``engine.trader.generate_orders_report``.
        backtest_id: FK value for the owning backtest row.
        strategy_id: FK value for the strategy that generated the order.
        strategy_code_hash: SHA256 of the strategy source at run time.

    Returns:
        A :class:`Trade` ready to be added to the session, or ``None``
        if the row lacks a usable timestamp and side.
    """
    executed_at = _pick_timestamp(order)
    side = _pick_str(order, ["side", "order_side"], default="BUY").upper()
    if side not in ("BUY", "SELL"):
        return None

    quantity = _pick_decimal(order, ["quantity", "filled_qty", "qty"], default=Decimal("0"))
    price = _pick_decimal(order, ["avg_px", "price", "last_px"], default=Decimal("0"))
    pnl = _pick_decimal(order, ["pnl", "realized_pnl"], default=Decimal("0"))
    instrument = _pick_str(order, ["instrument_id", "symbol", "instrument"], default="UNKNOWN")

    return Trade(
        backtest_id=backtest_id,
        deployment_id=None,
        strategy_id=strategy_id,
        strategy_code_hash=strategy_code_hash,
        instrument=str(instrument),
        side=side,
        quantity=quantity,
        price=price,
        commission=Decimal("0"),
        pnl=pnl,
        is_live=False,
        executed_at=executed_at,
    )


def _pick_timestamp(row: dict[str, Any]) -> datetime:
    """Return a UTC datetime from the first populated candidate field.

    Order matters: ``ts_last`` is the fill/event timestamp (when the
    trade actually executed). ``ts_init`` is just the order creation
    timestamp, which is earlier and would mis-order the trade log for
    any strategy that doesn't fill immediately.
    """
    for key in ("ts_last", "ts_event", "ts_init", "timestamp"):
        raw = row.get(key)
        if raw is None:
            continue
        parsed = pd.to_datetime(raw, utc=True, errors="coerce")
        if not pd.isna(parsed):
            return parsed.to_pydatetime()
    return datetime.now(UTC)


def _pick_str(row: dict[str, Any], candidates: list[str], *, default: str) -> str:
    """Return the first non-empty string value from a list of candidate keys."""
    for key in candidates:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return default


def _pick_decimal(
    row: dict[str, Any],
    candidates: list[str],
    *,
    default: Decimal,
) -> Decimal:
    """Return the first parseable Decimal value from a list of candidate keys."""
    for key in candidates:
        value = row.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except (ValueError, ArithmeticError):
            continue
    return default


# Legacy alias -- the old worker dispatched to ``run_backtest``.
run_backtest = run_backtest_job
