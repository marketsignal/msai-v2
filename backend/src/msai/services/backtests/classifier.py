"""Classify worker exceptions into a structured failure record.

Called by ``workers/backtest_job.py::_mark_backtest_failed`` at the
moment of failure, once per run. See PRD
``docs/prds/backtest-failure-surfacing.md`` for the contract.

``BacktestRunner.run()`` at
``backend/src/msai/services/nautilus/backtest_runner.py:239`` wraps any
child-process exception as ``RuntimeError(str(traceback))``. That means
``ImportError`` / ``SyntaxError`` / ``ValueError`` / etc. do NOT reach
this classifier as their real types when they fire inside the backtest
subprocess — they arrive as ``RuntimeError`` whose ``str()`` is the
full formatted traceback. We therefore peek at the message text to
recover STRATEGY_IMPORT_ERROR vs ENGINE_CRASH. ``FileNotFoundError``
and ``TimeoutError`` DO reach the classifier directly because they
fire in the worker's outer code path (``ensure_catalog_data`` and
``asyncio.to_thread(..., timeout)`` respectively).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from msai.schemas.backtest import Remediation
from msai.services.backtests.failure_code import FailureCode
from msai.services.backtests.sanitize import sanitize_public_message

if TYPE_CHECKING:
    from datetime import date


@dataclass(frozen=True, slots=True)
class FailureClassification:
    """Structured classifier output.

    Small dataclass beats a 4-tuple: named access,
    type-safe for callers, trivial to extend (e.g. an ``alert_level``
    field later) without breaking call-sites.
    """

    code: FailureCode
    public_message: str
    suggested_action: str | None
    remediation: Remediation | None


# Worker message shape for the common missing-data path:
#   "No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES. Run..."
_MISSING_DATA_RE = re.compile(
    r"No raw Parquet files found for '([^']+)' under /app/data/parquet/([^/]+)/"
)

# Patterns that indicate a user-strategy load/parse error
# even when wrapped by BacktestRunner's RuntimeError(traceback) layer.
# NameError is KEPT — the vast majority of real-world
# NameError traces are either module-level evaluation failures or
# first-bar references to an unresolved helper, both of which are
# strategy code defects. Engine_crash is reserved for Nautilus / engine
# internals failures, not user-code bugs.
_IMPORT_ERROR_TOKENS = re.compile(
    r"\b(ImportError|ModuleNotFoundError|SyntaxError|NameError)\b",
)


def classify_worker_failure(
    exc: BaseException,
    *,
    instruments: list[str],
    start_date: date,
    end_date: date,
    asset_class: str | None = None,
) -> FailureClassification:
    """Classify + describe a worker-side backtest failure.

    ``public_message`` is always non-empty — US-006 guarantees.
    ``suggested_action`` + ``remediation`` are populated only for
    MISSING_DATA in this PR.

    ``asset_class``: caller-known classification ("stocks" / "futures" /
    "options" / ...). The worker always has this from the config. Passed
    in because the regex-recovered asset_class only works when
    ``settings.parquet_root`` is ``/app/data/parquet`` (container default)
    — local dev with a different parquet_root would lose it, and the
    remediation command becomes generic instead of actionable.

    **Known limitation (Phase 5 iter-2, deferred):** the UI's Run Backtest
    form does not currently send ``config.asset_class``; the worker defaults
    to ``"stocks"``. So for a futures-backtest launched via UI against a
    symbol like ``ES.n.0``, the remediation command will incorrectly say
    ``msai ingest stocks ES.n.0 ...`` instead of ``msai ingest futures``.
    The user still sees that data is missing and what symbols to ingest —
    the only wrong part is the ``stocks`` positional argument. Follow-up
    PR: either add an asset_class dropdown to the UI, or derive it
    server-side from the resolved canonical instrument ID shape.
    """
    raw_message = str(exc) or exc.__class__.__name__

    # --- Missing data (outer worker path — FileNotFoundError raised by
    #     ensure_catalog_data before the BacktestRunner subprocess spawns).
    m = _MISSING_DATA_RE.search(raw_message)
    if isinstance(exc, FileNotFoundError) or m is not None:
        public_msg = sanitize_public_message(raw_message) or "Backtest data missing"
        # Prefer caller-supplied asset_class (always accurate); fall back to
        # regex capture for the container-default path shape.
        resolved_asset_class = asset_class or (m.group(2) if m else None)
        # Symbols for both the CLI command string and the structured
        # Remediation.symbols field — prefer the user-submitted list so
        # the command echoes exactly what they asked for; fall back to the
        # regex capture only when no instruments were bound.
        symbols_for_cmd = list(instruments) if instruments else ([m.group(1)] if m else [])
        if symbols_for_cmd and resolved_asset_class:
            action = (
                f"Run: msai ingest {resolved_asset_class} "
                f"{','.join(symbols_for_cmd)} "
                f"{start_date.isoformat()} {end_date.isoformat()}"
            )
        else:
            action = (
                "Run the data ingestion pipeline for the missing symbol(s) "
                "before re-running this backtest."
            )
        remediation = Remediation(
            kind="ingest_data",
            symbols=symbols_for_cmd,
            asset_class=resolved_asset_class,
            start_date=start_date,
            end_date=end_date,
            auto_available=False,  # MVP — follow-up PR flips this
        )
        return FailureClassification(
            code=FailureCode.MISSING_DATA,
            public_message=public_msg,
            suggested_action=action,
            remediation=remediation,
        )

    # --- Timeout (outer wrapper from asyncio.to_thread(..., timeout))
    if isinstance(exc, TimeoutError):
        public_msg = sanitize_public_message(raw_message) or "Backtest wall-clock timeout"
        return FailureClassification(
            code=FailureCode.TIMEOUT,
            public_message=public_msg,
            suggested_action=None,
            remediation=None,
        )

    # --- Strategy import error (direct OR wrapped)
    #
    # BacktestRunner wraps subprocess exceptions as RuntimeError(str(tb)).
    # We recognize import/syntax failures in the text.
    is_direct = isinstance(exc, (ImportError, SyntaxError, ModuleNotFoundError, NameError))
    is_wrapped_import = isinstance(exc, RuntimeError) and bool(
        _IMPORT_ERROR_TOKENS.search(raw_message)
    )
    if is_direct or is_wrapped_import:
        public_msg = sanitize_public_message(raw_message) or "Strategy module failed to import"
        return FailureClassification(
            code=FailureCode.STRATEGY_IMPORT_ERROR,
            public_message=public_msg,
            suggested_action=None,
            remediation=None,
        )

    # --- Engine crash (any RuntimeError we DIDN'T match as an import error)
    if isinstance(exc, RuntimeError):
        public_msg = (
            sanitize_public_message(raw_message)
            or "Backtest engine crashed; see server logs for details"
        )
        return FailureClassification(
            code=FailureCode.ENGINE_CRASH,
            public_message=public_msg,
            suggested_action=None,
            remediation=None,
        )

    # --- Unknown (truly unmatched — KeyboardInterrupt, CancelledError, etc.)
    public_msg = sanitize_public_message(raw_message) or (
        f"Backtest failed with {exc.__class__.__name__} (see server logs for details)"
    )
    return FailureClassification(
        code=FailureCode.UNKNOWN,
        public_message=public_msg,
        suggested_action=None,
        remediation=None,
    )
