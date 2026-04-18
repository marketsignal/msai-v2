"""End-to-end continuous-futures backtest resolve test.

Placeholder for the ``.Z.N`` cold-miss path:

1. ``raw_symbol_from_request("ES.Z.5")`` extracts the root.
2. Registry cold-miss under ``provider='databento'``.
3. :meth:`DatabentoClient.fetch_definition_instruments` is invoked against a
   committed ``ES_Z_5_small.definition.dbn.zst`` fixture to decode a handful
   of contract rows.
4. :func:`resolved_databento_definition` synthesizes a
   :class:`ResolvedInstrumentDefinition` with
   ``instrument_id = 'ES.Z.5.CME'``.
5. :meth:`SecurityMaster._upsert_definition_and_alias` writes a
   ``(raw_symbol='ES.Z.5', provider='databento',
   venue_format='databento_continuous')`` row.
6. A subsequent warm-path call returns the same synthetic id without
   re-fetching.

Skipped by default: the full DBN fixture isn't committed (binary +
size). Operators regenerate it via::

    msai data ingest --symbols ES.Z.5

then flesh out the assertions below.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_PATH = (
    Path(__file__).parent / "../fixtures/databento/ES_Z_5_small.definition.dbn.zst"
).resolve()


@pytest.mark.skipif(
    not FIXTURE_PATH.exists(),
    reason=(
        "Databento fixture not present — regenerate via "
        "`msai data ingest --symbols ES.Z.5`"
    ),
)
@pytest.mark.asyncio
async def test_continuous_futures_backtest_synthesizes_instrument() -> None:
    """End-to-end: ``.Z.N`` pattern → registry cold miss → DatabentoClient
    fetches definition.dbn.zst → ``resolved_databento_definition`` synthesizes
    → registry upsert → return synthetic canonical alias.

    Placeholder — full test requires:

    1. A real DBN fixture at :data:`FIXTURE_PATH` (not committed due to size).
    2. Mock ``DatabentoClient.fetch_definition_instruments`` to return
       instruments decoded from the fixture.
    3. Assert ``resolve_for_backtest(['ES.Z.5'], start=..., end=...)`` returns
       ``'ES.Z.5.CME'``.
    4. Assert registry row upserted with
       ``venue_format='databento_continuous'``.
    """
    pytest.skip("Full continuous-futures e2e test pending fixture commit")
