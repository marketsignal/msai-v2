"""Integration tests for the backtests API — auto-heal lifecycle fields.

Task B9 (backtest-auto-ingest-on-missing-data): verify that
``GET /api/v1/backtests/{id}/status`` and ``GET /api/v1/backtests/history``
surface the ``phase`` + ``progress_message`` columns on the ``Backtest``
row when the auto-heal cycle has populated them.

The API layer uses ``response_model_exclude_none=True`` on both
endpoints (PR #39 contract), so absent values stay ABSENT in the JSON
response — preserving backward compat for older callers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path  # noqa: TC003 — used at runtime by pytest's tmp_path fixture type
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.config import settings
from msai.core.database import get_db
from msai.main import app
from msai.models.backtest import Backtest

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    import httpx

    from msai.models.trade import Trade


# ---------------------------------------------------------------------------
# Helpers — local to keep the integration surface self-contained. Mirrors
# the shape of ``tests/unit/conftest.py::_make_backtest`` but independent
# so a future rename of that helper doesn't silently break these tests.
# ---------------------------------------------------------------------------


def _make_running_backtest_with_phase(
    *,
    phase: str | None,
    progress_message: str | None,
) -> Backtest:
    return Backtest(
        id=uuid4(),
        strategy_id=uuid4(),
        strategy_code_hash="x" * 64,
        config={},
        instruments=["AAPL.NASDAQ"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 15),
        status="running",
        progress=50,
        error_code="unknown",
        phase=phase,
        progress_message=progress_message,
        created_at=datetime.now(UTC),
    )


def _mock_session_returning(row: Backtest) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = row
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [row]
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one_or_none.return_value = row
    # ``len(items)`` on the list path goes through ``func.count()``, which
    # uses ``scalar_one``. Match the single-row count.
    mock_result.scalar_one.return_value = 1
    session.execute.return_value = mock_result
    return session


@pytest.fixture
def seed_running_backtest_awaiting_data() -> Generator[Backtest, None, None]:
    """Seed a single ``running`` row with ``phase='awaiting_data'``.

    Installs a ``get_db`` override yielding an AsyncMock session preloaded
    with the row. The root ``client`` fixture picks up the override
    transparently.
    """
    row = _make_running_backtest_with_phase(
        phase="awaiting_data",
        progress_message="Downloading AAPL...",
    )
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield row
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# GET /api/v1/backtests/{id}/status
# ---------------------------------------------------------------------------


async def test_status_endpoint_returns_phase_when_set(
    client: httpx.AsyncClient,
    seed_running_backtest_awaiting_data: Backtest,
) -> None:
    """``phase`` + ``progress_message`` surface in the /status JSON body
    when the row has them populated.
    """
    row = seed_running_backtest_awaiting_data

    async with client as ac:
        response = await ac.get(f"/api/v1/backtests/{row.id}/status")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "running"
    assert body["phase"] == "awaiting_data"
    assert body["progress_message"] == "Downloading AAPL..."


# ---------------------------------------------------------------------------
# GET /api/v1/backtests/history
# ---------------------------------------------------------------------------


async def test_history_endpoint_returns_phase_when_set(
    client: httpx.AsyncClient,
    seed_running_backtest_awaiting_data: Backtest,
) -> None:
    """The list endpoint mirrors /status — lifecycle fields ride on each
    ``BacktestListItem`` so the list page can render the "Fetching data…"
    badge render.
    """
    async with client as ac:
        response = await ac.get("/api/v1/backtests/history")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["status"] == "running"
    assert item["phase"] == "awaiting_data"
    assert item["progress_message"] == "Downloading AAPL..."


# ---------------------------------------------------------------------------
# B10 — Signed-URL machinery: POST /report-token + GET /report?token=
# ---------------------------------------------------------------------------


def _make_completed_backtest_with_report(report_path: str = "/tmp/r.html") -> Backtest:
    """In-memory Backtest row with a report_path populated."""
    from tests.unit.conftest import _make_backtest

    return _make_backtest(
        status="completed",
        metrics={"sharpe_ratio": 1.2},
        report_path=report_path,
    )


async def test_report_token_endpoint_returns_signed_url(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /report-token mints an HMAC URL when the report is deliverable
    (path set, file exists, under the sanctioned data_root/reports dir)."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report_file = reports_dir / "ready.html"
    report_file.write_text("<html>ok</html>")
    monkeypatch.setattr(settings, "data_root", tmp_path)

    row = _make_completed_backtest_with_report(report_path=str(report_file))
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.post(f"/api/v1/backtests/{row.id}/report-token")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["signed_url"].startswith(f"/api/v1/backtests/{row.id}/report?token=")
        assert "expires_at" in body
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_report_token_endpoint_refuses_undeliverable_report(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eligibility parity: if ``/report`` would 403/404 the file, ``/report-token``
    must refuse to mint a URL rather than hand the UI a token that the
    downstream GET will reject. Here the path is set but the file is missing.
    """
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    missing = reports_dir / "never-written.html"  # not created
    monkeypatch.setattr(settings, "data_root", tmp_path)

    row = _make_completed_backtest_with_report(report_path=str(missing))
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.post(f"/api/v1/backtests/{row.id}/report-token")
        assert response.status_code == 404, response.text
        body = response.json()
        assert body["error"]["code"] == "NO_REPORT"
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_report_endpoint_accepts_valid_token(
    client: httpx.AsyncClient,
    tmp_path: Path,  # type: ignore[name-defined]  # noqa: F821 — pathlib.Path TYPE_CHECKING
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /report with a valid ?token= returns the HTML file even without auth headers.

    /report's path-traversal guard requires report_path to be under
    `{settings.data_root}/reports/`. Monkeypatch data_root → tmp_path so
    the fake report file under tmp_path/reports/ passes the guard.
    """
    from datetime import UTC, datetime, timedelta

    from msai.core.config import settings
    from msai.services.report_signer import sign_report_token

    # Pass tmp_path directly (settings.data_root is typed Path); str() casting
    # worked by accident because the handler re-wraps via Path(...) but is fragile.
    monkeypatch.setattr(settings, "data_root", tmp_path)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report_file = reports_dir / "r.html"
    report_file.write_text("<html>tearsheet</html>")
    row = _make_completed_backtest_with_report(report_path=str(report_file))

    token = sign_report_token(
        backtest_id=row.id,
        user_sub="test-user",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret=settings.report_signing_secret,
    )

    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{row.id}/report?token={token}")
        assert response.status_code == 200, response.text
        assert "<html>" in response.text
        # Critical: the iframe flow requires Content-Disposition: inline.
        # A ``filename=...`` on FileResponse without an explicit
        # ``content_disposition_type="inline"`` defaults to ``attachment``,
        # which makes browsers download the HTML instead of rendering it
        # inside the "Full report" iframe.
        disposition = response.headers.get("content-disposition", "")
        assert disposition.startswith("inline"), (
            f"expected inline disposition for iframe rendering, got: {disposition!r}"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_report_endpoint_rejects_expired_token(
    client: httpx.AsyncClient,
) -> None:
    """Expired tokens → 401 INVALID_TOKEN."""
    from datetime import UTC, datetime, timedelta

    from msai.core.config import settings
    from msai.services.report_signer import sign_report_token

    row = _make_completed_backtest_with_report()
    token = sign_report_token(
        backtest_id=row.id,
        user_sub="u",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),  # expired
        secret=settings.report_signing_secret,
    )

    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{row.id}/report?token={token}")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_TOKEN"
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_report_endpoint_rejects_cross_backtest_token(
    client: httpx.AsyncClient,
) -> None:
    """A token minted for backtest A must not unlock backtest B."""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from msai.core.config import settings
    from msai.services.report_signer import sign_report_token

    # Token for a different backtest
    token_for_a = sign_report_token(
        backtest_id=uuid4(),
        user_sub="u",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret=settings.report_signing_secret,
    )

    # But B is the one we try to access
    row_b = _make_completed_backtest_with_report()
    session = _mock_session_returning(row_b)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{row_b.id}/report?token={token_for_a}")
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_report_endpoint_rejects_no_auth_no_token(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: the ``/report`` handler must reject a completely
    unauthenticated request (no Bearer, no X-API-Key, no ``?token=``).
    An earlier draft of the signed-URL flow would have silently served
    the file here; this test pins the fix.
    """
    # Strip the dev-mode MSAI_API_KEY so the test client's fallback header
    # doesn't accidentally satisfy auth and mask the regression.
    monkeypatch.setattr("msai.core.config.settings.msai_api_key", "")

    row = _make_completed_backtest_with_report()
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        # Use a fresh client with no default headers so the conftest's
        # auto-applied API-key doesn't satisfy auth.
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get(f"/api/v1/backtests/{row.id}/report")
        assert response.status_code == 401, response.text
        body = response.json()
        assert body.get("error", {}).get("code") == "UNAUTHENTICATED"
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_report_endpoint_rejects_invalid_token_string(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bogus ``?token=`` string must 401 ``INVALID_TOKEN`` — not 200 (bypass),
    not 500 (unhandled exception), not 400.
    """
    monkeypatch.setattr("msai.core.config.settings.msai_api_key", "")

    row = _make_completed_backtest_with_report()
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as ac:
            response = await ac.get(f"/api/v1/backtests/{row.id}/report?token=not-a-real-token")
        assert response.status_code == 401, response.text
        body = response.json()
        assert body.get("error", {}).get("code") == "INVALID_TOKEN"
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_report_endpoint_rejects_sub_mismatch_when_session_present(
    client: httpx.AsyncClient,
) -> None:
    """When a session is attached, its ``sub`` must match the token's
    ``user_sub``. Guards against shared-link replay by a different logged-in
    user.
    """
    from datetime import UTC, datetime, timedelta

    from msai.core.config import settings
    from msai.services.report_signer import sign_report_token

    row = _make_completed_backtest_with_report()
    session = _mock_session_returning(row)

    # Token minted for user-alice
    token = sign_report_token(
        backtest_id=row.id,
        user_sub="alice",
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        secret=settings.report_signing_secret,
    )

    # Active session belongs to user-bob (conftest's MOCK_CLAIMS overrides).
    from msai.core.auth import get_current_user_or_none

    async def _override_user() -> dict[str, str]:
        return {"sub": "bob", "preferred_username": "bob@example.com"}

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_current_user_or_none] = _override_user
    app.dependency_overrides[get_db] = _override_db
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{row.id}/report?token={token}")
        assert response.status_code == 403, response.text
        body = response.json()
        assert body.get("error", {}).get("code") == "TOKEN_SUB_MISMATCH"
    finally:
        app.dependency_overrides.pop(get_current_user_or_none, None)
        app.dependency_overrides.pop(get_db, None)


async def test_report_endpoint_rejects_path_traversal(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``report_path`` that resolves outside ``{data_root}/reports`` must
    return 403 ``FORBIDDEN``, even if the file happens to exist. Guards
    against a DB compromise that plants ``../../etc/passwd``.
    """
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    # Plant a real file OUTSIDE the sanctioned reports dir — on disk and
    # readable, but not under ``{data_root}/reports``.
    outside = tmp_path / "outside-report.html"
    outside.write_text("<html>nope</html>")
    monkeypatch.setattr(settings, "data_root", tmp_path)

    from msai.core.auth import get_current_user_or_none
    from tests.unit.conftest import _make_backtest

    row = _make_backtest(status="completed", report_path=str(outside))
    session = _mock_session_returning(row)

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        yield session

    async def _override_user() -> dict[str, str]:
        return {"sub": "test-user", "preferred_username": "test@example.com"}

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_or_none] = _override_user
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{row.id}/report")
        assert response.status_code == 403, response.text
        body = response.json()
        assert body.get("error", {}).get("code") == "FORBIDDEN"
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user_or_none, None)


