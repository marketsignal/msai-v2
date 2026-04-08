"""``OrderIntent`` sequence comparator (Phase 2 task 2.11).

The comparator answers a single question: given two sequences of
:class:`OrderIntent` tuples, do they represent the SAME strategy
decisions? If they don't, what's the first difference?

Used by:

- The determinism test (run the same backtest twice → assert
  ``compare(a, b)`` is empty).
- The post-hoc backtest-vs-paper-soak comparison documented in
  the plan: an operator re-runs the same strategy in backtest
  against the same Parquet window the paper soak ran live and
  the comparator surfaces any divergence.

Design:

- :func:`compare` returns a list of :class:`Divergence` records.
- Empty list means the sequences match exactly. The caller can
  treat this as a boolean ("parity OK?") via ``not divergences``.
- The comparator does NOT raise on a mismatch — it returns
  structured data so the caller can format / log / commit it
  however it wants.
- Divergence categories: ``LENGTH_MISMATCH`` (one sequence is
  longer than the other after pairwise comparison),
  ``FIELD_MISMATCH`` (positional rows differ on at least one
  field), ``EXTRA_LEFT`` / ``EXTRA_RIGHT`` (records present in
  one sequence but not the other after pairwise alignment).

The comparison is positional, not set-based: order matters
because the parity contract is about the SAME decisions in the
SAME order. Two sequences with identical content but different
ordering ARE considered divergent — that's the right behavior
for the determinism test (a strategy emitting the same set of
trades in different orders is non-deterministic by definition).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from msai.services.nautilus.parity.normalizer import OrderIntent


class DivergenceKind(StrEnum):
    """Categories of mismatch the comparator can report."""

    LENGTH_MISMATCH = "length_mismatch"
    """The sequences have different lengths. Reported once at the
    end of pairwise comparison; per-position differences in the
    overlap region are reported separately as ``FIELD_MISMATCH``."""

    FIELD_MISMATCH = "field_mismatch"
    """At a given index, the two sequences have intents that
    differ on at least one field. The :class:`Divergence` carries
    both ``left`` and ``right`` so callers can render the diff."""

    EXTRA_LEFT = "extra_left"
    """The left sequence has additional intents past the end of
    the right sequence. ``index`` is the position in ``left``;
    ``right`` is ``None``."""

    EXTRA_RIGHT = "extra_right"
    """Symmetric to ``EXTRA_LEFT`` — the right sequence has
    intents past the end of the left."""


@dataclass(slots=True, frozen=True)
class Divergence:
    """A single mismatch between two intent sequences.

    Both ``left`` and ``right`` are present for ``FIELD_MISMATCH``
    so callers can render a side-by-side diff. Only the relevant
    side is populated for ``EXTRA_LEFT`` / ``EXTRA_RIGHT``.
    ``LENGTH_MISMATCH`` carries no per-row data — it's a summary
    record.
    """

    kind: DivergenceKind
    index: int
    left: OrderIntent | None
    right: OrderIntent | None


def compare(
    left: list[OrderIntent],
    right: list[OrderIntent],
) -> list[Divergence]:
    """Compare two ``OrderIntent`` sequences positionally.

    Returns an empty list when the sequences match exactly.
    Otherwise returns a list of :class:`Divergence` records
    describing every mismatch.

    The algorithm walks both sequences simultaneously up to
    ``min(len(left), len(right))`` and emits a ``FIELD_MISMATCH``
    for any index where the intents differ. Then it emits
    ``EXTRA_LEFT`` / ``EXTRA_RIGHT`` for the trailing tail in
    whichever sequence is longer, plus a final
    ``LENGTH_MISMATCH`` summary so callers know to surface the
    length difference distinctly.
    """
    divergences: list[Divergence] = []
    overlap = min(len(left), len(right))

    for i in range(overlap):
        if left[i] != right[i]:
            divergences.append(
                Divergence(
                    kind=DivergenceKind.FIELD_MISMATCH,
                    index=i,
                    left=left[i],
                    right=right[i],
                )
            )

    if len(left) > overlap:
        for i in range(overlap, len(left)):
            divergences.append(
                Divergence(
                    kind=DivergenceKind.EXTRA_LEFT,
                    index=i,
                    left=left[i],
                    right=None,
                )
            )
        divergences.append(
            Divergence(
                kind=DivergenceKind.LENGTH_MISMATCH,
                index=overlap,
                left=None,
                right=None,
            )
        )

    if len(right) > overlap:
        for i in range(overlap, len(right)):
            divergences.append(
                Divergence(
                    kind=DivergenceKind.EXTRA_RIGHT,
                    index=i,
                    left=None,
                    right=right[i],
                )
            )
        divergences.append(
            Divergence(
                kind=DivergenceKind.LENGTH_MISMATCH,
                index=overlap,
                left=None,
                right=None,
            )
        )

    return divergences


def is_identical(
    left: list[OrderIntent],
    right: list[OrderIntent],
) -> bool:
    """Convenience boolean wrapper around :func:`compare`. The
    determinism test uses this so the assertion is a single
    line: ``assert is_identical(a, b)``."""
    return not compare(left, right)
