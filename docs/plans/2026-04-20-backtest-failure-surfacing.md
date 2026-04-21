# Backtest Failure Surfacing — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When a backtest fails, the root-cause reason (classified + sanitized + optionally remediation-hinted) flows from the worker → DB → API → CLI → UI — no more log-diving to find out why a run died.

**Architecture:** Worker's `_mark_backtest_failed` gains a structured classifier (mirroring `services/live/failure_kind.py::FailureKind.parse_or_unknown`). 4 new persisted columns on `backtests` (`error_code`, `error_public_message`, `error_suggested_action`, `error_remediation JSONB`). Pydantic `ErrorEnvelope` + `Remediation` models. `BacktestStatusResponse` gains `error: ErrorEnvelope | None`; `BacktestListItem` gains compact `error_code` + `error_public_message`. UI mounts `<TooltipProvider>` in root layout; list-view badge wrapped in `Tooltip`; `<FailureCard>` renders full envelope on `/backtests/[id]`. CLI needs zero changes — prints API JSON verbatim.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, Pydantic v2, Postgres 16 (JSONB), Next.js 15 + React + shadcn/ui (Tooltip), Tailwind v4.

**Reference documents:**

- PRD: `docs/prds/backtest-failure-surfacing.md`
- Discussion: `docs/prds/backtest-failure-surfacing-discussion.md`
- Research brief: `docs/research/2026-04-20-backtest-failure-surfacing.md`
- Approach comparison + Contrarian VALIDATE: `CONTINUITY.md` → `## Approach Comparison`
- Precedent (classifier pattern): `backend/src/msai/services/live/failure_kind.py`

**Task ordering rationale:** Backend foundation first (types → model → migration → classifier → worker wiring → API read paths), then frontend consumes the API. E2E is designed at task 3.2b (below) and executed at task QA-1.

## Plan Review History

**Iteration 1 — 2026-04-20:** Codex (gpt-5.4, xhigh) + Claude in parallel. **4 P1 + 2 P2 + 1 P3.** All applied in-place below; no separate revision file — search for `[iter-1]` markers for the spots that changed:

- **P1-a Backfill sanitization hole** → B4 no longer backfills `error_public_message` in SQL; `_build_error_envelope` (B8) sanitizes-on-read when the column is NULL AND `error_message` is populated.
- **P1-b History endpoint drift** → B8 now explicitly rewrites the `BacktestListItem(...)` constructor call-site at `api/backtests.py:192` with conditional `error_code = row.error_code if row.status == 'failed' else None` so non-failed rows return `null` (PRD §4 contract).
- **P1-c Classifier misaligned with real failure surface** → B6 dropped `MISSING_STRATEGY_DATA_FOR_PERIOD` (empty bars is a 0-trade success, not a failure). Classifier now regex-matches the wrapped `RuntimeError(traceback_text)` from `BacktestRunner.run():239` for `ImportError`/`ModuleNotFoundError`/`SyntaxError` tokens before falling through.
- **P1-d No nav to detail for failed rows** → Added F3.5: extend the existing action-button condition to render for `failed` too.
- **P2-a Fixture gap** → New task B0 sets up `seed_failed_backtest` + `seed_historical_failed_row` + `seed_pending_backtest` fixtures against the existing `mock_db` pattern. B8's test block now uses them.
- **P2-b B7 variable scope** → Rewrite uses `symbols` (raw user input, preserved across exception) and `backtest_row["start_date"]` / `backtest_row["end_date"]` which are bound before the try block. `instrument_ids` is initialized as `[]` at the top of the function so the except branch doesn't hit an UnboundLocalError.
- **P3 Tuple brittleness** → B6 classifier now returns a small `@dataclass(frozen=True)` called `FailureClassification` instead of a 4-tuple.

**Iteration 2 — 2026-04-20:** Codex gpt-5.4 @ xhigh. 2 P2s, both applied:

- **P2-a NameError exclusion regression**: iter-1's refinement (remove NameError from classifier) was wrong — NameError at strategy load or first-bar is ~always a user-strategy code defect and deserves `strategy_import_error`, not `engine_crash`. Reverted — NameError is back in both `is_direct` isinstance check and `_IMPORT_ERROR_TOKENS` regex.
- **P2-b B7 test coverage gap**: original B7 only tested FileNotFoundError wiring. Added 3 more wiring cases covering wrapped-RuntimeError-import, wrapped-RuntimeError-engine-crash, and timeout branches so persistence of all classifier codes is verified at the worker boundary.

**Iteration 3 — 2026-04-20:** Codex gpt-5.4 @ xhigh. 1 P2, applied:

- **P2 enum/test coverage mismatch**: iter-2 note claimed "every classifier code" but `config_rejected_at_worker` had no emission path and B7 didn't test `unknown`. Dropped `config_rejected_at_worker` from the enum (YAGNI — PR #38 already validates config at API submission; worker-side re-rejection has no emitter). Added `test_unknown_fallback_persists_as_unknown` to B7 so every remaining enum member (`missing_data`, `strategy_import_error`, `engine_crash`, `timeout`, `unknown`) has a persistence test.

**Iteration 4 — 2026-04-20:** Codex gpt-5.4 @ xhigh. 1 P2, applied:

