"""arq worker function that drives research jobs (parameter sweeps, walk-forward).

Lifecycle for a single job:

1. Mark the :class:`ResearchJob` row as ``running`` with the worker identity.
2. Acquire compute slots (Redis semaphore) — blocks up to the configured
   timeout if the cluster is at capacity.
3. Prepare the Nautilus catalog so all requested instruments have bar data.
4. Dispatch to :class:`ResearchEngine` via ``asyncio.to_thread()`` (the
   engine is synchronous).
5. Persist results: update the ``ResearchJob`` row with best_config,
   best_metrics, and per-trial results.  Create a :class:`ResearchTrial`
   row for every trial the engine ran.
6. On any error: mark the job as ``failed`` with a user-visible message.
7. In the ``finally`` block: release compute slots unconditionally.

A background heartbeat task renews the compute lease and updates the
``heartbeat_at`` column so the job watchdog can detect stale jobs.
"""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import UTC, date, datetime
from typing import Any

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.core.queue import get_redis_pool
from msai.models.research_job import ResearchJob
from msai.models.research_trial import ResearchTrial
from msai.services.compute_slots import (
    ComputeSlotUnavailableError,
    acquire_compute_slots,
    release_compute_slots,
    renew_compute_slots,
)
from msai.services.nautilus.catalog_builder import ensure_catalog_data
from msai.services.research_engine import ResearchEngine, extract_objective_value

log = get_logger("workers.research")


