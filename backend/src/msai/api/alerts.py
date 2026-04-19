"""Alerts API router — read-only audit trail of operational alerts.

Ported from codex-version/backend/src/msai/api/alerts.py. The history is
written by :data:`msai.services.alerting.alerting_service` whenever the
live supervisor, disconnect handler, or any worker emits an alert.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import ValidationError

from msai.core.auth import get_current_user
from msai.core.logging import get_logger
from msai.schemas.alert import AlertListResponse, AlertRecord
from msai.services.alerting import (
    _HISTORY_EXECUTOR,
    _HISTORY_WRITE_TIMEOUT_S,
    alerting_service,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


@router.get("/", response_model=AlertListResponse)
async def list_alerts(
    limit: int = 50,
    _: Mapping[str, object] = Depends(get_current_user),
) -> AlertListResponse:
    """Return the most recent alerts, newest first.

    ``limit`` is silently clamped to ``[1, 200]`` to match the Codex
    contract so a shared frontend hitting either backend behaves the same.
    Malformed entries on disk (operator hand-edits) are skipped rather
    than surfaced as a 500 — the audit log is opportunistic by design.

    ``list_alerts`` takes ``flock`` and does blocking file I/O; run it on
    the dedicated history executor (isolated from the default pool used
    by SMTP) and bound with ``_HISTORY_WRITE_TIMEOUT_S`` so a wedged
    writer holding the lock can't make this endpoint hang indefinitely —
    we fail open with an empty list so the dashboard stays responsive.
    """
    bounded_limit = max(1, min(limit, 200))
    loop = asyncio.get_running_loop()
    read_task = loop.run_in_executor(
        _HISTORY_EXECUTOR,
        lambda: alerting_service.list_alerts(limit=bounded_limit),
    )
    try:
        raw_entries = await asyncio.wait_for(
            asyncio.shield(read_task), timeout=_HISTORY_WRITE_TIMEOUT_S
        )
    except TimeoutError:
        log.warning(
            "alerts_api_read_timed_out",
            timeout_s=_HISTORY_WRITE_TIMEOUT_S,
        )
        return AlertListResponse(alerts=[])
    records: list[AlertRecord] = []
    for entry in raw_entries:
        try:
            records.append(AlertRecord.model_validate(entry))
        except ValidationError:
            log.warning("alerts_api_skipping_malformed_entry", entry=entry)
    return AlertListResponse(alerts=records)
