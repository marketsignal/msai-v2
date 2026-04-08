"""Unit tests for the deployment identity helper (Phase 1 task 1.1b).

Verifies the canonical-JSON sha256 identity model from decision #7:

- DeploymentIdentity dataclass produces deterministic, sortable canonical JSON
- compute_instruments_signature sorts before joining
- compute_config_hash accepts both Pydantic models and raw dicts
- compute_config_hash on a Pydantic model normalizes via model_dump(mode="json")
  so semantically-identical configs hash the same (Codex v5 P3 fix)
- derive_* helpers produce the right MSAI-{slug}, EMACrossStrategy-{slug},
  trader-MSAI-{slug}-stream values
- generate_deployment_slug returns 16-hex-char strings (64 bits)
- Same identity tuple → same identity_signature (warm restart)
- ANY field change (code_hash / config_hash / account_id / paper_trading /
  instruments / strategy_id / user) → DIFFERENT identity_signature (cold start)
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import BaseModel

from msai.services.live.deployment_identity import (
    DeploymentIdentity,
    canonicalize_user_id,
    compute_config_hash,
    compute_instruments_signature,
    derive_deployment_identity,
    derive_message_bus_stream,
    derive_strategy_id_full,
    derive_trader_id,
    generate_deployment_slug,
    normalize_request_config,
)

# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestComputeInstrumentsSignature:
    def test_sorts_alphabetically(self) -> None:
        sig = compute_instruments_signature(["MSFT.NASDAQ", "AAPL.NASDAQ"])
        assert sig == "AAPL.NASDAQ,MSFT.NASDAQ"

    def test_idempotent(self) -> None:
        a = compute_instruments_signature(["AAPL.NASDAQ", "MSFT.NASDAQ"])
        b = compute_instruments_signature(["MSFT.NASDAQ", "AAPL.NASDAQ"])
        assert a == b

    def test_single_instrument(self) -> None:
        assert compute_instruments_signature(["AAPL.NASDAQ"]) == "AAPL.NASDAQ"

    def test_empty_list(self) -> None:
        assert compute_instruments_signature([]) == ""

    def test_dedupe_not_performed(self) -> None:
        # Caller is responsible for de-duping; the helper preserves duplicates
        # to avoid silently masking caller bugs.
        assert (
            compute_instruments_signature(["AAPL.NASDAQ", "AAPL.NASDAQ"])
            == "AAPL.NASDAQ,AAPL.NASDAQ"
        )


class TestComputeConfigHash:
    def test_dict_canonical_sort(self) -> None:
        """Canonical JSON sorts keys, so dict order doesn't matter."""
        a = compute_config_hash({"x": 1, "y": 2})
        b = compute_config_hash({"y": 2, "x": 1})
        assert a == b

    def test_dict_different_values_different_hash(self) -> None:
        a = compute_config_hash({"x": 1})
        b = compute_config_hash({"x": 2})
        assert a != b

    def test_returns_64_hex_chars(self) -> None:
        h = compute_config_hash({"x": 1})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_pydantic_model_normalizes_via_model_dump(self) -> None:
        """Codex v5 P3 regression: hash the validated model, not the raw dict.

        A Pydantic model with `x: int` will coerce the string "5" to the
        int 5 during validation. The hash MUST come from the post-validation
        view so semantically-identical configs (one constructed from a
        string, one from an int) produce the same hash.
        """

        class StratConfig(BaseModel):
            x: int

        a = StratConfig(x=5)
        b = StratConfig(x="5")  # type: ignore[arg-type]  # Pydantic coerces
        assert a.x == b.x == 5
        assert compute_config_hash(a) == compute_config_hash(b)

    def test_raw_dicts_with_string_vs_int_hash_differently(self) -> None:
        """Counter-test for the previous case: raw dicts (no validation)
        with `{"x": 5}` vs `{"x": "5"}` should hash DIFFERENTLY. The
        Pydantic-model path is the only way to get the coercion benefit.
        """
        assert compute_config_hash({"x": 5}) != compute_config_hash({"x": "5"})


