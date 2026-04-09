from __future__ import annotations

import asyncio
import os
import socket
from datetime import date

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.core.queue import get_redis_pool
from msai.services.alerting import alerting_service
from msai.services.compute_slots import (
    ComputeSlotUnavailableError,
    acquire_compute_slots,
    release_compute_slots,
    renew_compute_slots,
)
from msai.services.nautilus.catalog_builder import ensure_catalog_data
from msai.services.nautilus.instrument_service import instrument_service
from msai.services.research_artifacts import ResearchArtifactService
from msai.services.research_engine import ResearchEngine
from msai.services.research_jobs import ResearchJobService

logger = get_logger("workers.research")


class ResearchJobCancelledError(RuntimeError):
    """Raised when a queued research job is cancelled by the operator/watchdog."""


async def run_research_job(
    ctx: dict,
    job_id: str,
    job_type: str,
    payload: dict,
) -> None:
    job_service = ResearchJobService()
    artifact_service = ResearchArtifactService()
    engine = ResearchEngine()
    worker_id = str(ctx.get("worker_instance_id") or f"{socket.gethostname()}:{os.getpid()}")
    stop_heartbeat = asyncio.Event()
    slot_lease_id: str | None = None
    slot_pool = await get_redis_pool()

    try:
        job_service.mark_running(job_id, worker_id=worker_id)

        async def _heartbeat_loop() -> None:
            while not stop_heartbeat.is_set():
                await asyncio.sleep(settings.research_job_heartbeat_seconds)
                if stop_heartbeat.is_set():
                    return
                state = job_service.load_job(job_id)
                if state.get("cancel_requested"):
                    return
                if slot_lease_id is not None:
                    await renew_compute_slots(slot_pool, slot_lease_id)
                job_service.heartbeat(job_id, worker_id=worker_id)

        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        requested_parallelism = max(
            1,
            int(payload.get("max_parallelism") or settings.research_max_parallelism),
        )
        job_service.heartbeat(
            job_id,
            worker_id=worker_id,
            progress=5,
            progress_message=f"Waiting for {requested_parallelism} compute slots",
        )
        slot_lease = await acquire_compute_slots(
            slot_pool,
            job_kind="research",
            job_id=job_id,
            requested_slots=requested_parallelism,
        )
        slot_lease_id = str(slot_lease["lease_id"])
        allocated_parallelism = int(slot_lease["slot_count"])

        async with async_session_factory() as session:
            definitions = await instrument_service.ensure_backtest_definitions(
                session,
                list(payload["instruments"]),
            )
            await session.commit()
        prepared_instruments = await asyncio.to_thread(
            ensure_catalog_data,
            definitions=definitions,
            raw_parquet_root=settings.parquet_root,
            catalog_root=settings.nautilus_catalog_root,
        )

        def _assert_not_cancelled() -> None:
            state = job_service.load_job(job_id)
            if state.get("cancel_requested"):
                raise ResearchJobCancelledError(f"Research job cancelled: {job_id}")

        def _progress_callback(update: dict[str, object]) -> None:
            _assert_not_cancelled()
            payload = {
                "progress": int(update.get("progress") or 0),
                "progress_message": str(update.get("message") or "Running"),
                "stage_index": int(update["stage_index"]) if update.get("stage_index") is not None else None,
                "stage_count": int(update["stage_count"]) if update.get("stage_count") is not None else None,
                "completed_trials": (
                    int(update["completed_trials"]) if update.get("completed_trials") is not None else None
                ),
                "total_trials": int(update["total_trials"]) if update.get("total_trials") is not None else None,
            }
            job_service.heartbeat(job_id, worker_id=worker_id, **payload)

        if job_type == "parameter_sweep":
            report = await asyncio.to_thread(
                engine.run_parameter_sweep,
                strategy_path=str(payload["strategy_path"]),
                base_config=dict(payload["base_config"]),
                parameter_grid=dict(payload["parameter_grid"]),
                instruments=list(prepared_instruments),
                start_date=str(payload["start_date"]),
                end_date=str(payload["end_date"]),
                data_path=settings.nautilus_catalog_root,
                objective=str(payload.get("objective") or "sharpe"),
                max_parallelism=allocated_parallelism,
                search_strategy=str(payload.get("search_strategy") or "auto"),
                study_key=str(payload.get("study_name")) if payload.get("study_name") else None,
                stage_fractions=payload.get("stage_fractions"),
                reduction_factor=int(payload.get("reduction_factor") or 2),
                min_trades=int(payload["min_trades"]) if payload.get("min_trades") is not None else None,
                require_positive_return=bool(payload.get("require_positive_return") or False),
                holdout_fraction=float(payload["holdout_fraction"]) if payload.get("holdout_fraction") is not None else None,
                holdout_days=int(payload["holdout_days"]) if payload.get("holdout_days") is not None else None,
                purge_days=int(payload.get("purge_days") or 0),
                instruments_prepared=True,
                progress_callback=_progress_callback,
            )
        elif job_type == "walk_forward":
            report = await asyncio.to_thread(
                engine.run_walk_forward,
                strategy_path=str(payload["strategy_path"]),
                base_config=dict(payload["base_config"]),
                parameter_grid=dict(payload["parameter_grid"]),
                instruments=list(prepared_instruments),
                start_date=date.fromisoformat(str(payload["start_date"])),
                end_date=date.fromisoformat(str(payload["end_date"])),
                train_days=int(payload["train_days"]),
                test_days=int(payload["test_days"]),
                step_days=int(payload["step_days"]) if payload.get("step_days") is not None else None,
                mode=str(payload.get("mode") or "rolling"),
                data_path=settings.nautilus_catalog_root,
                objective=str(payload.get("objective") or "sharpe"),
                max_parallelism=allocated_parallelism,
                search_strategy=str(payload.get("search_strategy") or "auto"),
                study_key=str(payload.get("study_name")) if payload.get("study_name") else None,
                stage_fractions=payload.get("stage_fractions"),
                reduction_factor=int(payload.get("reduction_factor") or 2),
                min_trades=int(payload["min_trades"]) if payload.get("min_trades") is not None else None,
                require_positive_return=bool(payload.get("require_positive_return") or False),
                holdout_fraction=float(payload["holdout_fraction"]) if payload.get("holdout_fraction") is not None else None,
                holdout_days=int(payload["holdout_days"]) if payload.get("holdout_days") is not None else None,
                purge_days=int(payload.get("purge_days") or 0),
                instruments_prepared=True,
                progress_callback=_progress_callback,
            )
        else:
            raise ValueError(f"Unsupported research job type: {job_type}")

        report_path = await asyncio.to_thread(
            engine.save_report,
            report,
            settings.research_root / f"{job_id}.json",
        )
        _, summary = artifact_service.load_report_detail(report_path.stem)
        job_service.mark_completed(
            job_id,
            report_id=report_path.stem,
            report_summary=summary.model_dump(),
        )
    except ResearchJobCancelledError:
        job_service.mark_cancelled(job_id)
    except ComputeSlotUnavailableError as exc:
        logger.exception("research_job_slots_unavailable", job_id=job_id, error=str(exc))
        alerting_service.send_alert(
            "error",
            "Research job waiting for compute slots timed out",
            f"job_id={job_id} job_type={job_type} strategy={payload.get('strategy_name')} error={exc}",
        )
        job_service.mark_failed(job_id, error_message=str(exc))
    except Exception as exc:
        logger.exception("research_job_failed", job_id=job_id, error=str(exc))
        alerting_service.send_alert(
            "error",
            "Research job failed",
            f"job_id={job_id} job_type={job_type} strategy={payload.get('strategy_name')} error={exc}",
        )
        job_service.mark_failed(job_id, error_message=str(exc))
    finally:
        stop_heartbeat.set()
        heartbeat_task = locals().get("heartbeat_task")
        if heartbeat_task is not None:
            await heartbeat_task
        if slot_lease_id is not None:
            await release_compute_slots(slot_pool, slot_lease_id)
