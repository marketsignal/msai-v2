"""Redis halt-key namespacing for the live-trading fleet.

Single source of truth — replaces three duplicate ``_HALT_KEY = "msai:risk:halt"``
definitions across api/live.py, live_supervisor/fleet_router.py, and
services/nautilus/disconnect_handler.py.

Halt-cause metadata is written as a companion key alongside the latch key so
operators can distinguish a manual ``/kill-all`` panic from a PR-1b data-stale
auto-halt. Per the decision-doc addendum 2026-05-28, reuse the same latch code
path; only the cause-attribution metadata differs.
"""

from __future__ import annotations

from enum import StrEnum

_FLEET_HALT = "msai:risk:halt"


class HaltCause(StrEnum):
    """Distinguishes halt callers so an operator can interpret a halt latch."""

    FLEET_EMERGENCY = "fleet_emergency"  # Manual /kill-all
    OPERATOR_DRAIN = "operator_drain"  # Account-scoped drain
    DATA_STALE = "data_stale"  # PR 1b — Databento freshness gate


def fleet_halt_key() -> str:
    """Return the global fleet-wide halt latch key."""
    return _FLEET_HALT


def account_halt_key(account_id: str) -> str:
    """Return the account-scoped halt latch key for *account_id*.

    PR 1 critical: keyed by ``account_id`` (DUP733214, DUP733215, …),
    NOT by ``ib_login_key``, so that draining one sub-account under a
    shared login does NOT halt the other sub-account.

    Citation: council 2026-05-27 blocking objection #4 — "Split halt
    semantics: account-scoped halt/drain + explicit fleet emergency
    halt". (F7 fix: the prior citation pointed at council 2026-05-29
    objection #11, which is actually about IB Gateway client-ID
    isolation — see ``docs/decisions/multi-account-broker-fleet.md:203``.)
    """
    if not account_id:
        raise ValueError("account_id must be non-empty")
    return f"{_FLEET_HALT}:account:{account_id}"


def halt_cause_key(scope: str, *, account_id: str | None = None) -> str:
    """Return the companion key holding ``HaltCause`` metadata for *scope*."""
    if scope == "fleet":
        return f"{_FLEET_HALT}:cause"
    if scope == "account":
        if not account_id:
            raise ValueError("account_id required when scope='account'")
        return f"{_FLEET_HALT}:account:{account_id}:cause"
    raise ValueError(f"unknown halt scope: {scope!r}")
