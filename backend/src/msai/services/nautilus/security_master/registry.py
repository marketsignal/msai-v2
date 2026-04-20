"""Async lookup layer over ``instrument_definitions`` + ``instrument_aliases``.

Owns: alias -> definition resolution, raw_symbol -> definition lookup,
effective-date window management for futures rolls, ambiguity detection
for dual-listings (PRD sections 97-98).

The strategy hot path does NOT touch this module -- pre-warm happens at
``/live/start-portfolio`` / ``backtests/run``. Hot-path access is Nautilus's
own ``cache.instrument(instrument_id)`` sync dict lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession


class RegistryDefinitionNotFoundError(Exception):
    """Raised when a requested symbol has no matching registry row."""


class AmbiguousSymbolError(Exception):
    """Raised when a raw symbol matches multiple definitions and the caller
    did not pin ``asset_class``.

    Schema uniqueness is ``(raw_symbol, provider, asset_class)`` — so a
    single ``(raw_symbol, provider)`` pair can legitimately have multiple
    rows across asset_classes (e.g. ``SPY`` as equity AND as option
    underlying). Without ``asset_class`` the resolver has no deterministic
    pick, so we refuse rather than silently grab one.

    Callers (e.g. ``lookup_for_live``) read ``symbol`` / ``provider`` /
    ``asset_classes`` as attributes rather than parsing the formatted
    message, so downstream wrapping into ``AmbiguousRegistryError`` is
    deterministic and doesn't depend on message-string stability.
    """

    def __init__(
        self,
        symbol: str,
        provider: str,
        asset_classes: list[str],
    ) -> None:
        self.symbol = symbol
        self.provider = provider
        self.asset_classes = asset_classes
        super().__init__(
            f"Symbol {symbol!r} matches {len(asset_classes)} definitions under "
            f"provider {provider!r} across asset_classes {sorted(asset_classes)}; "
            "specify asset_class explicitly."
        )


@dataclass
class InstrumentRegistry:
    session: AsyncSession

    async def find_by_alias(
        self,
        alias_string: str,
        *,
        provider: str,
        as_of_date: date,
    ) -> InstrumentDefinition | None:
        """Return the definition whose alias is active on ``as_of_date``.

        ``as_of_date`` is REQUIRED — the previous UTC-default fallback
        silently regressed roll-day correctness for callers that should
        have threaded an exchange-local date (e.g. Chicago-local
        ``spawn_today`` for CME quarterly rolls). Windows are
        ``effective_from <= as_of_date < effective_to``
        (or ``effective_to IS NULL`` for the open-ended current alias).
        """
        # Order by effective_from DESC so overlapping active windows
        # deterministically return the most-recent-start row. The
        # schema's (alias_string, provider, effective_from) uniqueness
        # makes same-day overlap impossible; older-start overlapping
        # rows are legal but rare (operator-seeded). When that happens,
        # "most recent effective_from wins" matches the PRD §4 US-003
        # tie-break rule and keeps this lookup deterministic across
        # retries. Ambiguity detection on same-day overlap lives
        # downstream in lookup_for_live's _pick_active_alias (which
        # reads all active rows for the definition, not just one).
        stmt = (
            select(InstrumentDefinition)
            .join(
                InstrumentAlias,
                InstrumentAlias.instrument_uid == InstrumentDefinition.instrument_uid,
            )
            .where(
                InstrumentAlias.alias_string == alias_string,
                InstrumentAlias.provider == provider,
                InstrumentAlias.effective_from <= as_of_date,
                or_(
                    InstrumentAlias.effective_to.is_(None),
                    InstrumentAlias.effective_to > as_of_date,
                ),
            )
            .order_by(InstrumentAlias.effective_from.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_by_raw_symbol(
        self,
        raw_symbol: str,
        *,
        provider: str,
        asset_class: str | None = None,
    ) -> InstrumentDefinition | None:
        """Return the definition for ``raw_symbol`` under ``provider`` (and
        optional ``asset_class``). Returns ``None`` on miss. Callers MUST
        specify ``provider`` -- cross-provider dual-listings are by design
        (schema uniqueness is ``(raw_symbol, provider, asset_class)``).

        Raises:
            AmbiguousSymbolError: ``asset_class`` was not specified and
                more than one row matches ``(raw_symbol, provider)``.
                The schema allows multiple rows per that pair across
                different asset_classes; without ``asset_class`` pinned
                the resolver cannot pick deterministically.
        """
        stmt = select(InstrumentDefinition).where(
            InstrumentDefinition.raw_symbol == raw_symbol,
            InstrumentDefinition.provider == provider,
        )
        if asset_class is not None:
            stmt = stmt.where(InstrumentDefinition.asset_class == asset_class)
            return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

        # Without asset_class, fetch all matches and detect ambiguity
        # rather than silently ``limit(1)`` onto an arbitrary row.
        rows = (await self.session.execute(stmt)).scalars().all()
        if len(rows) > 1:
            classes = sorted({r.asset_class for r in rows})
            raise AmbiguousSymbolError(
                symbol=raw_symbol,
                provider=provider,
                asset_classes=classes,
            )
        return rows[0] if rows else None

    async def require_definition(
        self,
        alias_string: str,
        *,
        provider: str,
        as_of_date: date,
    ) -> InstrumentDefinition:
        """Thin wrapper over :meth:`find_by_alias` that raises when the
        alias has no active registry row.

        ``as_of_date`` is REQUIRED for the same reason as
        :meth:`find_by_alias` — a default would silently regress roll-day
        correctness for callers that should have threaded an exchange-local
        date.
        """
        idef = await self.find_by_alias(alias_string, provider=provider, as_of_date=as_of_date)
        if idef is None:
            raise RegistryDefinitionNotFoundError(
                f"No registry row for alias {alias_string!r} under provider "
                f"{provider!r} as of {as_of_date}"
            )
        return idef
