"""Shared type aliases for the security_master surface.

The codebase has multiple distinct asset-class taxonomies plus a provider
enum. They are stringly-typed at SQL boundaries (CHECK constraints,
JSONB payloads) but the application code can use ``Literal`` types
so mypy --strict catches typo bugs that would otherwise route Parquet
writes to the wrong directory or warm-hit the wrong registry row.
"""

from __future__ import annotations

from typing import Literal

RegistryAssetClass = Literal["equity", "futures", "fx", "option", "crypto"]
"""Postgres CHECK constraint values for ``instrument_definitions.asset_class``
(``ck_instrument_definitions_asset_class``).
"""

IngestAssetClass = Literal["stocks", "futures", "options", "forex", "crypto"]
"""Ingest / Parquet directory taxonomy (``data/parquet/<class>/``)."""

Provider = Literal["interactive_brokers", "databento"]
"""Source provider for ``instrument_definitions.provider`` and
``instrument_aliases.provider``."""

VenueFormat = Literal["exchange_name", "mic_code", "databento_continuous"]
"""``instrument_aliases.venue_format`` — distinguishes raw Databento MIC
from normalized exchange-name (PR #44 venue normalization) and
continuous-futures aliases."""


REGISTRY_TO_INGEST_ASSET_CLASS: dict[RegistryAssetClass, IngestAssetClass] = {
    "equity": "stocks",
    "futures": "futures",
    "option": "options",
    "fx": "forex",
    "crypto": "crypto",
}
"""Canonical map from registry asset_class taxonomy (per
``ck_instrument_definitions_asset_class`` CHECK:
``equity|futures|fx|option|crypto``) to the ingest / Parquet-storage
taxonomy (``stocks|futures|options|forex|crypto``).

Consumed by both :class:`SecurityMaster` and the symbol-onboarding
orchestrator so the two paths never drift on the option/options key.
Hold this in ONE place — a parallel map in either caller is the kind
of silent-routing hazard
:meth:`SecurityMaster._upsert_definition_and_alias` warns about (Parquet
writes to the wrong directory).
"""
