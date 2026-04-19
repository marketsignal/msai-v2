"""Opt-in live-paper smoke test for ``msai instruments refresh
--provider interactive_brokers``.

Gated on ``RUN_PAPER_E2E=1`` (mirrors the existing opt-in e2e pattern
in the repo). Requires the paper IB Gateway container to be up on
port 4002 with a DU*/DF* account.

Verifies the three things mocks can't:

1. The Nautilus factory signatures are actually correct in 1.223.0
   (research brief finding: ``wait_until_ready`` is bypassed;
   ``_stop_async`` is awaited; factory globals are cleared between
   process invocations).
2. Idempotent re-run: two back-to-back CLI invocations produce the
   same row count in ``instrument_definitions`` +
   ``instrument_aliases``.
3. Warm resolve: after refresh, ``SecurityMaster.resolve_for_live``
   returns without touching IB.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

RUN_PAPER_E2E = os.getenv("RUN_PAPER_E2E") == "1"

pytestmark = [
    pytest.mark.ib_paper,
    pytest.mark.skipif(
        not RUN_PAPER_E2E,
        reason="set RUN_PAPER_E2E=1 to run paper IB Gateway smoke tests",
    ),
]


def _invoke_refresh(*symbols: str) -> subprocess.CompletedProcess[str]:
    """Run ``msai instruments refresh`` as a subprocess (fresh process
    — Nautilus factory globals are naturally fresh per invocation)."""
    return subprocess.run(  # noqa: S603 — operator-invoked harness
        [
            sys.executable,
            "-m",
            "msai.cli",
            "instruments",
            "refresh",
            "--symbols",
            ",".join(symbols),
            "--provider",
            "interactive_brokers",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


async def _count_rows() -> tuple[int, int]:
    """Return current (definition_count, alias_count) for the
    interactive_brokers provider. Small helper so each test's
    assertions are unambiguous."""
    from sqlalchemy import func, select

    from msai.core.database import async_session_factory
    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition

    async with async_session_factory() as session:
        def_count = await session.scalar(
            select(func.count())
            .select_from(InstrumentDefinition)
            .where(
                InstrumentDefinition.provider == "interactive_brokers",
            ),
        )
        alias_count = await session.scalar(
            select(func.count())
            .select_from(InstrumentAlias)
            .where(
                InstrumentAlias.provider == "interactive_brokers",
            ),
        )
    return def_count or 0, alias_count or 0


async def test_refresh_writes_rows_for_aapl_and_es() -> None:
    """First invocation qualifies AAPL + ES and writes registry rows.

    Asserts actual row appearance in ``instrument_definitions`` and
    ``instrument_aliases``, not just CLI exit code — PRD US-001
    acceptance criteria.
    """
    before_defs, before_aliases = await _count_rows()
    result = _invoke_refresh("AAPL", "ES")
    assert result.returncode == 0, f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    after_defs, after_aliases = await _count_rows()
    # Both symbols must have produced at least one new row each.
    assert after_defs >= before_defs + 2, (
        f"expected ≥2 new InstrumentDefinition rows; before={before_defs}, after={after_defs}"
    )
    assert after_aliases >= before_aliases + 2


async def test_refresh_is_idempotent_on_second_run() -> None:
    """Second invocation within 60s is a no-op upsert — no NEW rows
    added to instrument_definitions OR instrument_aliases.

    PRD US-002 acceptance: 'running the CLI twice with the same
    symbols produces NO duplicate alias-window rows'.
    """
    first = _invoke_refresh("AAPL")
    assert first.returncode == 0, first.stderr
    mid_defs, mid_aliases = await _count_rows()

    second = _invoke_refresh("AAPL")
    assert second.returncode == 0, f"second run failed (client_id=999 collision?): {second.stderr}"
    after_defs, after_aliases = await _count_rows()

    # Exact equality: no new rows on the idempotent re-run.
    assert after_defs == mid_defs, (
        f"idempotency broken: definition rows grew {mid_defs} → {after_defs} on a no-op re-run"
    )
    assert after_aliases == mid_aliases, (
        f"idempotency broken: alias rows grew {mid_aliases} → {after_aliases} on a no-op re-run"
    )


async def test_warm_resolve_does_not_touch_ib() -> None:
    """After a successful refresh, resolve_for_live returns from the
    registry without spawning a new IB client (warm-path proof — PRD
    US-001 post-condition, US-002 persistence check).
    """
    prewarm = _invoke_refresh("AAPL")
    assert prewarm.returncode == 0

    # Now call resolve_for_live with NO qualifier — SecurityMaster
    # must resolve from the DB only. If it tries to touch IB, it'll
    # raise because qualifier is None.
    from msai.core.database import async_session_factory
    from msai.services.nautilus.security_master.service import SecurityMaster

    async with async_session_factory() as session:
        sm = SecurityMaster(qualifier=None, db=session)
        resolved = await sm.resolve_for_live(["AAPL"])
        assert len(resolved) == 1
        assert "AAPL" in resolved[0]