async def run_research_job(
    ctx: dict[str, Any],
    job_id: str,
    job_type: str,
    payload: dict[str, Any],
) -> None:
    """Run a research job end-to-end and persist results.

    This is the function the arq worker dispatches when it picks up a
    ``run_research_job`` job from the ``msai:research`` Redis queue.

    Args:
        ctx: arq worker context (unused but required by arq's contract).
        job_id: UUID string of the :class:`ResearchJob` row.
        job_type: Either ``"parameter_sweep"`` or ``"walk_forward"``.
        payload: Full request payload serialised by the API route.
    """
    _ = ctx
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    lease_id: str | None = None
    stop_heartbeat = asyncio.Event()
    cancel_requested = asyncio.Event()
    try:
        redis = await get_redis_pool()
    except Exception as exc:
        log.error("research_redis_pool_failed", job_id=job_id, error=str(exc))
        await _mark_failed(job_id, f"Redis connection failed: {exc}")
        return

    log.info(
        "research_job_started",
        job_id=job_id,
        job_type=job_type,
        worker_id=worker_id,
    )

    heartbeat_task: asyncio.Task[None] | None = None

    try:
        # --- 1. Mark running (abort if already cancelled) ---------------------
        if not await _mark_running(job_id, worker_id):
            return

        # --- 2. Heartbeat task ------------------------------------------------
        async def _heartbeat_loop() -> None:
            """Renew lease and heartbeat; also poll for cancellation.

            Best-effort cancellation: if the job's status has been set to
            ``"cancelled"`` or the ``progress_message`` starts with ``"Cancel"``,
            we set ``cancel_requested`` so the outer function marks the job as
            cancelled after the engine returns.  We keep heartbeating and
            renewing the lease so the watchdog does not kill the job while
            the engine is still winding down.
            """
            while not stop_heartbeat.is_set():
                await asyncio.sleep(settings.compute_slot_lease_seconds / 3)
                if stop_heartbeat.is_set():
                    return
                if lease_id is not None:
                    await renew_compute_slots(redis, lease_id)
                await _update_heartbeat(job_id, worker_id)

                # Poll for cancellation — set flag but keep heartbeating
                try:
                    async with async_session_factory() as session:
                        job = await session.get(ResearchJob, job_id)
                        if job is not None and (
                            job.status == "cancelled"
                            or (job.progress_message or "").startswith("Cancel")
                        ):
                            if not cancel_requested.is_set():
                                log.info("research_job_cancel_detected", job_id=job_id)
                                cancel_requested.set()
                except Exception:
                    log.warning("research_heartbeat_cancel_check_failed", job_id=job_id)

        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        # --- 3. Acquire compute slots -----------------------------------------
        requested_parallelism = max(
            1,
            int(payload.get("max_parallelism") or settings.research_max_parallelism),
        )
        await _update_progress(
            job_id,
            progress=5,
            message=f"Waiting for {requested_parallelism} compute slot(s)",
        )
        lease_id = await acquire_compute_slots(
            redis,
            job_kind="research",
            job_id=job_id,
            slot_count=requested_parallelism,
        )

        # --- 4. Prepare Nautilus catalog --------------------------------------
        await _update_progress(job_id, progress=10, message="Preparing market data catalog")
        symbols = list(payload.get("instruments", []))
        asset_class = str(payload.get("asset_class", "stocks"))
        instrument_ids = await asyncio.to_thread(
            ensure_catalog_data,
            symbols,
            settings.parquet_root,
            settings.nautilus_catalog_root,
            asset_class=asset_class,
        )

        # --- 5. Progress callback for the engine -----------------------------
        loop = asyncio.get_running_loop()

        def _progress_callback(update: dict[str, Any]) -> None:
            """Synchronous callback invoked by the engine inside to_thread."""
            # Schedule the async DB update from inside the sync thread
            # without blocking the engine.  The event loop is running in
            # the main thread.
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    _update_progress(
                        job_id,
                        progress=int(update.get("progress", 0)),
                        message=str(update.get("message", "Running")),
                    )
                )
            )

        # --- 6. Dispatch to engine --------------------------------------------
        engine = ResearchEngine()
        if job_type == "parameter_sweep":
            report = await asyncio.to_thread(
                engine.run_parameter_sweep,
                strategy_path=str(payload["strategy_path"]),
                base_config=dict(payload.get("base_config", {})),
                parameter_grid=dict(payload["parameter_grid"]),
                instruments=instrument_ids,
                start_date=str(payload["start_date"]),
                end_date=str(payload["end_date"]),
                data_path=settings.nautilus_catalog_root,
                objective=str(payload.get("objective", "sharpe")),
                max_parallelism=requested_parallelism,
                search_strategy=str(payload.get("search_strategy", "auto")),
                stage_fractions=payload.get("stage_fractions"),
                reduction_factor=int(payload.get("reduction_factor", 2)),
                min_trades=(
                    int(payload["min_trades"])
                    if payload.get("min_trades") is not None
                    else None
                ),
                require_positive_return=bool(payload.get("require_positive_return", False)),
                holdout_fraction=(
                    float(payload["holdout_fraction"])
                    if payload.get("holdout_fraction") is not None
                    else None
                ),
                holdout_days=(
                    int(payload["holdout_days"])
                    if payload.get("holdout_days") is not None
                    else None
                ),
                purge_days=int(payload.get("purge_days", 5)),
                progress_callback=_progress_callback,
            )
        elif job_type == "walk_forward":
            report = await asyncio.to_thread(
                engine.run_walk_forward,
                strategy_path=str(payload["strategy_path"]),
                base_config=dict(payload.get("base_config", {})),
                parameter_grid=dict(payload["parameter_grid"]),
                instruments=instrument_ids,
                start_date=date.fromisoformat(str(payload["start_date"])),
                end_date=date.fromisoformat(str(payload["end_date"])),
                train_days=int(payload["train_days"]),
                test_days=int(payload["test_days"]),
                step_days=(
                    int(payload["step_days"])
                    if payload.get("step_days") is not None
                    else None
                ),
                mode=str(payload.get("mode", "rolling")),
                data_path=settings.nautilus_catalog_root,
                objective=str(payload.get("objective", "sharpe")),
                max_parallelism=requested_parallelism,
                search_strategy=str(payload.get("search_strategy", "auto")),
                stage_fractions=payload.get("stage_fractions"),
                reduction_factor=int(payload.get("reduction_factor", 2)),
                min_trades=(
                    int(payload["min_trades"])
                    if payload.get("min_trades") is not None
                    else None
                ),
                require_positive_return=bool(payload.get("require_positive_return", False)),
                holdout_fraction=(
                    float(payload["holdout_fraction"])
                    if payload.get("holdout_fraction") is not None
                    else None
                ),
                holdout_days=(
                    int(payload["holdout_days"])
                    if payload.get("holdout_days") is not None
                    else None
                ),
                purge_days=int(payload.get("purge_days", 5)),
                progress_callback=_progress_callback,
            )
        else:
            raise ValueError(f"Unsupported research job type: {job_type!r}")

        # --- 7. Persist results or honour cancellation -------------------------
        # Let any pending fire-and-forget progress writes flush before we
        # write the final state — prevents a late progress callback from
        # overwriting the completed/cancelled status.
        await asyncio.sleep(0.15)

        if cancel_requested.is_set():
            await _mark_cancelled(job_id)
            log.info("research_job_cancelled_after_engine", job_id=job_id, job_type=job_type)
        else:
            await _finalize_job(job_id, report)
            log.info(
                "research_job_completed",
                job_id=job_id,
                job_type=job_type,
                num_results=len(report.get("results", [])),
            )

    except ComputeSlotUnavailableError as exc:
        log.error("research_job_slots_unavailable", job_id=job_id, error=str(exc))
        await _mark_failed(job_id, str(exc))

    except Exception as exc:
        log.exception("research_job_failed", job_id=job_id, error=str(exc))
        await _mark_failed(job_id, str(exc))

    finally:
        stop_heartbeat.set()
        # Wait for the heartbeat task to exit cleanly
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        if lease_id is not None:
            await release_compute_slots(redis, lease_id)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


