"""Instrument registry API — Databento bootstrap.

POST /api/v1/instruments/bootstrap registers equity/ETF/futures symbols
using the Databento definition schema. Returns per-symbol outcomes with
three explicit readiness-state flags (registered, backtest_data_available,
live_qualified).

Status codes: 200 all-success, 207 mixed, 422 all-failed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from msai.api.backtests import _error_response
from msai.core.auth import get_current_user
from msai.core.database import get_session_factory
from msai.schemas.instrument_bootstrap import (
    BootstrapRequest,
    BootstrapResponse,
    BootstrapResultItem,
    CandidateInfo,
    build_bootstrap_response,
)
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.nautilus.security_master.databento_bootstrap import (
    BootstrapResult,
    DatabentoBootstrapService,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

router = APIRouter(prefix="/api/v1/instruments", tags=["instruments"])


@router.post(
    "/bootstrap",
    response_model=BootstrapResponse,
    responses={
        207: {"model": BootstrapResponse, "description": "Partial success"},
        422: {"description": "All symbols failed OR request validation error"},
    },
)
async def bootstrap_instruments(
    request: BootstrapRequest,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),  # noqa: B008
    _claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> JSONResponse:
    """Bootstrap a batch of symbols into the registry via Databento."""
    databento_client = DatabentoClient()
    if not databento_client.api_key:
        return _error_response(
            500,
            "DATABENTO_NOT_CONFIGURED",
            "DATABENTO_API_KEY environment variable not set on server",
        )

    svc = DatabentoBootstrapService(
        session_factory=session_factory,
        databento_client=databento_client,
        max_concurrent=request.max_concurrent,
    )
    results = await svc.bootstrap(
        symbols=request.symbols,
        asset_class_override=request.asset_class_override,
        exact_ids=request.exact_ids,
    )

    response_items = [_to_item(r) for r in results]
    response = build_bootstrap_response(response_items)

    num_success = sum(1 for r in results if r.registered)
    num_failure = len(results) - num_success
    if num_failure == 0:
        status_code = 200
    elif num_success > 0:
        status_code = 207  # Multi-Status — mixed
    else:
        status_code = 422  # all failed

    return JSONResponse(status_code=status_code, content=response.model_dump(mode="json"))


def _to_item(r: BootstrapResult) -> BootstrapResultItem:
    return BootstrapResultItem(
        symbol=r.symbol,
        outcome=r.outcome.value,
        registered=r.registered,
        backtest_data_available=r.backtest_data_available,
        live_qualified=r.live_qualified,
        canonical_id=r.canonical_id,
        dataset=r.dataset,
        asset_class=r.asset_class,
        candidates=[CandidateInfo(**c) for c in r.candidates],
        diagnostics=r.diagnostics,
    )
