"""F8 fix (Codex iter 2 P2 / pr-toolkit code-reviewer): the supervisor's
venue-suffix stripping helper must keep dotted share-class roots like
``BRK.B`` when stripping ``.NYSE``. The old ``.split(".")[0]`` returned
``BRK`` for ``BRK.B.NYSE`` — wrong root + wrong resolved instrument.
"""

from __future__ import annotations

import pytest

from msai.live_supervisor.__main__ import _strip_venue_suffix


class TestStripVenueSuffix:
    @pytest.mark.parametrize(
        ("instrument", "expected"),
        [
            # Plain SYM.VENUE — strips venue, returns SYM.
            ("AAPL.NASDAQ", "AAPL"),
            ("MSFT.IBKR", "MSFT"),
            ("GE.NYSE", "GE"),
            # F8 critical case: share-class ticker like BRK.B with a
            # venue suffix. ``rsplit(".", 1)`` keeps ``BRK.B``.
            ("BRK.B.NYSE", "BRK.B"),
            ("BRK.B.IBKR", "BRK.B"),
            # Three-dotted share-class via the IBKR canonical path.
            ("BF.B.NYSE", "BF.B"),
            # No-dot bare symbol passes through unchanged.
            ("AAPL", "AAPL"),
            ("BRK.B", "BRK"),  # bare share-class without venue: ambiguous; rsplit strips B
        ],
    )
    def test_rsplit_keeps_dotted_root(self, instrument: str, expected: str) -> None:
        assert _strip_venue_suffix(instrument) == expected

    def test_brk_b_with_venue_does_not_lose_share_class(self) -> None:
        """F8 regression: ``BRK.B.NYSE`` is the security_master's
        fully-qualified form for the BRK.B share class. The supervisor
        must extract ``BRK.B`` as the root — not ``BRK`` — or the
        per-account payload subscribes / resolves the wrong instrument.
        """
        assert _strip_venue_suffix("BRK.B.NYSE") == "BRK.B"
        # Sanity check: the old (buggy) ``.split(".")[0]`` would have
        # returned "BRK"; we explicitly assert that's not what we get.
        assert _strip_venue_suffix("BRK.B.NYSE") != "BRK"
