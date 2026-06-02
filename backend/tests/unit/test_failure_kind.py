"""Unit tests for ``FailureKind`` (Phase 1 task 1.7 support)."""

from __future__ import annotations

from msai.services.live.failure_kind import FailureKind


class TestFailureKindValues:
    def test_all_expected_values_present(self) -> None:
        # Guard against accidental deletions — these are the string
        # literals the endpoint (Task 1.14) and migration backfills depend on.
        expected = {
            "none",
            "halt_active",
            "account_halt_active",
            "spawn_failed_permanent",
            "node_crashed",
            "spawn_failed_transient",
            "build_timeout",
            "reconciliation_failed",
            "heartbeat_timeout",
            "registry_miss",
            "registry_incomplete",
            "unsupported_asset_class",
            "ambiguous_registry",
            "in_flight",
            "body_mismatch",
            "api_poll_timeout",
            "unknown",
        }
        actual = {f.value for f in FailureKind}
        assert actual == expected

    def test_is_str_enum(self) -> None:
        """Storing a ``FailureKind`` in the DB column must serialize
        as the bare string (``'none'``, not ``'FailureKind.NONE'``)."""
        assert str(FailureKind.NONE) == "none"
        assert FailureKind.NONE == "none"


class TestParseOrUnknown:
    def test_none_input_returns_unknown(self) -> None:
        assert FailureKind.parse_or_unknown(None) is FailureKind.UNKNOWN

    def test_empty_string_returns_unknown(self) -> None:
        assert FailureKind.parse_or_unknown("") is FailureKind.UNKNOWN

    def test_unrecognized_string_returns_unknown(self) -> None:
        """A row written by a newer codebase version shouldn't crash the
        endpoint reading it with an older enum definition."""
        assert FailureKind.parse_or_unknown("future_failure_kind") is FailureKind.UNKNOWN

    def test_recognized_values_round_trip(self) -> None:
        for kind in FailureKind:
            assert FailureKind.parse_or_unknown(kind.value) is kind


class TestIsRecoverableCrash:
    """The ONE recovery-eligibility predicate shared by the reaper +
    rescan (PR 2 / F2). Pins the GOVERNING PRINCIPLE: re-drive ONLY a node
    that RAN-then-crashed or an orphaned in-flight start — never a pre-spawn
    START failure that never ran."""

    def test_pre_spawn_never_ran_kinds_are_not_recoverable(self) -> None:
        # Permanent pre-spawn config / never-ran kinds + halt-blocked starts.
        not_recoverable = {
            FailureKind.SPAWN_FAILED_PERMANENT,
            FailureKind.REGISTRY_MISS,
            FailureKind.REGISTRY_INCOMPLETE,
            FailureKind.UNSUPPORTED_ASSET_CLASS,
            FailureKind.AMBIGUOUS_REGISTRY,
            FailureKind.HALT_ACTIVE,
            FailureKind.ACCOUNT_HALT_ACTIVE,
            FailureKind.NONE,
            FailureKind.IN_FLIGHT,
            FailureKind.API_POLL_TIMEOUT,
            FailureKind.BODY_MISMATCH,
        }
        for kind in not_recoverable:
            assert kind.is_recoverable_crash() is False, kind

    def test_runtime_crash_kinds_are_recoverable(self) -> None:
        recoverable = {
            FailureKind.NODE_CRASHED,
            FailureKind.RECONCILIATION_FAILED,
            FailureKind.HEARTBEAT_TIMEOUT,
            FailureKind.BUILD_TIMEOUT,
            FailureKind.SPAWN_FAILED_TRANSIENT,
            # NULL / unrecognised → UNKNOWN: the genuine outage-window crash
            # presents this way; fail TOWARD recovery (ceiling brakes a loop).
            FailureKind.UNKNOWN,
        }
        for kind in recoverable:
            assert kind.is_recoverable_crash() is True, kind

    def test_null_failure_kind_is_recoverable(self) -> None:
        """A terminal ``failed`` row with a NULL ``failure_kind`` (the genuine
        outage-window crash) parses to UNKNOWN and MUST be recoverable —
        otherwise US-2 self-heal would silently strand a real-money account."""
        assert FailureKind.parse_or_unknown(None).is_recoverable_crash() is True

    def test_every_kind_is_partitioned(self) -> None:
        """Every enum value is decisively recoverable XOR not — no kind is
        left ambiguous by the predicate."""
        for kind in FailureKind:
            assert isinstance(kind.is_recoverable_crash(), bool)
