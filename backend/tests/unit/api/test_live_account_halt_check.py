"""Unit tests for the account-scoped halt latch check on ``/start-portfolio``.

Codex iter 1 P2-1 of PR 1 (multi-account-broker-fleet). The
``POST /api/v1/live/drain/{account_id}`` endpoint writes
``account_halt_key(account_id)`` in Redis to block the drained
sub-account, but ``/start-portfolio`` previously ignored that key — a
queued or operator-initiated start could spawn the drained account
right after a drain. These tests pin the gap closed.

The covered surfaces:

- :func:`msai.api.live._account_halt_is_active` — the Redis check
  helper. ``True`` when the latch is set, ``False`` when not.
- :meth:`EndpointOutcome.account_halt_active` — the structured 503
  response factory. Distinct from :meth:`EndpointOutcome.halt_active`
  (fleet-wide kill switch); carries ``ACCOUNT_HALT_ACTIVE`` so the
  caller can distinguish a sub-account drain from a fleet emergency.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from msai.api.live import _account_halt_is_active
from msai.core.halt_keys import account_halt_key
from msai.services.live.failure_kind import FailureKind
from msai.services.live.idempotency import EndpointOutcome


@pytest.fixture
def fake_bus() -> MagicMock:
    """Stub bus with a fake Redis that records calls to ``exists``."""
    bus = MagicMock()
    fake_redis = MagicMock()
    fake_redis.exists = AsyncMock(return_value=0)
    bus._redis = fake_redis  # noqa: SLF001 — mirrors api.live's usage
    return bus


@pytest.mark.asyncio
async def test_account_halt_is_active_returns_true_when_redis_key_set(
    fake_bus: MagicMock,
) -> None:
    """Arrange: Redis ``EXISTS`` reports the latch key is set.

    Act: call the helper with the matching account_id.

    Assert: helper returns True AND queried the expected
    ``msai:risk:halt:account:{account_id}`` key — proving the helper
    reads the per-account latch (not the fleet latch)."""
    fake_bus._redis.exists.return_value = 1

    result = await _account_halt_is_active(fake_bus, "DUP733214")

    assert result is True
    fake_bus._redis.exists.assert_awaited_once_with(account_halt_key("DUP733214"))


@pytest.mark.asyncio
async def test_account_halt_is_active_returns_false_when_unset(
    fake_bus: MagicMock,
) -> None:
    """The default Redis return (0) means the latch is NOT set."""
    fake_bus._redis.exists.return_value = 0

    result = await _account_halt_is_active(fake_bus, "DUP733214")

    assert result is False


@pytest.mark.asyncio
async def test_account_halt_is_active_returns_false_for_empty_account_id(
    fake_bus: MagicMock,
) -> None:
    """Empty ``account_id`` short-circuits to False without hitting
    Redis — back-compat for callers that haven't migrated to the
    per-account model yet."""
    result = await _account_halt_is_active(fake_bus, "")

    assert result is False
    fake_bus._redis.exists.assert_not_called()


def test_endpoint_outcome_account_halt_active_returns_503_with_code() -> None:
    """The ``account_halt_active`` factory MUST produce a 503 response
    with a structured ``ACCOUNT_HALT_ACTIVE`` code distinct from the
    fleet ``halt_active``. Operator tooling discriminates on the code."""
    outcome = EndpointOutcome.account_halt_active("DUP733214")

    assert outcome.status_code == 503
    assert outcome.cacheable is False
    # F1 fix (Codex iter 2 P1 / pr-toolkit convergent finding): the
    # factory MUST tag ``failure_kind=ACCOUNT_HALT_ACTIVE`` (the dedicated
    # enum value added in PR 1 T8), NOT the fleet-wide ``HALT_ACTIVE``.
    # The previous value collapsed the distinct halt scopes downstream.
    assert outcome.failure_kind == FailureKind.ACCOUNT_HALT_ACTIVE
    assert outcome.response["error"]["code"] == "ACCOUNT_HALT_ACTIVE"
    assert outcome.response["error"]["account_id"] == "DUP733214"
    # The detail string carries the account id so operators don't have
    # to cross-reference the structured error block to identify the
    # affected sub-account.
    assert "DUP733214" in outcome.response["detail"]


def test_endpoint_outcome_account_halt_active_differs_from_fleet_halt() -> None:
    """Two distinct factories — a fleet ``halt_active`` and a per-account
    ``account_halt_active`` — so the API caller can tell them apart.
    Council 2026-05-29 obj #11: sibling sub-accounts under the same
    TWS login must remain independently controllable."""
    fleet = EndpointOutcome.halt_active()
    account = EndpointOutcome.account_halt_active("DUP733214")

    # Both 503, both not cacheable — same shape on the wire-level
    # status. The structured body is where they diverge.
    assert fleet.status_code == account.status_code
    assert fleet.cacheable == account.cacheable
    # The fleet response has no ``error.code`` block; the account one
    # does — that's the discriminator.
    assert "error" not in fleet.response
    assert account.response["error"]["code"] == "ACCOUNT_HALT_ACTIVE"
    # F1: distinct failure_kind values so observability/classification
    # can tell manual /kill-all apart from /drain/{account_id}.
    assert fleet.failure_kind == FailureKind.HALT_ACTIVE
    assert account.failure_kind == FailureKind.ACCOUNT_HALT_ACTIVE
    assert fleet.failure_kind != account.failure_kind
