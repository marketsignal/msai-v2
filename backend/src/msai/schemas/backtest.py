"""Pydantic schemas for backtest API endpoints."""

from __future__ import annotations

from datetime import (  # noqa: TC003 — Pydantic v2 needs concrete types at model build time
    date,
    datetime,
)
from datetime import date as _date
from typing import Any, Literal
from uuid import UUID  # noqa: TC003 — same reason

from pydantic import BaseModel, Field, field_validator, model_validator


class BacktestRunRequest(BaseModel):
    """Request schema for launching a new backtest."""

    strategy_id: UUID
    config: dict[str, Any]
    instruments: list[str]
    start_date: date
    end_date: date
    smoke: bool = False
    """Tag this row as a deploy-time data-path smoke (Phase 12 of
    ``deploy-on-vm.sh``). Smoke rows are filtered out of
    ``GET /api/v1/backtests/history`` by default and are eligible for
    cleanup by the deploy rollback path. Defaults to ``False`` so normal
    user-initiated backtests never accidentally enter the smoke namespace.
    """


class BacktestStatusResponse(BaseModel):
    """Response schema for backtest status polling."""

    id: UUID
    status: str
    progress: int
    started_at: datetime | None
    completed_at: datetime | None
    error: ErrorEnvelope | None = None
    # Auto-heal lifecycle. When an auto-heal cycle is in flight,
    # ``phase == "awaiting_data"`` and ``progress_message`` carries the
    # user-facing "Downloading ..." text. Both are ``None`` outside heal
    # windows. The endpoint keeps ``response_model_exclude_none=True`` so
    # older clients see no-op-absent behaviour.
    phase: Literal["awaiting_data"] | None = None
    progress_message: str | None = None

    model_config = {"from_attributes": True}


class BacktestResultsResponse(BaseModel):
    """Response schema for GET /api/v1/backtests/{id}/results.

    Aggregate metrics + canonical daily-normalized `series` payload + trade count.
    Trades are no longer inline — see `GET /api/v1/backtests/{id}/trades` for
    paginated fills. `has_report` is derived server-side from
    `Backtest.report_path is not None` + a file-existence check; the raw
    path is not exposed.
    """

    id: UUID
    metrics: dict[str, Any] | None = None
    trade_count: int
    series: SeriesPayload | None = None
    series_status: SeriesStatus = "not_materialized"
    has_report: bool = False

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _series_ready_iff_payload(self) -> BacktestResultsResponse:
        """Enforce ``series_status == 'ready'`` iff ``series is not None``.

        Either direction means something broke upstream:
        * ``ready`` + ``series is None`` — worker wrote a half-transaction
          (status changed but payload didn't). Fail loudly at read-time.
        * non-``ready`` + ``series is not None`` — dead payload lingered
          after a status was rewritten (manual SQL repair, migration slip).
        """
        if self.series_status == "ready" and self.series is None:
            raise ValueError("series_status='ready' requires series payload to be present")
        if self.series_status != "ready" and self.series is not None:
            raise ValueError(
                f"series_status={self.series_status!r} must not carry a series payload"
            )
        return self


class BacktestListItem(BaseModel):
    """Summary schema for a backtest in list responses."""

    id: UUID
    strategy_id: UUID
    status: str
    start_date: date
    end_date: date
    created_at: datetime
    error_code: str | None = None
    error_public_message: str | None = None
    # See :class:`BacktestStatusResponse.phase` for semantics.
    phase: Literal["awaiting_data"] | None = None
    progress_message: str | None = None

    model_config = {"from_attributes": True}


class BacktestListResponse(BaseModel):
    """Paginated list response for backtests."""

    items: list[BacktestListItem]
    total: int


# ---------------------------------------------------------------------------
# Error envelope (failure-surfacing PR)
# ---------------------------------------------------------------------------


class Remediation(BaseModel):
    """Machine-readable remediation metadata.

    MVP-only ``kind == 'ingest_data'`` carries full fields. Other kinds
    stay minimal in this PR; the follow-up auto-ingest PR flips
    ``auto_available`` to ``True`` for the kinds it can handle.

    Keep ``kind`` as ``Literal[...]`` so OpenAPI emits a proper
    ``enum`` and client-side type-narrowing works without loading a
    separate enum module.
    """

    kind: Literal["ingest_data", "contact_support", "retry", "none"]
    symbols: list[str] | None = None
    asset_class: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    auto_available: bool = False


