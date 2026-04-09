from msai.services.data_ingestion import run_ingest
from msai.workers.backtest_job import run_backtest
from msai.workers.backtest_settings import BacktestWorkerSettings
from msai.workers.ingest_settings import IngestWorkerSettings
from msai.workers.live_settings import LiveWorkerSettings
from msai.workers.portfolio_job import run_portfolio_job
from msai.workers.portfolio_settings import PortfolioWorkerSettings
from msai.workers.research_job import run_research_job
from msai.workers.research_settings import ResearchWorkerSettings


def test_worker_functions_registered() -> None:
    assert run_backtest in BacktestWorkerSettings.functions
    assert run_ingest in IngestWorkerSettings.functions
    assert run_research_job in ResearchWorkerSettings.functions
    assert run_portfolio_job in PortfolioWorkerSettings.functions


def test_worker_settings_register_lifecycle_hooks() -> None:
    assert callable(ResearchWorkerSettings.on_startup)
    assert callable(ResearchWorkerSettings.on_shutdown)
    assert callable(BacktestWorkerSettings.on_startup)
    assert callable(IngestWorkerSettings.on_startup)
    assert callable(PortfolioWorkerSettings.on_startup)
    assert callable(LiveWorkerSettings.on_startup)
    assert ResearchWorkerSettings.ctx["worker_role"] == "research-worker"
    assert ResearchWorkerSettings.ctx["queue_name"]