- **P2 stale B8 fixture block**: my iter-1 P2-a fix added Task B0 with the canonical fixtures but I forgot to delete an alternate-recipe fixture block (using `async_session`) that was still living inside Task B8. Removed entirely — B8 now points at B0's fixtures. Also swept all `async_client` references in B8's test code → the existing `client` fixture (which is what B0's get_db-override path requires).

**Iteration 5 — 2026-04-20:** Codex gpt-5.4 @ xhigh. 2 P2s, both applied:

- **P2 duplicate `client` fixture**: B0 re-declared `client` even though `backend/tests/conftest.py:33` already provides it. Dropped the local fixture; added a note that the shared client picks up B0's `get_db` override automatically.
- **P2 historical-row wording drift**: B0 + B8 test docstring + UC-BFS-005 all had slightly different claims about `error_code` NULL vs `"unknown"` and whether `error_public_message` was backfilled. Normalized: post-migration historical rows read as `error_code="unknown"` (server_default), `error_public_message=NULL`, raw `error_message` populated; `_build_error_envelope` sanitizes-on-read.

**Iteration 6 — 2026-04-20:** Codex gpt-5.4 @ xhigh. 1 P2 applied — UNKNOWN enum docstring fixed to reflect the migration's DDL server_default (was stale "NULL error_code").

**Iteration 7 — 2026-04-20:** Codex gpt-5.4 @ xhigh. 1 higher-severity issue applied: three TSX snippets had auto-formatter-inserted trailing `;` inside bare `{...}` blocks, making them invalid if copied literally. Wrapped each in a plausible parent (`<TableCell>` / `<div>`) so the JSX expression block is unambiguous and prettier leaves them alone.

**Iteration 8 — 2026-04-20:** Codex gpt-5.4. 1 P2 applied — two TSX snippets still had stray trailing `;` after outer JSX. Rewrote all three snippets as `const renderXxx = (…) => (…)` named expressions so prettier's `;` is now syntactically correct TypeScript.

**Iteration 9 — 2026-04-20:** Codex final sweep. Note (not P0/P1/P2 — wording): the `const renderStatusCell / renderActionCell / renderDetail` wrappers in F3/F3.5/F4 are presentation-only — they keep the snippet valid TSX in this markdown plan, but the real implementer should **inline** the returned JSX directly where the existing badge/button/detail renders. Do NOT create standalone `renderXxx` helpers in the code unless it actually cleans up the calling site. **PLAN APPROVED — PROCEED TO PHASE 4.**

---

## Tasks

### Task B0: Shared fixtures for failed-backtest tests

[iter-1 P2-a] The API tests in Task B8 need fixtures that seed a specific `Backtest` row and have the mock session return it from `session.get(Backtest, id)` + `session.execute(select(Backtest))`. The repo's existing pattern uses `mock_db` (empty-by-default `AsyncMock`) + `client_with_mock_db`. We add three helpers on top of that pattern — no new dependency on real Postgres, no integration test split.

**Files:**

- Modify: `backend/tests/unit/conftest.py` — add `seed_failed_backtest`, `seed_historical_failed_row`, `seed_pending_backtest`.

**Step 1: Write failing smoke test**

```python
# backend/tests/unit/test_backtest_fixtures.py  (new, tiny, deletable)
"""Smoke test for the failed-backtest fixtures added in B0."""
from __future__ import annotations


async def test_seed_failed_backtest_fixture_returns_row(seed_failed_backtest):
    bt_id, raw_msg = seed_failed_backtest
    assert bt_id
    assert raw_msg


async def test_seed_historical_failed_row_fixture_returns_id(seed_historical_failed_row):
    assert seed_historical_failed_row


async def test_seed_pending_backtest_fixture_returns_id(seed_pending_backtest):
    assert seed_pending_backtest
```

**Step 2: Run — expect 3 fixture-not-found errors.**

**Step 3: Add fixtures to `backend/tests/unit/conftest.py`**

```python
# Append to backend/tests/unit/conftest.py
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.database import get_db
from msai.main import app
from msai.models.backtest import Backtest


def _make_backtest(**overrides) -> Backtest:
    """Factory — sensible defaults for a minimal Backtest row."""
    base: dict = dict(
        id=uuid4(),
        strategy_id=uuid4(),
        strategy_code_hash="x" * 64,
        config={},
        instruments=["ES.n.0"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 15),
        status="pending",
        progress=0,
    )
    base.update(overrides)
    return Backtest(**base)


def _mock_session_returning(row: Backtest) -> AsyncMock:
    """Build an AsyncMock session whose `get()` and `execute()` return `row`."""
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = row

    # For both single-row (scalar_one_or_none) and list (scalars().all()) patterns.
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [row]
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one_or_none.return_value = row
    mock_result.scalar_one.return_value = 1  # func.count() = 1 row
    session.execute.return_value = mock_result
    return session


@pytest.fixture
async def seed_failed_backtest() -> AsyncGenerator[tuple[str, str], None]:
    """Seed a failed backtest with a fully populated error envelope.

    Yields (backtest_id_str, raw_error_message). The `client_with_mock_db`
    dep-override inside the fixture wires a session whose `get` + `execute`
    return this row.
    """
    raw_msg = (
        "No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES."
    )
    row = _make_backtest(
        status="failed",
        error_message=raw_msg,
        error_code="missing_data",
        error_public_message="<DATA_ROOT>/parquet/stocks/ES is empty",
        error_suggested_action=(
            "Run: msai ingest stocks ES 2025-01-02 2025-01-15"
        ),
        error_remediation={
            "kind": "ingest_data",
            "symbols": ["ES.n.0"],
            "asset_class": "stocks",
            "start_date": "2025-01-02",
            "end_date": "2025-01-15",
            "auto_available": False,
        },
        completed_at=datetime.now(UTC),
    )
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield str(row.id), raw_msg
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def seed_historical_failed_row() -> AsyncGenerator[str, None]:
    """Pre-migration shape — error_code='unknown' default, _public_message NULL."""
    row = _make_backtest(
        status="failed",
        error_message="some historical error text /app/data/parquet/missing",
        error_code="unknown",  # server_default after migration
        error_public_message=None,
        error_suggested_action=None,
        error_remediation=None,
        completed_at=datetime.now(UTC),
    )
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield str(row.id)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def seed_pending_backtest() -> AsyncGenerator[str, None]:
    """Pending (not-yet-run) backtest — error fields should be null in response."""
    row = _make_backtest(status="pending", error_code="unknown")  # default
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield str(row.id)
    finally:
        app.dependency_overrides.pop(get_db, None)


# [iter-5] NO local `client` fixture — backend/tests/conftest.py:33 already
# provides one. Our seed_* fixtures install their own get_db override via
# app.dependency_overrides, which the shared client picks up.
```

**Step 4: Run — expect 3 passed.**

```bash
cd backend && uv run python -m pytest tests/unit/test_backtest_fixtures.py -v
```

**Step 5: Commit**

```bash
git add backend/tests/unit/conftest.py backend/tests/unit/test_backtest_fixtures.py
git commit -m "test(backtests): add seed fixtures for failure-surfacing tests"
```

---

### Task B1: `FailureCode` StrEnum (mirror `live/failure_kind.py` pattern)

**Files:**

- Create: `backend/src/msai/services/backtests/__init__.py` (empty)
- Create: `backend/src/msai/services/backtests/failure_code.py`
- Test: `backend/tests/unit/test_backtest_failure_code.py`

**Step 1: Write failing test**

```python
# backend/tests/unit/test_backtest_failure_code.py
"""Tests for the backtest failure code enum."""

from __future__ import annotations

from msai.services.backtests.failure_code import FailureCode


def test_failure_code_members_match_prd():
    # PRD 4.1 lists exactly these codes; any divergence should be
    # caught here so the docs + code stay in lockstep.
    # [iter-1] Dropped MISSING_STRATEGY_DATA_FOR_PERIOD — empty bars is a
    # 0-trade success in BacktestRunner, not a failure. Codex P1-c.
    assert {c.value for c in FailureCode} == {
        "missing_data",
        "strategy_import_error",
        "engine_crash",
        "timeout",
        "unknown",
    }


def test_parse_or_unknown_accepts_none():
    assert FailureCode.parse_or_unknown(None) is FailureCode.UNKNOWN


def test_parse_or_unknown_accepts_unknown_string():
    # Historical rows may carry values not in the current enum —
    # those must degrade to UNKNOWN, not raise.
    assert FailureCode.parse_or_unknown("historical_legacy_code") is FailureCode.UNKNOWN


def test_parse_or_unknown_accepts_real_value():
    assert FailureCode.parse_or_unknown("missing_data") is FailureCode.MISSING_DATA
```

**Step 2: Run test — expect ImportError**

```bash
cd backend && uv run python -m pytest tests/unit/test_backtest_failure_code.py -v
```

Expected: `ImportError: No module named 'msai.services.backtests.failure_code'`.

**Step 3: Minimal implementation**

```python
# backend/src/msai/services/backtests/failure_code.py
"""Structured failure classification for the backtest worker.

Mirrors the pattern established in
``backend/src/msai/services/live/failure_kind.py``: classify at
write time, persist a stable enum on the row, read through
:meth:`parse_or_unknown` to handle NULL + unrecognized historical
values safely.
"""

from __future__ import annotations

from enum import StrEnum


class FailureCode(StrEnum):
    """Why a backtest row reached terminal ``status == 'failed'``."""

    MISSING_DATA = "missing_data"
    """No raw Parquet files found for one or more requested symbols."""

    STRATEGY_IMPORT_ERROR = "strategy_import_error"
    """The strategy's Python file failed to import (syntax / ImportError)."""

    ENGINE_CRASH = "engine_crash"
    """NautilusTrader subprocess raised during ``node.run()`` (not in
    startup, not in data-load). Usually a bug in user strategy code."""

    TIMEOUT = "timeout"
    """arq job timeout (wall-clock) fired. The inner work may have been
    proceeding fine — we just exceeded the per-job ceiling."""

    # [iter-3 P2] CONFIG_REJECTED_AT_WORKER removed (YAGNI) — PR #38
    # already validates config at API submission. Worker-side re-rejection
    # has no emission path today. Re-add if a second validation layer
    # gets added in the worker later.

    UNKNOWN = "unknown"
    """Fallback for historical rows (which carry ``error_code="unknown"``
    via the migration's DDL ``server_default``) and for failures the
    classifier couldn't match. Writers should use a specific code;
    an UNKNOWN write is a classifier bug to fix, not an OK state."""

    @classmethod
    def parse_or_unknown(cls, value: str | None) -> FailureCode:
        """Null-safe read path for pre-migration rows."""
        if value is None:
            return cls.UNKNOWN
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN
```

Create `backend/src/msai/services/backtests/__init__.py` as an empty file.

**Step 4: Run — expect PASS**

```bash
cd backend && uv run python -m pytest tests/unit/test_backtest_failure_code.py -v
```

Expected: 4 passed. (Enum-members test now checks 6 values, not 7 — iter-1 P1-c dropped `missing_strategy_data_for_period`.)

**Step 5: Commit**

```bash
git add backend/src/msai/services/backtests/__init__.py backend/src/msai/services/backtests/failure_code.py backend/tests/unit/test_backtest_failure_code.py
git commit -m "feat(backtests): add FailureCode enum with parse_or_unknown"
```

---

### Task B2: Sanitization helper

**Files:**

- Create: `backend/src/msai/services/backtests/sanitize.py`
- Test: `backend/tests/unit/test_backtest_sanitize.py`

**Step 1: Write failing test**

```python
# backend/tests/unit/test_backtest_sanitize.py
"""Tests for the backtest failure-message sanitizer."""

from __future__ import annotations

from msai.services.backtests.sanitize import sanitize_public_message


class TestSanitizePublicMessage:
    def test_strips_container_data_root(self):
        raw = "No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES."
        assert "/app/data" not in sanitize_public_message(raw)
        assert "<DATA_ROOT>/parquet/stocks/ES" in sanitize_public_message(raw)

    def test_strips_home_paths(self):
        raw = "File not found: /Users/pablo/.secrets/token"
        assert "/Users/pablo" not in sanitize_public_message(raw)
        assert "<HOME>" in sanitize_public_message(raw)

    def test_strips_stack_trace_file_lines(self):
        raw = 'Traceback (most recent call last):\n  File "/app/src/msai/foo.py", line 42, in bar\n    raise ValueError("boom")\nValueError: boom'
        out = sanitize_public_message(raw)
        # Keep the final exception line; drop the trace bookkeeping.
        assert "ValueError: boom" in out
        assert 'File "/app/src/msai/foo.py", line 42' not in out

    def test_redacts_jwt_shaped_tokens(self):
        raw = "Bad Authorization header: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.abc123def"
        out = sanitize_public_message(raw)
        assert "eyJhbGc" not in out
        assert "<redacted>" in out

    def test_preserves_short_human_messages(self):
        raw = "No bars found for ES.CME in range 2025-01-02..2025-01-15"
        assert sanitize_public_message(raw) == raw  # Nothing to strip.

    def test_truncates_to_1kb(self):
        raw = "x" * 5000
        out = sanitize_public_message(raw)
        assert len(out) <= 1024

    def test_handles_none(self):
        assert sanitize_public_message(None) is None

    def test_handles_empty_string(self):
        assert sanitize_public_message("") == ""
```

**Step 2: Run — expect ImportError.**

**Step 3: Implementation**

```python
# backend/src/msai/services/backtests/sanitize.py
"""Sanitize raw worker exception messages before surfacing to clients.

Strips:
- Absolute container paths ``/app/...`` → ``<DATA_ROOT>/...`` or ``<APP>/...``
- Absolute home paths ``/Users/...`` / ``/home/...`` → ``<HOME>``
- Stack-trace ``File "...", line N`` bookkeeping (keeps the final exception)
- JWT-shaped triples + common secret patterns → ``<redacted>``

Truncates to 1 KB. Does NOT try to defeat a determined adversary —
this is single-user box hygiene, not multi-tenant security.
"""

from __future__ import annotations

import re

_MAX_LEN = 1024

# Order matters — more-specific patterns first.
_DATA_ROOT = re.compile(r"/app/data(?=/|\b)")
_APP_ROOT = re.compile(r"/app(?=/|\b)")
_HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s:]+")
_TRACEBACK_FILE_LINE = re.compile(r'\s*File "[^"]+", line \d+, in [^\n]+\n[^\n]*\n?')
_TRACEBACK_HEADER = re.compile(r"^Traceback \(most recent call last\):\s*\n", re.MULTILINE)
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}")
_SECRET_KV = re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[=:]\s*['\"]?[A-Za-z0-9._\-]{8,}['\"]?")


def sanitize_public_message(raw: str | None) -> str | None:
    """Public-safe version of a raw worker exception message.

    ``None`` passes through unchanged. Empty string → empty string.
    """
    if raw is None:
        return None
    s = raw

    s = _TRACEBACK_FILE_LINE.sub("", s)
    s = _TRACEBACK_HEADER.sub("", s)
    s = _DATA_ROOT.sub("<DATA_ROOT>", s)
    s = _APP_ROOT.sub("<APP>", s)
    s = _HOME_PATH.sub("<HOME>", s)
    s = _JWT.sub("<redacted>", s)
    s = _BEARER.sub("<redacted>", s)
    s = _SECRET_KV.sub(r"\1=<redacted>", s)
    s = s.strip()

    if len(s) > _MAX_LEN:
        s = s[: _MAX_LEN - 3] + "..."
    return s
```

**Step 4: Run — expect 8 passed.**

**Step 5: Commit**

```bash
git add backend/src/msai/services/backtests/sanitize.py backend/tests/unit/test_backtest_sanitize.py
git commit -m "feat(backtests): add sanitize_public_message for worker error text"
```

---

### Task B3: Pydantic `ErrorEnvelope` + `Remediation` schemas

**Files:**

- Modify: `backend/src/msai/schemas/backtest.py`
- Test: `backend/tests/unit/test_backtest_schemas.py` (new file — existing tests live in `test_backtests_api.py`)

**Step 1: Write failing test**

```python
# backend/tests/unit/test_backtest_schemas.py
"""Tests for the backtest error-envelope + remediation Pydantic models."""

from __future__ import annotations

from datetime import date

import pytest

from msai.schemas.backtest import ErrorEnvelope, Remediation


class TestRemediation:
    def test_ingest_data_kind_happy_path(self):
        r = Remediation(
            kind="ingest_data",
            symbols=["ES.n.0"],
            asset_class="futures",
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 15),
        )
        assert r.auto_available is False  # MVP default

    def test_kind_is_literal_union(self):
        # Unknown kinds are rejected — forward-compat via Literal expansion, not duck typing.
        with pytest.raises(ValueError):
            Remediation(kind="nuke_the_server")  # type: ignore[arg-type]

    def test_none_kind_is_valid_placeholder(self):
        r = Remediation(kind="none")
        assert r.symbols is None
        assert r.auto_available is False


class TestErrorEnvelope:
    def test_minimal_envelope(self):
        e = ErrorEnvelope(code="unknown", message="something broke")
        assert e.suggested_action is None
        assert e.remediation is None

    def test_full_envelope_round_trips_through_json(self):
        e = ErrorEnvelope(
            code="missing_data",
            message="<DATA_ROOT>/parquet/stocks/ES is empty",
            suggested_action="Run: msai ingest stocks ES 2025-01-02 2025-01-15",
            remediation=Remediation(
                kind="ingest_data",
                symbols=["ES"],
                asset_class="stocks",
                start_date=date(2025, 1, 2),
                end_date=date(2025, 1, 15),
            ),
        )
        dumped = e.model_dump(mode="json")
        assert dumped["remediation"]["start_date"] == "2025-01-02"
        assert dumped["remediation"]["auto_available"] is False
        reparsed = ErrorEnvelope.model_validate(dumped)
        assert reparsed == e
```

**Step 2: Run — expect ImportError from `ErrorEnvelope` / `Remediation`.**

**Step 3: Implementation — append to `backend/src/msai/schemas/backtest.py`**

```python
# Add these imports at the top if not already present:
from typing import Literal

# --- Append after the existing Response models ---

class Remediation(BaseModel):
    """Machine-readable remediation metadata.

    MVP-only ``kind == 'ingest_data'`` carries full fields. Other kinds
    stay minimal in this PR; the follow-up auto-ingest PR flips
    ``auto_available`` to ``True`` for the kinds it can handle.

    Keep ``kind`` as ``Literal[...]`` so OpenAPI emits a proper
    ``enum`` and client-side type-narrowing works without loading a
    separate enum module.
    """

    kind: Literal["ingest_data", "contact_support", "retry", "none"]
    symbols: list[str] | None = None
    asset_class: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    auto_available: bool = False


class ErrorEnvelope(BaseModel):
    """Structured failure payload surfaced on `BacktestStatusResponse.error`.

    Deliberately symmetric with the api-design.md error envelope used by
    the 422 path on ``POST /backtests/run`` (see PR #38): same
    ``{code, message, ...}`` top-level shape so UI / CLI can share
    rendering helpers.
    """

    code: str
    message: str
    suggested_action: str | None = None
    remediation: Remediation | None = None
```

**Step 4: Run — expect 5 passed.**

**Step 5: Commit**

```bash
git add backend/src/msai/schemas/backtest.py backend/tests/unit/test_backtest_schemas.py
git commit -m "feat(backtests): add ErrorEnvelope + Remediation schemas"
```

---

### Task B4: Alembic migration — add 4 new columns

**Files:**

- Create: `backend/alembic/versions/x2r3s4t5u6v7_add_backtest_error_classification.py`

**Step 1: Read head + branch-point**

```bash
cd backend && uv run alembic current
# Expected: v0q1r2s3t4u5 (head on main before this branch)
```

**Step 2: Create revision skeleton**

```bash
cd backend && uv run alembic revision -m "add backtest error classification columns"
```

Rename the generated file to `x2r3s4t5u6v7_add_backtest_error_classification.py` and set:

```python
revision: str = "x2r3s4t5u6v7"
down_revision: str = "v0q1r2s3t4u5"
```

> **Why this revision id:** deliberately skipping `w1r2s3t4u5v6` because PR #38 (strategy-config-schema-extraction) — not yet merged to main — owns that id. Going forward 2 letters avoids a collision on merge.

**Step 3: Write migration body**

```python
"""add backtest error classification columns

Revision ID: x2r3s4t5u6v7
Revises: v0q1r2s3t4u5
Create Date: 2026-04-20 22:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "x2r3s4t5u6v7"
down_revision: str = "v0q1r2s3t4u5"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # Postgres 16 add-column-with-default is a catalog-only op (attmissingval fast path)
    # so a NOT NULL + DEFAULT add is safe even on a populated table. Research brief §4.
    op.add_column(
        "backtests",
        sa.Column(
            "error_code",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "backtests",
        sa.Column("error_public_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "backtests",
        sa.Column("error_suggested_action", sa.Text(), nullable=True),
    )
    op.add_column(
        "backtests",
        sa.Column(
            "error_remediation",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # [iter-1 P1-a] NO SQL backfill of error_public_message from error_message.
    # The raw column can carry /app/... paths, JWT fragments, stack traces —
    # putting that into error_public_message without the sanitizer would leak
    # through the API. _build_error_envelope (Task B8) sanitizes-on-read when
    # error_public_message IS NULL but error_message is populated. That's a
    # per-GET regex pass, acceptable for the ~100s of historical failed rows
    # this project will ever have; if it grows, a one-time backfill script
    # that runs the sanitizer can land later.


def downgrade() -> None:
    op.drop_column("backtests", "error_remediation")
    op.drop_column("backtests", "error_suggested_action")
    op.drop_column("backtests", "error_public_message")
    op.drop_column("backtests", "error_code")
```

**Step 4: Apply + verify**

```bash
cd backend && uv run alembic upgrade head
uv run python -c "
import asyncio
from msai.core.database import async_session_factory
from sqlalchemy import text
async def check():
    async with async_session_factory() as s:
        r = await s.execute(text('SELECT column_name FROM information_schema.columns WHERE table_name=:t ORDER BY ordinal_position'), {'t':'backtests'})
        cols = [row[0] for row in r]
        print('cols:', cols)
        assert 'error_code' in cols
        assert 'error_public_message' in cols
        assert 'error_suggested_action' in cols
        assert 'error_remediation' in cols
asyncio.run(check())
"
```

**Step 5: Commit**

```bash
git add backend/alembic/versions/x2r3s4t5u6v7_add_backtest_error_classification.py
git commit -m "feat(db): add error classification columns to backtests"
```

---

### Task B5: Extend `Backtest` SQLAlchemy model

**Files:**

- Modify: `backend/src/msai/models/backtest.py:44` (insert 4 columns next to existing `error_message`)
- Test: `backend/tests/unit/test_backtest_model.py` (new file)

**Step 1: Write failing test**

```python
# backend/tests/unit/test_backtest_model.py
"""Tests for the Backtest model's new error classification columns."""

from __future__ import annotations

from msai.models.backtest import Backtest


def test_backtest_has_error_code_column():
    assert hasattr(Backtest, "error_code")
    assert Backtest.__table__.c.error_code.type.length == 32
    assert not Backtest.__table__.c.error_code.nullable
    assert Backtest.__table__.c.error_code.server_default.arg == "unknown"


def test_backtest_has_error_public_message_column():
    assert hasattr(Backtest, "error_public_message")
    assert Backtest.__table__.c.error_public_message.nullable is True


def test_backtest_has_error_suggested_action_column():
    assert hasattr(Backtest, "error_suggested_action")
    assert Backtest.__table__.c.error_suggested_action.nullable is True


def test_backtest_has_error_remediation_column():
    assert hasattr(Backtest, "error_remediation")
    # JSONB subtype
    assert Backtest.__table__.c.error_remediation.nullable is True
    assert "JSONB" in str(Backtest.__table__.c.error_remediation.type)
```

**Step 2: Run — expect AttributeError on `error_code`.**

**Step 3: Insert after line 44 (`error_message`)**

```python
    # --- Error classification (added by PR #<this>) ----------------------
    # Populated by the worker at ``_mark_backtest_failed`` time via
    # ``services/backtests/classifier.py``. Read back by the API's
    # ``_build_error_envelope`` helper, which returns ``None`` for non-failed
    # rows and uses :meth:`FailureCode.parse_or_unknown` + sanitizer for
    # pre-migration rows that carry ``error_code == 'unknown'`` + a raw
    # ``error_message`` but no ``error_public_message``.
    error_code: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unknown"
    )
    error_public_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_suggested_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_remediation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

**Step 4: Run — expect 4 passed.**

**Step 5: Commit**

```bash
git add backend/src/msai/models/backtest.py backend/tests/unit/test_backtest_model.py
git commit -m "feat(backtests): add error classification columns to Backtest model"
```

---

### Task B6: The classifier

**Files:**

- Create: `backend/src/msai/services/backtests/classifier.py`
- Test: `backend/tests/unit/test_backtest_classifier.py`

**Step 1: Write failing test**

```python
# backend/tests/unit/test_backtest_classifier.py
"""Tests for the backtest failure classifier."""

from __future__ import annotations

from datetime import date

from msai.services.backtests.classifier import (
    FailureClassification,
    classify_worker_failure,
)
from msai.services.backtests.failure_code import FailureCode


class TestClassifyWorkerFailure:
    """``classify_worker_failure(exc, instruments, start_date, end_date)``
    returns a ``FailureClassification`` dataclass. [iter-1 P3] Small struct
    beats a 4-tuple for readability + mis-wire safety.
    """

    def test_missing_data_filenotfounderror_is_classified(self):
        exc = FileNotFoundError(
            "No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES. "
            "Run the data ingestion pipeline for this symbol before backtesting."
        )
        result = classify_worker_failure(
            exc,
            instruments=["ES.n.0"],
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 15),
        )
        assert result.code is FailureCode.MISSING_DATA
        assert "<DATA_ROOT>/parquet/stocks/ES" in result.public_message
        assert "/app/" not in result.public_message  # sanitized
        assert result.suggested_action is not None
        assert "msai ingest" in result.suggested_action
        assert "ES" in result.suggested_action
        assert "2025-01-02" in result.suggested_action
        assert "2025-01-15" in result.suggested_action
        assert result.remediation is not None
        assert result.remediation.kind == "ingest_data"
        assert result.remediation.symbols == ["ES.n.0"]
        assert result.remediation.asset_class == "stocks"
        assert result.remediation.auto_available is False

    def test_timeout_error_is_classified(self):
        exc = TimeoutError("Backtest exceeded 900s wall clock")
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date(2025, 1, 1), end_date=date(2025, 1, 2)
        )
        assert result.code is FailureCode.TIMEOUT
        assert result.remediation is None

    def test_import_error_as_direct_exception_type(self):
        # Import errors that reach the worker directly (before
        # BacktestRunner subprocess wraps them).
        exc = ImportError("No module named 'strategies.missing'")
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date.today(), end_date=date.today()
        )
        assert result.code is FailureCode.STRATEGY_IMPORT_ERROR

    def test_import_error_wrapped_by_backtest_runner_subprocess(self):
        # [iter-1 P1-c] BacktestRunner.run wraps the child process's
        # exception as ``RuntimeError(traceback_text)`` at line 239 of
        # backtest_runner.py. The classifier MUST peek into the message
        # text to recover STRATEGY_IMPORT_ERROR vs ENGINE_CRASH.
        traceback_text = (
            'Traceback (most recent call last):\n'
            '  File "/app/src/msai/services/nautilus/backtest_runner.py", line 290, in _run_backtest\n'
            '    strategy_cls = load_strategy_class(strategy_path)\n'
            'ModuleNotFoundError: No module named \'strategies.broken\''
        )
        exc = RuntimeError(traceback_text)
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date.today(), end_date=date.today()
        )
        assert result.code is FailureCode.STRATEGY_IMPORT_ERROR

    def test_syntax_error_wrapped_by_backtest_runner(self):
        traceback_text = (
            'Traceback (most recent call last):\n'
            '  File "strategies/foo.py", line 12\n'
            '    def bad(:\n'
            '           ^\n'
            'SyntaxError: invalid syntax'
        )
        exc = RuntimeError(traceback_text)
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date.today(), end_date=date.today()
        )
        assert result.code is FailureCode.STRATEGY_IMPORT_ERROR

    def test_generic_runtime_error_is_engine_crash(self):
        # [iter-1 P1-c] Wrapped generic runtime error → engine_crash, not unknown.
        # Only fall through to UNKNOWN when we can't match any pattern.
        traceback_text = (
            'Traceback (most recent call last):\n'
            '  File "/app/.venv/lib/python3.12/site-packages/nautilus_trader/...", line 500, in _run\n'
            '    raise ValueError("bar_type mismatch")\n'
            'ValueError: bar_type mismatch'
        )
        exc = RuntimeError(traceback_text)
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date.today(), end_date=date.today()
        )
        assert result.code is FailureCode.ENGINE_CRASH

    def test_truly_unknown_falls_back(self):
        exc = KeyboardInterrupt()
        result = classify_worker_failure(
            exc, instruments=["AAPL"], start_date=date.today(), end_date=date.today()
        )
        assert result.code is FailureCode.UNKNOWN
        # Never blank — US-006 requires a useful message even for unknown.
        assert result.public_message
        assert result.suggested_action is None
        assert result.remediation is None

    def test_public_message_never_empty(self):
        exc = Exception("")  # pathological — empty message
        result = classify_worker_failure(
            exc, instruments=[], start_date=date.today(), end_date=date.today()
        )
        assert result.public_message  # falls back to a generic string
