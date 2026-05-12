"""Integration tests for :meth:`SecurityMaster.resolve_for_backtest`.

Exercises the four paths of the registry-backed backtest resolve:

- Empty registry + bare ticker → ``DatabentoDefinitionMissing`` (operator
  hasn't run ``msai instruments refresh`` yet).
- ``.Z.N`` continuous pattern with no ``DatabentoClient`` configured →
  ``DatabentoClientUnavailableError`` (cold-miss requires the Databento
  fetch). Subclasses ``LookupError`` for symmetry with
  :class:`IBContractNotFoundError`.
- ``.Z.N`` happy path — mocked ``DatabentoClient.fetch_definition_instruments``
  + mocked ``resolved_databento_definition`` → synthesis path upserts a
  definition + active alias via the shared
  :meth:`SecurityMaster._upsert_definition_and_alias` helper and returns
  the synthetic Nautilus ``InstrumentId`` string.

Follows the per-module ``session_factory`` + ``isolated_postgres_url``
fixture pattern from ``test_security_master_resolve_live.py`` /
``test_instrument_registry.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models import Base
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.nautilus.security_master.continuous_futures import (
    ResolvedInstrumentDefinition,
)
from msai.services.nautilus.security_master.service import (
    DatabentoClientUnavailableError,
    DatabentoDefinitionMissing,
    SecurityMaster,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest_asyncio.fixture
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_for_backtest_raises_on_empty_registry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty registry + bare ticker → fail-loud DatabentoDefinitionMissing.

    Backtests must NOT call IB on cold-miss — the operator is expected to
    pre-warm the registry first. For Databento equity/ETF symbols the
    correct CLI is ``msai instruments bootstrap --provider databento``
    (Codex revised Item 4 / 2026-05-12). The error message previously
    suggested ``msai instruments refresh``, which is the IB-qualification
    path and silently fails for Databento stocks because the resolver
    inside ``refresh`` cold-misses without bootstrap having run first.
    """
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)

        with pytest.raises(DatabentoDefinitionMissing) as exc:
            await sm.resolve_for_backtest(["AAPL"])

        msg = str(exc.value)
        assert "AAPL" in msg
        # Must point to the correct provider-specific CLI subcommand.
        assert "instruments bootstrap" in msg
        assert "--provider databento" in msg
        # Regression guard: do NOT suggest the IB-only ``refresh`` subcommand
        # for a Databento cold-miss.
        assert "instruments refresh" not in msg


