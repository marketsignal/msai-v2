"""Symbol onboarding services — manifest parsing, cost estimation, coverage, orchestration.

Also exports ``normalize_asset_class_for_ingest`` — a single translation
seam from the user-facing/registry taxonomy (``equity | futures | fx |
option``; used by ``OnboardSymbolSpec.asset_class``) to the ingest /
Parquet storage taxonomy (``stocks | futures | forex | option``; used
by ``DataIngestionService.ingest_historical`` and the Parquet directory
layout ``{DATA_ROOT}/parquet/{asset_class}/{symbol}/...``).

Keep this in ONE place. Callers that cross the boundary (orchestrator,
cost estimator, coverage scanner) import this helper; they do not
hard-code either vocabulary.
"""

from __future__ import annotations

__all__ = ["normalize_asset_class_for_ingest"]


_REGISTRY_TO_INGEST: dict[str, str] = {
    "equity": "stocks",
    "futures": "futures",
    "fx": "forex",
    "option": "option",
}


def normalize_asset_class_for_ingest(registry_asset_class: str) -> str:
    """Translate the user-facing ``asset_class`` to the ingest taxonomy.

    Raises ``ValueError`` on unknown inputs — fail-loud so an unmapped
    asset class doesn't silently route to the wrong provider.
    """
    try:
        return _REGISTRY_TO_INGEST[registry_asset_class]
    except KeyError as exc:
        raise ValueError(
            f"Unknown registry asset_class {registry_asset_class!r}; "
            f"expected one of {sorted(_REGISTRY_TO_INGEST)}"
        ) from exc
