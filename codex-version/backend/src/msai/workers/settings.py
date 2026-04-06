from __future__ import annotations

from msai.core.config import settings
from msai.services.data_ingestion import run_ingest
from msai.workers.backtest_job import run_backtest


class WorkerSettings:
    functions = [run_backtest, run_ingest]
    max_jobs = settings.max_worker_jobs
    job_timeout = max(settings.backtest_timeout_seconds, settings.ingestion_timeout_seconds)
    max_tries = settings.queue_retry_attempts + 1