@pytest.mark.asyncio
async def test_resolve_for_backtest_continuous_requires_databento_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``.Z.N`` cold-miss + ``databento_client=None`` → typed exception.

    The continuous-futures synthesis path needs a live
    :class:`DatabentoClient` to download the ``.definition.dbn.zst`` file.
    Constructing the :class:`SecurityMaster` with ``databento_client=None``
    and requesting a ``.Z.N`` symbol must raise
    :class:`DatabentoClientUnavailableError` (subclass of ``LookupError``,
    symmetric with :class:`IBContractNotFoundError`) rather than silently
    dereferencing ``None``.
    """
    async with session_factory() as session:
        sm = SecurityMaster(db=session, databento_client=None)

        with pytest.raises(DatabentoClientUnavailableError, match="DatabentoClient required"):
            await sm.resolve_for_backtest(["ES.Z.0"], start="2024-01-01", end="2024-03-01")

        # Symmetry contract: subclass of LookupError so cross-provider
        # cold-miss handlers can catch the IB + Databento side together.
        with pytest.raises(LookupError):
            await sm.resolve_for_backtest(["ES.Z.0"], start="2024-01-01", end="2024-03-01")


@pytest.mark.asyncio
async def test_resolve_for_backtest_continuous_happy_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``.Z.N`` cold-miss happy path — synthesis writes definition + alias.

    Mocks ``DatabentoClient.fetch_definition_instruments`` (returns a sentinel
    list; we don't care about the actual Nautilus objects because
    ``resolved_databento_definition`` is also mocked) and patches the
    synthesis helper at the *call site* inside ``service.py`` so the
    :meth:`_resolve_databento_continuous` path lines up with the mocked
    return value.

    Asserts:
      1. The returned list contains the synthetic ``{raw}.{venue}`` id.
      2. An :class:`InstrumentDefinition` row is inserted with
         provider=``databento``.
      3. An :class:`InstrumentAlias` row with venue_format
         ``databento_continuous`` is inserted.
      4. The Databento client was called once with the expected args.
    """
    async with session_factory() as session:
        fake_instruments = [MagicMock()]  # sentinel — opaque to the SUT
        mock_databento = MagicMock()
        mock_databento.fetch_definition_instruments = AsyncMock(return_value=fake_instruments)

        sm = SecurityMaster(db=session, databento_client=mock_databento)

        resolved = ResolvedInstrumentDefinition(
            instrument_id="ES.Z.0.CME",
            raw_symbol="ES.Z.0",
            listing_venue="CME",
            routing_venue="CME",
            asset_class="futures",
            provider="databento",
            contract_details={
                "dataset": "GLBX.MDP3",
                "schema": "definition",
                "definition_start": "2024-01-01",
                "definition_end": "2024-03-01",
                "definition_file_path": "(mocked)",
                "requested_symbol": "ES.Z.0",
                "underlying_instrument_id": "ESH4.CME",
                "underlying_raw_symbol": "ESH4",
            },
        )

        with patch(
            "msai.services.nautilus.security_master.service.resolved_databento_definition",
            return_value=resolved,
        ) as mock_resolved:
            # Act
            ids = await sm.resolve_for_backtest(["ES.Z.0"], start="2024-01-01", end="2024-03-01")

        # Assert — return value
        assert ids == ["ES.Z.0.CME"]

        # Assert — Databento client was invoked once
        mock_databento.fetch_definition_instruments.assert_awaited_once()
        call = mock_databento.fetch_definition_instruments.await_args
        assert call.args[0] == "ES.Z.0"
        assert call.kwargs["dataset"] == "GLBX.MDP3"

        # Assert — synthesis helper was invoked once with our fake instruments
        mock_resolved.assert_called_once()
        assert mock_resolved.call_args.kwargs["instruments"] is fake_instruments

        # Assert — definition row was upserted
        from sqlalchemy import select

        idef_row = (
            await session.execute(
                select(InstrumentDefinition).where(
                    InstrumentDefinition.raw_symbol == "ES.Z.0",
                    InstrumentDefinition.provider == "databento",
                )
            )
        ).scalar_one()
        assert idef_row.listing_venue == "CME"
        assert idef_row.routing_venue == "CME"
        assert idef_row.asset_class == "futures"

        # Assert — alias row uses venue_format=databento_continuous
        alias_row = (
            await session.execute(
                select(InstrumentAlias).where(
                    InstrumentAlias.alias_string == "ES.Z.0.CME",
                    InstrumentAlias.provider == "databento",
                )
            )
        ).scalar_one()
        assert alias_row.venue_format == "databento_continuous"
        assert alias_row.effective_to is None


async def _seed_aapl_with_venue_swap(
    session: AsyncSession,
) -> InstrumentDefinition:
    """Seed AAPL under two consecutive venue aliases.

    - ``AAPL.NASDAQ`` active 2020-01-01 → 2023-01-01 (closed window).
    - ``AAPL.ARCA`` active 2023-01-01 → NULL (open window, currently live).

    Used by the start-date windowing tests below.
    """
    from datetime import date

    idef = InstrumentDefinition(
        raw_symbol="AAPL",
        listing_venue="NASDAQ",
        routing_venue="NASDAQ",
        asset_class="equity",
        provider="databento",
        roll_policy="none",
        lifecycle_state="active",
    )
    session.add(idef)
    await session.flush()

    session.add_all(
        [
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="AAPL.NASDAQ",
                venue_format="exchange_name",
                provider="databento",
                effective_from=date(2020, 1, 1),
                effective_to=date(2023, 1, 1),
            ),
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="AAPL.ARCA",
                venue_format="exchange_name",
                provider="databento",
                effective_from=date(2023, 1, 1),
                effective_to=None,
            ),
        ]
    )
    await session.commit()
    return idef


