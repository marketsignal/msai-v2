"""Tests for the backtest failure classifier."""

from __future__ import annotations

from datetime import date

from msai.services.backtests.classifier import (
    FailureClassification,
    classify_worker_failure,
)
from msai.services.backtests.failure_code import FailureCode


class TestClassifyWorkerFailure:
    """``classify_worker_failure(exc, instruments, start_date, end_date)``
    returns a ``FailureClassification`` dataclass. [iter-1 P3] Small struct
    beats a 4-tuple for readability + mis-wire safety.
    """

    def test_missing_data_filenotfounderror_is_classified(self):
        exc = FileNotFoundError(
            "No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES. "
            "Run the data ingestion pipeline for this symbol before backtesting."
        )
        result = classify_worker_failure(
            exc,
            instruments=["ES.n.0"],
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 15),
        )
        assert result.code is FailureCode.MISSING_DATA
        assert "<DATA_ROOT>/parquet/stocks/ES" in result.public_message
        assert "/app/" not in result.public_message  # sanitized
        assert result.suggested_action is not None
        assert "msai ingest" in result.suggested_action
        assert "ES" in result.suggested_action
        assert "2025-01-02" in result.suggested_action
        assert "2025-01-15" in result.suggested_action
        assert result.remediation is not None
        assert result.remediation.kind == "ingest_data"
        assert result.remediation.symbols == ["ES.n.0"]
        # B3: shape-derivation beats the regex-captured path fragment.
        # ``ES.n.0`` is unambiguously a futures continuous symbol — the
        # old assertion expected ``"stocks"`` (the worker default that
        # slipped through pre-B3 because there was no server-authoritative
        # derivation). Closing PR #39's scope-defer flips this to the
        # correct asset_class.
        assert result.remediation.asset_class == "futures"
        assert result.remediation.auto_available is False

    def test_timeout_error_is_classified(self):
        exc = TimeoutError("Backtest exceeded 900s wall clock")
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date(2025, 1, 1), end_date=date(2025, 1, 2)
        )
        assert result.code is FailureCode.TIMEOUT
        assert result.remediation is None

    def test_import_error_as_direct_exception_type(self):
        exc = ImportError("No module named 'strategies.missing'")
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date.today(), end_date=date.today()
        )
        assert result.code is FailureCode.STRATEGY_IMPORT_ERROR

    def test_import_error_wrapped_by_backtest_runner_subprocess(self):
        # [iter-1 P1-c] BacktestRunner.run wraps the child process's
        # exception as ``RuntimeError(traceback_text)`` at line 239 of
        # backtest_runner.py. The classifier MUST peek into the message
        # text to recover STRATEGY_IMPORT_ERROR vs ENGINE_CRASH.
        traceback_text = (
            "Traceback (most recent call last):\n"
            '  File "/app/src/msai/services/nautilus/backtest_runner.py", '
            "line 290, in _run_backtest\n"
            "    strategy_cls = load_strategy_class(strategy_path)\n"
            "ModuleNotFoundError: No module named 'strategies.broken'"
        )
        exc = RuntimeError(traceback_text)
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date.today(), end_date=date.today()
        )
        assert result.code is FailureCode.STRATEGY_IMPORT_ERROR

    def test_syntax_error_wrapped_by_backtest_runner(self):
        traceback_text = (
            "Traceback (most recent call last):\n"
            '  File "strategies/foo.py", line 12\n'
            "    def bad(:\n"
            "           ^\n"
            "SyntaxError: invalid syntax"
        )
        exc = RuntimeError(traceback_text)
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date.today(), end_date=date.today()
        )
        assert result.code is FailureCode.STRATEGY_IMPORT_ERROR

    def test_generic_runtime_error_is_engine_crash(self):
        traceback_text = (
            "Traceback (most recent call last):\n"
            '  File "/app/.venv/lib/python3.12/site-packages/nautilus_trader/...", '
            "line 500, in _run\n"
            '    raise ValueError("bar_type mismatch")\n'
            "ValueError: bar_type mismatch"
        )
        exc = RuntimeError(traceback_text)
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date.today(), end_date=date.today()
        )
        assert result.code is FailureCode.ENGINE_CRASH

    def test_truly_unknown_falls_back(self):
        exc = KeyboardInterrupt()
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date.today(), end_date=date.today()
        )
        assert result.code is FailureCode.UNKNOWN
        assert result.public_message
        assert result.suggested_action is None
        assert result.remediation is None

    def test_public_message_never_empty(self):
        exc = Exception("")  # pathological — empty message
        result = classify_worker_failure(
            exc, instruments=[], start_date=date.today(), end_date=date.today()
        )
        assert result.public_message  # falls back to a generic string

    def test_caller_asset_class_used_when_regex_would_miss(self):
        # [Phase 5 P1] Local dev with ``parquet_root`` != ``/app/data/parquet``
        # hits a FileNotFoundError whose message does NOT match the container-path
        # regex. The caller (worker) passes ``asset_class`` directly; the
        # remediation must use that, not fall through to a generic hint.
        exc = FileNotFoundError(
            "No raw Parquet files found for 'ES' under /custom/path/futures/ES."
        )
        result = classify_worker_failure(
            exc,
            instruments=["ES.n.0"],
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 15),
            asset_class="futures",  # caller-known override
        )
        assert result.code is FailureCode.MISSING_DATA
        assert result.remediation is not None
        assert result.remediation.asset_class == "futures"
        assert result.suggested_action is not None
        assert "msai ingest futures" in result.suggested_action

    def test_caller_asset_class_overrides_regex_capture(self):
        # If both caller passes asset_class AND regex captures one, caller wins.
        exc = FileNotFoundError(
            "No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES."
        )
        result = classify_worker_failure(
            exc,
            instruments=["ES.n.0"],
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 15),
            asset_class="futures",  # ground truth from worker config
        )
        assert result.remediation is not None
        assert result.remediation.asset_class == "futures"


def test_failure_classification_is_a_dataclass():
    """Sanity check: imported name is accessible and constructable."""
    c = FailureClassification(
        code=FailureCode.UNKNOWN,
        public_message="x",
        suggested_action=None,
        remediation=None,
    )
    assert c.code is FailureCode.UNKNOWN
