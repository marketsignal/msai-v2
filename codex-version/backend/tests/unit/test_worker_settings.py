from msai.services.data_ingestion import run_ingest
from msai.workers.backtest_job import run_backtest
from msai.workers.settings import WorkerSettings


def test_worker_functions_registered() -> None:
    assert run_backtest in WorkerSettings.functions
    assert run_ingest in WorkerSettings.functions
