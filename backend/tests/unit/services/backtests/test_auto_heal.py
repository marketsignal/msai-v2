"""Tests for the auto-heal orchestrator (Task B7).

Exercises :func:`run_auto_heal` end-to-end with mocked arq pool, ingest
enqueue, SecurityMaster, and ``verify_catalog_coverage``. The
orchestrator is the one piece that ties B2-B6 together, so these tests
lean on AsyncMock / MagicMock rather than spinning up real infra.

Covers the 8 scenarios enumerated in Task B7's plan section:

1. Happy path — enqueue, poll, coverage pass → SUCCESS.
2. Guardrail rejection (``asset_class="options"``) → GUARDRAIL_REJECTED.
3. Dedupe lock held by a prior caller → polls existing job, no enqueue.
4. Wall-clock cap tripped → TIMEOUT.
5. ``ingest_job.result()`` raises → INGEST_FAILED.
6. Coverage re-check still finds gaps → COVERAGE_STILL_MISSING.
7. Structured log events fire in order on the happy path.
8. Lua CAS returns 0 (placeholder lost) — warning logged, poll continues.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing
from arq.jobs import JobStatus

from msai.services.backtests.auto_heal import (
    AutoHealOutcome,
    AutoHealResult,
    run_auto_heal,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_pool(*, set_result: Any = True, eval_result: int = 1) -> MagicMock:
    """Construct a pool mock.

    ``set_result`` / ``eval_result`` are kept for back-compat with tests
    that still reference them, but the orchestrator now routes CAS
    through ``AutoHealLock.compare_and_swap`` (see ``lock_mock``).
    """
    pool = MagicMock()
    pool.set = AsyncMock(return_value=set_result)
    pool.eval = AsyncMock(return_value=eval_result)
    return pool


def _make_ingest_job(
    *,
    job_id: str = "ingest-job-123",
    status_sequence: list[JobStatus] | None = None,
    result_exc: BaseException | None = None,
    result_return: Any = None,
) -> MagicMock:
    """Build a mock arq Job whose ``status()`` cycles through a sequence.

    Once the sequence is exhausted the final value repeats — lets tests
    assert "always in_progress" by passing a single-element list.
    """
    seq = status_sequence or [JobStatus.in_progress, JobStatus.complete]
    job = MagicMock()
    job.job_id = job_id
    idx = {"i": 0}

    async def _status() -> JobStatus:
        i = min(idx["i"], len(seq) - 1)
        idx["i"] += 1
        return seq[i]

    async def _result(timeout: float = 5.0) -> Any:
        if result_exc is not None:
            raise result_exc
        return result_return

    job.status = _status
    job.result = _result
    return job


@pytest.fixture
def fast_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Shrink poll / wall-clock so tests run in under a second."""
    from msai.core.config import settings

    monkeypatch.setattr(settings, "auto_heal_wall_clock_cap_seconds", 2)
    monkeypatch.setattr(settings, "auto_heal_poll_interval_seconds", 0)  # near-zero sleep
    yield


