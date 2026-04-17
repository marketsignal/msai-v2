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
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class RegistryDefinitionNotFoundError(Exception):
    """Raised when a requested symbol has no matching registry row."""


@dataclass
class InstrumentRegistry:
    session: AsyncSession

    async def find_by_alias(
        self,
        alias_string: str,
        *,
        provider: str,
        as_of_date: date | None = None,
    ) -> InstrumentDefinition | None:
        """Return the definition whose alias is active on ``as_of_date``.

        Default ``as_of_date`` = today UTC. Windows are
        ``effective_from <= as_of < effective_to``
        (or ``effective_to IS NULL`` for the open-ended current alias).
        """
        as_of = as_of_date or datetime.now(UTC).date()
        stmt = (
            select(InstrumentDefinition)
            .join(
                InstrumentAlias,
                InstrumentAlias.instrument_uid == InstrumentDefinition.instrument_uid,
            )
            .where(
                InstrumentAlias.alias_string == alias_string,
                InstrumentAlias.provider == provider,
                InstrumentAlias.effective_from <= as_of,
                or_(
                    InstrumentAlias.effective_to.is_(None),
                    InstrumentAlias.effective_to > as_of,
                ),
            )
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
        (schema uniqueness is ``(raw_symbol, provider, asset_class)``)."""
        stmt = select(InstrumentDefinition).where(
            InstrumentDefinition.raw_symbol == raw_symbol,
            InstrumentDefinition.provider == provider,
        )
        if asset_class is not None:
            stmt = stmt.where(InstrumentDefinition.asset_class == asset_class)
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

    async def require_definition(
        self,
        alias_string: str,
        *,
        provider: str,
        as_of_date: date | None = None,
    ) -> InstrumentDefinition:
        idef = await self.find_by_alias(
            alias_string, provider=provider, as_of_date=as_of_date
        )
        if idef is None:
            raise RegistryDefinitionNotFoundError(
                f"No registry row for alias {alias_string!r} under provider "
                f"{provider!r}"
                + (f" as of {as_of_date}" if as_of_date else "")
            )
        return idef
