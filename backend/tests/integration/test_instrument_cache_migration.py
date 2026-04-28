"""Integration tests for instrument-cache → registry migration revisions.

Revision A: d1e2f3g4h5i6 (additive trading_hours column).
Revision B: e2f3g4h5i6j7 (data migration + DROP instrument_cache).

Uses the project's testcontainers Postgres pattern from
test_instrument_cache_model.py / test_security_master.py — per-module
session_factory + isolated_postgres_url fixtures.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from tests.integration._alembic_subprocess import run_alembic, run_alembic_raw

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

# Re-export so ruff/F401 doesn't strip the runtime imports needed by the
# session_factory fixture decorator below.
_ = (pytest_asyncio, async_sessionmaker)


REV_A = "d1e2f3g4h5i6"
REV_B = "e2f3g4h5i6j7"
PRIOR_HEAD = "c7d8e9f0a1b2"  # down_revision of REV_A — alembic doesn't support `REV^` syntax


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


def _run_alembic(cmd: list[str], db_url: str) -> None:
    """Run alembic; raise on non-zero exit. Thin wrapper around the
    shared helper that preserves the local ``(cmd, db_url)`` calling
    convention used throughout this module.
    """
    run_alembic(db_url, *cmd)


def _run_alembic_raw(cmd: list[str], db_url: str) -> subprocess.CompletedProcess[str]:
    """Run alembic without raising; for fail-loud branch tests."""
    return run_alembic_raw(db_url, *cmd)


@pytest.mark.asyncio
async def test_revision_a_adds_trading_hours_column_to_instrument_definitions(
    isolated_postgres_url: str,
) -> None:
    # Arrange — migrate up to PRIOR head, confirm column doesn't exist
    _run_alembic(["upgrade", PRIOR_HEAD], isolated_postgres_url)
    engine: AsyncEngine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='instrument_definitions' "
                "AND column_name='trading_hours'"
            )
        )
        assert result.scalar_one_or_none() is None, "column should not exist before rev A"

    # Act — migrate up to revision A
    _run_alembic(["upgrade", REV_A], isolated_postgres_url)

    # Assert — column exists with correct type + nullable
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name='instrument_definitions' AND column_name='trading_hours'"
            )
        )
        row = result.one_or_none()
        assert row is not None, "trading_hours column missing after rev A"
        assert row[0] == "jsonb", f"expected jsonb, got {row[0]}"
        assert row[1] == "YES", f"expected nullable, got {row[1]}"

    await engine.dispose()


@pytest.mark.asyncio
async def test_revision_a_downgrade_removes_trading_hours_column(
    isolated_postgres_url: str,
) -> None:
    # Arrange — at revision A
    _run_alembic(["upgrade", REV_A], isolated_postgres_url)

    # Act — downgrade
    _run_alembic(["downgrade", "-1"], isolated_postgres_url)

    # Assert — column is gone
    engine: AsyncEngine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='instrument_definitions' AND column_name='trading_hours'"
            )
        )
        assert result.scalar_one_or_none() is None, "trading_hours column should be dropped"
    await engine.dispose()


@pytest.mark.asyncio
async def test_revision_b_migrates_cache_rows_to_registry_then_drops_table(
    isolated_postgres_url: str,
) -> None:
    """Revision B: copy instrument_cache → registry, then DROP instrument_cache."""
    # Arrange — migrate to revision A, seed instrument_cache with one row
    _run_alembic(["upgrade", REV_A], isolated_postgres_url)
    engine: AsyncEngine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO instrument_cache
                  (canonical_id, asset_class, venue, ib_contract_json,
                   nautilus_instrument_json, trading_hours, last_refreshed_at,
                   created_at, updated_at)
                VALUES (
                  'AAPL.NASDAQ', 'equity', 'NASDAQ',
                  '{"secType":"STK","symbol":"AAPL","exchange":"SMART"}',
                  '{"type":"Equity","id":"AAPL.NASDAQ"}',
                  '{"timezone":"America/New_York","rth":[],"eth":[]}',
                  now(), now(), now()
                )
                """
            )
        )

    # Act — apply revision B
    _run_alembic(["upgrade", REV_B], isolated_postgres_url)

    # Assert — cache table dropped + registry has the row
    async with engine.connect() as conn:
        # Table is gone
        tables = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name='instrument_cache'"
            )
        )
        assert tables.scalar_one_or_none() is None, "instrument_cache should be dropped"

        # instrument_definitions has the migrated row
        defs = await conn.execute(
            text(
                """
                SELECT raw_symbol, asset_class, listing_venue, routing_venue, trading_hours
                FROM instrument_definitions
                WHERE raw_symbol='AAPL'
                """
            )
        )
        row = defs.one_or_none()
        assert row is not None
        assert row[0] == "AAPL"
        assert row[1] == "equity"
        assert row[2] == "NASDAQ"
        # routing_venue prefers ib_contract_json["exchange"] = "SMART"
        assert row[3] == "SMART"
        assert row[4] == {"timezone": "America/New_York", "rth": [], "eth": []}

        # instrument_aliases has the matching alias
        aliases = await conn.execute(
            text(
                """
                SELECT alias_string, provider, effective_from FROM instrument_aliases
                WHERE alias_string='AAPL.NASDAQ'
                """
            )
        )
        alias_row = aliases.one_or_none()
        assert alias_row is not None
        assert alias_row[1] == "interactive_brokers"
        # Pin the sentinel: the migration writes ``effective_from =
        # date(2000, 1, 1)`` (NOT ``date.today()``) to dodge the
        # UTC-vs-Chicago timezone trap and let idempotent re-runs collapse
        # on the ``(alias_string, provider, effective_from)`` unique
        # constraint regardless of which calendar day they execute. A
        # "helpful refactor" that regresses to ``date.today()`` MUST
        # fail this test.
        assert alias_row[2] == date(2000, 1, 1), (
            f"effective_from should be the migration's sentinel "
            f"date(2000, 1, 1); got {alias_row[2]!r}"
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_revision_b_idempotent_when_rerun_against_seeded_registry(
    isolated_postgres_url: str,
) -> None:
    """Revision B uses ON CONFLICT — running it against a registry that
    already has the row is a no-op (single definition stays single)."""
    # Reset registry state — module-scoped Postgres carries rows from prior
    # tests in the same module, and re-running rev B against a non-empty
    # state is exactly what we want to test, but we need a deterministic
    # starting point. Downgrade B (recreates empty instrument_cache table),
    # then truncate registry rows so the pre-seed below owns the state.
    _run_alembic(["downgrade", REV_A], isolated_postgres_url)
    engine: AsyncEngine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE instrument_aliases, instrument_definitions CASCADE"))

    aapl_uid = uuid4()
    async with engine.begin() as conn:
        # Pre-seed registry with AAPL
        await conn.execute(
            text(
                """
                INSERT INTO instrument_definitions
                  (instrument_uid, raw_symbol, provider, asset_class,
                   listing_venue, routing_venue, lifecycle_state, created_at, updated_at)
                VALUES (:uid, 'AAPL', 'interactive_brokers', 'equity',
                        'NASDAQ', 'SMART', 'active', now(), now())
                """
            ),
            {"uid": str(aapl_uid)},
        )
        await conn.execute(
            text(
                """
                INSERT INTO instrument_aliases
                  (id, instrument_uid, alias_string, venue_format, provider,
                   effective_from, created_at)
                VALUES (:aid, :uid, 'AAPL.NASDAQ', 'exchange_name',
                        'interactive_brokers', '2026-01-01', now())
                """
            ),
            {"aid": str(uuid4()), "uid": str(aapl_uid)},
        )
        # Seed instrument_cache with the same canonical
        await conn.execute(
            text(
                """
                INSERT INTO instrument_cache
                  (canonical_id, asset_class, venue, ib_contract_json,
                   nautilus_instrument_json, last_refreshed_at, created_at, updated_at)
                VALUES ('AAPL.NASDAQ', 'equity', 'NASDAQ', '{}', '{}',
                        now(), now(), now())
                """
            )
        )

    # Act — rev B should NOT raise on the duplicate
    _run_alembic(["upgrade", REV_B], isolated_postgres_url)

    # Assert — registry still has exactly one AAPL definition
    async with engine.connect() as conn:
        count = await conn.execute(
            text("SELECT count(*) FROM instrument_definitions WHERE raw_symbol='AAPL'")
        )
        assert count.scalar_one() == 1
    await engine.dispose()


# ---------------------------------------------------------------------------
# T8: preflight script tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory(
    isolated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Per-test session factory that ensures the schema is at alembic head
    and clears all rows the preflight tests touch (registry tables +
    portfolio/deployment/user/strategy chain) so each preflight test
    starts from a deterministic empty state.

    Module-scoped Postgres carries rows from sibling tests; this fixture
    is the cleanup boundary for the preflight class of tests.
    """
    _run_alembic(["upgrade", "head"], isolated_postgres_url)

    engine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE live_deployments, live_portfolio_revision_strategies, "
                "live_portfolio_revisions, live_portfolios, "
                "instrument_aliases, instrument_definitions, "
                "strategies, users CASCADE"
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_preflight_passes_when_registry_covers_active_deployments(
    isolated_postgres_url: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preflight exits 0 when every active deployment's strategy
    instruments resolve through the registry today."""

    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition
    from tests.integration._deployment_factory import make_live_deployment

    # Arrange — seed registry with AAPL.NASDAQ; create a deployment whose
    # revision-strategy member references "AAPL".
    async with session_factory() as session:
        aapl_uid = uuid4()
        session.add(
            InstrumentDefinition(
                instrument_uid=aapl_uid,
                raw_symbol="AAPL",
                provider="interactive_brokers",
                asset_class="equity",
                listing_venue="NASDAQ",
                routing_venue="SMART",
                lifecycle_state="active",
            )
        )
        session.add(
            InstrumentAlias(
                id=uuid4(),
                instrument_uid=aapl_uid,
                alias_string="AAPL.NASDAQ",
                venue_format="exchange_name",
                provider="interactive_brokers",
                effective_from=date(2026, 1, 1),
                effective_to=None,
            )
        )
        await session.commit()

        await make_live_deployment(session, status="running", member_instruments=["AAPL"])
        await session.commit()

    # Act
    backend_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/preflight_cache_migration.py"],
        cwd=backend_root,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": isolated_postgres_url,
            "PYTHONPATH": str(backend_root / "src"),
        },
    )

    # Assert
    assert result.returncode == 0, (
        f"preflight should pass; stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "preflight passed" in result.stdout.lower(), result.stdout


@pytest.mark.asyncio
async def test_preflight_fails_with_operator_hint_on_missing_alias(
    isolated_postgres_url: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preflight exits non-zero with `msai instruments refresh` hint
    when an active deployment's strategy instruments has a miss."""
    from tests.integration._deployment_factory import make_live_deployment

    # Arrange — registry has NO alias for ES; deployment references it.
    async with session_factory() as session:
        await make_live_deployment(session, status="running", member_instruments=["ES"])
        await session.commit()

    # Act
    backend_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/preflight_cache_migration.py"],
        cwd=backend_root,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": isolated_postgres_url,
            "PYTHONPATH": str(backend_root / "src"),
        },
    )

    # Assert
    assert result.returncode != 0, f"preflight should fail; stdout:\n{result.stdout}"
    combined = result.stdout + result.stderr
    assert "ES" in combined
    assert "msai instruments refresh" in combined


