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
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models.backtest import Backtest
from msai.models.trade import Trade
from msai.services.nautilus.backtest_runner import BacktestResult, BacktestRunner
from msai.services.nautilus.catalog_builder import ensure_catalog_data
from msai.services.report_generator import ReportGenerator

log = get_logger(__name__)


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
        ctx: arq worker context (unused here but part of the arq contract).
        backtest_id: UUID string of the :class:`Backtest` row to execute.
        strategy_path: Absolute path to the strategy source file on disk.
            The backtest runner will import this file in a spawned
            subprocess and instantiate its Nautilus ``Strategy`` subclass.
        config: Raw config dict enqueued by the API.  We forward it
            directly to the Nautilus ``StrategyConfig`` so it must match
            the strategy's expected schema (instrument_id, bar_type,
            period knobs, trade size, ...).
    """
    _ = ctx
    log.info(
        "backtest_job_started",
        backtest_id=backtest_id,
        strategy_path=strategy_path,
    )

    # --- 1. Mark running ---------------------------------------------------
    backtest_row = await _start_backtest(backtest_id)
    if backtest_row is None:
        log.error("backtest_not_found", backtest_id=backtest_id)
        return

    symbols: list[str] = list(backtest_row["instruments"])
    start_iso: str = backtest_row["start_date"].isoformat()
    end_iso: str = backtest_row["end_date"].isoformat()
    strategy_id = backtest_row["strategy_id"]
    strategy_code_hash = backtest_row["strategy_code_hash"]

    try:
        # --- 2. Build / refresh the Nautilus catalog ----------------------
        instrument_ids = ensure_catalog_data(
            symbols=symbols,
            raw_parquet_root=settings.parquet_root,
            catalog_root=settings.nautilus_catalog_root,
        )
        log.info(
            "backtest_catalog_ready",
            backtest_id=backtest_id,
            instrument_ids=instrument_ids,
        )

        # --- 3. Build the strategy config with the resolved instrument ----
        # If the caller didn't supply an instrument_id / bar_type we inject
        # them from the backtest row so the Nautilus StrategyConfig can be
        # instantiated inside the subprocess.
        strategy_config = _prepare_strategy_config(config, instrument_ids)

        # --- 4. Run the backtest ------------------------------------------
        runner = BacktestRunner()
        result: BacktestResult = runner.run(
            strategy_file=strategy_path,
            strategy_config=strategy_config,
            instrument_ids=instrument_ids,
            start_date=start_iso,
            end_date=end_iso,
            catalog_path=settings.nautilus_catalog_root,
            timeout_seconds=settings.backtest_timeout_seconds,
        )

        # --- 5. Generate QuantStats report -------------------------------
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

        # --- 6. Persist results + trade rows -----------------------------
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

    except FileNotFoundError as exc:
        # Missing raw data -- most common failure mode, deserves a clean
        # error message rather than a raw traceback.
        log.error(
            "backtest_missing_data",
            backtest_id=backtest_id,
            error=str(exc),
        )
        await _mark_backtest_failed(backtest_id, str(exc))

    except TimeoutError as exc:
        log.error(
            "backtest_timeout",
            backtest_id=backtest_id,
            error=str(exc),
        )
        await _mark_backtest_failed(backtest_id, f"Backtest timed out: {exc}")

    except Exception as exc:
        log.exception(
            "backtest_job_failed",
            backtest_id=backtest_id,
            error=str(exc),
        )
        await _mark_backtest_failed(backtest_id, str(exc))


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
        await session.commit()
        return {
            "instruments": list(backtest.instruments),
            "start_date": backtest.start_date,
            "end_date": backtest.end_date,
            "strategy_id": backtest.strategy_id,
            "strategy_code_hash": backtest.strategy_code_hash,
        }


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


async def _mark_backtest_failed(backtest_id: str, error_message: str) -> None:
    """Update a backtest row to ``failed`` with a user-visible error message.

    Swallows all exceptions from the update itself -- if we can't even
    reach the database there's nothing more we can do, and we don't want
    to override the original failure with a DB error.
    """
    try:
        async with async_session_factory() as session:
            row = await session.get(Backtest, backtest_id)
            if row is None:
                return
            row.status = "failed"
            row.error_message = error_message
            row.completed_at = datetime.now(UTC)
            await session.commit()
    except Exception:
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
    """Pull a returns series out of the Nautilus account report.

    Nautilus's :func:`generate_account_report` does not always include a
    ``returns`` column (it depends on how the account evolved over the
    run).  We handle that gracefully by returning an empty series so the
    QuantStats fallback report still renders.

    Args:
        account_df: DataFrame returned by
            ``engine.trader.generate_account_report(venue=...)``.

    Returns:
        A pandas Series of period-over-period returns, or an empty
        float series if the column is missing.
    """
    if account_df.empty or "returns" not in account_df.columns:
        return pd.Series(dtype=float)
    return account_df["returns"].astype(float)


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
    """Return a UTC datetime from the first populated candidate field."""
    for key in ("ts_init", "ts_last", "ts_event", "timestamp"):
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