```

**Step 2: Run — expect ImportError.**

**Step 3: Implementation**

```python
# backend/src/msai/services/backtests/classifier.py
"""Classify worker exceptions into a structured failure record.

Called by ``workers/backtest_job.py::_mark_backtest_failed`` at the
moment of failure, once per run. See PRD
``docs/prds/backtest-failure-surfacing.md`` for the contract.

[iter-1 P1-c] ``BacktestRunner.run()`` at
``backend/src/msai/services/nautilus/backtest_runner.py:239`` wraps any
child-process exception as ``RuntimeError(str(traceback))``. That means
``ImportError`` / ``SyntaxError`` / ``ValueError`` / etc. do NOT reach
this classifier as their real types when they fire inside the backtest
subprocess — they arrive as ``RuntimeError`` whose ``str()`` is the
full formatted traceback. We therefore peek at the message text to
recover STRATEGY_IMPORT_ERROR vs ENGINE_CRASH. ``FileNotFoundError``
and ``TimeoutError`` DO reach the classifier directly because they
fire in the worker's outer code path (``ensure_catalog_data`` and
``asyncio.to_thread(..., timeout)`` respectively).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from msai.schemas.backtest import Remediation
from msai.services.backtests.failure_code import FailureCode
from msai.services.backtests.sanitize import sanitize_public_message


@dataclass(frozen=True, slots=True)
class FailureClassification:
    """Structured classifier output.

    [iter-1 P3] Small dataclass beats a 4-tuple: named access,
    type-safe for callers, trivial to extend (e.g. an ``alert_level``
    field later) without breaking call-sites.
    """

    code: FailureCode
    public_message: str
    suggested_action: str | None
    remediation: Remediation | None


# Worker message shape for the common missing-data path:
#   "No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES. Run..."
_MISSING_DATA_RE = re.compile(
    r"No raw Parquet files found for '([^']+)' under /app/data/parquet/([^/]+)/"
)

# [iter-1 P1-c] Patterns that indicate a user-strategy load/parse error
# even when wrapped by BacktestRunner's RuntimeError(traceback) layer.
# [iter-2 P2] NameError is KEPT — the vast majority of real-world
# NameError traces are either module-level evaluation failures or
# first-bar references to an unresolved helper, both of which are
# strategy code defects. Engine_crash is reserved for Nautilus / engine
# internals failures, not user-code bugs.
_IMPORT_ERROR_TOKENS = re.compile(
    r"\b(ImportError|ModuleNotFoundError|SyntaxError|NameError)\b",
)


def classify_worker_failure(
    exc: BaseException,
    *,
    instruments: list[str],
    start_date: date,
    end_date: date,
) -> FailureClassification:
    """Classify + describe a worker-side backtest failure.

    ``public_message`` is always non-empty — US-006 guarantees.
    ``suggested_action`` + ``remediation`` are populated only for
    MISSING_DATA in this PR.
    """
    raw_message = str(exc) or exc.__class__.__name__

    # --- Missing data (outer worker path — FileNotFoundError raised by
    #     ensure_catalog_data before the BacktestRunner subprocess spawns).
    m = _MISSING_DATA_RE.search(raw_message)
    if isinstance(exc, FileNotFoundError) or m is not None:
        public_msg = sanitize_public_message(raw_message) or "Backtest data missing"
        asset_class = m.group(2) if m else None
        symbols_for_cmd = list(instruments) if instruments else (
            [m.group(1)] if m else []
        )
        if symbols_for_cmd and asset_class:
            action = (
                f"Run: msai ingest {asset_class} "
                f"{','.join(symbols_for_cmd)} "
                f"{start_date.isoformat()} {end_date.isoformat()}"
            )
        else:
            action = (
                "Run the data ingestion pipeline for the missing symbol(s) "
                "before re-running this backtest."
            )
        remediation = Remediation(
            kind="ingest_data",
            symbols=list(instruments) if instruments else ([m.group(1)] if m else []),
            asset_class=asset_class,
            start_date=start_date,
            end_date=end_date,
            auto_available=False,  # MVP — follow-up PR flips this
        )
        return FailureClassification(
            code=FailureCode.MISSING_DATA,
            public_message=public_msg,
            suggested_action=action,
            remediation=remediation,
        )

    # --- Timeout (outer wrapper from asyncio.to_thread(..., timeout))
    if isinstance(exc, TimeoutError):
        public_msg = (
            sanitize_public_message(raw_message) or "Backtest wall-clock timeout"
        )
        return FailureClassification(
            code=FailureCode.TIMEOUT,
            public_message=public_msg,
            suggested_action=None,
            remediation=None,
        )

    # --- Strategy import error (direct OR wrapped)
    #
    # BacktestRunner wraps subprocess exceptions as RuntimeError(str(tb)).
    # We recognize import/syntax failures in the text.
    is_direct = isinstance(
        exc, (ImportError, SyntaxError, ModuleNotFoundError, NameError)
    )
    is_wrapped_import = isinstance(exc, RuntimeError) and bool(
        _IMPORT_ERROR_TOKENS.search(raw_message)
    )
    if is_direct or is_wrapped_import:
        public_msg = (
            sanitize_public_message(raw_message)
            or "Strategy module failed to import"
        )
        return FailureClassification(
            code=FailureCode.STRATEGY_IMPORT_ERROR,
            public_message=public_msg,
            suggested_action=None,
            remediation=None,
        )

    # --- Engine crash (any RuntimeError we DIDN'T match as an import error)
    #
    # This is the BacktestRunner's "child process raised" wrapper —
    # at this point the message is a formatted traceback and the most
    # useful thing we can do is sanitize + surface it.
    if isinstance(exc, RuntimeError):
        public_msg = (
            sanitize_public_message(raw_message)
            or "Backtest engine crashed; see server logs for details"
        )
        return FailureClassification(
            code=FailureCode.ENGINE_CRASH,
            public_message=public_msg,
            suggested_action=None,
            remediation=None,
        )

    # --- Unknown (truly unmatched — KeyboardInterrupt, asyncio.CancelledError, etc.)
    public_msg = sanitize_public_message(raw_message) or (
        f"Backtest failed with {exc.__class__.__name__} "
        "(see server logs for details)"
    )
    return FailureClassification(
        code=FailureCode.UNKNOWN,
        public_message=public_msg,
        suggested_action=None,
        remediation=None,
    )
```

**Step 4: Run — expect 8 passed.** (iter-1 P1-c added 2 tests for the `RuntimeError` wrapping path.)

**Step 5: Commit**

```bash
git add backend/src/msai/services/backtests/classifier.py backend/tests/unit/test_backtest_classifier.py
git commit -m "feat(backtests): add classify_worker_failure"
```

---

### Task B7: Wire classifier into `_mark_backtest_failed`

**Files:**

- Modify: `backend/src/msai/workers/backtest_job.py` — 3 call-sites + function signature
- Test: `backend/tests/unit/test_backtest_mark_failed.py` (new file — there's no existing test for this helper)

**Step 1: Write failing test**

```python
# backend/tests/unit/test_backtest_mark_failed.py
"""Tests for _mark_backtest_failed classifier wiring."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from msai.models.backtest import Backtest
from msai.workers.backtest_job import _mark_backtest_failed