async def _mark_running(job_id: str, worker_id: str) -> bool:
    """Flip the research job row to ``running`` with worker identity.

    Returns ``False`` if the job was not found or has already been cancelled,
    indicating the caller should abort without executing the engine.
    """
    async with async_session_factory() as session:
        job = await session.get(ResearchJob, job_id)
        if job is None:
            log.error("research_job_not_found", job_id=job_id)
            return False
        if job.status in ("completed", "failed", "cancelled"):
            log.info("research_job_already_terminal", job_id=job_id, status=job.status)
            return False
        job.status = "running"
        job.started_at = datetime.now(UTC)
        job.worker_id = worker_id
        job.heartbeat_at = datetime.now(UTC)
        job.attempt = (job.attempt or 0) + 1
        await session.commit()
    return True


async def _update_heartbeat(job_id: str, worker_id: str) -> None:
    """Touch the heartbeat timestamp so the watchdog knows we're alive."""
    try:
        async with async_session_factory() as session:
            job = await session.get(ResearchJob, job_id)
            if job is not None:
                job.heartbeat_at = datetime.now(UTC)
                job.worker_id = worker_id
                await session.commit()
    except Exception:
        log.warning("research_heartbeat_update_failed", job_id=job_id)


async def _update_progress(
    job_id: str,
    *,
    progress: int,
    message: str,
) -> None:
    """Update the progress fields on the research job row."""
    try:
        async with async_session_factory() as session:
            job = await session.get(ResearchJob, job_id)
            if job is not None:
                job.progress = progress
                job.progress_message = message
                await session.commit()
    except Exception:
        log.warning("research_progress_update_failed", job_id=job_id)


async def _finalize_job(job_id: str, report: dict[str, Any]) -> None:
    """Persist engine results to the ResearchJob and create ResearchTrial rows."""
    async with async_session_factory() as session:
        job = await session.get(ResearchJob, job_id)
        if job is None:
            return

        job.status = "completed"
        job.progress = 100
        job.progress_message = "Completed"
        job.completed_at = datetime.now(UTC)
        job.results = report

        best = report.get("summary", {}).get("best_result")
        if best is None:
            # Walk-forward: derive best from window with best test Sharpe
            windows = report.get("windows", [])
            if windows:
                best_window = max(
                    (w for w in windows if w.get("test_result")),
                    key=lambda w: (w.get("test_result") or {}).get("metrics", {}).get(
                        "sharpe_ratio", 0
                    ),
                    default=None,
                )
                if best_window and best_window.get("best_train_result"):
                    best = best_window["best_train_result"]

        job.best_config = best.get("config") if best else None
        job.best_metrics = best.get("metrics") if best else None

        # Create trial rows for each individual result
        objective = str(report.get("objective", "sharpe"))
        results_list: list[dict[str, Any]] = report.get("results", [])
        if not results_list:
            # Walk-forward: create trials from windows
            for i, window in enumerate(report.get("windows", [])):
                train = window.get("best_train_result") or {}
                test = window.get("test_result") or {}
                trial = ResearchTrial(
                    research_job_id=job_id,
                    trial_number=i,
                    config=train.get("config", {}),
                    metrics=test.get("metrics"),
                    status="completed",
                    objective_value=_safe_float(
                        extract_objective_value(test.get("metrics", {}), objective)
                    ),
                )
                session.add(trial)
        for index, result in enumerate(results_list):
            # Derive objective_value from metrics if not set directly
            raw_obj = result.get("objective_value")
            if raw_obj is None:
                metrics = result.get("metrics") or {}
                raw_obj = extract_objective_value(metrics, objective)
            trial = ResearchTrial(
                research_job_id=job_id,
                trial_number=index,
                config=result.get("config", {}),
                metrics=result.get("metrics"),
                status="completed" if result.get("error") is None else "failed",
                objective_value=_safe_float(raw_obj),
            )
            session.add(trial)

        await session.commit()


async def _mark_cancelled(job_id: str) -> None:
    """Mark the research job as cancelled after the engine has finished."""
    try:
        async with async_session_factory() as session:
            job = await session.get(ResearchJob, job_id)
            if job is None:
                return
            job.status = "cancelled"
            job.progress_message = "Cancelled"
            job.completed_at = datetime.now(UTC)
            await session.commit()
    except Exception:
        log.exception("research_cancel_update_failed", job_id=job_id)


async def _mark_failed(job_id: str, error_message: str) -> None:
    """Mark the research job as failed with a user-visible error message."""
    try:
        async with async_session_factory() as session:
            job = await session.get(ResearchJob, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error_message = error_message
            job.completed_at = datetime.now(UTC)
            await session.commit()
    except Exception:
        log.exception("research_status_update_failed", job_id=job_id)


def _safe_float(value: Any) -> float | None:
    """Convert a value to float, returning None if not parseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
