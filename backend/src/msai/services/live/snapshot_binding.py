"""Snapshot binding — verify a frozen portfolio member matches its graduated candidate.

Bug #3 of the live-deploy-safety-trio. Replaces the temporary
``LIVE_DEPLOY_BLOCKED`` 503 guard from PR #63 with real per-member
verification: `config` (minus deploy-injected fields) AND
`instruments` (as a sorted set) must match the approved
:class:`GraduationCandidate` at the time of live deploy. This is the
final safety check that lets ``paper_trading=false`` deployments
through the API.

See ``docs/plans/2026-05-13-live-deploy-safety-trio.md`` §Bug #3.

Design notes (carrying forward 10 Codex plan-review iterations):

- **Canonical-JSON comparison** uses an inline ``_canonicalize_config``
  helper rather than reusing ``services/live/portfolio_composition.py``'s
  ``_canonicalize_member`` — the latter canonicalizes the FULL member
  (strategy_id + order_index + config + instruments + weight) which is
  the wrong shape for binding. We only want config-level equality.
- **Strip deploy-injected fields** (``manage_stop``, ``order_id_tag``,
  ``market_exit_time_in_force``) before comparison — MSAI injects these
  at deploy time, NOT at strategy-design time, so they must not cause
  spurious binding mismatches.
- **Strip ``instruments`` from config comparison** — member has BOTH
  ``member.config`` AND ``member.instruments`` (separate column). If we
  left ``instruments`` in the config compare we'd either double-count
  or get spurious diffs. Compare instruments separately as a sorted
  set.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from msai.models.graduation_candidate import GraduationCandidate
    from msai.models.live_portfolio_revision_strategy import LivePortfolioRevisionStrategy


_DEPLOY_INJECTED_FIELDS: frozenset[str] = frozenset(
    {"manage_stop", "order_id_tag", "market_exit_time_in_force"}
)
"""Fields MSAI injects into ImportableStrategyConfig at deploy time.
Must not affect binding equality — the candidate's config (as
graduated) does not contain them; the member's config (frozen at
revision-snapshot time) also does not, but a future operator-edit
to the member could add them by accident. Stripping both sides
makes the comparison robust."""

_COMPARISON_STRIPPED_FIELDS: frozenset[str] = _DEPLOY_INJECTED_FIELDS | frozenset({"instruments"})
"""Stripped from config before canonicalization for both binding
verification AND binding-fingerprint computation. ``instruments``
is checked separately as a sorted set (see ``instruments_match``)."""


class BindingMismatchError(ValueError):
    """A frozen portfolio member's `config` or `instruments` diverged
    from its approved :class:`GraduationCandidate`.

    Surfaces as HTTP 422 with ``error.code = "BINDING_MISMATCH"`` and
    a ``details`` list naming each divergent field.
    """

    def __init__(self, mismatches: list[dict[str, Any]]) -> None:
        self.mismatches = mismatches
        super().__init__(
            "Frozen portfolio member diverged from its graduated candidate: "
            + ", ".join(m["field"] for m in mismatches)
        )


class BindingInstrumentsMissingError(ValueError):
    """The graduated candidate has no ``instruments`` key in its
    ``config`` dict. Pre-Bug-#3 candidates predate the snapshot-binding
    contract — operator must re-graduate or run the one-shot backfill
    script at ``scripts/backfill_candidate_instruments.py``.

    Surfaces as HTTP 422 with ``error.code = "BINDING_INSTRUMENTS_MISSING"``.
    """


def strip_for_comparison(config: dict[str, Any]) -> dict[str, Any]:
    """Return ``config`` with deploy-injected fields + ``instruments``
    removed.

    Used by both the binding verifier (to compare member.config to
    candidate.config) AND the binding-fingerprint computation (so the
    fingerprint is stable across stage transitions that don't actually
    change strategy parameters).
    """
    return {k: v for k, v in config.items() if k not in _COMPARISON_STRIPPED_FIELDS}


def _canonicalize_config(config: dict[str, Any]) -> str:
    """Deterministic round-trip JSON string for config comparison.

    ``sort_keys=True`` + ``separators=(",", ":")`` produces an output
    where ``{"a":1,"b":{"c":2,"d":3}}`` and ``{"b":{"d":3,"c":2},"a":1}``
    canonicalize identically. Decimal / UUID / datetime are NOT expected
    in strategy config — if encountered, ``json.dumps`` raises TypeError
    and the API surfaces a 500 (config has an unserializable value).
    """
    return json.dumps(config, sort_keys=True, separators=(",", ":"))


def candidate_instruments(candidate: GraduationCandidate) -> list[str]:
    """Read instruments from the candidate's ``config["instruments"]``.

    Raises :class:`BindingInstrumentsMissingError` if absent — pre-Bug-#3
    candidates won't have this key (research promotion at
    ``api/research.py`` did not stamp it). The backfill script handles
    the transition; new candidates get instruments stamped at promotion.
    """
    raw = candidate.config.get("instruments")
    if not isinstance(raw, list) or not raw:
        raise BindingInstrumentsMissingError(
            f"Candidate {candidate.id} has no `instruments` in config — "
            "this candidate predates the snapshot-binding contract. "
            "Re-graduate it OR run `scripts/backfill_candidate_instruments.py`."
        )
    return [str(x) for x in raw]


def instruments_match(member_instruments: list[str], cand_instruments: list[str]) -> bool:
    """Strict sorted-set equality for instrument lists.

    Set semantics so order is irrelevant; dedupe is implicit in ``set()``.
    Callers ensure both sides are already in the SAME canonical form
    (typically ``"SYMBOL.VENUE"`` from ``lookup_for_live`` — see
    ``api/live.py`` for the canonicalization call site).
    """
    return set(member_instruments) == set(cand_instruments)


def verify_member_matches_candidate(
    member: LivePortfolioRevisionStrategy,
    candidate: GraduationCandidate,
    *,
    member_instruments_canonical: list[str] | None = None,
    candidate_instruments_canonical: list[str] | None = None,
) -> None:
    """Raise :class:`BindingMismatchError` if the frozen member's
    ``config`` (minus deploy-injected fields) OR ``instruments``
    (sorted-set) diverge from the candidate's.

    When the optional ``*_canonical`` lists are provided (Codex round-1
    P2 fix), they're used as the authoritative canonical form for the
    instruments comparison — typically resolved by the caller via
    ``lookup_for_live(as_of_date=exchange_local_today())`` so futures
    rolls / alias drift don't false-reject equivalent symbols. If
    omitted, falls back to the raw model fields (only safe for assets
    with no roll/alias semantics — equity/FX).
    """
    mismatches: list[dict[str, Any]] = []

    member_cfg_canonical = _canonicalize_config(strip_for_comparison(member.config))
    cand_cfg_canonical = _canonicalize_config(strip_for_comparison(candidate.config))
    if member_cfg_canonical != cand_cfg_canonical:
        mismatches.append(
            {
                "field": "config",
                "member_value": json.loads(member_cfg_canonical),
                "candidate_value": json.loads(cand_cfg_canonical),
            }
        )

    cand_inst = (
        candidate_instruments_canonical
        if candidate_instruments_canonical is not None
        else candidate_instruments(candidate)  # raises if missing
    )
    member_inst = (
        member_instruments_canonical
        if member_instruments_canonical is not None
        else list(member.instruments)
    )
    if not instruments_match(member_inst, cand_inst):
        mismatches.append(
            {
                "field": "instruments",
                "member_value": sorted(set(member_inst)),
                "candidate_value": sorted(set(cand_inst)),
            }
        )

    if mismatches:
        raise BindingMismatchError(mismatches)


def compute_member_fingerprint(
    *,
    member_id: str,
    member_config: dict[str, Any],
    member_instruments_canonical: list[str],
    candidate_id: str,
    candidate_config: dict[str, Any],
    candidate_instruments_canonical: list[str],
) -> str:
    """Per-member contribution to the binding fingerprint.

    Used by the API's pre-reserve sequence to detect candidate drift
    (re-graduation, instrument edits) and force a fresh idempotency
    body_hash so the binding gets re-checked. Stable across stage
    transitions (the candidate's stage isn't part of the input) so a
    successful first deploy's replay still hits the cached outcome.

    The candidate side is hashed (rather than included raw) so the
    final fingerprint stays a constant 64-char hex regardless of
    candidate config size.
    """
    cand_content_hash = hashlib.sha256(
        _canonicalize_config(strip_for_comparison(candidate_config)).encode("utf-8")
        + b"|"
        + "|".join(sorted(set(candidate_instruments_canonical))).encode("utf-8")
    ).hexdigest()
    return "|".join(
        [
            member_id,
            _canonicalize_config(strip_for_comparison(member_config)),
            "|".join(sorted(set(member_instruments_canonical))),
            candidate_id,
            cand_content_hash,
        ]
    )


def compute_binding_fingerprint(member_parts: list[str]) -> str:
    """Aggregate per-member fingerprints into a single 64-char hex digest.

    The API folds this into the idempotency ``body_hash`` BEFORE
    ``idem.reserve(...)`` so candidate drift invalidates cached
    outcomes naturally — no need to re-run binding verification
    inside the cached-outcome path.
    """
    return hashlib.sha256("||".join(member_parts).encode("utf-8")).hexdigest()
