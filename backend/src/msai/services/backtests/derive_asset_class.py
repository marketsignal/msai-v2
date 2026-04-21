"""Server-authoritative ``asset_class`` derivation (Task B3).

Closes the PR #39 scope-defer: the classifier / orchestrator no longer
needs the caller to hand-roll an ``asset_class`` hint for a symbol like
``"ES.n.0"`` — we derive it from the symbol shape (fast path) and, when
a DB session is available, prefer the instrument registry's
authoritative answer.

Two public surfaces:

- :func:`derive_asset_class_sync` — pure, no DB; safe in any context
  (e.g. the sync ``classify_worker_failure`` path where we have no
  ``AsyncSession`` handy).
- :func:`derive_asset_class` — async; tries the registry first, falls
  back to the shape heuristic on miss / exception.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from msai.core.logging import get_logger

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession


log = get_logger(__name__)


# Symbol-shape patterns → ingest-taxonomy asset_class.
# Ordering matters: options is checked first (OPRA suffix is unambiguous),
# then futures (broadest set of venues), then forex, then stocks. Unknown
# → "stocks" with a warning log.
_OPTIONS_PATTERNS = (re.compile(r"\.OPRA$"),)
_FUTURES_PATTERNS = (
    re.compile(r"\.n\.0$"),
    re.compile(r"\.CME$"),
    re.compile(r"\.GLBX$"),
    re.compile(r"\.XCME$"),
    re.compile(r"^[A-Z]{1,3}[FGHJKMNQUVXZ]\d\."),
)
_FOREX_PATTERNS = (re.compile(r"/.+\."),)
_STOCKS_PATTERNS = (
    re.compile(r"\.NASDAQ$"),
    re.compile(r"\.ARCA$"),
    re.compile(r"\.NYSE$"),
    re.compile(r"\.XNAS$"),
    re.compile(r"\.BATS$"),
)


def derive_asset_class_sync(symbols: list[str]) -> str | None:
    """Shape-only derivation — safe in any context (no DB access).

    Returns the ingest-taxonomy asset_class string (one of
    ``"stocks"`` / ``"futures"`` / ``"options"`` / ``"forex"`` /
    ``"crypto"``) when the first symbol's shape matches a known
    pattern, or ``None`` when the shape is ambiguous / unknown.

    Returning ``None`` (rather than a ``"stocks"`` default) lets the
    classifier chain fall through to the caller-supplied
    ``asset_class`` hint and finally the regex path-capture. See the
    Task B3 iter-2 P2 finding: a non-null default here silently
    overrode a correct ``asset_class="options"`` hint to ``"stocks"``.

    Empty input returns ``None`` — no symbols means no basis to infer.

    Mixed-asset-class inputs return the first symbol's class — rare in
    practice, and the caller's explicit hint takes precedence in the
    classifier chain.
    """
    if not symbols:
        return None
    first = symbols[0]
    for pattern in _OPTIONS_PATTERNS:
        if pattern.search(first):
            return "options"
    for pattern in _FUTURES_PATTERNS:
        if pattern.search(first):
            return "futures"
    for pattern in _FOREX_PATTERNS:
        if pattern.search(first):
            return "forex"
    for pattern in _STOCKS_PATTERNS:
        if pattern.search(first):
            return "stocks"
    log.warning("asset_class_derivation_fallback", symbol=first)
    return None


async def derive_asset_class(
    symbols: list[str],
    *,
    start: date,
    db: AsyncSession | None,
) -> str | None:
    """Async server-authoritative derivation — registry first, shape fallback.

    When a DB session is supplied we resolve the first symbol through
    :class:`SecurityMaster` and look up its ingest-taxonomy asset_class via
    :meth:`SecurityMaster.asset_class_for_alias`. A registry miss or any
    failure (DB offline, unknown venue, etc.) silently falls back to the
    pure-shape :func:`derive_asset_class_sync` — auto-heal must never die
    because the registry is unreachable.

    Returns ``None`` when neither the registry nor the shape heuristic
    can identify the asset class. Callers own the final default — see
    REV B7-v2's orchestrator pattern
    ``derive_asset_class(...) or caller_asset_class_hint or "stocks"``.
    """
    if not symbols:
        return None
    if db is not None:
        try:
            from msai.services.nautilus.security_master.service import (
                SecurityMaster,
            )

            master = SecurityMaster(db=db)
            resolved = await master.resolve_for_backtest([symbols[0]], start=start.isoformat())
            if resolved:
                asset_class = master.asset_class_for_alias(resolved[0])
                if asset_class:
                    return asset_class
        except Exception:  # noqa: BLE001 — registry failure never kills auto-heal
            log.warning(
                "asset_class_registry_lookup_failed",
                symbol=symbols[0],
                exc_info=True,
            )
    return derive_asset_class_sync(symbols)
