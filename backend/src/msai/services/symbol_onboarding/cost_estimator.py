from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal, Protocol

import structlog

if TYPE_CHECKING:
    from msai.schemas.symbol_onboarding import OnboardSymbolSpec
    from msai.services.symbol_onboarding.manifest import ParsedManifest

from msai.services.nautilus.security_master.continuous_futures import (
    is_databento_continuous_pattern,
)

log = structlog.get_logger(__name__)

__all__ = [
    "CostEstimate",
    "CostLine",
    "UnpriceableAssetClassError",
    "estimate_cost",
]

_ASSET_TO_DATASET: dict[str, str] = {
    "equity": "XNAS.ITCH",
    "futures": "GLBX.MDP3",
}


class UnpriceableAssetClassError(ValueError):
    """Raised when a manifest references an asset class that has no
    Databento dataset mapped for cost estimation.

    Lifting this from a silent ``log.warning + continue`` to a typed
    raise keeps the cost-ceiling enforcement structurally sound — without
    it, a request with N pricable + M unpricable symbols would silently
    estimate only the N pricable lines and slip past ``cost_ceiling_usd``
    when the M unpricable symbols actually run.
    """

    def __init__(self, symbol: str, asset_class: str) -> None:
        self.symbol = symbol
        self.asset_class = asset_class
        super().__init__(
            f"asset_class={asset_class!r} (symbol={symbol!r}) has no Databento "
            "dataset mapping; v1 cost estimator supports only equity + futures."
        )


class _DatabentoMetadataProto(Protocol):
    def get_cost(
        self,
        *,
        dataset: str,
        symbols: list[str],
        schema: str,
        stype_in: str,
        start: str,
        end: str,
    ) -> float: ...


class _DatabentoClientProto(Protocol):
    metadata: _DatabentoMetadataProto


@dataclass(frozen=True, slots=True)
class CostLine:
    symbol: str
    asset_class: str
    dataset: str
    usd: float


@dataclass(frozen=True, slots=True)
class CostEstimate:
    total_usd: float
    symbol_count: int
    breakdown: list[CostLine]
    confidence: Literal["high", "medium", "low"]
    basis: str


async def estimate_cost(
    manifest: ParsedManifest,
    *,
    client: _DatabentoClientProto,
    today: date | None = None,
) -> CostEstimate:
    """Estimate Databento cost for a watchlist.

    Bucketing trade-off: symbols are grouped by ``(dataset, start, end)`` so
    each bucket needs ONE ``metadata.get_cost`` call rather than per-symbol.
    Worst case (every symbol on a different dataset and window) collapses to
    one call per symbol; the common case (same dataset, same window across
    the watchlist) collapses to one call total.
    """
    today = today or date.today()

    buckets: dict[tuple[str, date, date], list[OnboardSymbolSpec]] = defaultdict(list)
    for spec in manifest.symbols:
        dataset = _ASSET_TO_DATASET.get(spec.asset_class)
        if dataset is None:
            # Fail loud rather than silently dropping the symbol from the
            # estimate — the API layer catches this and returns 422 so
            # cost_ceiling_usd enforcement stays structurally correct.
            raise UnpriceableAssetClassError(spec.symbol, spec.asset_class)
        buckets[(dataset, spec.start, spec.end)].append(spec)

    breakdown: list[CostLine] = []
    total = 0.0
    upstream_failure: str | None = None

    for (dataset, start, end), specs in buckets.items():
        symbols = [s.symbol for s in specs]
        try:
            bucket_usd = await asyncio.to_thread(
                client.metadata.get_cost,
                dataset=dataset,
                symbols=symbols,
                schema="ohlcv-1m",
                stype_in="raw_symbol",
                start=start.isoformat(),
                end=end.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "cost_estimator_upstream_error",
                dataset=dataset,
                symbols=symbols,
                error=repr(exc),
            )
            upstream_failure = f"unavailable: {type(exc).__name__}"
            continue

        per_symbol = float(bucket_usd) / max(len(specs), 1)
        for spec in specs:
            breakdown.append(
                CostLine(
                    symbol=spec.symbol,
                    asset_class=spec.asset_class,
                    dataset=dataset,
                    usd=per_symbol,
                )
            )
        total += float(bucket_usd)

    if upstream_failure is not None and not breakdown:
        return CostEstimate(
            total_usd=0.0,
            symbol_count=len(manifest.symbols),
            breakdown=[],
            confidence="low",
            basis=upstream_failure,
        )

    confidence: Literal["high", "medium", "low"] = _classify_confidence(manifest, today=today)
    basis = (
        "databento.metadata.get_cost (1m OHLCV)"
        if upstream_failure is None
        else f"partial: {upstream_failure}"
    )

    return CostEstimate(
        total_usd=total,
        symbol_count=len(manifest.symbols),
        breakdown=breakdown,
        confidence=confidence,
        basis=basis,
    )


def _classify_confidence(
    manifest: ParsedManifest, *, today: date
) -> Literal["high", "medium", "low"]:
    cutoff = today - timedelta(days=2)
    for spec in manifest.symbols:
        if spec.end >= cutoff:
            return "medium"
        if is_databento_continuous_pattern(spec.symbol):
            return "medium"
    return "high"