@pytest.fixture()
def fake_row():
    return Backtest(
        id=uuid4(),
        strategy_id=uuid4(),
        strategy_code_hash="x" * 64,
        config={},
        instruments=["ES.n.0"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 15),
        status="running",
    )


class TestMarkBacktestFailed:
    """[iter-2 P2] Wiring tests cover each classifier branch so the
    persisted envelope contract is verified end-to-end at the worker
    boundary — not just in the classifier's unit tests.
    """

    async def _invoke_mark_failed(self, row, exc):
        """Helper: patches session factory to return our fake row + runs."""
        session = AsyncMock()
        session.get.return_value = row
        with patch(
            "msai.workers.backtest_job.async_session_factory"
        ) as factory:
            factory.return_value.__aenter__.return_value = session
            await _mark_backtest_failed(
                backtest_id=str(row.id),
                exc=exc,
                instruments=list(row.instruments),
                start_date=row.start_date,
                end_date=row.end_date,
            )

    async def test_missing_data_writes_full_envelope(self, fake_row):
        exc = FileNotFoundError(
            "No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES."
        )
        await self._invoke_mark_failed(fake_row, exc)

        assert fake_row.status == "failed"
        assert fake_row.error_code == "missing_data"
        assert fake_row.error_message  # raw stays populated
        assert fake_row.error_public_message
        assert "/app/" not in fake_row.error_public_message  # sanitized
        assert fake_row.error_suggested_action
        assert "msai ingest" in fake_row.error_suggested_action
        assert fake_row.error_remediation is not None
        assert fake_row.error_remediation["kind"] == "ingest_data"
        assert fake_row.error_remediation["symbols"] == ["ES.n.0"]
        assert fake_row.completed_at is not None

    async def test_wrapped_import_error_persists_as_strategy_import(self, fake_row):
        # [iter-2 P2] BacktestRunner subprocess wraps as RuntimeError(tb).
        tb = (
            "Traceback (most recent call last):\n"
            '  File "strategies/broken.py", line 1, in <module>\n'
            "ModuleNotFoundError: No module named 'missing_dep'"
        )
        await self._invoke_mark_failed(fake_row, RuntimeError(tb))

        assert fake_row.status == "failed"
        assert fake_row.error_code == "strategy_import_error"
        assert fake_row.error_public_message
        assert fake_row.error_suggested_action is None
        assert fake_row.error_remediation is None

    async def test_wrapped_engine_crash_persists_as_engine_crash(self, fake_row):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "/app/.venv/.../nautilus.py", line 500, in _run\n'
            '    raise ValueError("bar_type mismatch")\n'
            "ValueError: bar_type mismatch"
        )
        await self._invoke_mark_failed(fake_row, RuntimeError(tb))

        assert fake_row.status == "failed"
        assert fake_row.error_code == "engine_crash"
        assert fake_row.error_public_message
        assert "/app/" not in fake_row.error_public_message  # sanitized
        assert fake_row.error_remediation is None

    async def test_timeout_persists_as_timeout(self, fake_row):
        await self._invoke_mark_failed(
            fake_row, TimeoutError("Backtest exceeded 900s wall clock")
        )
        assert fake_row.error_code == "timeout"
        assert fake_row.error_public_message
        assert fake_row.error_remediation is None

    async def test_unknown_fallback_persists_as_unknown(self, fake_row):
        # [iter-3 P2] Verify the UNKNOWN writer path lands in the DB too,
        # so every enum value has at least one wiring-level test.
        await self._invoke_mark_failed(fake_row, KeyboardInterrupt())
        assert fake_row.error_code == "unknown"
        assert fake_row.error_public_message  # never blank (US-006)
        assert fake_row.error_suggested_action is None
        assert fake_row.error_remediation is None
```

**Step 2: Run — expect signature mismatch (old helper takes `error_message: str`).**

**Step 3: Update `_mark_backtest_failed` and its 3 call-sites**

[iter-1 P2-b] Replace lines 204-228 + lines 346-363 of `backend/src/msai/workers/backtest_job.py`. Use variables that are bound BEFORE the try block (`symbols`, `backtest_row["start_date"]`, `backtest_row["end_date"]`) so the except clause never hits UnboundLocalError. `instrument_ids` is ONLY bound inside the try block — don't reference it from the classifier path.

```python
    except Exception as exc:
        # Single exit path — classifier discriminates by exception type + message.
        # Preserve structured log events operators grep on; the classifier's
        # error_code becomes a first-class log field, not a replacement.
        if isinstance(exc, FileNotFoundError):
            log.error("backtest_missing_data", backtest_id=backtest_id, error=str(exc))
        elif isinstance(exc, TimeoutError):
            log.error("backtest_timeout", backtest_id=backtest_id, error=str(exc))
        else:
            log.exception(
                "backtest_job_failed",
                backtest_id=backtest_id,
                error=str(exc),
                exc_type=exc.__class__.__name__,
            )
        await _mark_backtest_failed(
            backtest_id=backtest_id,
            exc=exc,
            # `symbols` is bound on line ~89 from backtest_row["instruments"],
            # i.e. the user-submitted list — exactly what the remediation
            # command needs to echo back. `instrument_ids` (the canonicalized
            # post-ensure_catalog_data list) is intentionally NOT used here
            # because it's unbound when the exception fires inside
            # ensure_catalog_data.
            instruments=list(symbols),
            start_date=backtest_row["start_date"],
            end_date=backtest_row["end_date"],
        )