@pytest.mark.asyncio
async def test_resolve_for_backtest_dotted_alias_honors_start_date(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Path 2 (dotted alias) must window ``find_by_alias`` by ``start``.

    Historical backtest with ``start="2022-06-01"`` requests
    ``AAPL.NASDAQ`` — the alias active in 2022 (closed 2023). With today
    > 2023 the default (today) windowing in ``find_by_alias`` returns
    ``None`` and the resolver raises ``DatabentoDefinitionMissing``.
    After the fix, threading ``start`` into ``find_by_alias`` hits the
    closed-window row and returns the original ``AAPL.NASDAQ`` string.
    """
    async with session_factory() as session:
        await _seed_aapl_with_venue_swap(session)
        sm = SecurityMaster(db=session, databento_client=None)

        ids = await sm.resolve_for_backtest(["AAPL.NASDAQ"], start="2022-06-01", end="2022-12-31")

        assert ids == ["AAPL.NASDAQ"]


@pytest.mark.asyncio
async def test_resolve_for_backtest_bare_ticker_honors_start_date(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Path 3 (bare ticker) must pick the alias active on ``start``.

    With two aliases on the same definition (NASDAQ closed 2023, ARCA
    open), a historical backtest with ``start="2022-06-01"`` must
    resolve ``AAPL`` → ``AAPL.NASDAQ`` (the venue the symbol was
    actually listed on during the window). The current code picks the
    one with ``effective_to IS NULL`` → ``AAPL.ARCA``, which would
    mis-partition parquet reads / silently return wrong data.
    """
    async with session_factory() as session:
        await _seed_aapl_with_venue_swap(session)
        sm = SecurityMaster(db=session, databento_client=None)

        ids = await sm.resolve_for_backtest(["AAPL"], start="2022-06-01", end="2022-12-31")

        assert ids == ["AAPL.NASDAQ"]


@pytest.mark.asyncio
async def test_resolve_for_backtest_bare_ticker_no_start_uses_today(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression guard: ``start=None`` must still resolve to today's
    active alias.

    Same seed as the windowing tests, but no ``start`` passed → today's
    window is in [2023-01-01, NULL), so the bare-ticker path must return
    ``AAPL.ARCA``. Prevents the fix from over-correcting.
    """
    async with session_factory() as session:
        await _seed_aapl_with_venue_swap(session)
        sm = SecurityMaster(db=session, databento_client=None)

        ids = await sm.resolve_for_backtest(["AAPL"])

        assert ids == ["AAPL.ARCA"]


# ──────────────────────────────────────────────────────────────────────
# Databento MIC vs exchange-name read-boundary normalization
# (fresh-VM data-path closure — Codex revised Item 4, 2026-05-12)
#
# Background: ingest emits Databento MIC aliases (``AAPL.XNAS``); the
# registry write boundary translates MIC → exchange-name (``AAPL.NASDAQ``)
# via ``venue_normalization.normalize_alias_for_registry`` so the same row
# is visible to both backtest and the IB-backed ``lookup_for_live`` which
# exact-matches on the exchange-name form. The backtest resolver's path 2
# previously did NOT apply the same normalization, so a user submitting
# ``AAPL.XNAS`` (the form ``msai ingest stocks`` prints) cold-missed
# against an ``AAPL.NASDAQ`` registry row.
# ──────────────────────────────────────────────────────────────────────


async def _seed_aapl_nasdaq_only(session: AsyncSession) -> InstrumentDefinition:
    """Single-alias seed mirroring what ``msai instruments bootstrap`` writes today.

    One open-window ``AAPL.NASDAQ`` alias (exchange-name canonical form) on
    a Databento-provided definition with ``listing_venue=XNAS``. Matches
    the prod-VM row shape from the 2026-05-12 incident.
    """
    from datetime import date

    idef = InstrumentDefinition(
        raw_symbol="AAPL",
        listing_venue="XNAS",
        routing_venue="XNAS",
        asset_class="equity",
        provider="databento",
        roll_policy="none",
        lifecycle_state="active",
    )
    session.add(idef)
    await session.flush()
    session.add(
        InstrumentAlias(
            instrument_uid=idef.instrument_uid,
            alias_string="AAPL.NASDAQ",
            venue_format="exchange_name",
            provider="databento",
            source_venue_raw="XNAS",
            effective_from=date(2020, 1, 1),
            effective_to=None,
        )
    )
    await session.commit()
    return idef


@pytest.mark.asyncio
async def test_resolve_for_backtest_databento_mic_input_normalizes_to_exchange_name(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """User submits ``AAPL.XNAS`` (Databento MIC) → resolves to ``AAPL.NASDAQ``.

    Tonight's bug: ``msai ingest stocks`` prints ``instrument_id=AAPL.XNAS``,
    the operator submits a backtest with that string, the resolver path 2
    does an exact-match on ``AAPL.XNAS`` against a registry row written as
    ``AAPL.NASDAQ`` → 422. Fix: normalize Databento MIC input at the read
    boundary using the same map the writer uses.
    """
    async with session_factory() as session:
        await _seed_aapl_nasdaq_only(session)
        sm = SecurityMaster(db=session, databento_client=None)

        ids = await sm.resolve_for_backtest(["AAPL.XNAS"])

        # Returns the canonical registry alias, NOT the raw user input.
        assert ids == ["AAPL.NASDAQ"]


@pytest.mark.asyncio
async def test_resolve_for_backtest_exchange_name_input_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """User submits ``AAPL.NASDAQ`` (exchange-name) → still resolves.

    The read-boundary normalization must be idempotent on already-canonical
    inputs. Anyone who scripted against ``.NASDAQ`` (the form
    ``lookup_for_live`` documents) keeps working unchanged.
    """
    async with session_factory() as session:
        await _seed_aapl_nasdaq_only(session)
        sm = SecurityMaster(db=session, databento_client=None)

        ids = await sm.resolve_for_backtest(["AAPL.NASDAQ"])

        assert ids == ["AAPL.NASDAQ"]


async def _seed_aapl_xnas_to_nasdaq_swap(
    session: AsyncSession,
) -> InstrumentDefinition:
    """Two-alias seed: historical ``AAPL.XNAS`` (closed) → current ``AAPL.NASDAQ`` (open).

    Mirrors what real Databento + bootstrap state looks like after a
    bootstrap-pattern migration: an older row was written under the
    pre-``venue_normalization`` MIC form, then the writer changed and a
    new row was opened under the canonical exchange-name. The
    resolver must hit BOTH forms depending on the requested
    ``start_date``.

    Discovered by verify-e2e UC1/UC2 on 2026-05-12 — see report
    ``tests/e2e/reports/2026-05-12-14-15-fresh-vm-data-path-closure.md``.
    """
    from datetime import date

    idef = InstrumentDefinition(
        raw_symbol="AAPL",
        listing_venue="XNAS",
        routing_venue="XNAS",
        asset_class="equity",
        provider="databento",
        roll_policy="none",
        lifecycle_state="active",
    )
    session.add(idef)
    await session.flush()
    session.add_all(
        [
            # Historical row in MIC form (pre-normalization). Covers the
            # 2020 → 2025-11 window the user's 6-month backtest would land in.
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="AAPL.XNAS",
                venue_format="mic_code",
                provider="databento",
                source_venue_raw="XNAS",
                effective_from=date(2020, 1, 1),
                effective_to=date(2026, 4, 24),
            ),
            # Current row in exchange-name canonical form (post-normalization).
            InstrumentAlias(
                instrument_uid=idef.instrument_uid,
                alias_string="AAPL.NASDAQ",
                venue_format="exchange_name",
                provider="databento",
                source_venue_raw="XNAS",
                effective_from=date(2026, 4, 24),
                effective_to=None,
            ),
        ]
    )
    await session.commit()
    return idef


@pytest.mark.asyncio
async def test_resolve_for_backtest_mic_input_historical_window_hits_historical_alias(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``AAPL.XNAS`` + historical start_date → hits the MIC historical alias.

    E2E-discovered regression: when both ``AAPL.XNAS`` (historical) and
    ``AAPL.NASDAQ`` (current) aliases exist on the same definition,
    requesting a backtest with the MIC form and a historical start_date
    must hit the MIC row. Without the historical fallback in
    ``resolve_for_backtest`` path 2, the normalizer would rewrite
    ``AAPL.XNAS`` → ``AAPL.NASDAQ`` and the NASDAQ row's
    ``effective_from`` would mask the still-valid MIC row covering the
    requested date.
    """
    async with session_factory() as session:
        await _seed_aapl_xnas_to_nasdaq_swap(session)
        sm = SecurityMaster(db=session, databento_client=None)

        # 2025-11-03 falls in the historical XNAS window (2020-01-01 →
        # 2026-04-24). Submit with the MIC form (what ``msai ingest stocks``
        # would print to the user during that era).
        ids = await sm.resolve_for_backtest(["AAPL.XNAS"], start="2025-11-03", end="2026-04-29")

        # Returns the historical MIC form — the alias the registry actually
        # has for that date — NOT the normalized form (which has no row
        # covering 2025-11-03).
        assert ids == ["AAPL.XNAS"]


@pytest.mark.asyncio
async def test_resolve_for_backtest_mic_input_current_window_hits_normalized_alias(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``AAPL.XNAS`` + post-cutover start_date → hits the NASDAQ canonical alias.

    The fallback's "no-regression" partner to
    ``test_resolve_for_backtest_mic_input_historical_window_hits_historical_alias``:
    when the requested date falls in the CURRENT (post-normalization)
    window, the user's MIC input must still resolve via the canonical
    NASDAQ alias. The fallback is ordered try-canonical-first, so this
    case never reaches the historical fallback.
    """
    async with session_factory() as session:
        await _seed_aapl_xnas_to_nasdaq_swap(session)
        sm = SecurityMaster(db=session, databento_client=None)

        # 2026-05-01 is after the XNAS→NASDAQ cutover (2026-04-24).
        ids = await sm.resolve_for_backtest(["AAPL.XNAS"], start="2026-05-01", end="2026-05-10")

        # Returns the canonical NASDAQ form (the row that actually covers
        # the date), NOT the user's MIC input.
        assert ids == ["AAPL.NASDAQ"]


async def _seed_aapl_xnas_only(session: AsyncSession) -> InstrumentDefinition:
    """Single-row seed with ONLY the MIC alias (``AAPL.XNAS``), no NASDAQ row.

    Mirrors a real prod state where bootstrap ran BEFORE
    ``venue_normalization`` shipped — every row was stored in MIC form.
    Discovered by verify-e2e pass-2 UC2 on 2026-05-12.
    """
    from datetime import date

    idef = InstrumentDefinition(
        raw_symbol="AAPL",
        listing_venue="XNAS",
        routing_venue="XNAS",
        asset_class="equity",
        provider="databento",
        roll_policy="none",
        lifecycle_state="active",
    )
    session.add(idef)
    await session.flush()
    session.add(
        InstrumentAlias(
            instrument_uid=idef.instrument_uid,
            alias_string="AAPL.XNAS",
            venue_format="mic_code",
            provider="databento",
            source_venue_raw="XNAS",
            effective_from=date(2020, 1, 1),
            effective_to=None,
        )
    )
    await session.commit()
    return idef


@pytest.mark.asyncio
async def test_resolve_for_backtest_exchange_name_input_falls_back_to_raw_symbol(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``AAPL.NASDAQ`` input + only ``AAPL.XNAS`` in registry → raw_symbol fallback finds it.

    Symmetric to the MIC-historical case but for the *forward* direction:
    user supplies the documented exchange-name form, registry has only
    the MIC form (legacy state). Direct alias-form lookups both miss
    (``AAPL.NASDAQ`` and ``AAPL.NASDAQ`` are identical after the no-op
    idempotency-preserving normalization). The path-2c raw_symbol
    fallback derives ``AAPL`` from the dotted input and returns the
    active alias — which IS ``AAPL.XNAS`` in this seed.
    """
    async with session_factory() as session:
        await _seed_aapl_xnas_only(session)
        sm = SecurityMaster(db=session, databento_client=None)

        ids = await sm.resolve_for_backtest(["AAPL.NASDAQ"])

        # Returns the actual registry alias, NOT the user input. This is
        # the contract for the raw_symbol fallback: align downstream
        # catalog reads with the row that holds data.
        assert ids == ["AAPL.XNAS"]


@pytest.mark.asyncio
async def test_resolve_for_backtest_venue_mismatch_fails_loud(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """User submits ``AAPL.NYSE`` (wrong venue) → fails loud, not silent.

    Codex P2 catch (PR #61 round 4): the path-2c raw_symbol fallback
    was too lenient — when the user typed an alias with a venue
    suffix that names a DIFFERENT instrument than the registry's
    listing venue (e.g. ``AAPL.NYSE`` while AAPL is on NASDAQ),
    silently returning ``AAPL.NASDAQ`` would let the backtest run
    against the wrong-venue contract. The fix: in path 2c, compare
    the normalized form of the user input with the normalized form
    of the active alias; if they differ, raise.
    """
    async with session_factory() as session:
        await _seed_aapl_nasdaq_only(session)  # seeded earlier in this file
        sm = SecurityMaster(db=session, databento_client=None)

        with pytest.raises(DatabentoDefinitionMissing) as exc:
            await sm.resolve_for_backtest(["AAPL.NYSE"])

        msg = str(exc.value)
        # The error must clearly name the venue mismatch + both sides.
        assert "Venue mismatch" in msg
        assert "AAPL.NYSE" in msg
        assert "AAPL.NASDAQ" in msg


@pytest.mark.asyncio
async def test_resolve_for_backtest_unknown_mic_fails_loudly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unmapped MIC (``AAPL.FAKEMIC``) must surface as a typed miss.

    Fail-loud is mandatory at this boundary — silent passthrough would
    recreate the invisible-row failure mode for new venues. The resolver
    must NOT confuse a write-boundary lineage issue with the read-boundary
    rejection.
    """
    async with session_factory() as session:
        await _seed_aapl_nasdaq_only(session)
        sm = SecurityMaster(db=session, databento_client=None)

        with pytest.raises(DatabentoDefinitionMissing) as exc:
            await sm.resolve_for_backtest(["AAPL.FAKEMIC"])

        # The error must mention the operator-facing CLI to recover the
        # registry, and that command for Databento stocks is ``bootstrap``,
        # NOT the ``refresh`` (futures/IB) the legacy message suggested.
        assert "bootstrap" in str(exc.value).lower()