class TestDeriveHelpers:
    def test_derive_trader_id(self) -> None:
        assert derive_trader_id("a1b2c3d4e5f60718") == "MSAI-a1b2c3d4e5f60718"

    def test_derive_strategy_id_full(self) -> None:
        assert (
            derive_strategy_id_full("EMACrossStrategy", "a1b2c3d4e5f60718")
            == "EMACrossStrategy-a1b2c3d4e5f60718"
        )

    def test_derive_message_bus_stream(self) -> None:
        assert (
            derive_message_bus_stream("a1b2c3d4e5f60718") == "trader-MSAI-a1b2c3d4e5f60718-stream"
        )


class TestGenerateDeploymentSlug:
    def test_returns_16_hex_chars(self) -> None:
        slug = generate_deployment_slug()
        assert len(slug) == 16
        assert all(c in "0123456789abcdef" for c in slug)

    def test_random_each_call(self) -> None:
        slugs = {generate_deployment_slug() for _ in range(100)}
        # 100 random 64-bit values are virtually guaranteed to be distinct.
        assert len(slugs) == 100


# ---------------------------------------------------------------------------
# DeploymentIdentity dataclass tests
# ---------------------------------------------------------------------------


def _build(**overrides: object) -> DeploymentIdentity:
    """Construct a DeploymentIdentity with sensible defaults; overrides
    let each test isolate ONE field at a time."""
    base: dict[str, object] = {
        "started_by": "00000000000000000000000000000001",
        "strategy_id": "00000000000000000000000000000002",
        "strategy_code_hash": "deadbeef" * 8,
        "config_hash": "cafebabe" * 8,
        "account_id": "DU1234567",
        "paper_trading": True,
        "instruments_signature": "AAPL.NASDAQ,MSFT.NASDAQ",
    }
    base.update(overrides)
    return DeploymentIdentity(**base)  # type: ignore[arg-type]


class TestDeploymentIdentitySignature:
    def test_signature_is_64_hex_chars(self) -> None:
        sig = _build().signature()
        assert len(sig) == 64
        assert all(c in "0123456789abcdef" for c in sig)

    def test_warm_restart_same_inputs_same_signature(self) -> None:
        a = _build().signature()
        b = _build().signature()
        assert a == b

    def test_cold_start_different_strategy_code_hash(self) -> None:
        a = _build(strategy_code_hash="aaaa" * 16).signature()
        b = _build(strategy_code_hash="bbbb" * 16).signature()
        assert a != b

    def test_cold_start_different_config_hash(self) -> None:
        a = _build(config_hash="aaaa" * 16).signature()
        b = _build(config_hash="bbbb" * 16).signature()
        assert a != b

    def test_cold_start_different_account_id(self) -> None:
        a = _build(account_id="DU1111111").signature()
        b = _build(account_id="DU2222222").signature()
        assert a != b

    def test_cold_start_different_paper_trading_flag(self) -> None:
        a = _build(paper_trading=True).signature()
        b = _build(paper_trading=False).signature()
        assert a != b

    def test_cold_start_different_instruments(self) -> None:
        a = _build(instruments_signature="AAPL.NASDAQ").signature()
        b = _build(instruments_signature="MSFT.NASDAQ").signature()
        assert a != b

    def test_cold_start_different_strategy(self) -> None:
        a = _build(strategy_id="00000000000000000000000000000001").signature()
        b = _build(strategy_id="00000000000000000000000000000099").signature()
        assert a != b

    def test_cold_start_different_user(self) -> None:
        a = _build(started_by="11111111111111111111111111111111").signature()
        b = _build(started_by="22222222222222222222222222222222").signature()
        assert a != b


class TestCanonicalJson:
    def test_sorts_keys(self) -> None:
        canonical = _build().to_canonical_json().decode("utf-8")
        # Keys must appear in alphabetical order in the canonical form so
        # the hash is reproducible regardless of dataclass field order.
        keys_in_order = [
            "account_id",
            "config_hash",
            "instruments_signature",
            "paper_trading",
            "started_by",
            "strategy_code_hash",
            "strategy_id",
        ]
        positions = [canonical.index(f'"{k}":') for k in keys_in_order]
        assert positions == sorted(positions)

    def test_no_whitespace(self) -> None:
        canonical = _build().to_canonical_json().decode("utf-8")
        # separators=(",", ":") means no spaces after commas or colons.
        assert " " not in canonical


# ---------------------------------------------------------------------------
# derive_deployment_identity convenience function
# ---------------------------------------------------------------------------