class ErrorEnvelope(BaseModel):
    """Structured failure payload surfaced on `BacktestStatusResponse.error`.

    Deliberately symmetric with the api-design.md error envelope used by
    the 422 path on ``POST /backtests/run`` (see PR #38): same
    ``{code, message, ...}`` top-level shape so UI / CLI can share
    rendering helpers.
    """

    code: str
    message: str
    suggested_action: str | None = None
    remediation: Remediation | None = None


# ---------------------------------------------------------------------------
# Canonical analytics series payload — JSONB contract between the backtest
# worker (writer) and the ``/results`` endpoint + frontend (readers).
# ---------------------------------------------------------------------------


SeriesStatus = Literal["ready", "not_materialized", "failed"]
"""Lifecycle marker for the ``series`` column.

- ``ready``           — ``series`` populated with a valid :class:`SeriesPayload`.
- ``not_materialized`` — legacy rows (pre-migration default) or not yet
  computed. ``series`` column is ``NULL``.
- ``failed``           — the worker attempted to build the payload but hit an
  error; the backtest itself may still be ``completed``. ``series`` column is
  ``NULL``.
"""


class SeriesDailyPoint(BaseModel):
    """One day of the canonical normalized returns series."""

    date: str  # ISO YYYY-MM-DD
    # ``ge=0.0`` (not ``gt``): a total-loss day legitimately produces
    # ``equity == 0.0`` (margin call, halt-to-zero, leveraged wipeout).
    # Negative equity still rejects — no short-base-value convention here.
    equity: float = Field(..., ge=0.0)
    drawdown: float = Field(..., le=0.0)  # non-positive by construction
    daily_return: float

    @field_validator("date")
    @classmethod
    def _validate_iso_date(cls, v: str) -> str:
        _date.fromisoformat(v)  # raises ValueError on bad format
        return v


class SeriesMonthlyReturn(BaseModel):
    """Month-end return aggregate."""

    # Regex enforces month in 01..12 at the pattern layer (visible in OpenAPI);
    # the field validator below catches anything the regex can't express.
    month: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    pct: float

    @field_validator("month")
    @classmethod
    def _validate_month_format(cls, v: str) -> str:
        try:
            _date.fromisoformat(f"{v}-01")
        except ValueError as exc:
            raise ValueError(f"month must be YYYY-MM with MM in 01..12, got {v!r}") from exc
        return v


class SeriesPayload(BaseModel):
    """Canonical analytics payload written by worker, consumed by API + UI."""

    daily: list[SeriesDailyPoint]
    monthly_returns: list[SeriesMonthlyReturn]


# ---------------------------------------------------------------------------
# Signed-URL response
# ---------------------------------------------------------------------------


class BacktestReportTokenResponse(BaseModel):
    """Response for ``POST /api/v1/backtests/{id}/report-token``.

    ``signed_url`` is an absolute path (``/api/v1/backtests/{id}/report?token=...``)
    that the frontend iframe loads verbatim. ``expires_at`` lets the UI proactively
    re-mint before the URL goes cold rather than wait for a 401 bounce.
    """

    signed_url: str
    expires_at: datetime


# ---------------------------------------------------------------------------
# Paginated trade log — ``GET /api/v1/backtests/{id}/trades?page=N&page_size=K``.
# Extracted from ``/results`` because long backtests blew the payload budget.
# page_size ceiling 500, clamped server-side (not 422'd).
# ---------------------------------------------------------------------------


class BacktestTradeItem(BaseModel):
    """One individual Nautilus fill from a backtest."""

    id: UUID
    instrument: str
    # Narrowed to match TS ``"BUY" | "SELL"``; writer uses ``OrderSide.name``.
    side: Literal["BUY", "SELL"]
    quantity: float = Field(..., ge=0.0)
    price: float
    pnl: float
    commission: float = Field(..., ge=0.0)
    executed_at: datetime  # tz-aware — frontend renders in user's locale


class BacktestTradesResponse(BaseModel):
    """Paginated response for ``GET /api/v1/backtests/{id}/trades``."""

    items: list[BacktestTradeItem]
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=500)
