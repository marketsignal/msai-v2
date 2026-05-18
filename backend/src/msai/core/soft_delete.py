"""SQLAlchemy 2.0 ``do_orm_execute`` listener that hides soft-deleted strategies.

Registers a global SELECT-time filter on :class:`msai.models.strategy.Strategy`
that injects ``WHERE deleted_at IS NULL`` into every ORM SELECT by default.
Per-statement opt-out: pass ``execution_options(include_deleted=True)`` on
the SELECT (or the session) to see archived rows for DETAIL / SUPERVISOR /
SYNC code paths (see plan revision R20 in
``docs/plans/2026-05-16-ui-completeness.md``).

Design
------
SQLAlchemy 2.0 exposes ``Session.do_orm_execute`` as the recommended hook
for global filtering — ``with_loader_criteria`` propagates to relationship
loaders (lazy + selectin), which a plain ``Query.filter`` cannot.
``AsyncSession`` delegates ORM events to its underlying sync ``Session``,
so the listener target is always the sync class. References:

- https://docs.sqlalchemy.org/en/20/orm/session_events.html#do-orm-execute
- https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#sqlalchemy.ext.asyncio.AsyncSession.sync_session
- https://docs.sqlalchemy.org/en/20/orm/queryguide/api.html#sqlalchemy.orm.with_loader_criteria

Registration
------------
``register_soft_delete_listeners()`` attaches the filter to the sync
``Session`` class — every ``Session`` (or ``AsyncSession``, which wraps
one) inherits it. The function is idempotent so a re-import or repeated
registration does not create duplicate listeners (which would multiply
the WHERE clause and slow every query).
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from msai.models.strategy import Strategy

__all__ = ["register_soft_delete_listeners", "soft_delete_filter"]


def soft_delete_filter(state: ORMExecuteState) -> None:
    """Inject ``Strategy.deleted_at IS NULL`` into eligible ORM SELECTs.

    Skips:

    - Non-SELECT statements (UPDATE / DELETE / bulk operations).
    - Column-only refresh loads.
    - Any statement explicitly opted out via
      ``execution_options(include_deleted=True)``.

    Relationship-load semantics: ``selectinload`` / ``joinedload`` issue
    secondary SELECTs that go through ``do_orm_execute``; in SQLAlchemy
    2.0 those secondaries report ``state.is_relationship_load=False`` for
    selectinload, so the early-skip below catches lazy loads but NOT
    selectinload. Combined with ``propagate_to_loaders=False`` on the
    criteria, the practical contract is:

    - ``select(Strategy)`` without opt-in → archived rows hidden.
    - ``select(Backtest).options(selectinload(Backtest.strategy))``
      without opt-in → archived strategy still hidden (relationship
      resolves to None).
    - ``select(Backtest).options(...).execution_options(include_deleted=True)``
      → archived strategy resolves. This is what historical-backtest
      reads MUST use (plan R20 DETAIL classification). See
      ``tests/unit/test_soft_delete_listener.py::test_relationship_load_requires_include_deleted_opt_in``
      for the pin.
    """
    if not state.is_select:
        return
    if state.is_column_load or state.is_relationship_load:
        return
    if state.execution_options.get("include_deleted", False):
        return

    state.statement = state.statement.options(
        with_loader_criteria(
            Strategy,
            lambda cls: cls.deleted_at.is_(None),
            include_aliases=True,
            propagate_to_loaders=False,
        )
    )


def register_soft_delete_listeners() -> None:
    """Attach :func:`soft_delete_filter` to the sync ``Session`` class.

    Idempotent — repeated calls are no-ops. The listener target is the
    abstract ``Session`` class because SQLAlchemy 2.0 routes
    ``AsyncSession`` ORM events through its underlying ``sync_session``;
    listening on ``Session`` covers both sync and async usage with one
    binding.
    """
    if event.contains(Session, "do_orm_execute", soft_delete_filter):
        return
    event.listen(Session, "do_orm_execute", soft_delete_filter)