class TestDeriveDeploymentIdentity:
    @pytest.fixture
    def baseline_args(self) -> dict[str, object]:
        return {
            "user_id": UUID("00000000-0000-0000-0000-000000000001"),
            "strategy_id": UUID("00000000-0000-0000-0000-000000000002"),
            "strategy_code_hash": "deadbeef" * 8,
            "config": {"fast": 10, "slow": 20},
            "account_id": "DU1234567",
            "paper_trading": True,
            "instruments": ["AAPL.NASDAQ", "MSFT.NASDAQ"],
        }

    def test_warm_restart_identical_inputs(self, baseline_args: dict[str, object]) -> None:
        a = derive_deployment_identity(**baseline_args).signature()
        b = derive_deployment_identity(**baseline_args).signature()
        assert a == b

    def test_config_change_produces_cold_start(self, baseline_args: dict[str, object]) -> None:
        a = derive_deployment_identity(**baseline_args).signature()
        changed = baseline_args | {"config": {"fast": 50, "slow": 200}}
        b = derive_deployment_identity(**changed).signature()
        assert a != b

    def test_instruments_order_does_not_matter(self, baseline_args: dict[str, object]) -> None:
        a = derive_deployment_identity(**baseline_args).signature()
        reordered = baseline_args | {"instruments": ["MSFT.NASDAQ", "AAPL.NASDAQ"]}
        b = derive_deployment_identity(**reordered).signature()
        assert a == b, "instruments_signature normalizes ordering"

    def test_two_parameterizations_of_same_strategy_get_distinct_signatures(
        self, baseline_args: dict[str, object]
    ) -> None:
        """The decision #7 motivation: EMA(10,20) and EMA(50,200) on the
        same instruments must be distinguishable so they can run as
        independent deployments with isolated state."""
        ema_short = derive_deployment_identity(
            **baseline_args | {"config": {"fast": 10, "slow": 20}}
        ).signature()
        ema_long = derive_deployment_identity(
            **baseline_args | {"config": {"fast": 50, "slow": 200}}
        ).signature()
        assert ema_short != ema_long

    def test_null_user_id_canonicalizes_to_empty_string(
        self, baseline_args: dict[str, object]
    ) -> None:
        """Codex Task 1.1b P2 fix — API-key requests (user_id=None) must
        canonicalize to the same ``""`` that the Alembic backfill uses,
        so an anonymous deployment hashes identically across the migration
        boundary. Regression guard against the previous `UUID(int=0).hex`
        mismatch.
        """
        anonymous = derive_deployment_identity(**baseline_args | {"user_id": None})
        assert anonymous.started_by == ""
        # And the signature must equal what you'd get by constructing a
        # DeploymentIdentity with started_by="" directly (same path as the
        # backfill).
        from_backfill = DeploymentIdentity(
            started_by="",
            strategy_id=anonymous.strategy_id,
            strategy_code_hash=anonymous.strategy_code_hash,
            config_hash=anonymous.config_hash,
            account_id=anonymous.account_id,
            paper_trading=anonymous.paper_trading,
            instruments_signature=anonymous.instruments_signature,
        )
        assert anonymous.signature() == from_backfill.signature()

    def test_null_user_id_distinct_from_zero_uuid(self, baseline_args: dict[str, object]) -> None:
        """``user_id=None`` must NOT collide with ``user_id=UUID(int=0)``.

        The old /start code used ``user_id or UUID(int=0)`` as a NULL
        fallback, which produced the 32-char zero hex string. That's a
        real (if improbable) user id; anonymous traffic must remain
        distinct from it so the two audiences don't share warm-restart
        state.
        """
        anonymous = derive_deployment_identity(**baseline_args | {"user_id": None})
        zero_uuid = derive_deployment_identity(**baseline_args | {"user_id": UUID(int=0)})
        assert anonymous.signature() != zero_uuid.signature()

    def test_user_sub_fallback_distinguishes_unresolved_users(
        self, baseline_args: dict[str, object]
    ) -> None:
        """Codex Task 1.1b iteration 5, P1 fix: two first-time JWT users
        whose ``users`` rows haven't been provisioned yet must NOT
        collapse to the same anonymous identity. Passing ``user_sub``
        preserves caller distinctness.
        """
        alice = derive_deployment_identity(
            **baseline_args | {"user_id": None, "user_sub": "alice@example.com"}
        )
        bob = derive_deployment_identity(
            **baseline_args | {"user_id": None, "user_sub": "bob@example.com"}
        )
        assert alice.signature() != bob.signature()

    def test_user_sub_fallback_distinct_from_anonymous(
        self, baseline_args: dict[str, object]
    ) -> None:
        """A JWT user with unresolved user_id must NOT hash the same as
        a truly anonymous (API-key) request with no sub at all."""
        jwt_user = derive_deployment_identity(
            **baseline_args | {"user_id": None, "user_sub": "someone@example.com"}
        )
        anonymous = derive_deployment_identity(**baseline_args | {"user_id": None})
        assert jwt_user.signature() != anonymous.signature()

    def test_user_sub_ignored_when_user_id_resolved(self, baseline_args: dict[str, object]) -> None:
        """Once the ``users`` row exists, identity is keyed by the UUID.
        The sub fallback must be a no-op in that case, otherwise the
        pre-/auth/me and post-/auth/me starts of the same caller would
        never warm-restart."""
        without_sub = derive_deployment_identity(**baseline_args)
        with_sub = derive_deployment_identity(**baseline_args | {"user_sub": "anything"})
        assert without_sub.signature() == with_sub.signature()