# Common inputs for most tests.
_INSTRUMENTS = ["AAPL", "MSFT"]
_START = date(2024, 1, 1)
_END = date(2024, 3, 31)
_CATALOG_ROOT = Path("/tmp/catalog")  # never actually touched — coverage is mocked


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    asset_class: str = "stocks",
    resolved_ids: list[str] | None = None,
    coverage_gaps: list[tuple[str, list[tuple[int, int]]]] | None = None,
    phase_update: AsyncMock | None = None,
) -> dict[str, AsyncMock | MagicMock]:
    """Patch the collaborators of ``run_auto_heal`` and return the mocks."""
    derive_mock = AsyncMock(return_value=asset_class)
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.derive_asset_class",
        derive_mock,
    )

    # Async session factory → returns a mock async context manager whose
    # __aenter__ returns a MagicMock "session".
    fake_db = MagicMock()

    class _FakeSessionCtx:
        async def __aenter__(self) -> MagicMock:
            return fake_db

        async def __aexit__(self, *exc: Any) -> None:
            return None

    session_factory = MagicMock(side_effect=lambda: _FakeSessionCtx())
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.async_session_factory",
        session_factory,
    )

    # Mock SecurityMaster constructor at call site (coverage re-check)
    master_instance = MagicMock()
    resolved = resolved_ids if resolved_ids is not None else list(_INSTRUMENTS)
    master_instance.resolve_for_backtest = AsyncMock(return_value=resolved)
    master_cls = MagicMock(return_value=master_instance)
    monkeypatch.setattr(
        "msai.services.nautilus.security_master.service.SecurityMaster",
        master_cls,
    )

    # verify_catalog_coverage
    gaps = coverage_gaps if coverage_gaps is not None else [(iid, []) for iid in resolved]
    coverage_mock = MagicMock(return_value=gaps)
    monkeypatch.setattr(
        "msai.services.nautilus.catalog_builder.verify_catalog_coverage",
        coverage_mock,
    )

    # Phase update — optional override, else a simple AsyncMock.
    phase_mock = phase_update or AsyncMock(return_value=None)
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal._set_backtest_phase",
        phase_mock,
    )

    return {
        "derive": derive_mock,
        "session_factory": session_factory,
        "master_cls": master_cls,
        "master": master_instance,
        "coverage": coverage_mock,
        "phase": phase_mock,
    }


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_enqueues_ingest_updates_phase_polls_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    fast_settings: None,
) -> None:
    mocks = _patch_common(monkeypatch)

    pool = _make_pool(eval_result=1)
    # Lock acquired (NX SET returns True).
    lock_mock = MagicMock()
    lock_mock.try_acquire = AsyncMock(return_value=True)
    lock_mock.get_holder = AsyncMock(return_value="ingest-job-123")
    lock_mock.release = AsyncMock(return_value=None)
    lock_mock.compare_and_swap = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.AutoHealLock",
        MagicMock(return_value=lock_mock),
    )

    ingest_job = _make_ingest_job(
        status_sequence=[JobStatus.in_progress, JobStatus.complete],
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.enqueue_ingest",
        AsyncMock(return_value=ingest_job),
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.Job",
        MagicMock(return_value=ingest_job),
    )

    result = await run_auto_heal(
        backtest_id="bt-1",
        instruments=_INSTRUMENTS,
        start=_START,
        end=_END,
        catalog_root=_CATALOG_ROOT,
        caller_asset_class_hint=None,
        pool=pool,
    )

    assert result.outcome == AutoHealOutcome.SUCCESS
    assert result.asset_class == "stocks"
    assert result.resolved_instrument_ids == _INSTRUMENTS
    # Phase is set at the start and cleared at the end (2 calls minimum).
    assert mocks["phase"].await_count >= 2
    # First call sets phase=awaiting_data.
    first_kwargs = mocks["phase"].await_args_list[0].kwargs
    assert first_kwargs["phase"] == "awaiting_data"
    # Last call clears phase.
    last_kwargs = mocks["phase"].await_args_list[-1].kwargs
    assert last_kwargs["phase"] is None


# ---------------------------------------------------------------------------
# 2. Guardrail rejection
# ---------------------------------------------------------------------------


async def test_guardrail_rejection_does_not_enqueue_and_clears_phase(
    monkeypatch: pytest.MonkeyPatch,
    fast_settings: None,
) -> None:
    mocks = _patch_common(monkeypatch, asset_class="options")

    pool = _make_pool()
    enqueue_mock = AsyncMock()
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.enqueue_ingest",
        enqueue_mock,
    )

    with structlog.testing.capture_logs() as captured:
        result = await run_auto_heal(
            backtest_id="bt-2",
            instruments=["SPY_CALL_400.OPRA"],
            start=_START,
            end=_END,
            catalog_root=_CATALOG_ROOT,
            caller_asset_class_hint=None,
            pool=pool,
        )

    assert result.outcome == AutoHealOutcome.GUARDRAIL_REJECTED
    assert result.asset_class == "options"
    enqueue_mock.assert_not_awaited()
    assert any(e["event"] == "backtest_auto_heal_guardrail_rejected" for e in captured)
    # Guardrail rejection returns before phase is ever set to awaiting_data
    # — no phase row to clear. The finally block only runs _set_backtest_phase
    # on the post-enqueue path where we actually touched the row.
    assert mocks["phase"].await_count == 0


