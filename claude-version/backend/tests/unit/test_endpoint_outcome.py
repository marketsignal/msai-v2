"""Unit tests for ``EndpointOutcome`` factories (Phase 1 task 1.14).

Covers:
- Each factory produces the right ``status_code`` / ``cacheable`` /
  ``failure_kind`` combination (plan v7/v8/v9 regression guards).
- ``permanent_failure`` accepts ONLY the four permitted
  ``FailureKind`` values; passing a transient/success kind asserts.
- ``body_mismatch`` is cacheable=False (Codex v7 P0 regression —
  caching would poison the original correct response at the same key).
"""

from __future__ import annotations

import pytest

from msai.services.live.failure_kind import FailureKind
from msai.services.live.idempotency import EndpointOutcome


class TestReadyFactory:
    def test_ready_is_201_cacheable_none(self) -> None:
        outcome = EndpointOutcome.ready({"id": "abc", "status": "running"})
        assert outcome.status_code == 201
        assert outcome.cacheable is True
        assert outcome.failure_kind == FailureKind.NONE
        assert outcome.response == {"id": "abc", "status": "running"}


class TestAlreadyActiveFactory:
    def test_already_active_is_200_not_201(self) -> None:
        """Plan v7 regression for Codex v6 P1 — the v6 workflow had
        a 200 vs 201 mismatch between this factory and the store's
        allowlist. v7+: 200 and cacheable."""
        outcome = EndpointOutcome.already_active({"id": "abc"})
        assert outcome.status_code == 200
        assert outcome.cacheable is True
        assert outcome.failure_kind == FailureKind.NONE


class TestStoppedFactory:
    def test_stopped_is_200_cacheable(self) -> None:
        outcome = EndpointOutcome.stopped({"id": "abc", "status": "stopped"})
        assert outcome.status_code == 200
        assert outcome.cacheable is True
        assert outcome.failure_kind == FailureKind.NONE


class TestHaltActiveFactory:
    def test_halt_active_is_503_not_cacheable(self) -> None:
        outcome = EndpointOutcome.halt_active()
        assert outcome.status_code == 503
        assert outcome.cacheable is False
        assert outcome.failure_kind == FailureKind.HALT_ACTIVE
        assert "kill switch" in outcome.response["detail"].lower()


class TestInFlightFactory:
    def test_in_flight_is_425_not_cacheable(self) -> None:
        outcome = EndpointOutcome.in_flight()
        assert outcome.status_code == 425
        assert outcome.cacheable is False
        assert outcome.failure_kind == FailureKind.IN_FLIGHT
        assert "in flight" in outcome.response["detail"].lower()


class TestApiPollTimeoutFactory:
    def test_api_poll_timeout_is_504_not_cacheable(self) -> None:
        outcome = EndpointOutcome.api_poll_timeout()
        assert outcome.status_code == 504
        assert outcome.cacheable is False
        assert outcome.failure_kind == FailureKind.API_POLL_TIMEOUT


class TestBodyMismatchFactory:
    def test_body_mismatch_is_422_not_cacheable(self) -> None:
        """Codex v7 P0 regression: body_mismatch must be
        cacheable=False. A body-mismatch caller does NOT own the
        reservation slot — caching this 422 would overwrite the
        original correct cached response at the same key."""
        outcome = EndpointOutcome.body_mismatch()
        assert outcome.status_code == 422
        assert outcome.cacheable is False  # critical
        assert outcome.failure_kind == FailureKind.BODY_MISMATCH


class TestPermanentFailureFactory:
    @pytest.mark.parametrize(
        "kind",
        [
            FailureKind.SPAWN_FAILED_PERMANENT,
            FailureKind.RECONCILIATION_FAILED,
            FailureKind.BUILD_TIMEOUT,
            FailureKind.UNKNOWN,
        ],
    )
    def test_permanent_kinds_produce_cacheable_503(self, kind: FailureKind) -> None:
        outcome = EndpointOutcome.permanent_failure(kind, "diagnosis")
        assert outcome.status_code == 503
        assert outcome.cacheable is True  # cacheable!
        assert outcome.failure_kind == kind
        assert outcome.response["detail"] == "diagnosis"
        assert outcome.response["failure_kind"] == kind.value

    @pytest.mark.parametrize(
        "kind",
        [
            FailureKind.NONE,
            FailureKind.HALT_ACTIVE,
            FailureKind.IN_FLIGHT,
            FailureKind.API_POLL_TIMEOUT,
            FailureKind.BODY_MISMATCH,
        ],
    )
    def test_non_permanent_kinds_are_rejected(self, kind: FailureKind) -> None:
        """permanent_failure must only accept the 4 terminal kinds —
        transient / success / body-mismatch kinds assert."""
        with pytest.raises(AssertionError):
            EndpointOutcome.permanent_failure(kind, "diagnosis")
