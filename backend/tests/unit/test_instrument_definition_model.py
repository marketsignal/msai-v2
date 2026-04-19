from __future__ import annotations

import uuid
from datetime import date

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition


def test_instrument_definition_accepts_basic_row() -> None:
    idef = InstrumentDefinition(
        raw_symbol="AAPL",
        listing_venue="NASDAQ",
        routing_venue="NASDAQ",
        asset_class="equity",
        provider="interactive_brokers",
    )
    assert idef.instrument_uid is None or isinstance(idef.instrument_uid, uuid.UUID)


def test_instrument_alias_accepts_basic_row() -> None:
    uid = uuid.uuid4()
    alias = InstrumentAlias(
        instrument_uid=uid,
        alias_string="AAPL.NASDAQ",
        venue_format="exchange_name",
        provider="interactive_brokers",
        effective_from=date(2026, 1, 1),
    )
    assert alias.alias_string == "AAPL.NASDAQ"
