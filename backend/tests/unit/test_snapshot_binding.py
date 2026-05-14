"""Unit tests for ``services/live/snapshot_binding.py`` — Bug #3 of the
live-deploy-safety-trio.

Verifies the helpers that replace PR #63's temporary 503
LIVE_DEPLOY_BLOCKED guard with real per-member config + instruments
verification.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from msai.services.live.snapshot_binding import (
    BindingInstrumentsMissingError,
    BindingMismatchError,
    _canonicalize_config,
    candidate_instruments,
    compute_binding_fingerprint,
    compute_member_fingerprint,
    instruments_match,
    strip_for_comparison,
    verify_member_matches_candidate,
)

# ---------------------------------------------------------------------------
# _canonicalize_config
# ---------------------------------------------------------------------------


class TestCanonicalizeConfig:
    def test_key_order_invariant(self) -> None:
        """The whole point — `{"a":1,"b":{"c":2,"d":3}}` and
        `{"b":{"d":3,"c":2},"a":1}` must canonicalize identically."""
        a = _canonicalize_config({"a": 1, "b": {"c": 2, "d": 3}})
        b = _canonicalize_config({"b": {"d": 3, "c": 2}, "a": 1})
        assert a == b

    def test_compact_separators(self) -> None:
        """No whitespace in output — `json.dumps` with
        `separators=(",", ":")` keeps the string compact."""
        out = _canonicalize_config({"a": 1, "b": 2})
        assert " " not in out

    def test_empty_dict(self) -> None:
        assert _canonicalize_config({}) == "{}"


# ---------------------------------------------------------------------------
# strip_for_comparison
# ---------------------------------------------------------------------------


class TestStripForComparison:
    def test_removes_deploy_injected_fields(self) -> None:
        result = strip_for_comparison(
            {
                "fast_ema_period": 10,
                "manage_stop": True,
                "order_id_tag": "0-slug",
                "market_exit_time_in_force": 5,
            }
        )
        assert result == {"fast_ema_period": 10}

    def test_removes_instruments(self) -> None:
        """Instruments are compared separately as a sorted set."""
        result = strip_for_comparison({"a": 1, "instruments": ["AAPL.NASDAQ"]})
        assert result == {"a": 1}

    def test_preserves_strategy_params(self) -> None:
        result = strip_for_comparison(
            {
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
                "fast_ema_period": 10,
                "slow_ema_period": 30,
            }
        )
        # `instrument_id` is NOT stripped — it's a strategy param, not
        # the list-of-instruments column.
        assert result == {
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "fast_ema_period": 10,
            "slow_ema_period": 30,
        }


# ---------------------------------------------------------------------------
# instruments_match
# ---------------------------------------------------------------------------


class TestInstrumentsMatch:
    def test_identical(self) -> None:
        assert instruments_match(["AAPL.NASDAQ"], ["AAPL.NASDAQ"])

    def test_order_invariant(self) -> None:
        assert instruments_match(["AAPL.NASDAQ", "MSFT.NASDAQ"], ["MSFT.NASDAQ", "AAPL.NASDAQ"])

    def test_dedupe_irrelevant(self) -> None:
        """Set semantics — dupes don't break match. Real callers
        shouldn't be sending dupes anyway."""
        assert instruments_match(["AAPL.NASDAQ", "AAPL.NASDAQ"], ["AAPL.NASDAQ"])

    def test_subset_mismatch(self) -> None:
        assert not instruments_match(["AAPL.NASDAQ"], ["AAPL.NASDAQ", "MSFT.NASDAQ"])

    def test_disjoint_mismatch(self) -> None:
        assert not instruments_match(["AAPL.NASDAQ"], ["MSFT.NASDAQ"])

    def test_empty_both_sides_match(self) -> None:
        # Edge — both empty technically matches. Caller is responsible
        # for rejecting empty-instruments candidates upstream via
        # `candidate_instruments()`.
        assert instruments_match([], [])


# ---------------------------------------------------------------------------
# candidate_instruments
# ---------------------------------------------------------------------------


def _make_candidate(config: dict[str, Any]) -> Any:
    return SimpleNamespace(id=uuid4(), config=config)


