"""Structured failure classification for the backtest worker.

Mirrors the pattern established in
``backend/src/msai/services/live/failure_kind.py``: classify at
write time, persist a stable enum on the row, read through
:meth:`parse_or_unknown` to handle NULL + unrecognized historical
values safely.
"""

from __future__ import annotations

from enum import StrEnum


class FailureCode(StrEnum):
    """Why a backtest row reached terminal ``status == 'failed'``."""

    MISSING_DATA = "missing_data"
    """No raw Parquet files found for one or more requested symbols."""

    STRATEGY_IMPORT_ERROR = "strategy_import_error"
    """The strategy's Python file failed to import (syntax / ImportError)."""

    ENGINE_CRASH = "engine_crash"
    """NautilusTrader subprocess raised during ``node.run()`` (not in
    startup, not in data-load). Usually a bug in user strategy code."""

    TIMEOUT = "timeout"
    """arq job timeout (wall-clock) fired. The inner work may have been
    proceeding fine — we just exceeded the per-job ceiling."""

    UNKNOWN = "unknown"
    """Fallback for historical rows (which carry ``error_code='unknown'``
    via the migration's DDL ``server_default``) and for failures the
    classifier couldn't match. Writers should use a specific code; an
    UNKNOWN write is a classifier bug to fix, not an OK state."""

    @classmethod
    def parse_or_unknown(cls, value: str | None) -> FailureCode:
        """Null-safe read path for pre-migration rows."""
        if value is None:
            return cls.UNKNOWN
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN
