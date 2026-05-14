"""Backfill ``GraduationCandidate.config["instruments"]`` for pre-Bug-#3 rows.

Bug #3 of the live-deploy-safety-trio (2026-05-13). Before this fix,
research promotion at ``api/research.py`` did NOT stamp ``instruments``
into the candidate's config — the field was a top-level request key on
``ResearchJob.config``, not in ``best_config`` / ``trial.config``. The
snapshot-binding verifier at ``POST /api/v1/live/start-portfolio``
requires it. Existing candidates predating the stamp need a one-shot
repair before they can be deployed via the new path.

Usage::

    # Dry-run (default — prints what would change, exits non-zero if any
    # candidate cannot be repaired automatically).
    python scripts/backfill_candidate_instruments.py

    # Apply (commits).
    python scripts/backfill_candidate_instruments.py --apply

Strategy:

1. Find all `live_eligible` candidates (stage in ELIGIBLE_FOR_LIVE_PORTFOLIO)
   whose ``config`` has no ``"instruments"`` key, an empty list, or a
   non-list value.
2. For each: try to read ``candidate.research_job.config["instruments"]``.
   If found → stamp into the candidate's config.
3. Candidates created manually (no ``research_job_id``) cannot be
   auto-repaired. Print them and exit non-zero so the operator
   re-graduates them manually.

Idempotent: re-running on an already-repaired DB is a no-op.

Lives under top-level ``scripts/`` (operator-invokable), NOT under
``backend/scripts/``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_SRC = _REPO_ROOT / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)

from msai.core.config import settings  # noqa: E402
from msai.models.graduation_candidate import GraduationCandidate  # noqa: E402
from msai.models.research_job import ResearchJob  # noqa: E402
from msai.services.graduation import ELIGIBLE_FOR_LIVE_PORTFOLIO  # noqa: E402

log = logging.getLogger("backfill_candidate_instruments")


def _needs_repair(config: dict | None) -> bool:
    """A candidate row needs repair if its config has no usable
    instruments list."""
    if not isinstance(config, dict):
        return True
    raw = config.get("instruments")
    return not isinstance(raw, list) or not raw


async def _backfill(apply: bool) -> int:
    """Returns process exit code: 0 = nothing to do or success,
    1 = at least one candidate could not be repaired automatically."""
    engine = create_async_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    repaired: list[tuple[str, list[str]]] = []
    skipped_no_job: list[str] = []
    skipped_job_missing_instruments: list[str] = []
    already_ok = 0

    eligible_stages = list(ELIGIBLE_FOR_LIVE_PORTFOLIO)

    try:
        async with sessionmaker() as session:
            candidates = list(
                (
                    await session.execute(
                        select(GraduationCandidate).where(
                            GraduationCandidate.stage.in_(eligible_stages)
                        )
                    )
                )
                .scalars()
                .all()
            )

            for cand in candidates:
                if not _needs_repair(cand.config):
                    already_ok += 1
                    continue

                if cand.research_job_id is None:
                    skipped_no_job.append(str(cand.id))
                    continue

                job = await session.get(ResearchJob, cand.research_job_id)
                if job is None or not isinstance(job.config, dict):
                    skipped_job_missing_instruments.append(str(cand.id))
                    continue

                job_instruments = job.config.get("instruments")
                if not isinstance(job_instruments, list) or not job_instruments:
                    skipped_job_missing_instruments.append(str(cand.id))
                    continue

                # Repair: stamp instruments into candidate config.
                # SQLAlchemy needs an explicit assignment for the
                # JSONB column to be marked dirty — mutate-in-place
                # of `cand.config["..."]` is NOT detected by the
                # change tracker.
                new_config = dict(cand.config or {})
                new_config["instruments"] = list(job_instruments)
                cand.config = new_config
                repaired.append((str(cand.id), list(job_instruments)))

            if apply and repaired:
                await session.commit()
                log.info("committed %d candidate repair(s)", len(repaired))
            elif repaired:
                log.info("DRY RUN — would repair %d candidate(s)", len(repaired))
    finally:
        await engine.dispose()

    log.info("already-ok candidates: %d", already_ok)
    log.info("repaired: %d", len(repaired))
    for cand_id, instruments in repaired:
        log.info("  - %s ← %s", cand_id, instruments)
    if skipped_no_job:
        log.warning(
            "skipped (manual candidate — no research_job_id): %d",
            len(skipped_no_job),
        )
        for cand_id in skipped_no_job:
            log.warning("  - %s (re-graduate manually with explicit instruments)", cand_id)
    if skipped_job_missing_instruments:
        log.warning(
            "skipped (parent job has no instruments in config): %d",
            len(skipped_job_missing_instruments),
        )
        for cand_id in skipped_job_missing_instruments:
            log.warning("  - %s", cand_id)

    if skipped_no_job or skipped_job_missing_instruments:
        log.error(
            "%d candidate(s) require manual operator action — re-graduate them",
            len(skipped_no_job) + len(skipped_job_missing_instruments),
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the repair. Default is dry-run (prints intended changes).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    return asyncio.run(_backfill(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
