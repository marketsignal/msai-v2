#!/usr/bin/env python
"""Preflight gate for the instrument-cache → registry migration.

Operator step BEFORE ``alembic upgrade head``. Validates that every active
``LiveDeployment``'s strategy-member instrument list resolves through the
registry today. Source-of-truth instrument list lives on
``LivePortfolioRevisionStrategy.instruments`` (NOT on ``LiveDeployment``
itself — there's no ``canonical_instruments`` column on the deployment
row). The supervisor does the same lookup at spawn time via
``live_resolver.lookup_for_live(member.instruments, ...)``.

Exits 0 on success, non-zero on miss with operator-action hint. A miss is
an active-deployment breakage waiting to happen on the next supervisor
restart, NOT harmless legacy dirt. The operator must run::

    msai instruments refresh --symbols X --provider interactive_brokers \\
        --asset-class <stk|fut|cash>

to seed the missing alias, then re-run preflight.

Usage:
    cd backend && uv run python scripts/preflight_cache_migration.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.core.config import settings
from msai.models.live_deployment import LiveDeployment
from msai.models.live_portfolio_revision import LivePortfolioRevision
from msai.models.live_portfolio_revision_strategy import (
    LivePortfolioRevisionStrategy,
)

# Active deployments per the current state machine. The supervisor's
# spawn-time check uses {starting, running}. ``paused`` is NOT in the
# live-state vocabulary today.
ACTIVE_STATUSES = ("starting", "running")

log = logging.getLogger("preflight_cache_migration")


async def _check_active_deployments() -> int:
    """Return exit code: 0 if all active deployments resolve, 1 otherwise."""
    # Local import — ``live_resolver`` pulls in heavy dependencies (Nautilus
    # adapter modules) that should not load if ``--help`` is queried.
    from sqlalchemy.exc import ProgrammingError

    from msai.services.nautilus.live_instrument_bootstrap import (
        exchange_local_today,
    )
    from msai.services.nautilus.security_master.live_resolver import (
        LiveResolverError,
        RegistryMissError,
        lookup_for_live,
    )

    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Step 1 — count legacy instrument_cache rows (informational). Use a
    # dedicated session so a missing table (post-migration) doesn't poison
    # the per-deployment lookup sessions below.
    async with factory() as session:
        try:
            cache_count = (
                await session.execute(text("SELECT count(*) FROM instrument_cache"))
            ).scalar_one()
            print(f"[info] instrument_cache row count: {cache_count}")
        except ProgrammingError as exc:  # UndefinedTable after migration B
            print(f"[info] instrument_cache table dropped (post-migration): {exc}")
            # Roll back the failed transaction so the session is reusable.
            await session.rollback()

    # Step 2 — JOIN active deployments → portfolio revision → strategy
    # members. Each deployment can have multiple member rows
    # (multi-strategy portfolio); each member has its own
    # ``instruments`` list. Use a dedicated session for the catalog query.
    async with factory() as session:
        stmt = (
            select(
                LiveDeployment.deployment_slug,
                LivePortfolioRevisionStrategy.instruments,
            )
            .join(
                LivePortfolioRevision,
                LiveDeployment.portfolio_revision_id == LivePortfolioRevision.id,
            )
            .join(
                LivePortfolioRevisionStrategy,
                LivePortfolioRevisionStrategy.revision_id == LivePortfolioRevision.id,
            )
            .where(LiveDeployment.status.in_(ACTIVE_STATUSES))
        )
        rows = (await session.execute(stmt)).all()
        print(f"[info] active deployment members (status in {ACTIVE_STATUSES}): {len(rows)}")

        # Per-deployment empty-member check: enumerate active deployments
        # INDEPENDENTLY and fail loud on ANY deployment with zero member
        # rows — even if other deployments are healthy (mixed state). The
        # supervisor treats per-deployment empty-members as fatal at
        # spawn time, so pre-cutover we should surface it.
        all_active_slugs = (
            (
                await session.execute(
                    select(LiveDeployment.deployment_slug).where(
                        LiveDeployment.status.in_(ACTIVE_STATUSES)
                    )
                )
            )
            .scalars()
            .all()
        )
        slugs_with_members = {slug for slug, _ in rows}
        empty_slugs = [s for s in all_active_slugs if s not in slugs_with_members]
        if empty_slugs:
            print()
            print(
                f"[FAIL] {len(empty_slugs)} active deployment(s) have ZERO "
                f"`live_portfolio_revision_strategies` rows. This is a "
                f"corrupt state — the supervisor would crash on next spawn. "
                f"Investigate before migrating."
            )
            for slug in empty_slugs:
                print(f"  - deployment {slug!r}")
            await engine.dispose()
            return 1

    if not rows:
        print("[ok] No active deployments — no strategy instruments to validate. Preflight passed.")
        await engine.dispose()
        return 0

    # Exchange-local (America/Chicago) date matches what the supervisor
    # passes to ``lookup_for_live(as_of_date=spawn_today)`` — same alias
    # window evaluation ensures preflight agrees with runtime.
    today = exchange_local_today()
    misses: list[tuple[str, str, str]] = []  # (deployment_slug, sym, error_kind)

    # deployment_slugs whose member.instruments=[]
    empty_instrument_members: list[str] = []

    for deployment_slug, instruments in rows:
        # An empty ``member.instruments`` is fatal at spawn — surface
        # pre-cutover. Also defends against ``lookup_for_live`` raising
        # plain ``ValueError("symbols cannot be empty")`` on empty
        # input which would otherwise crash this preflight script
        # instead of producing operator-readable output.
        if not instruments:
            empty_instrument_members.append(deployment_slug)
            continue

        # Per-deployment session: a `lookup_for_live` raise inside one
        # iteration leaves its own transaction aborted (asyncpg's
        # InFailedSQLTransactionError contagion), but the next
        # deployment opens a fresh session and isn't affected. Also
        # avoids `idle_in_transaction_session_timeout` on long batches.
        try:
            async with factory() as session:
                await lookup_for_live(
                    list(instruments),
                    as_of_date=today,
                    session=session,
                )
        except RegistryMissError as exc:
            # ``exc.symbols`` is the actual subset that missed — don't
            # pin the whole member set, that turns a single missing
            # alias into N spurious refresh hints.
            for sym in exc.symbols:
                misses.append((deployment_slug, sym, "RegistryMissError"))
        except LiveResolverError as exc:
            # Other LiveResolverError subclasses
            # (RegistryIncompleteError / AmbiguousRegistryError /
            # UnsupportedAssetClassError) — fall back to pinning the
            # member set since they don't carry a uniform ``.symbols``
            # attribute.
            kind = type(exc).__name__
            target_syms = getattr(exc, "symbols", None) or instruments
            for sym in target_syms:
                misses.append((deployment_slug, sym, kind))
        except TypeError as exc:
            # Defensive: ``lookup_for_live`` raises ``TypeError`` on a
            # non-``date`` ``as_of_date``. Surface as operator-actionable
            # instead of crashing the script — we own the as_of_date
            # construction above, but the fail-loud safety net lives here.
            misses.append((deployment_slug, "<TypeError>", str(exc)))
        except ValueError as exc:
            # ``lookup_for_live``'s "symbols cannot be empty" guard is
            # the only known plain-ValueError path; any other ValueError
            # is unexpected so surface it loudly instead of swallowing.
            misses.append((deployment_slug, "<bare-ValueError>", str(exc)))

    if empty_instrument_members:
        print()
        print(
            f"[FAIL] {len(empty_instrument_members)} active "
            f"`live_portfolio_revision_strategies` member row(s) have "
            f"an EMPTY `instruments` list. The supervisor rejects this "
            f"as a fatal portfolio-revision freeze bug. Investigate "
            f"before migrating."
        )
        for slug in empty_instrument_members:
            print(f"  - deployment {slug!r}")
        await engine.dispose()
        return 1

    await engine.dispose()

    if not misses:
        print(
            f"[ok] All {len(rows)} active deployment-member rows' "
            f"instruments resolve through the registry. Preflight passed."
        )
        return 0

    # Fail-loud with operator-action hint
    print()
    print("[FAIL] Preflight failed — registry misses on active deployments:")
    seen: set[tuple[str, str]] = set()
    for slug, sym, kind in misses:
        key = (slug, sym)
        if key in seen:
            continue
        seen.add(key)
        root = sym.split(".", 1)[0] if "." in sym else sym
        print(f"  - deployment {slug!r}: {sym!r} ({kind})")
        print(
            f"    Run: msai instruments refresh --symbols {root} "
            f"--provider interactive_brokers --asset-class <stk|fut|cash>"
        )
    print()
    print("After seeding the missing aliases, re-run this preflight.")
    print("Do NOT run `alembic upgrade head` until preflight exits 0.")
    return 1


def main() -> None:
    sys.exit(asyncio.run(_check_active_deployments()))


if __name__ == "__main__":
    main()