# ---------------------------------------------------------------------------
# B7 — GET /api/v1/backtests/{id}/results (updated shape)
# ---------------------------------------------------------------------------
#
# Contract change from the pre-B7 shape:
#   - ``trades`` (inline fill array) REMOVED. See GET /trades for pagination (B8).
#   - ``series`` (canonical SeriesPayload | None) ADDED.
#   - ``series_status`` (Literal["ready","not_materialized","failed"]) ADDED.
#   - ``has_report`` (bool, server-derived) ADDED. Raw ``report_path`` NOT exposed.
#   - 404 envelope shape: top-level ``{"error": {"code", "message"}}`` via
#     ``JSONResponse`` (not FastAPI's ``HTTPException(detail=...)`` wrapper).
#   - ``trade_count`` now sourced from ``SELECT COUNT(*)`` — no inline list.


def _mock_session_for_results(
    backtest: Backtest | None,
    *,
    trade_count: int = 0,
) -> AsyncMock:
    """Mock session where the first execute() returns the backtest lookup
    and the second execute() returns the ``COUNT(*)`` scalar.

    FastAPI's handler calls ``db.execute`` twice (SELECT backtest, then
    SELECT COUNT trades). The default ``_mock_session_returning`` can't
    distinguish; this helper wires ``side_effect`` so each call gets a
    tailored result. Pattern mirrors other mock-based tests in this file.
    """
    session = AsyncMock(spec=AsyncSession)

    # Result #1: SELECT Backtest WHERE id == job_id
    backtest_result = MagicMock()
    backtest_result.scalar_one_or_none.return_value = backtest

    # Result #2: SELECT COUNT(*) FROM trades WHERE backtest_id == job_id
    count_result = MagicMock()
    count_result.scalar_one.return_value = trade_count

    session.execute.side_effect = [backtest_result, count_result]
    return session