# ---------------------------------------------------------------------------
# 3. Dedupe — lock already held by prior caller
# ---------------------------------------------------------------------------


async def test_dedupe_lock_already_held_waits_for_existing_holder(
    monkeypatch: pytest.MonkeyPatch,
    fast_settings: None,
) -> None:
    _patch_common(monkeypatch)

    pool = _make_pool()
    lock_mock = MagicMock()
    lock_mock.try_acquire = AsyncMock(return_value=False)  # already held
    # Existing holder value IS the prior job id (not a "reserving:" placeholder).
    lock_mock.get_holder = AsyncMock(return_value="existing-ingest-job-xyz")
    lock_mock.release = AsyncMock(return_value=None)
    lock_mock.compare_and_swap = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.AutoHealLock",
        MagicMock(return_value=lock_mock),
    )

    enqueue_mock = AsyncMock()
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.enqueue_ingest",
        enqueue_mock,
    )

    ingest_job = _make_ingest_job(
        job_id="existing-ingest-job-xyz",
        status_sequence=[JobStatus.complete],
    )
    job_cls = MagicMock(return_value=ingest_job)
    monkeypatch.setattr("msai.services.backtests.auto_heal.Job", job_cls)

    result = await run_auto_heal(
        backtest_id="bt-3",
        instruments=_INSTRUMENTS,
        start=_START,
        end=_END,
        catalog_root=_CATALOG_ROOT,
        caller_asset_class_hint=None,
        pool=pool,
    )

    assert result.outcome == AutoHealOutcome.SUCCESS
    enqueue_mock.assert_not_awaited()
    # Job() was constructed with the existing holder's id.
    job_cls.assert_called_once()
    _, kwargs = job_cls.call_args
    # positional first arg is job id
    assert job_cls.call_args.args[0] == "existing-ingest-job-xyz"


# ---------------------------------------------------------------------------
# 4. Timeout
# ---------------------------------------------------------------------------


async def test_wall_clock_cap_transitions_to_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from msai.core.config import settings

    monkeypatch.setattr(settings, "auto_heal_wall_clock_cap_seconds", 1)
    monkeypatch.setattr(settings, "auto_heal_poll_interval_seconds", 0)

    _patch_common(monkeypatch)

    pool = _make_pool()
    lock_mock = MagicMock()
    lock_mock.try_acquire = AsyncMock(return_value=True)
    lock_mock.get_holder = AsyncMock(return_value="ingest-slow")
    lock_mock.release = AsyncMock(return_value=None)
    lock_mock.compare_and_swap = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.AutoHealLock",
        MagicMock(return_value=lock_mock),
    )

    ingest_job = _make_ingest_job(
        job_id="ingest-slow",
        status_sequence=[JobStatus.in_progress],  # never completes
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.enqueue_ingest",
        AsyncMock(return_value=ingest_job),
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.Job",
        MagicMock(return_value=ingest_job),
    )

    with structlog.testing.capture_logs() as captured:
        result = await run_auto_heal(
            backtest_id="bt-4",
            instruments=_INSTRUMENTS,
            start=_START,
            end=_END,
            catalog_root=_CATALOG_ROOT,
            caller_asset_class_hint=None,
            pool=pool,
        )

    assert result.outcome == AutoHealOutcome.TIMEOUT
    assert any(e["event"] == "backtest_auto_heal_timeout" for e in captured)


# ---------------------------------------------------------------------------
# 5. Ingest result raises → INGEST_FAILED
# ---------------------------------------------------------------------------


async def test_ingest_job_result_raises_returns_ingest_failed(
    monkeypatch: pytest.MonkeyPatch,
    fast_settings: None,
) -> None:
    _patch_common(monkeypatch)

    pool = _make_pool()
    lock_mock = MagicMock()
    lock_mock.try_acquire = AsyncMock(return_value=True)
    lock_mock.get_holder = AsyncMock(return_value="ingest-bad")
    lock_mock.release = AsyncMock(return_value=None)
    lock_mock.compare_and_swap = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.AutoHealLock",
        MagicMock(return_value=lock_mock),
    )

    ingest_job = _make_ingest_job(
        job_id="ingest-bad",
        status_sequence=[JobStatus.complete],
        result_exc=RuntimeError("provider rate-limited"),
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.enqueue_ingest",
        AsyncMock(return_value=ingest_job),
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.Job",
        MagicMock(return_value=ingest_job),
    )

    result = await run_auto_heal(
        backtest_id="bt-5",
        instruments=_INSTRUMENTS,
        start=_START,
        end=_END,
        catalog_root=_CATALOG_ROOT,
        caller_asset_class_hint=None,
        pool=pool,
    )

    assert result.outcome == AutoHealOutcome.INGEST_FAILED