```

Replace `_mark_backtest_failed`:

```python
async def _mark_backtest_failed(
    *,
    backtest_id: str,
    exc: BaseException,
    instruments: list[str],
    start_date: date,
    end_date: date,
) -> None:
    """Update a backtest row to ``failed`` with structured classification.

    Classifies ``exc`` via :func:`classify_worker_failure` and persists
    the envelope fields alongside the raw ``error_message`` (kept for
    operators who want the full unsanitized exception).

    Swallows all exceptions from the update itself — if we can't even
    reach the database there's nothing more we can do, and we don't want
    to override the original failure with a DB error.
    """
    from msai.services.backtests.classifier import classify_worker_failure

    classification = classify_worker_failure(
        exc,
        instruments=instruments,
        start_date=start_date,
        end_date=end_date,
    )
    remediation_json = (
        classification.remediation.model_dump(mode="json")
        if classification.remediation is not None
        else None
    )

    try:
        async with async_session_factory() as session:
            row = await session.get(Backtest, backtest_id)
            if row is None:
                return
            row.status = "failed"
            row.error_message = str(exc) or exc.__class__.__name__  # raw
            row.error_code = classification.code.value
            row.error_public_message = classification.public_message
            row.error_suggested_action = classification.suggested_action
            row.error_remediation = remediation_json
            row.completed_at = datetime.now(UTC)
            await session.commit()
    except Exception:
        log.exception("backtest_status_update_failed", backtest_id=backtest_id)
```

Update the `import` list at the top of `backtest_job.py` to include `date` if not already present.

**Step 4: Run — expect 1 passed + existing worker tests still pass.**

```bash
cd backend && uv run python -m pytest tests/unit/test_backtest_mark_failed.py tests/integration/test_backtest_worker*.py -v
```

**Step 5: Commit**

```bash
git add backend/src/msai/workers/backtest_job.py backend/tests/unit/test_backtest_mark_failed.py
git commit -m "feat(backtests): wire classifier into worker failure path"
```

---

### Task B8: Extend API response schemas + endpoints

**Files:**

- Modify: `backend/src/msai/schemas/backtest.py` — extend `BacktestStatusResponse`, `BacktestListItem`.
- Modify: `backend/src/msai/api/backtests.py` — 2 endpoints.
- Test: `backend/tests/unit/test_backtests_api.py` — extend existing class.

**Step 1: Write failing tests**

```python
# tests/unit/test_backtests_api.py — add this class
class TestStatusEndpointReturnsErrorEnvelope:
    async def test_failed_row_returns_structured_envelope(self, client, seed_failed_backtest):
        bt_id, _raw_msg = seed_failed_backtest  # fixture seeds a failed row
        response = await client.get(f"/api/v1/backtests/{bt_id}/status")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"] is not None
        assert body["error"]["code"] == "missing_data"
        assert body["error"]["message"]
        assert body["error"]["suggested_action"]
        assert body["error"]["remediation"]["kind"] == "ingest_data"

    async def test_pending_row_has_no_error_field(self, client, seed_pending_backtest):
        bt_id = seed_pending_backtest
        response = await client.get(f"/api/v1/backtests/{bt_id}/status")
        assert response.status_code == 200
        assert response.json().get("error") is None

    async def test_historical_row_degrades_to_unknown(self, client, seed_historical_failed_row):
        """US-006: post-migration, historical ``failed`` rows have
        ``error_code='unknown'`` (server_default), ``error_public_message=NULL``,
        and their raw ``error_message`` populated. The API must surface
        the stored message through ``error.message`` (sanitized-on-read)
        — never a blank envelope."""
        bt_id = seed_historical_failed_row
        response = await client.get(f"/api/v1/backtests/{bt_id}/status")
        body = response.json()
        assert body["error"]["code"] == "unknown"
        assert body["error"]["message"]  # stored raw message surfaces


class TestHistoryEndpointReturnsCompactError:
    async def test_failed_rows_include_error_code_and_message(self, client, seed_failed_backtest):
        response = await client.get("/api/v1/backtests/history")
        assert response.status_code == 200
        items = response.json()["items"]
        # At least one of the items should have the classification fields.
        failed = [i for i in items if i["status"] == "failed"]
        assert failed, "fixture must seed at least one failed row"
        f = failed[0]
        assert "error_code" in f
        assert "error_public_message" in f
        # History is compact — no suggested_action/remediation here.
        assert "suggested_action" not in f
        assert "remediation" not in f
```

[iter-4 P2] Fixtures live in `backend/tests/unit/conftest.py` (Task B0). Use `seed_failed_backtest`, `seed_historical_failed_row`, and `seed_pending_backtest` directly — the alternate `async_session`-based recipe that lived here in an earlier draft was stale and has been removed. Also replace `async_client` references in the test code above with the repo's existing `client` fixture (installed by the seed fixtures themselves via `app.dependency_overrides[get_db]`).

**Step 2: Run — expect AttributeError (`BacktestStatusResponse.error` missing).**

**Step 3: Implementation**

Extend `backend/src/msai/schemas/backtest.py`:

```python
class BacktestStatusResponse(BaseModel):
    """Response schema for backtest status polling."""

    id: UUID
    status: str
    progress: int
    started_at: datetime | None
    completed_at: datetime | None
    error: ErrorEnvelope | None = None

    model_config = {"from_attributes": True}


class BacktestListItem(BaseModel):
    """Summary schema for a backtest in list responses."""

    id: UUID
    strategy_id: UUID
    status: str
    start_date: date
    end_date: date
    created_at: datetime
    error_code: str | None = None
    error_public_message: str | None = None

    model_config = {"from_attributes": True}
```

Add a builder helper at the bottom of `backend/src/msai/api/backtests.py`:

```python
def _build_error_envelope(row: Backtest) -> ErrorEnvelope | None:
    """Return the structured error envelope for a ``failed`` row, or None.

    Non-failed rows (pending/running/completed) always return ``None``.
    Historical rows (pre-migration) with ``error_code == 'unknown'`` still
    surface with their stored ``error_message`` — US-006 null-safe read
    — but sanitized on the fly so raw paths/tokens don't leak.

    [iter-1 P1-a] The migration does NOT backfill ``error_public_message``
    from the raw ``error_message`` column, because that would leak
    unsanitized content. Instead, when the column is NULL here AND the
    raw message is populated (pre-migration row or a classifier bug),
    we sanitize-on-read.
    """
    from msai.services.backtests.failure_code import FailureCode
    from msai.services.backtests.sanitize import sanitize_public_message

    if row.status != "failed":
        return None

    code = FailureCode.parse_or_unknown(row.error_code)
    message = (
        row.error_public_message
        or sanitize_public_message(row.error_message)
        or f"Backtest failed (code={code.value}); see server logs for details"
    )

    remediation = None
    if row.error_remediation is not None:
        remediation = Remediation.model_validate(row.error_remediation)

    return ErrorEnvelope(
        code=code.value,
        message=message,
        suggested_action=row.error_suggested_action,
        remediation=remediation,
    )
