"""Unit tests for EndpointOutcome.registry_permanent_failure (Task 12).

Exercises all 4 registry FailureKind variants + JSON parse fallback +
assertion guard on non-registry kinds.
"""

from __future__ import annotations

import json

import pytest

from msai.services.live.failure_kind import FailureKind
from msai.services.live.idempotency import EndpointOutcome


def test_registry_miss_factory_parses_json_envelope() -> None:
    error_message = json.dumps(
        {
            "code": "REGISTRY_MISS",
            "message": (
                "Symbol(s) not in registry: ['QQQ'] as of 2026-04-20. "
                "Run: msai instruments refresh --symbols QQQ --provider interactive_brokers"
            ),
            "details": {"missing_symbols": ["QQQ"], "as_of_date": "2026-04-20"},
        }
    )
    outcome = EndpointOutcome.registry_permanent_failure(
        FailureKind.REGISTRY_MISS,
        error_message,
    )
    assert outcome.status_code == 422
    assert outcome.cacheable is False
    body = outcome.response
    assert body["error"]["code"] == "REGISTRY_MISS"
    assert "msai instruments refresh" in body["error"]["message"]
    assert body["error"]["details"]["missing_symbols"] == ["QQQ"]
    assert body["failure_kind"] == "registry_miss"


def test_registry_incomplete_factory_parses_json_envelope() -> None:
    error_message = json.dumps(
        {
            "code": "REGISTRY_INCOMPLETE",
            "message": "Registry row for 'NVDA' is incomplete: missing 'listing_venue'",
            "details": {"symbol": "NVDA", "missing_field": "listing_venue"},
        }
    )
    outcome = EndpointOutcome.registry_permanent_failure(
        FailureKind.REGISTRY_INCOMPLETE,
        error_message,
    )
    assert outcome.status_code == 422
    assert outcome.response["error"]["code"] == "REGISTRY_INCOMPLETE"
    assert outcome.response["error"]["details"]["symbol"] == "NVDA"


def test_unsupported_asset_class_factory_parses_json_envelope() -> None:
    error_message = json.dumps(
        {
            "code": "UNSUPPORTED_ASSET_CLASS",
            "message": "Symbol 'SPY_CALL' resolved to asset_class='option'",
            "details": {"symbol": "SPY_CALL", "asset_class": "option"},
        }
    )
    outcome = EndpointOutcome.registry_permanent_failure(
        FailureKind.UNSUPPORTED_ASSET_CLASS,
        error_message,
    )
    assert outcome.response["error"]["code"] == "UNSUPPORTED_ASSET_CLASS"
    assert outcome.response["error"]["details"]["asset_class"] == "option"


def test_ambiguous_registry_factory_parses_json_envelope() -> None:
    error_message = json.dumps(
        {
            "code": "AMBIGUOUS_REGISTRY",
            "message": "Symbol 'ES' has multiple active aliases",
            "details": {
                "symbol": "ES",
                "reason": "same_day_overlap",
                "conflicts": ["ESM6.CME", "ESU6.CME"],
            },
        }
    )
    outcome = EndpointOutcome.registry_permanent_failure(
        FailureKind.AMBIGUOUS_REGISTRY,
        error_message,
    )
    assert outcome.response["error"]["code"] == "AMBIGUOUS_REGISTRY"
    assert outcome.response["error"]["details"]["reason"] == "same_day_overlap"


def test_registry_permanent_failure_rejects_non_registry_kind() -> None:
    with pytest.raises(AssertionError, match="non-registry kind"):
        EndpointOutcome.registry_permanent_failure(
            FailureKind.SPAWN_FAILED_PERMANENT,
            "{}",
        )


def test_registry_permanent_failure_falls_back_on_non_json_message() -> None:
    """Defensive: legacy row or corrupt write → well-formed body with
    raw string as message and empty details dict."""
    outcome = EndpointOutcome.registry_permanent_failure(
        FailureKind.REGISTRY_MISS,
        "plain text from older version",
    )
    body = outcome.response
    assert body["error"]["code"] == "REGISTRY_MISS"
    assert body["error"]["message"] == "plain text from older version"
    assert body["error"]["details"] == {}


def test_registry_permanent_failure_falls_back_on_empty_string() -> None:
    outcome = EndpointOutcome.registry_permanent_failure(
        FailureKind.REGISTRY_MISS,
        "",
    )
    body = outcome.response
    assert body["error"]["code"] == "REGISTRY_MISS"


def test_registry_permanent_failure_falls_back_on_non_dict_json() -> None:
    """Parsed JSON is a list or string, not a dict → fall back."""
    outcome = EndpointOutcome.registry_permanent_failure(
        FailureKind.REGISTRY_MISS,
        '["not", "a", "dict"]',
    )
    body = outcome.response
    assert body["error"]["code"] == "REGISTRY_MISS"
    assert body["error"]["details"] == {}
