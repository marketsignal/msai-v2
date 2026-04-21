"""Unit tests for ``msai.schemas.backtest`` Pydantic models.

Covers the auto-heal lifecycle additions introduced by Task B9
(backtest-auto-ingest-on-missing-data): ``phase`` + ``progress_message``
on both :class:`BacktestStatusResponse` and :class:`BacktestListItem`.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from msai.schemas.backtest import BacktestListItem, BacktestStatusResponse

_STATUS_ID = UUID("00000000-0000-0000-0000-000000000001")
_LIST_ID = UUID("00000000-0000-0000-0000-000000000002")
_STRATEGY_ID = UUID("00000000-0000-0000-0000-00000000aaaa")


# ---------------------------------------------------------------------------
# BacktestStatusResponse
# ---------------------------------------------------------------------------


def test_backtest_status_response_accepts_phase_and_progress_message() -> None:
    """Happy path — both lifecycle fields round-trip on construction."""
    resp = BacktestStatusResponse(
        id=_STATUS_ID,
        status="running",
        progress=50,
        started_at=None,
        completed_at=None,
        phase="awaiting_data",
        progress_message="Downloading AAPL...",
    )

    assert resp.phase == "awaiting_data"
    assert resp.progress_message == "Downloading AAPL..."


def test_backtest_status_response_rejects_unknown_phase() -> None:
    """Non-Literal phase value raises ``ValidationError`` at construction."""
    with pytest.raises(ValidationError):
        BacktestStatusResponse(
            id=_STATUS_ID,
            status="running",
            progress=50,
            started_at=None,
            completed_at=None,
            phase="bogus",  # type: ignore[arg-type]
            progress_message=None,
        )


@pytest.mark.parametrize(
    ("kwargs", "expected_phase", "expected_message"),
    [
        pytest.param({}, None, None, id="absent"),
        pytest.param({"phase": None, "progress_message": None}, None, None, id="null"),
    ],
)
def test_backtest_status_response_accepts_absent_or_null_phase(
    kwargs: dict[str, object],
    expected_phase: str | None,
    expected_message: str | None,
) -> None:
    """``phase`` / ``progress_message`` omitted OR explicitly ``None`` both
    resolve to ``None`` — so older callers that predate the field keep
    parsing identically.
    """
    resp = BacktestStatusResponse(
        id=_STATUS_ID,
        status="running",
        progress=50,
        started_at=None,
        completed_at=None,
        **kwargs,  # type: ignore[arg-type]
    )

    assert resp.phase is expected_phase
    assert resp.progress_message is expected_message


# ---------------------------------------------------------------------------
# BacktestListItem
# ---------------------------------------------------------------------------


def _list_item_required_kwargs() -> dict[str, object]:
    return {
        "id": _LIST_ID,
        "strategy_id": _STRATEGY_ID,
        "status": "running",
        "start_date": date(2025, 1, 2),
        "end_date": date(2025, 1, 15),
        "created_at": datetime(2025, 1, 2, 12, 0, 0),
    }


def test_backtest_list_item_accepts_phase_and_progress_message() -> None:
    """Happy path — both lifecycle fields round-trip on list items too.

    Mirrors the status-response test so the list page badge wiring (F2)
    and the detail page indicator consume the same typed shape.
    """
    item = BacktestListItem(
        **_list_item_required_kwargs(),
        phase="awaiting_data",
        progress_message="Downloading AAPL...",
    )

    assert item.phase == "awaiting_data"
    assert item.progress_message == "Downloading AAPL..."


def test_backtest_list_item_rejects_unknown_phase() -> None:
    """Non-Literal ``phase`` on the list item is rejected same as status."""
    with pytest.raises(ValidationError):
        BacktestListItem(
            **_list_item_required_kwargs(),
            phase="bogus",  # type: ignore[arg-type]
            progress_message=None,
        )


@pytest.mark.parametrize(
    ("kwargs", "expected_phase", "expected_message"),
    [
        pytest.param({}, None, None, id="absent"),
        pytest.param({"phase": None, "progress_message": None}, None, None, id="null"),
    ],
)
def test_backtest_list_item_accepts_absent_or_null_phase(
    kwargs: dict[str, object],
    expected_phase: str | None,
    expected_message: str | None,
) -> None:
    item = BacktestListItem(
        **_list_item_required_kwargs(),
        **kwargs,  # type: ignore[arg-type]
    )

    assert item.phase is expected_phase
    assert item.progress_message is expected_message