```

Update the two endpoints (`POST /run` line 164 + `GET /{id}/status` line 223) to include `error=_build_error_envelope(row)` in the `BacktestStatusResponse(...)` constructor.

[iter-1 P1-b] Update `GET /history` explicitly. The current `list_backtests()` at `backend/src/msai/api/backtests.py:192` manually constructs each `BacktestListItem` — `from_attributes=True` does NOT short-circuit an explicit constructor. Also: the DB column `error_code` is `NOT NULL DEFAULT 'unknown'`, so blind pass-through would show `"unknown"` on non-failed rows and violate the PRD's "null for non-failed" contract.

Rewrite the list-comprehension (find it by grep'ing `BacktestListItem(` in `api/backtests.py`):

```python
items = [
    BacktestListItem(
        id=row.id,
        strategy_id=row.strategy_id,
        status=row.status,
        start_date=row.start_date,
        end_date=row.end_date,
        created_at=row.created_at,
        # [iter-1 P1-b] Only surface error fields for failed rows. Sanitize-on-read
        # when error_public_message is NULL (pre-migration rows) but
        # error_message is populated.
        error_code=row.error_code if row.status == "failed" else None,
        error_public_message=(
            row.error_public_message
            or sanitize_public_message(row.error_message)
            if row.status == "failed"
            else None
        ),
    )
    for row in rows
]
```

Import `sanitize_public_message` at the top of `api/backtests.py`:

```python
from msai.services.backtests.sanitize import sanitize_public_message
```

**Step 4: Run — expect the 3 new test classes pass + existing 12+ tests still pass.**

```bash
cd backend && uv run python -m pytest tests/unit/test_backtests_api.py -v
```

**Step 5: Commit**

```bash
git add backend/src/msai/api/backtests.py backend/src/msai/schemas/backtest.py backend/tests/unit/test_backtests_api.py
git commit -m "feat(backtests): surface structured error envelope on status + history endpoints"
```

---

### Task F1: TypeScript types for `ErrorEnvelope` + `Remediation`

**Files:**

- Modify: `frontend/src/lib/api.ts` — extend `BacktestStatusResponse` + `BacktestHistoryItem`.

**Step 1: Find the existing interfaces**

```bash
grep -n "BacktestStatusResponse\|BacktestHistoryItem" frontend/src/lib/api.ts
```

**Step 2: Add new types + extend existing**

At the appropriate point in `frontend/src/lib/api.ts`:

```typescript
export type RemediationKind =
  | "ingest_data"
  | "contact_support"
  | "retry"
  | "none";

export interface Remediation {
  kind: RemediationKind;
  symbols?: string[] | null;
  asset_class?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  auto_available: boolean;
}

export interface ErrorEnvelope {
  code: string;
  message: string;
  suggested_action?: string | null;
  remediation?: Remediation | null;
}
```

Extend `BacktestStatusResponse`:

```typescript
export interface BacktestStatusResponse {
  id: string;
  status: "pending" | "running" | "failed" | "completed";
  progress: number;
  started_at: string | null;
  completed_at: string | null;
  error?: ErrorEnvelope | null; // NEW
}
```

Extend `BacktestHistoryItem`:

```typescript
export interface BacktestHistoryItem {
  // ...existing fields
  error_code?: string | null; // NEW
  error_public_message?: string | null; // NEW
}
```

**Step 3: Run typecheck**

```bash
cd frontend && pnpm exec tsc --noEmit
```

Expected: clean.

**Step 4: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(frontend): add ErrorEnvelope + Remediation types"
```

---

### Task F2: Mount `<TooltipProvider>` in root layout

**Files:**

- Modify: `frontend/src/app/layout.tsx`

**Step 1: Check current layout**

```bash
grep -n "TooltipProvider\|Providers" frontend/src/app/layout.tsx
```

**Step 2: Wrap app children**

Add at top:

```tsx
import { TooltipProvider } from "@/components/ui/tooltip";
```

Wrap the existing `<Providers>` inner children (or `{children}` directly):

```tsx
<TooltipProvider delayDuration={200}>{children}</TooltipProvider>
```

> **Why delayDuration=200?** The shadcn default `delayDuration=0` makes tooltips pop on mousemove — noisy. 200 ms matches the `shadcn/ui` recommended "intentional hover" threshold.

**Step 3: Build check**

```bash
cd frontend && pnpm exec tsc --noEmit
```

**Step 4: Commit**

```bash
git add frontend/src/app/layout.tsx
git commit -m "feat(frontend): mount TooltipProvider in root layout"
```

---

### Task F3: Tooltip on the `/backtests` history-list status badge

**Files:**

- Modify: `frontend/src/app/backtests/page.tsx` — wrap the status `<Badge>` for `failed` rows.

**Step 1: Find the badge**

Around line 148 of `frontend/src/app/backtests/page.tsx`:

```tsx
<Badge variant="secondary" className={statusColor(bt.status)}>
  {bt.status}
</Badge>
```

**Step 2: Wrap with Tooltip for failed rows only**

```tsx
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

// Inside the status cell renderer (a function returning JSX from the
// existing ``backtests.map`` body). Replace the current bare ``<Badge>``
// return with:
const renderStatusCell = (bt: BacktestHistoryItem) => (
  <TableCell>
    {bt.status === "failed" && bt.error_public_message ? (
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge
            variant="secondary"
            className={statusColor(bt.status)}
            data-testid={`backtest-status-${bt.id}`}
          >
            {bt.status}
          </Badge>
        </TooltipTrigger>
        <TooltipContent
          side="top"
          className="max-w-xs whitespace-pre-wrap text-xs"
          data-testid={`backtest-error-tooltip-${bt.id}`}
        >
          {bt.error_public_message.length > 150
            ? `${bt.error_public_message.slice(0, 150)}…`
            : bt.error_public_message}
        </TooltipContent>
      </Tooltip>
    ) : (
      <Badge variant="secondary" className={statusColor(bt.status)}>
        {bt.status}
      </Badge>
    )}
  </TableCell>
);
```

> **Accessibility note:** `TooltipTrigger asChild` forwards the ref so the badge itself is the trigger. Adding `data-testid` makes Playwright E2E deterministic (research brief §5).

**Step 3: Build + lint**

```bash
cd frontend && pnpm exec tsc --noEmit && pnpm lint
```

**Step 4: Commit**

```bash
git add frontend/src/app/backtests/page.tsx
git commit -m "feat(frontend): failure tooltip on backtests history badge"
```

---

### Task F3.5: Navigate failed rows to detail page

[iter-1 P1-d] The list page's action-button currently renders ONLY for `completed` rows (`backtests/page.tsx:160`). Without this task, the `<FailureCard>` we build in F4 is orphaned — no UI path reaches it.

**Files:**

- Modify: `frontend/src/app/backtests/page.tsx` — extend the condition.

**Step 1: Find the action cell**

Around line 160:

```tsx
const renderActionCell = (bt: BacktestHistoryItem) => (
  <TableCell>
    {bt.status === "completed" && (
      <Button asChild variant="ghost" size="icon-xs">
        <Link href={`/backtests/${bt.id}`}>
          <ExternalLink className="size-3.5" />
        </Link>
      </Button>
    )}
  </TableCell>
);
```

**Step 2: Replace — render for both completed AND failed**

```tsx
const renderActionCell = (bt: BacktestHistoryItem) => (
  <TableCell>
    {(bt.status === "completed" || bt.status === "failed") && (
      <Button
        asChild
        variant="ghost"
        size="icon-xs"
        aria-label={
          bt.status === "failed"
            ? "View failure details"
            : "View backtest results"
        }
      >
        <Link
          href={`/backtests/${bt.id}`}
          data-testid={`backtest-detail-link-${bt.id}`}
        >
          <ExternalLink className="size-3.5" />
        </Link>
      </Button>
    )}
  </TableCell>
);
```

**Step 3: Build + lint**

```bash
cd frontend && pnpm exec tsc --noEmit && pnpm lint
```

**Step 4: Commit**

```bash
git add frontend/src/app/backtests/page.tsx
git commit -m "feat(frontend): link failed backtest rows to detail page"
```

---

### Task F4: `<FailureCard>` on `/backtests/[id]`

**Files:**

- Create: `frontend/src/components/backtests/failure-card.tsx`
- Modify: `frontend/src/app/backtests/[id]/page.tsx` — render `<FailureCard>` when `status === "failed"`.

**Step 1: Component**

```tsx
// frontend/src/components/backtests/failure-card.tsx
"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Copy, Check } from "lucide-react";
import type { ErrorEnvelope } from "@/lib/api";

interface FailureCardProps {
  error: ErrorEnvelope;
}

export function FailureCard({ error }: FailureCardProps): React.ReactElement {
  const [copied, setCopied] = useState(false);

  const onCopy = async (): Promise<void> => {
    if (!error.suggested_action) return;
    await navigator.clipboard.writeText(error.suggested_action);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Card
      className="border-red-500/30 bg-red-500/5"
      data-testid="backtest-failure-card"
    >
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Badge
            variant="secondary"
            className="font-mono text-xs"
            data-testid="backtest-error-code"
          >
            {error.code.toUpperCase()}
          </Badge>
          <span>Backtest failed</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p
          className="whitespace-pre-wrap text-sm text-muted-foreground"
          data-testid="backtest-error-message"
        >
          {error.message}
        </p>

        {error.suggested_action && (
          <div className="space-y-2">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Suggested action
            </p>
            <div className="flex items-start gap-2">
              <pre
                className="flex-1 overflow-x-auto rounded-md bg-muted p-3 font-mono text-xs"
                data-testid="backtest-error-suggested-action"
              >
                <code>{error.suggested_action}</code>
              </pre>
              <Button
                variant="outline"
                size="icon"
                onClick={() => void onCopy()}
                aria-label="Copy command"
              >
                {copied ? (
                  <Check className="size-3.5" />
                ) : (
                  <Copy className="size-3.5" />
                )}
              </Button>
            </div>
          </div>
        )}

        {error.remediation && error.remediation.kind === "ingest_data" && (
          <div className="space-y-1 rounded-md border border-border/50 p-3 text-xs">
            <p className="font-medium">Remediation details</p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-muted-foreground">
              {error.remediation.symbols && (
                <>
                  <dt>Symbols:</dt>
                  <dd className="font-mono">
                    {error.remediation.symbols.join(", ")}
                  </dd>
                </>
              )}
              {error.remediation.asset_class && (
                <>
                  <dt>Asset class:</dt>
                  <dd>{error.remediation.asset_class}</dd>
                </>
              )}
              {error.remediation.start_date && (
                <>
                  <dt>Date range:</dt>
                  <dd>
                    {error.remediation.start_date}
                    {" → "}
                    {error.remediation.end_date}
                  </dd>
                </>
              )}
            </dl>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
```

**Step 2: Mount in the detail page**

In `frontend/src/app/backtests/[id]/page.tsx`, find where the page renders the backtest detail and add near the top of the content area:

```tsx
import { FailureCard } from "@/components/backtests/failure-card";

// The page returns JSX; add the failure card alongside the existing detail content:
const renderDetail = () => (
  <div className="space-y-4">
    {status?.status === "failed" && status.error && (
      <FailureCard error={status.error} />
    )}
    {/* existing completed-backtest detail content stays here */}
  </div>
);
```

> **Important:** the detail page currently only fetches a backtest's results (for completed runs) — it may not fetch the status endpoint. If so, add a `useEffect` that fetches `/api/v1/backtests/{id}/status` whenever the backtest is not completed, so the failure envelope surfaces. Verify during implementation.

**Step 3: Build + lint**

```bash
cd frontend && pnpm exec tsc --noEmit && pnpm lint
```

**Step 4: Commit**

```bash
git add frontend/src/components/backtests/failure-card.tsx frontend/src/app/backtests/[id]/page.tsx
git commit -m "feat(frontend): FailureCard on backtest detail page"
```

---

## E2E Use Cases (Phase 3.2b)

Project type: **fullstack** → API-first + UI verification.

### UC-BFS-001: Failed backtest exposes structured envelope via API

- **Interface:** API
- **Setup (ARRANGE):** Submit a backtest via `POST /api/v1/backtests/run` with `instruments=["ES.n.0"]` against the dev stack (which has no Parquet data). Poll `/status` until `status == "failed"` (≤ 30s worker runtime).
- **Steps:**
  1. `GET /api/v1/backtests/{id}/status`
- **Verification:**
  - HTTP 200
  - `body.status == "failed"`
  - `body.error.code == "missing_data"`
  - `body.error.message` contains `"ES"` and does NOT contain `"/app/"` (sanitized)
  - `body.error.suggested_action` starts with `"Run: msai ingest"`
  - `body.error.remediation.kind == "ingest_data"` with correct symbols + dates
- **Persistence:** `GET` a second time — envelope is identical (stable read).

### UC-BFS-002: Failed badge tooltip on `/backtests` history page

- **Interface:** UI (Playwright MCP)
- **Setup:** The failed backtest from UC-BFS-001 is already in history. Navigate to `http://localhost:3300/backtests`.
- **Steps:**
  1. Locate the row where `data-testid === "backtest-status-<id>"` with text `"failed"`.
  2. Hover the badge.
  3. Verify `data-testid === "backtest-error-tooltip-<id>"` appears with the first 150 chars of the error message.
- **Verification:** Tooltip is visible after hover; content contains a partial match of the envelope's `message`.
- **Persistence:** Navigate away + back → tooltip reappears on hover (not a one-shot).

### UC-BFS-003: CLI `msai backtest show` prints the envelope

- **Interface:** CLI
- **Setup:** The failed backtest from UC-BFS-001.
- **Steps:**
  1. Run `docker exec msai-claude-backend uv run msai backtest show <id>`
- **Verification:**
  - Exit code 0
  - Stdout JSON contains `"error": {"code": "missing_data", "message": "...", ...}`
- **Persistence:** Re-run the same command → identical output.

### UC-BFS-004: Detail page renders `<FailureCard>`

- **Interface:** UI (Playwright MCP)
- **Setup:** The failed backtest from UC-BFS-001.
- **Steps:**
  1. Navigate to `http://localhost:3300/backtests/<id>`.
  2. Locate `data-testid="backtest-failure-card"`.
- **Verification:**
  - `data-testid="backtest-error-code"` shows `"MISSING_DATA"`.
  - `data-testid="backtest-error-message"` contains the envelope message.
  - `data-testid="backtest-error-suggested-action"` contains a `msai ingest` command.
  - Copy button is present and clickable (not verified to actually copy — that requires clipboard permissions; smoke-test button presence only).
  - Remediation details list shows symbols + date range.
- **Persistence:** Reload the page → FailureCard still renders with the same content.

### UC-BFS-005: Historical failed row (pre-migration) surfaces with `unknown` code

- **Interface:** API + UI
- **Setup (ARRANGE):** The three pre-existing `failed` rows on the dev stack from PR #38 testing — their `error_code` defaults to `"unknown"` per the migration's DDL server_default; their `error_message` is populated but `error_public_message` is `NULL` (the migration does NOT backfill — `_build_error_envelope` sanitizes on read).
- **Steps:**
  1. API: `GET /api/v1/backtests/{id}/status` for one of those rows.
  2. UI: hover the badge; open the detail page.
- **Verification:**
  - API: `body.error.code == "unknown"`, `body.error.message` is present (stored raw or sanitized fallback), no blank fields.
  - UI: tooltip shows a useful fallback message (not empty); detail page shows FailureCard with `UNKNOWN` code badge.
- **Persistence:** Remains stable on reload.

---

## Quality Gate Checklist (Phase 5 reminder)

- Code-review loop: Codex + pr-review-toolkit, iterate until no P0/P1/P2.
- Simplify: `/simplify` on modified files.
- Verify: `verify-app` agent — backend tests, ruff, mypy, frontend tsc + lint + build.
- E2E: all 5 UCs above via `verify-e2e` agent (UI ones via Playwright MCP if agent toolbox lacks it).
- E2E regression (5.4b): scan `tests/e2e/use-cases/` for any pre-existing UCs (currently `live/` + `strategies/` — both should be unaffected).

## Files Summary

**Backend:**

- Create: `backend/src/msai/services/backtests/__init__.py`
- Create: `backend/src/msai/services/backtests/failure_code.py`
- Create: `backend/src/msai/services/backtests/sanitize.py`
- Create: `backend/src/msai/services/backtests/classifier.py`
- Create: `backend/alembic/versions/x2r3s4t5u6v7_add_backtest_error_classification.py`
- Create: `backend/tests/unit/test_backtest_failure_code.py`
- Create: `backend/tests/unit/test_backtest_sanitize.py`
- Create: `backend/tests/unit/test_backtest_classifier.py`
- Create: `backend/tests/unit/test_backtest_mark_failed.py`
- Create: `backend/tests/unit/test_backtest_model.py`
- Create: `backend/tests/unit/test_backtest_schemas.py`
- Modify: `backend/src/msai/models/backtest.py` (+4 cols)
- Modify: `backend/src/msai/schemas/backtest.py` (+2 models, extend 2 schemas)
- Modify: `backend/src/msai/api/backtests.py` (+1 helper, wire into 2 endpoints)
- Modify: `backend/src/msai/workers/backtest_job.py` (rewrite `_mark_backtest_failed` signature + 3 call-sites collapse)
- Modify: `backend/tests/unit/test_backtests_api.py` (+3 test classes)

**Frontend:**

- Create: `frontend/src/components/backtests/failure-card.tsx`
- Modify: `frontend/src/lib/api.ts` (+2 interfaces, extend 2)
- Modify: `frontend/src/app/layout.tsx` (mount `TooltipProvider`)
- Modify: `frontend/src/app/backtests/page.tsx` (Tooltip on badge)
- Modify: `frontend/src/app/backtests/[id]/page.tsx` (render `FailureCard`)

**Docs:**

- PRD (existing): `docs/prds/backtest-failure-surfacing.md`
- Plan (this file): `docs/plans/2026-04-20-backtest-failure-surfacing.md`
- Research: `docs/research/2026-04-20-backtest-failure-surfacing.md`

---

## Out-of-scope (explicit — confirmed in PRD §2 non-goals)

- Auto-ingest on missing data (separate follow-up PR; `Remediation.auto_available` stays `false`).
- Research-jobs + live-deployments failure surfacing (same pattern, files as follow-ups).
- UI retry / re-run button.
- Multi-tenant-grade sanitization.
- Alerting-routing changes (backtest failures do NOT fire alerts in this PR).