async def test_results_returns_series_when_ready(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed backtest with ``series_status='ready'`` surfaces the full
    canonical payload — daily points + monthly returns — plus aggregate
    metrics + ``has_report=True`` (derived from report_path + file exists).
    """
    from tests.unit.conftest import _make_backtest_completed_with_series

    # ``has_report`` is true iff the file exists under ``{data_root}/reports``.
    # Stage a real file so the containment + existence checks both pass.
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report_file = reports_dir / "ready-report.html"
    report_file.write_text("<html>tear sheet</html>")
    monkeypatch.setattr(settings, "data_root", tmp_path)

    row = _make_backtest_completed_with_series(report_path=str(report_file))
    session = _mock_session_for_results(row, trade_count=4)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{row.id}/results")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["id"] == str(row.id)
        assert body["series_status"] == "ready"
        assert body["has_report"] is True
        assert body["trade_count"] == 4
        assert body["metrics"]["sharpe_ratio"] == 2.1

        # Canonical series payload round-trips end-to-end.
        assert body["series"] is not None
        assert len(body["series"]["daily"]) == 2
        assert body["series"]["daily"][0]["date"] == "2024-01-02"
        assert body["series"]["daily"][0]["equity"] == 100_500.0
        assert body["series"]["monthly_returns"] == [{"month": "2024-01", "pct": 0.01}]

        # No inline trades array — fills moved to the paginated /trades endpoint.
        assert "trades" not in body
        # report_path is NEVER exposed; only derived ``has_report`` is.
        assert "report_path" not in body
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_results_returns_not_materialized_for_legacy(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-PR backtests (``series=None``, ``series_status='not_materialized'``)
    return a valid response — the UI renders an empty-state, not a 500.
    The metrics + has_report + trade_count payload still populates.
    """
    from tests.unit.conftest import _make_backtest_legacy

    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report_file = reports_dir / "legacy-report.html"
    report_file.write_text("<html>legacy</html>")
    monkeypatch.setattr(settings, "data_root", tmp_path)

    row = _make_backtest_legacy(report_path=str(report_file))
    session = _mock_session_for_results(row, trade_count=10)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{row.id}/results")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["series"] is None
        assert body["series_status"] == "not_materialized"
        assert body["has_report"] is True
        assert body["trade_count"] == 10
        assert body["metrics"]["num_trades"] == 10
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_results_returns_failed_status(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``series_status='failed'`` surfaces to the UI so it can render the
    "analytics unavailable — download the QuantStats report" banner per
    the PRD's distinct-failure-state user story (US-006).
    """
    from tests.unit.conftest import _make_backtest_failed_series

    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report_file = reports_dir / "fail-report.html"
    report_file.write_text("<html>report</html>")
    monkeypatch.setattr(settings, "data_root", tmp_path)

    row = _make_backtest_failed_series(report_path=str(report_file))
    session = _mock_session_for_results(row, trade_count=6)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{row.id}/results")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["series"] is None
        assert body["series_status"] == "failed"
        assert body["has_report"] is True
        assert body["trade_count"] == 6
        # Aggregate metrics still render — analytics failure is orthogonal
        # to Nautilus's own stats pipeline.
        assert body["metrics"]["sharpe_ratio"] == 0.8
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_results_returns_has_report_false_when_file_missing(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale-pointer case: ``Backtest.report_path`` is set but the file
    on disk was removed (housekeeping script, DR volume restore). The
    detail page must hide the "Full report" tab rather than show a
    spinner that bounces to 404 when clicked.
    """
    from tests.unit.conftest import _make_backtest_completed_with_series

    # Point report_path at a location inside the sanctioned reports dir
    # (so the containment check passes), but do NOT create the file —
    # ``is_file()`` must return False and flip ``has_report`` to False.
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    missing = reports_dir / "never-written.html"
    monkeypatch.setattr(settings, "data_root", tmp_path)

    row = _make_backtest_completed_with_series(report_path=str(missing))
    session = _mock_session_for_results(row, trade_count=4)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{row.id}/results")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["has_report"] is False
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_results_returns_404_for_missing_backtest(
    client: httpx.AsyncClient,
) -> None:
    """Unknown UUID → 404 with the top-level ``{"error": {...}}`` envelope
    Uses ``JSONResponse`` to skip FastAPI's ``detail`` wrapping per
    ``.claude/rules/api-design.md``.
    """
    from uuid import uuid4

    session = _mock_session_for_results(None, trade_count=0)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{uuid4()}/results")
        assert response.status_code == 404
        body = response.json()

        # Top-level envelope, NOT nested under "detail".
        assert "error" in body
        assert "detail" not in body
        assert body["error"]["code"] == "NOT_FOUND"
        assert "not found" in body["error"]["message"].lower()
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# B8 — GET /api/v1/backtests/{id}/trades (paginated fills)
# ---------------------------------------------------------------------------
#
# Endpoint contract:
#   - ``?page`` defaults to 1, ``?page_size`` defaults to 100.
#   - ``page_size`` clamps to 500 (not 422'd) — convention match with
#     ResearchJobListResponse / GraduationCandidateListResponse.
#   - 404 envelope shape: top-level ``{"error": {"code", "message"}}``.
#   - Ordering: ``(executed_at, id) ASC`` for deterministic pagination.
#
# The handler calls ``db.execute`` THREE times: exists-check, COUNT(*),
# then the paged SELECT. The helper below wires ``side_effect`` so each
# call gets its tailored result.


def _mock_trades_session(
    *,
    backtest_exists: bool,
    total: int,
    rows: list[Trade],
) -> AsyncMock:
    """Mock session for the /trades handler's three-query flow.

    1. ``SELECT Backtest.id WHERE id == job_id`` (existence check).
    2. ``SELECT COUNT(*) FROM trades WHERE backtest_id == job_id``.
    3. ``SELECT Trade rows ORDER BY (executed_at, id) OFFSET LIMIT``.
    """
    session = AsyncMock(spec=AsyncSession)

    exists_result = MagicMock()
    exists_result.scalar_one_or_none.return_value = uuid4() if backtest_exists else None

    count_result = MagicMock()
    count_result.scalar_one.return_value = total

    rows_result = MagicMock()
    rows_scalars = MagicMock()
    rows_scalars.all.return_value = rows
    rows_result.scalars.return_value = rows_scalars

    session.execute.side_effect = [exists_result, count_result, rows_result]
    return session


async def test_trades_endpoint_paginates(
    client: httpx.AsyncClient,
) -> None:
    """150 trades, page=1 page_size=100 → first 100 items + total=150."""
    from tests.unit.conftest import _make_backtest_with_trades

    bt, trades = _make_backtest_with_trades(150)
    first_page = trades[:100]
    session = _mock_trades_session(backtest_exists=True, total=150, rows=first_page)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{bt.id}/trades?page=1&page_size=100")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["total"] == 150
        assert body["page"] == 1
        assert body["page_size"] == 100
        assert len(body["items"]) == 100
        # Spot-check the shape of an item.
        item = body["items"][0]
        assert set(item.keys()) == {
            "id",
            "instrument",
            "side",
            "quantity",
            "price",
            "pnl",
            "commission",
            "executed_at",
        }
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_trades_endpoint_second_page(
    client: httpx.AsyncClient,
) -> None:
    """150 trades, page=2 page_size=100 → remaining 50 items + total=150."""
    from tests.unit.conftest import _make_backtest_with_trades

    bt, trades = _make_backtest_with_trades(150)
    second_page = trades[100:]
    session = _mock_trades_session(backtest_exists=True, total=150, rows=second_page)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{bt.id}/trades?page=2&page_size=100")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["total"] == 150
        assert body["page"] == 2
        assert body["page_size"] == 100
        assert len(body["items"]) == 50
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_trades_endpoint_empty_beyond_range(
    client: httpx.AsyncClient,
) -> None:
    """50 trades, page=99 → items=[] + total=50 (200, not 404).

    Out-of-range pages are a valid query — total surfaces and items is empty.
    """
    from tests.unit.conftest import _make_backtest_with_trades

    bt, _trades = _make_backtest_with_trades(50)
    session = _mock_trades_session(backtest_exists=True, total=50, rows=[])

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{bt.id}/trades?page=99&page_size=100")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["total"] == 50
        assert body["page"] == 99
        assert body["page_size"] == 100
        assert body["items"] == []
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_trades_endpoint_clamps_page_size(
    client: httpx.AsyncClient,
) -> None:
    """page_size=9999 clamps to MAX_TRADE_PAGE_SIZE=500 (not 422)."""
    from tests.unit.conftest import _make_backtest_with_trades

    bt, trades = _make_backtest_with_trades(600)
    # Handler's LIMIT becomes 500; mock returns 500 rows.
    clamped_rows = trades[:500]
    session = _mock_trades_session(backtest_exists=True, total=600, rows=clamped_rows)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{bt.id}/trades?page=1&page_size=9999")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["page_size"] == 500  # clamped
        assert body["total"] == 600
        assert len(body["items"]) == 500
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_trades_endpoint_rejects_zero_page(
    client: httpx.AsyncClient,
) -> None:
    """page=0 → 422 (Query(..., ge=1) rejects before any DB call).

    No session override needed — FastAPI's query-validation runs before
    the ``get_db`` dependency, so the handler never executes.
    """
    async with client as ac:
        response = await ac.get(f"/api/v1/backtests/{uuid4()}/trades?page=0&page_size=100")
    assert response.status_code == 422


async def test_trades_endpoint_rejects_zero_page_size(
    client: httpx.AsyncClient,
) -> None:
    """page_size=0 must also 422 — ``Query(..., ge=1)`` on the param
    rejects it before the DB call. Guards against a future client that
    passes 0 intending "server default".
    """
    async with client as ac:
        response = await ac.get(f"/api/v1/backtests/{uuid4()}/trades?page=1&page_size=0")
    assert response.status_code == 422


async def test_trades_endpoint_preserves_db_order_on_equal_timestamps(
    client: httpx.AsyncClient,
) -> None:
    """With equal ``executed_at`` values, the serialization preserves the
    exact order the DB returned. The handler's ``ORDER BY (executed_at, id)``
    is what actually breaks ties on the DB side — this test pins the
    "serializer doesn't re-sort" contract so a future JSON shaping change
    can't silently scramble rows the DB deliberately ordered.
    """
    from datetime import UTC, datetime
    from decimal import Decimal
    from uuid import UUID

    from msai.models.trade import Trade
    from tests.unit.conftest import _make_backtest

    bt = _make_backtest(status="completed")
    same_ts = datetime(2024, 1, 2, 15, 30, 0, tzinfo=UTC)

    def _make_trade(trade_id: UUID, side: str, price: float, pnl: float) -> Trade:
        return Trade(
            id=trade_id,
            backtest_id=bt.id,
            strategy_id=bt.strategy_id,
            strategy_code_hash=bt.strategy_code_hash,
            instrument="SPY.XNAS",
            side=side,
            quantity=Decimal("1"),
            price=Decimal(str(price)),
            pnl=Decimal(str(pnl)),
            commission=Decimal("0"),
            executed_at=same_ts,
        )

    trades = [
        _make_trade(UUID("00000000-0000-0000-0000-00000000000a"), "BUY", 400.0, 0.0),
        _make_trade(UUID("00000000-0000-0000-0000-00000000000b"), "SELL", 401.0, 1.0),
        _make_trade(UUID("00000000-0000-0000-0000-00000000000c"), "BUY", 402.0, 0.0),
    ]
    session = _mock_trades_session(backtest_exists=True, total=3, rows=trades)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{bt.id}/trades?page=1&page_size=100")
        assert response.status_code == 200
        items = response.json()["items"]
        assert [it["id"] for it in items] == [
            "00000000-0000-0000-0000-00000000000a",
            "00000000-0000-0000-0000-00000000000b",
            "00000000-0000-0000-0000-00000000000c",
        ]
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_trades_endpoint_backtest_not_found(
    client: httpx.AsyncClient,
) -> None:
    """Fake UUID → 404 with ``{"error": {"code": "NOT_FOUND", ...}}`` envelope."""
    session = _mock_trades_session(backtest_exists=False, total=0, rows=[])

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        async with client as ac:
            response = await ac.get(f"/api/v1/backtests/{uuid4()}/trades")
        assert response.status_code == 404
        body = response.json()

        # Top-level envelope shape (JSONResponse, not FastAPI's detail wrapper).
        assert "error" in body
        assert "detail" not in body
        assert body["error"]["code"] == "NOT_FOUND"
        assert "not found" in body["error"]["message"].lower()
    finally:
        app.dependency_overrides.pop(get_db, None)