class TestCandidateInstruments:
    def test_extracts_list(self) -> None:
        c = _make_candidate({"instruments": ["AAPL.NASDAQ"]})
        assert candidate_instruments(c) == ["AAPL.NASDAQ"]

    def test_missing_key_raises(self) -> None:
        c = _make_candidate({"fast_ema_period": 10})  # no instruments
        with pytest.raises(BindingInstrumentsMissingError) as exc:
            candidate_instruments(c)
        assert "predates the snapshot-binding contract" in str(exc.value)

    def test_empty_list_raises(self) -> None:
        """Empty `instruments: []` is also pre-contract — operator
        needs to re-graduate."""
        c = _make_candidate({"instruments": []})
        with pytest.raises(BindingInstrumentsMissingError):
            candidate_instruments(c)

    def test_non_list_raises(self) -> None:
        c = _make_candidate({"instruments": "AAPL.NASDAQ"})  # string not list
        with pytest.raises(BindingInstrumentsMissingError):
            candidate_instruments(c)


# ---------------------------------------------------------------------------
# verify_member_matches_candidate
# ---------------------------------------------------------------------------


def _make_member(
    *,
    config: dict[str, Any],
    instruments: list[str],
) -> Any:
    return SimpleNamespace(
        id=uuid4(),
        config=config,
        instruments=instruments,
    )


class TestVerifyMemberMatchesCandidate:
    def test_exact_match_passes(self) -> None:
        cfg = {"instrument_id": "AAPL.NASDAQ", "fast_ema_period": 10}
        m = _make_member(config=cfg, instruments=["AAPL.NASDAQ"])
        c = _make_candidate({**cfg, "instruments": ["AAPL.NASDAQ"]})
        verify_member_matches_candidate(m, c)  # does not raise

    def test_deploy_injected_fields_ignored(self) -> None:
        """`manage_stop`, `order_id_tag`, `market_exit_time_in_force`
        added at deploy time must not cause a binding mismatch."""
        m_cfg = {"fast_ema_period": 10, "manage_stop": True, "order_id_tag": "x"}
        c_cfg = {"fast_ema_period": 10, "instruments": ["AAPL.NASDAQ"]}
        m = _make_member(config=m_cfg, instruments=["AAPL.NASDAQ"])
        c = _make_candidate(c_cfg)
        verify_member_matches_candidate(m, c)

    def test_divergent_config_raises_with_diff(self) -> None:
        m = _make_member(config={"fast_ema_period": 99}, instruments=["AAPL.NASDAQ"])
        c = _make_candidate({"fast_ema_period": 10, "instruments": ["AAPL.NASDAQ"]})
        with pytest.raises(BindingMismatchError) as exc:
            verify_member_matches_candidate(m, c)
        assert any(d["field"] == "config" for d in exc.value.mismatches)

    def test_divergent_instruments_raises_with_diff(self) -> None:
        cfg = {"fast_ema_period": 10}
        m = _make_member(config=cfg, instruments=["AAPL.NASDAQ"])
        c = _make_candidate({**cfg, "instruments": ["MSFT.NASDAQ"]})
        with pytest.raises(BindingMismatchError) as exc:
            verify_member_matches_candidate(m, c)
        instr_diff = next(d for d in exc.value.mismatches if d["field"] == "instruments")
        assert instr_diff["member_value"] == ["AAPL.NASDAQ"]
        assert instr_diff["candidate_value"] == ["MSFT.NASDAQ"]

    def test_both_divergent_returns_both(self) -> None:
        m = _make_member(config={"a": 1}, instruments=["AAPL.NASDAQ"])
        c = _make_candidate({"a": 2, "instruments": ["MSFT.NASDAQ"]})
        with pytest.raises(BindingMismatchError) as exc:
            verify_member_matches_candidate(m, c)
        fields = {d["field"] for d in exc.value.mismatches}
        assert fields == {"config", "instruments"}

    def test_candidate_without_instruments_raises_specific_error(self) -> None:
        """The instruments check fires AFTER the config check; if the
        config matches but instruments are missing on the candidate
        side, the error is BindingInstrumentsMissingError (not
        BindingMismatchError). The API maps these to different error
        codes."""
        cfg = {"a": 1}
        m = _make_member(config=cfg, instruments=["AAPL.NASDAQ"])
        c = _make_candidate(cfg)  # no instruments key
        with pytest.raises(BindingInstrumentsMissingError):
            verify_member_matches_candidate(m, c)