# ---------------------------------------------------------------------------
# 6. Coverage still missing
# ---------------------------------------------------------------------------


async def test_coverage_still_missing_after_ingest_returns_partial_gap(
    monkeypatch: pytest.MonkeyPatch,
    fast_settings: None,
) -> None:
    gaps = [("AAPL.NASDAQ", [(1_700_000_000_000_000_000, 1_700_000_060_000_000_000)])]
    _patch_common(
        monkeypatch,
        resolved_ids=["AAPL.NASDAQ", "MSFT.NASDAQ"],
        coverage_gaps=gaps,
    )

    pool = _make_pool()
    lock_mock = MagicMock()
    lock_mock.try_acquire = AsyncMock(return_value=True)
    lock_mock.get_holder = AsyncMock(return_value="ingest-partial")
    lock_mock.release = AsyncMock(return_value=None)
    lock_mock.compare_and_swap = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.AutoHealLock",
        MagicMock(return_value=lock_mock),
    )

    ingest_job = _make_ingest_job(
        job_id="ingest-partial",
        status_sequence=[JobStatus.complete],
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.enqueue_ingest",
        AsyncMock(return_value=ingest_job),
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.Job",
        MagicMock(return_value=ingest_job),
    )

    result = await run_auto_heal(
        backtest_id="bt-6",
        instruments=_INSTRUMENTS,
        start=_START,
        end=_END,
        catalog_root=_CATALOG_ROOT,
        caller_asset_class_hint=None,
        pool=pool,
    )

    assert result.outcome == AutoHealOutcome.COVERAGE_STILL_MISSING
    assert result.gaps == gaps


# ---------------------------------------------------------------------------
# 7. Structured log event order on happy path
# ---------------------------------------------------------------------------


async def test_happy_path_emits_all_structured_log_events(
    monkeypatch: pytest.MonkeyPatch,
    fast_settings: None,
) -> None:
    _patch_common(monkeypatch)

    pool = _make_pool(eval_result=1)
    lock_mock = MagicMock()
    lock_mock.try_acquire = AsyncMock(return_value=True)
    lock_mock.get_holder = AsyncMock(return_value="ingest-log")
    lock_mock.release = AsyncMock(return_value=None)
    lock_mock.compare_and_swap = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.AutoHealLock",
        MagicMock(return_value=lock_mock),
    )

    ingest_job = _make_ingest_job(
        job_id="ingest-log",
        status_sequence=[JobStatus.complete],
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.enqueue_ingest",
        AsyncMock(return_value=ingest_job),
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.Job",
        MagicMock(return_value=ingest_job),
    )

    with structlog.testing.capture_logs() as captured:
        result = await run_auto_heal(
            backtest_id="bt-7",
            instruments=_INSTRUMENTS,
            start=_START,
            end=_END,
            catalog_root=_CATALOG_ROOT,
            caller_asset_class_hint=None,
            pool=pool,
        )

    assert result.outcome == AutoHealOutcome.SUCCESS
    # Extract events in order.
    expected_events_in_order = [
        "backtest_auto_heal_started",
        "backtest_auto_heal_ingest_enqueued",
        "backtest_auto_heal_ingest_completed",
        "backtest_auto_heal_completed",
    ]
    observed = [e["event"] for e in captured if e["event"] in expected_events_in_order]
    assert observed == expected_events_in_order


# ---------------------------------------------------------------------------
# 8. Lua CAS lost → warning + continues
# ---------------------------------------------------------------------------


