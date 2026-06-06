"""Unit tests for SecurityMaster._preferred_provider (pure staticmethod).

Pins the provider-preference policy (databento > interactive_brokers >
lexicographically-first remaining) so a future edit can't silently regress
the ordering that the readiness unpinned-read default relies on.
"""

from __future__ import annotations

import pytest

from msai.services.nautilus.security_master.service import SecurityMaster


@pytest.mark.parametrize(
    ("provider_set", "expected"),
    [
        # Preference beats lexicographic order: "interactive_brokers" wins over
        # "alpaca" even though "alpaca" sorts first.
        ({"interactive_brokers", "alpaca"}, "interactive_brokers"),
        # databento outranks interactive_brokers.
        ({"databento", "interactive_brokers"}, "databento"),
        # Neither preferred provider present → deterministic lexicographic fallback.
        ({"zebra"}, "zebra"),
    ],
)
def test_preferred_provider_applies_policy(provider_set: set[str], expected: str) -> None:
    # Act
    result = SecurityMaster._preferred_provider(provider_set)

    # Assert
    assert result == expected