# ---------------------------------------------------------------------------
# compute_member_fingerprint + compute_binding_fingerprint
# ---------------------------------------------------------------------------


class TestComputeMemberFingerprint:
    def test_stable_across_irrelevant_changes(self) -> None:
        """Adding deploy-injected fields to either side must NOT change
        the fingerprint — that's the whole point."""
        base = dict(
            member_id="m1",
            member_config={"fast_ema_period": 10},
            member_instruments_canonical=["AAPL.NASDAQ"],
            candidate_id="c1",
            candidate_config={"fast_ema_period": 10, "instruments": ["AAPL.NASDAQ"]},
            candidate_instruments_canonical=["AAPL.NASDAQ"],
        )
        fp1 = compute_member_fingerprint(**base)
        # Add deploy-injected fields — fingerprint must stay the same.
        base2 = {**base, "member_config": {"fast_ema_period": 10, "manage_stop": True}}
        fp2 = compute_member_fingerprint(**base2)
        assert fp1 == fp2

    def test_changes_when_candidate_config_diverges(self) -> None:
        base = dict(
            member_id="m1",
            member_config={"fast_ema_period": 10},
            member_instruments_canonical=["AAPL.NASDAQ"],
            candidate_id="c1",
            candidate_config={"fast_ema_period": 10, "instruments": ["AAPL.NASDAQ"]},
            candidate_instruments_canonical=["AAPL.NASDAQ"],
        )
        fp1 = compute_member_fingerprint(**base)
        # Candidate re-graduated with different params → fingerprint changes.
        base2 = {
            **base,
            "candidate_config": {"fast_ema_period": 99, "instruments": ["AAPL.NASDAQ"]},
        }
        fp2 = compute_member_fingerprint(**base2)
        assert fp1 != fp2

    def test_changes_when_member_instruments_change(self) -> None:
        base = dict(
            member_id="m1",
            member_config={"a": 1},
            member_instruments_canonical=["AAPL.NASDAQ"],
            candidate_id="c1",
            candidate_config={"a": 1, "instruments": ["AAPL.NASDAQ"]},
            candidate_instruments_canonical=["AAPL.NASDAQ"],
        )
        fp1 = compute_member_fingerprint(**base)
        base2 = {**base, "member_instruments_canonical": ["MSFT.NASDAQ"]}
        fp2 = compute_member_fingerprint(**base2)
        assert fp1 != fp2

    def test_instrument_order_does_not_matter(self) -> None:
        """The sort makes the fingerprint stable against accidental
        re-orderings on the input lists."""
        base = dict(
            member_id="m1",
            member_config={"a": 1},
            member_instruments_canonical=["AAPL.NASDAQ", "MSFT.NASDAQ"],
            candidate_id="c1",
            candidate_config={"a": 1, "instruments": ["AAPL.NASDAQ", "MSFT.NASDAQ"]},
            candidate_instruments_canonical=["AAPL.NASDAQ", "MSFT.NASDAQ"],
        )
        fp1 = compute_member_fingerprint(**base)
        base2 = {
            **base,
            "member_instruments_canonical": ["MSFT.NASDAQ", "AAPL.NASDAQ"],
            "candidate_instruments_canonical": ["MSFT.NASDAQ", "AAPL.NASDAQ"],
        }
        fp2 = compute_member_fingerprint(**base2)
        assert fp1 == fp2


class TestComputeBindingFingerprint:
    def test_returns_64_char_hex(self) -> None:
        result = compute_binding_fingerprint(["part1", "part2"])
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_member_order_matters(self) -> None:
        """Unlike instruments-within-a-member (sorted), the order of
        MEMBERS in the portfolio is itself part of the identity — a
        portfolio of [A, B] differs from [B, A] if the order_index
        differs (which is encoded into member.id implicitly)."""
        a = compute_binding_fingerprint(["m1", "m2"])
        b = compute_binding_fingerprint(["m2", "m1"])
        assert a != b
