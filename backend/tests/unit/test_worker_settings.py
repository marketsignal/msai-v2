"""Unit tests for arq worker settings modules.

Verifies that each worker settings class can be imported and exposes the
attributes arq requires: ``functions``, ``redis_settings``, ``queue_name``.
"""

from __future__ import annotations

import pytest


class TestResearchWorkerSettings:
    """Tests for ResearchWorkerSettings."""

    def test_import_and_has_functions(self) -> None:
        from msai.workers.research_settings import ResearchWorkerSettings

        assert hasattr(ResearchWorkerSettings, "functions")
        assert len(ResearchWorkerSettings.functions) >= 1

    def test_has_redis_settings(self) -> None:
        from msai.workers.research_settings import ResearchWorkerSettings

        assert hasattr(ResearchWorkerSettings, "redis_settings")
        assert ResearchWorkerSettings.redis_settings is not None

    def test_has_queue_name(self) -> None:
        from msai.workers.research_settings import ResearchWorkerSettings

        assert hasattr(ResearchWorkerSettings, "queue_name")
        assert isinstance(ResearchWorkerSettings.queue_name, str)
        assert len(ResearchWorkerSettings.queue_name) > 0


class TestPortfolioWorkerSettings:
    """Tests for portfolio WorkerSettings."""

    def test_import_and_has_functions(self) -> None:
        from msai.workers.portfolio_settings import WorkerSettings

        assert hasattr(WorkerSettings, "functions")
        assert len(WorkerSettings.functions) >= 1

    def test_has_redis_settings(self) -> None:
        from msai.workers.portfolio_settings import WorkerSettings

        assert hasattr(WorkerSettings, "redis_settings")
        assert WorkerSettings.redis_settings is not None


class TestIngestWorkerSettings:
    """Tests for IngestWorkerSettings."""

    def test_import_and_has_functions(self) -> None:
        from msai.workers.ingest_settings import IngestWorkerSettings

        assert hasattr(IngestWorkerSettings, "functions")
        assert len(IngestWorkerSettings.functions) >= 1

    def test_has_redis_settings(self) -> None:
        from msai.workers.ingest_settings import IngestWorkerSettings

        assert hasattr(IngestWorkerSettings, "redis_settings")
        assert IngestWorkerSettings.redis_settings is not None

    def test_has_queue_name(self) -> None:
        from msai.workers.ingest_settings import IngestWorkerSettings

        assert hasattr(IngestWorkerSettings, "queue_name")
        assert IngestWorkerSettings.queue_name == "msai:ingest"

    def test_max_jobs_is_one(self) -> None:
        from msai.workers.ingest_settings import IngestWorkerSettings

        assert IngestWorkerSettings.max_jobs == 1

    def test_job_timeout_is_one_hour(self) -> None:
        from msai.workers.ingest_settings import IngestWorkerSettings

        assert IngestWorkerSettings.job_timeout == 3600