async def test_cas_lost_logs_warning_but_continues_polling(
    monkeypatch: pytest.MonkeyPatch,
    fast_settings: None,
) -> None:
    _patch_common(monkeypatch)

    pool = _make_pool(eval_result=0)
    lock_mock = MagicMock()
    lock_mock.try_acquire = AsyncMock(return_value=True)
    lock_mock.get_holder = AsyncMock(return_value="someone-elses-id")
    lock_mock.release = AsyncMock(return_value=None)
    # CAS failed — placeholder expired and another caller grabbed the lock.
    lock_mock.compare_and_swap = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.AutoHealLock",
        MagicMock(return_value=lock_mock),
    )

    ingest_job = _make_ingest_job(
        job_id="ingest-cas",
        status_sequence=[JobStatus.complete],
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.enqueue_ingest",
        AsyncMock(return_value=ingest_job),
    )
    monkeypatch.setattr(
        "msai.services.backtests.auto_heal.Job",
        MagicMock(return_value=ingest_job),
    )

    with structlog.testing.capture_logs() as captured:
        result = await run_auto_heal(
            backtest_id="bt-8",
            instruments=_INSTRUMENTS,
            start=_START,
            end=_END,
            catalog_root=_CATALOG_ROOT,
            caller_asset_class_hint=None,
            pool=pool,
        )

    # CAS loss logs a warning but polling still proceeds; catalog coverage
    # is the real gate, and our patched coverage returns empty gaps.
    assert result.outcome == AutoHealOutcome.SUCCESS
    assert any(e["event"] == "auto_heal_lock_cas_lost" for e in captured)


# ---------------------------------------------------------------------------
# Bonus: AutoHealResult / AutoHealOutcome surface
# ---------------------------------------------------------------------------


def test_autoheal_outcome_string_values() -> None:
    assert AutoHealOutcome.SUCCESS.value == "success"
    assert AutoHealOutcome.GUARDRAIL_REJECTED.value == "guardrail_rejected"
    assert AutoHealOutcome.TIMEOUT.value == "timeout"
    assert AutoHealOutcome.INGEST_FAILED.value == "ingest_failed"
    assert AutoHealOutcome.COVERAGE_STILL_MISSING.value == "coverage_still_missing"


def test_autoheal_result_is_frozen() -> None:
    r = AutoHealResult(
        outcome=AutoHealOutcome.SUCCESS,
        asset_class="stocks",
        resolved_instrument_ids=["AAPL"],
        reason_human=None,
    )
    with pytest.raises((AttributeError, Exception)):
        r.asset_class = "futures"  # type: ignore[misc]


def test_auto_heal_result_rejects_inconsistent_outcome_and_gaps() -> None:
    """__post_init__ enforces outcome-dependent field invariants."""
    # Non-COVERAGE outcome carrying gaps → reject.
    with pytest.raises(ValueError, match="gaps must be None for outcome="):
        AutoHealResult(
            outcome=AutoHealOutcome.SUCCESS,
            asset_class="stocks",
            resolved_instrument_ids=["AAPL"],
            reason_human=None,
            gaps=[("AAPL", [])],
        )
    # SUCCESS with reason_human set → reject.
    with pytest.raises(ValueError, match="SUCCESS must have reason_human=None"):
        AutoHealResult(
            outcome=AutoHealOutcome.SUCCESS,
            asset_class="stocks",
            resolved_instrument_ids=["AAPL"],
            reason_human="should be None",
        )
    # Non-SUCCESS without reason_human → reject.
    with pytest.raises(ValueError, match="requires reason_human to be set"):
        AutoHealResult(
            outcome=AutoHealOutcome.TIMEOUT,
            asset_class="stocks",
            resolved_instrument_ids=None,
            reason_human=None,
        )


def test_coverage_still_missing_allows_none_gaps() -> None:
    """COVERAGE_STILL_MISSING with gaps=None is valid (means verification errored)."""
    result = AutoHealResult(
        outcome=AutoHealOutcome.COVERAGE_STILL_MISSING,
        asset_class="stocks",
        resolved_instrument_ids=["AAPL"],
        reason_human="verification errored",
        gaps=None,
    )
    assert result.outcome == AutoHealOutcome.COVERAGE_STILL_MISSING
    assert result.gaps is None