@pytest.mark.asyncio
async def test_preflight_fails_on_active_deployment_with_zero_member_rows(
    isolated_postgres_url: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``LiveDeployment`` in ``running`` state with zero
    ``LivePortfolioRevisionStrategy`` rows is corrupt state — the
    supervisor rejects it as fatal at spawn time. Preflight surfaces
    this BEFORE cutover so the operator can fix it.

    Arrange the corrupt state directly (the factory creates a member
    by default; we want zero members). Then assert the script exits
    non-zero with the specific ``ZERO `live_portfolio_revision_strategies```
    failure phrase.
    """
    from msai.models.live_deployment import LiveDeployment
    from msai.models.live_portfolio import LivePortfolio
    from msai.models.live_portfolio_revision import LivePortfolioRevision
    from msai.models.user import User
    from msai.services.live.deployment_identity import (
        derive_message_bus_stream,
        derive_strategy_id_full,
        derive_trader_id,
        generate_deployment_slug,
    )

    async with session_factory() as session:
        user = User(
            id=uuid4(),
            entra_id=f"empty-{uuid4().hex}",
            email=f"empty-{uuid4().hex}@test.com",
            role="trader",
        )
        session.add(user)
        await session.flush()

        portfolio = LivePortfolio(
            id=uuid4(),
            name=f"empty-rev-{uuid4().hex[:8]}",
            description="zero-member fixture",
            created_by=user.id,
        )
        session.add(portfolio)
        await session.flush()

        revision = LivePortfolioRevision(
            id=uuid4(),
            portfolio_id=portfolio.id,
            revision_number=1,
            composition_hash=uuid4().hex + uuid4().hex,
            is_frozen=True,
        )
        session.add(revision)
        await session.flush()
        # Intentionally NO LivePortfolioRevisionStrategy row — corrupt state.

        slug = generate_deployment_slug()
        deployment = LiveDeployment(
            id=uuid4(),
            strategy_id=None,
            status="running",
            paper_trading=True,
            started_by=user.id,
            deployment_slug=slug,
            identity_signature=uuid4().hex + uuid4().hex,
            trader_id=derive_trader_id(slug),
            strategy_id_full=derive_strategy_id_full("EMACrossStrategy", slug),
            account_id="DU1234567",
            ib_login_key="msai-paper-primary",
            portfolio_revision_id=revision.id,
            message_bus_stream=derive_message_bus_stream(slug),
        )
        session.add(deployment)
        await session.commit()

    # Act
    backend_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/preflight_cache_migration.py"],
        cwd=backend_root,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": isolated_postgres_url,
            "PYTHONPATH": str(backend_root / "src"),
        },
    )

    # Assert — exit non-zero + names the empty deployment + the constraint.
    assert result.returncode != 0, (
        f"preflight should fail on zero member rows; "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "ZERO `live_portfolio_revision_strategies`" in combined, combined


# ---------------------------------------------------------------------------
# T13: Full migration round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_round_trip_upgrade_a_b_downgrade_b_a_re_upgrade(
    isolated_postgres_url: str,
) -> None:
    """End-to-end: A up → B up → B down (data lost, schema-only) →
    A down → A up → B up → still works."""
    _run_alembic(["upgrade", REV_A], isolated_postgres_url)
    _run_alembic(["upgrade", REV_B], isolated_postgres_url)
    _run_alembic(["downgrade", "-1"], isolated_postgres_url)  # back to A
    _run_alembic(["downgrade", "-1"], isolated_postgres_url)  # back to PRIOR_HEAD
    _run_alembic(["upgrade", REV_A], isolated_postgres_url)
    _run_alembic(["upgrade", REV_B], isolated_postgres_url)

    # Final state assertion
    engine: AsyncEngine = create_async_engine(isolated_postgres_url, future=True)
    async with engine.connect() as conn:
        head = await conn.execute(text("SELECT version_num FROM alembic_version"))
        assert head.scalar_one() == REV_B
    await engine.dispose()


# ---------------------------------------------------------------------------
# Iter-1 review fix tests — coverage gaps + fail-loud branches
# ---------------------------------------------------------------------------


async def _reset_to_rev_a_with_clean_state(db_url: str) -> AsyncEngine:
    """Reset to REV_A with empty instrument_cache + registry tables.

    Module-scoped Postgres carries rows + version state from prior tests.
    Strategy: try to downgrade to REV_A first (recreates empty cache table
    if we were at REV_B); if that fails (already at or below REV_A), upgrade
    to REV_A. Then truncate cache + registry so each test owns its ARRANGE
    state cleanly.
    """
    downgrade_result = _run_alembic_raw(["downgrade", REV_A], db_url)
    if downgrade_result.returncode != 0:
        _run_alembic(["upgrade", REV_A], db_url)

    engine: AsyncEngine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE instrument_cache, instrument_aliases, instrument_definitions CASCADE")
        )
    return engine


@pytest.mark.asyncio
async def test_revision_b_preserves_existing_trading_hours_on_conflict(
    isolated_postgres_url: str,
) -> None:
    """ON CONFLICT path: pre-existing instrument_definitions row has
    trading_hours populated; instrument_cache row for same canonical_id
    has trading_hours=NULL. The COALESCE(EXCLUDED, current) clause must
    keep the registry's existing dict, NOT clobber it with NULL.
    """
    engine = await _reset_to_rev_a_with_clean_state(isolated_postgres_url)

    pre_existing_uid = uuid4()
    pre_existing_th = {
        "timezone": "America/New_York",
        "rth": [{"open": "09:30", "close": "16:00"}],
        "eth": [],
    }

    async with engine.begin() as conn:
        # Pre-seed registry with MSFT and a populated trading_hours.
        await conn.execute(
            text(
                """
                INSERT INTO instrument_definitions
                  (instrument_uid, raw_symbol, provider, asset_class,
                   listing_venue, routing_venue, lifecycle_state,
                   trading_hours, refreshed_at, created_at, updated_at)
                VALUES (:uid, 'MSFT', 'interactive_brokers', 'equity',
                        'NASDAQ', 'SMART', 'active',
                        CAST(:th AS JSONB),
                        now(), now(), now())
                """
            ),
            {
                "uid": str(pre_existing_uid),
                "th": (
                    '{"timezone":"America/New_York",'
                    '"rth":[{"open":"09:30","close":"16:00"}],'
                    '"eth":[]}'
                ),
            },
        )
        # Seed instrument_cache with the same canonical and trading_hours=NULL
        await conn.execute(
            text(
                """
                INSERT INTO instrument_cache
                  (canonical_id, asset_class, venue, ib_contract_json,
                   nautilus_instrument_json, trading_hours, last_refreshed_at,
                   created_at, updated_at)
                VALUES ('MSFT.NASDAQ', 'equity', 'NASDAQ',
                        '{"secType":"STK","symbol":"MSFT","exchange":"SMART"}',
                        '{}',
                        NULL,
                        now(), now(), now())
                """
            )
        )

    # Apply revision B
    _run_alembic(["upgrade", REV_B], isolated_postgres_url)

    # Assert: the pre-existing trading_hours dict survived the COALESCE.
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT trading_hours FROM instrument_definitions "
                "WHERE raw_symbol='MSFT' AND provider='interactive_brokers' "
                "AND asset_class='equity'"
            )
        )
        row = result.one_or_none()
        assert row is not None, "MSFT row missing after migration"
        assert row[0] == pre_existing_th, (
            f"COALESCE clobbered pre-existing trading_hours; got {row[0]!r}"
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_revision_b_routing_venue_falls_back_to_listing_when_exchange_missing(
    isolated_postgres_url: str,
) -> None:
    """ib_contract_json without an ``exchange`` key must fall back to the
    canonical_id suffix as routing_venue (mirrors the existing fallback
    at the ``ib_contract_json.get("exchange") or listing_venue`` line).
    """
    engine = await _reset_to_rev_a_with_clean_state(isolated_postgres_url)
    async with engine.begin() as conn:
        # ib_contract_json='{}' — no "exchange" key
        await conn.execute(
            text(
                """
                INSERT INTO instrument_cache
                  (canonical_id, asset_class, venue, ib_contract_json,
                   nautilus_instrument_json, last_refreshed_at,
                   created_at, updated_at)
                VALUES ('IBM.NYSE', 'equity', 'NYSE', '{}', '{}',
                        now(), now(), now())
                """
            )
        )

    _run_alembic(["upgrade", REV_B], isolated_postgres_url)

    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT listing_venue, routing_venue FROM instrument_definitions "
                "WHERE raw_symbol='IBM'"
            )
        )
        row = result.one_or_none()
        assert row is not None
        assert row[0] == "NYSE"  # listing_venue
        assert row[1] == "NYSE", f"routing_venue should fall back to listing_venue; got {row[1]!r}"

    await engine.dispose()


@pytest.mark.asyncio
async def test_revision_b_fails_loud_on_index_asset_class(
    isolated_postgres_url: str,
) -> None:
    """``asset_class='index'`` has no registry equivalent. The migration
    must raise with an explicit, ``index``-naming message so the operator
    knows which case they hit (vs. the generic "not in map" branch).
    """
    engine = await _reset_to_rev_a_with_clean_state(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO instrument_cache
                  (canonical_id, asset_class, venue, ib_contract_json,
                   nautilus_instrument_json, last_refreshed_at,
                   created_at, updated_at)
                VALUES ('SPX.CBOE', 'index', 'CBOE',
                        '{"secType":"IND","symbol":"SPX"}',
                        '{}',
                        now(), now(), now())
                """
            )
        )
    await engine.dispose()

    result = _run_alembic_raw(["upgrade", REV_B], isolated_postgres_url)

    assert result.returncode != 0, (
        f"migration should have failed loud on asset_class='index'; "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "index" in combined, f"failure message must name 'index' explicitly; got:\n{combined}"


@pytest.mark.asyncio
async def test_revision_b_fails_loud_on_share_class_ticker_not_in_known_venues(
    isolated_postgres_url: str,
) -> None:
    """``BRK.B`` parses to listing_venue=``B``, which is NOT a real venue.
    The closed-allowlist guard must catch this and refuse to write a
    silently-corrupt registry row.
    """
    engine = await _reset_to_rev_a_with_clean_state(isolated_postgres_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO instrument_cache
                  (canonical_id, asset_class, venue, ib_contract_json,
                   nautilus_instrument_json, last_refreshed_at,
                   created_at, updated_at)
                VALUES ('BRK.B', 'equity', 'B',
                        '{"secType":"STK","symbol":"BRK.B"}',
                        '{}',
                        now(), now(), now())
                """
            )
        )
    await engine.dispose()

    result = _run_alembic_raw(["upgrade", REV_B], isolated_postgres_url)

    assert result.returncode != 0, (
        f"migration should have failed loud on share-class ticker BRK.B; "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "BRK.B" in combined, (
        f"failure message must name the offending canonical_id; got:\n{combined}"
    )
    # The hint should reference share-class tickers OR list known venues
    # so the operator immediately understands what to fix.
    assert "share-class" in combined or "NASDAQ" in combined, (
        f"failure message must include the operator hint; got:\n{combined}"
    )