class TestCanonicalizeUserId:
    def test_none_maps_to_empty_string(self) -> None:
        assert canonicalize_user_id(None) == ""

    def test_uuid_maps_to_32_char_hex(self) -> None:
        assert (
            canonicalize_user_id(UUID("deadbeef-dead-beef-dead-beefdeadbeef"))
            == "deadbeefdeadbeefdeadbeefdeadbeef"
        )

    def test_sub_fallback_when_user_id_none(self) -> None:
        assert canonicalize_user_id(None, fallback_sub="alice@x.com") == "sub:alice@x.com"

    def test_sub_fallback_ignored_when_user_id_present(self) -> None:
        uid = UUID("deadbeef-dead-beef-dead-beefdeadbeef")
        assert canonicalize_user_id(uid, fallback_sub="anything") == uid.hex

    def test_empty_sub_fallback_treated_as_none(self) -> None:
        assert canonicalize_user_id(None, fallback_sub="") == ""
        assert canonicalize_user_id(None, fallback_sub=None) == ""


class TestNormalizeRequestConfig:
    """Codex Task 1.1b P2 fix — default-config merge makes ``{}`` and
    ``{"fast": 10}`` hash identically when the strategy's stored default
    is ``{"fast": 10}``, so explicit-vs-default parameter passing doesn't
    produce spurious cold starts."""

    def test_no_default_returns_copy_of_request(self) -> None:
        request = {"fast": 10}
        result = normalize_request_config(request, None)
        assert result == {"fast": 10}
        # Must be a new dict — caller should not be able to mutate our view.
        result["fast"] = 99
        assert request == {"fast": 10}

    def test_empty_default_returns_copy_of_request(self) -> None:
        assert normalize_request_config({"fast": 10}, {}) == {"fast": 10}

    def test_fills_in_missing_keys_from_default(self) -> None:
        request = {"fast": 10}
        default = {"fast": 10, "slow": 30}
        assert normalize_request_config(request, default) == {"fast": 10, "slow": 30}

    def test_request_overrides_default(self) -> None:
        request = {"fast": 99}
        default = {"fast": 10, "slow": 30}
        assert normalize_request_config(request, default) == {"fast": 99, "slow": 30}

    def test_empty_request_fully_resolved_to_default(self) -> None:
        default = {"fast": 10, "slow": 30}
        assert normalize_request_config({}, default) == {"fast": 10, "slow": 30}

    def test_omitted_default_equals_explicit_default_after_hash(self) -> None:
        """The actual regression scenario: same hash whether the caller
        omits a defaulted parameter or passes it explicitly."""
        default = {"fast": 10, "slow": 30}
        omitted = normalize_request_config({}, default)
        explicit = normalize_request_config({"fast": 10, "slow": 30}, default)
        assert compute_config_hash(omitted) == compute_config_hash(explicit)

    def test_does_not_mutate_inputs(self) -> None:
        request = {"fast": 99}
        default = {"fast": 10, "slow": 30}
        _ = normalize_request_config(request, default)
        assert request == {"fast": 99}
        assert default == {"fast": 10, "slow": 30}
