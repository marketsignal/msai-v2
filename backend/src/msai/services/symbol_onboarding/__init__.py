"""Symbol onboarding services — manifest parsing, cost estimation, coverage, orchestration.

Also exports ``normalize_asset_class_for_ingest`` — a single translation
seam from the user-facing/registry taxonomy (``equity | futures | fx |
option | crypto``; used by ``OnboardSymbolSpec.asset_class``) to the
ingest / Parquet storage taxonomy (``stocks | futures | forex |
options | crypto``; used by ``DataIngestionService.ingest_historical``
and the Parquet directory layout
``{DATA_ROOT}/parquet/{asset_class}/{symbol}/...``).

The map itself lives at
:data:`msai.services.nautilus.security_master.types.REGISTRY_TO_INGEST_ASSET_CLASS`
so this module and :class:`SecurityMaster` cannot drift on the option/options
key — drift would silently route Parquet writes to two different directories.
"""

from __future__ import annotations

from msai.services.nautilus.security_master.types import REGISTRY_TO_INGEST_ASSET_CLASS

__all__ = ["normalize_asset_class_for_ingest"]


def normalize_asset_class_for_ingest(registry_asset_class: str) -> str:
    """Translate the user-facing ``asset_class`` to the ingest taxonomy.

    Raises ``ValueError`` on unknown inputs — fail-loud so an unmapped
    asset class doesn't silently route to the wrong provider.
    """
    try:
        return REGISTRY_TO_INGEST_ASSET_CLASS[registry_asset_class]  # type: ignore[index]
    except KeyError as exc:
        raise ValueError(
            f"Unknown registry asset_class {registry_asset_class!r}; "
            f"expected one of {sorted(REGISTRY_TO_INGEST_ASSET_CLASS)}"
        ) from exc
