from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from databento.common.error import BentoClientError, BentoServerError

from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.data_sources.databento_errors import (
    DatabentoRateLimitedError,
    DatabentoUnauthorizedError,
)


@pytest.mark.asyncio
async def test_retry_recovers_from_429(tmp_path: Path) -> None:
    client = DatabentoClient(api_key="test-key")
    call_count = [0]

    def _mock_get_range(*args: object, **kwargs: object) -> None:
        call_count[0] += 1
        if call_count[0] < 3:
            raise BentoClientError(http_status=429, message="Rate limited", http_body=b"")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock(side_effect=_mock_get_range)
        with patch("msai.services.data_sources.databento_client.DatabentoDataLoader"):
            target = tmp_path / "out.dbn.zst"
            (target.with_suffix(target.suffix + ".tmp")).touch()
            await client.fetch_definition_instruments(
                symbol="AAPL",
                start="2024-01-01",
                end="2024-01-02",
                dataset="XNAS.ITCH",
                target_path=target,
            )
    assert call_count[0] == 3


@pytest.mark.asyncio
async def test_429_exhaustion_raises_rate_limited(tmp_path: Path) -> None:
    client = DatabentoClient(api_key="test-key")

    def _always_429(*args: object, **kwargs: object) -> None:
        raise BentoClientError(http_status=429, message="Rate limited", http_body=b"")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock(side_effect=_always_429)
        with pytest.raises(DatabentoRateLimitedError) as exc_info:
            await client.fetch_definition_instruments(
                symbol="AAPL",
                start="2024-01-01",
                end="2024-01-02",
                dataset="XNAS.ITCH",
                target_path=tmp_path / "out.dbn.zst",
            )
    assert exc_info.value.http_status == 429
    assert exc_info.value.dataset == "XNAS.ITCH"


@pytest.mark.asyncio
async def test_401_no_retry(tmp_path: Path) -> None:
    client = DatabentoClient(api_key="test-key")
    call_count = [0]

    def _always_401(*args: object, **kwargs: object) -> None:
        call_count[0] += 1
        raise BentoClientError(http_status=401, message="Unauthorized", http_body=b"")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock(side_effect=_always_401)
        with pytest.raises(DatabentoUnauthorizedError) as exc_info:
            await client.fetch_definition_instruments(
                symbol="AAPL",
                start="2024-01-01",
                end="2024-01-02",
                dataset="XNAS.ITCH",
                target_path=tmp_path / "out.dbn.zst",
            )
    assert call_count[0] == 1, "401 should fail on first attempt, not retry"
    assert exc_info.value.http_status == 401


@pytest.mark.asyncio
async def test_500_retries_then_succeeds(tmp_path: Path) -> None:
    client = DatabentoClient(api_key="test-key")
    call_count = [0]

    def _mock(*args: object, **kwargs: object) -> None:
        call_count[0] += 1
        if call_count[0] == 1:
            raise BentoServerError(http_status=500, message="Internal error", http_body=b"")

    with patch("databento.Historical") as mock_historical:
        mock_historical.return_value.timeseries.get_range = MagicMock(side_effect=_mock)
        with patch("msai.services.data_sources.databento_client.DatabentoDataLoader"):
            target = tmp_path / "out.dbn.zst"
            (target.with_suffix(target.suffix + ".tmp")).touch()
            await client.fetch_definition_instruments(
                symbol="AAPL",
                start="2024-01-01",
                end="2024-01-02",
                dataset="XNAS.ITCH",
                target_path=target,
            )
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_500_exhaustion_raises_upstream_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All 3 tenacity attempts return 5xx → DatabentoUpstreamError with
    http_status=500 and call_count==3 (guards stop_after_attempt(3) from
    regressing)."""
    from msai.services.data_sources.databento_errors import DatabentoUpstreamError

    # Patch tenacity wait to zero so test runs fast (no 1s/3s/9s delay).
    monkeypatch.setattr(
        "msai.services.data_sources.databento_client.wait_exponential",
        lambda *a, **kw: lambda *_a, **_kw: 0,
    )

    client = DatabentoClient(api_key="test-key")
    call_count = [0]

    def _mock(*args: object, **kwargs: object) -> None:
        call_count[0] += 1
        raise BentoServerError(http_status=500, message="Internal error", http_body=b"")

    with (
        patch("databento.Historical") as mock_historical,
        patch("msai.services.data_sources.databento_client.DatabentoDataLoader"),
    ):
        mock_historical.return_value.timeseries.get_range = MagicMock(side_effect=_mock)
        target = tmp_path / "out.dbn.zst"
        with pytest.raises(DatabentoUpstreamError) as exc_info:
            await client.fetch_definition_instruments(
                symbol="AAPL",
                start="2024-01-01",
                end="2024-01-02",
                dataset="XNAS.ITCH",
                target_path=target,
            )

    assert call_count[0] == 3
    assert exc_info.value.http_status == 500
