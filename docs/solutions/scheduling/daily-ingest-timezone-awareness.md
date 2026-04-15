# Daily ingest scheduler — timezone-aware + idempotent

## Problem

Claude's daily-ingest cron was hardcoded to fire at `06:00 UTC` via
`_cron(_nightly, hour=6, minute=0)`. Two real-world issues:

1. **Non-US markets**: An operator running LSE-tracked strategies wanted
   the ingest to fire after London close (16:30 BST/GMT). With the
   hardcoded UTC schedule, that required editing the source and rebuilding
   the worker container. Same for Tokyo (TSE 15:00 JST), Sydney (16:00
   AEDT), etc.
2. **No idempotency across restarts**: arq cron fires once per scheduled
   minute, but a worker restart spanning `06:00 UTC` could trigger a
   second run. There was no record of the most recent run date, so two
   ingests for the same date would compete (Polygon/Databento billing,
   parquet dedup churn).

## Root cause

Two missing capabilities:

1. The scheduler had no notion of timezone — the arq `cron` builder takes
   raw UTC hour/minute integers and the codebase had no `ZoneInfo` plumbing.
2. The scheduler was stateless — `arq` itself does not persist
   "last-fired" across restarts in a way the cron decision logic can read.

## Solution

Port Codex's `daily_scheduler.py` design but adapt to Claude's
arq-cron-based execution model (Codex uses a standalone asyncio loop):

1. **New settings** in `core/config.py`:
   - `daily_ingest_enabled: bool = True`
   - `daily_ingest_timezone: str = "America/New_York"`
   - `daily_ingest_hour: int = 18`
   - `daily_ingest_minute: int = 0`
   - `scheduler_state_path` property → `{data_root}/scheduler/daily_ingest_state.json`

2. **New wrapper** `run_nightly_ingest_if_due` in `workers/nightly_ingest.py`:
   - Returns early if disabled.
   - Parses `daily_ingest_timezone` via `ZoneInfo` (fails closed on bad tz).
   - Computes "now in tz" via `datetime.now(UTC).astimezone(zone)`.
   - Calls `_is_due(current, last_enqueued_date)` — True only when local time
     is past `(hour, minute, 0)` AND `last_enqueued_date != current.date()`.
   - On True, awaits `run_nightly_ingest(ctx)` (Claude's existing job).
   - **Only on success**, writes `current.date().isoformat()` to the state file.
   - On exception, state file is untouched → next minute's tick retries.

3. **arq cron change** in `workers/settings.py`:
   - Old: `_cron(_nightly, hour=6, minute=0)` — fires once daily at 06:00 UTC.
   - New: `_cron(_nightly_if_due, minute=None, second=0)` — fires every
     minute at :00, wrapper decides.

4. **State file resilience** — `_load_last_enqueued_date` tolerates
   missing files, `JSONDecodeError`, and valid-but-wrong-shape JSON
   (returns `None` so the next eligible tick self-heals the file).

## Prevention

- 24-test regression suite: tz boundaries (London/Tokyo parametrized),
  state-file round-trip + corrupt + wrong-shape, idempotency across
  same-day ticks, fires-again-next-day, ingest-failure-leaves-state-untouched,
  WorkerSettings registration positive + inverse guards (catches the
  next person who tries to re-register the bare `run_nightly_ingest`).
- The `test_no_cron_registers_bare_run_nightly_ingest` guard fails loudly
  if anyone bypasses the wrapper — this is the most likely future regression.

## Trade-off vs Codex's design

Codex runs a **standalone asyncio loop** as a separate worker process
that polls every 60s. Claude keeps the **arq cron** as the trigger so
the rest of the cron infrastructure (job_watchdog, pnl_aggregation) stays
on one execution model and Redis-backed cron uniqueness still applies.
The wrapper bridges the two designs without losing the operator-visible
configurability.

## Reference

- Claude wrapper: `claude-version/backend/src/msai/workers/nightly_ingest.py`
- Codex original: `codex-version/backend/src/msai/workers/daily_scheduler.py`
- Phase 2 audit: `docs/plans/2026-04-13-codex-claude-subsystem-audit.md`
