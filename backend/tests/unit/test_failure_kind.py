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
            "spawn_failed_permanent",
            "spawn_failed_transient",
            "build_timeout",
            "reconciliation_failed",
            "heartbeat_timeout",
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
