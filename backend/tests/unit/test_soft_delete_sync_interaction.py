"""Soft-delete × ``sync_strategies_to_db`` interaction (plan R2 / T3).

Soft-deleting a strategy and then calling ``sync_strategies_to_db()`` —
which discovers the same file on disk via ``discover_strategies`` — must
NOT un-archive the row or create a new active row for the same file
path. The sync opts into ``include_deleted=True`` so it sees the
archived row, then takes a no-op branch instead of creating a duplicate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from msai.models.strategy import Strategy
from msai.services.strategy_registry import sync_strategies_to_db


# Mirror ``_FakeAsyncSession`` from test_strategy_registry.py but kept
# local so a rename there does not silently break this test.
class _FakeAsyncSession:
    """Async-session stub covering the sync function's surface."""

    def __init__(self, existing: list[Strategy] | None = None) -> None:
        self._rows: list[Strategy] = list(existing or [])
        self.added: list[Strategy] = []
        self.deleted: list[Strategy] = []

    async def execute(self, _stmt: object) -> _FakeAsyncSession:
        return self

    def scalars(self) -> _FakeAsyncSession:
        return self

    def all(self) -> list[Strategy]:
        # The fake ignores ``execution_options`` and returns every row —
        # mirroring the include_deleted=True semantics of the production
        # path. The unit-listener test exercises the actual filter.
        return [r for r in self._rows if r not in self.deleted]

    def add(self, row: Strategy) -> None:
        self._rows.append(row)
        self.added.append(row)

    async def delete(self, row: Strategy) -> None:
        self.deleted.append(row)


# Real on-disk strategies used by the project's example dir — sync needs
# real importable files (it shells out to ``discover_strategies``).
STRATEGIES_DIR = Path(__file__).resolve().parents[3] / "strategies" / "example"


async def test_sync_leaves_archived_row_archived_when_file_still_on_disk() -> None:
    """An archived row + the same file still on disk → sync MUST NOT
    un-archive the row and MUST NOT create a duplicate active row."""
    # Arrange: pre-archived row pointing at one of the real example files.
    target_file = STRATEGIES_DIR / "ema_cross.py"
    assert target_file.is_file()

    archived_row = Strategy(
        id=uuid4(),
        name="example.ema_cross",
        file_path=str(target_file),
        strategy_class="EMACrossStrategy",
        config_class="EMACrossConfig",
        config_schema=None,
        default_config=None,
        config_schema_status="no_config_class",
        code_hash="deadbeef" * 8,
        deleted_at=datetime.now(UTC),
    )
    archived_stamp = archived_row.deleted_at
    session = _FakeAsyncSession(existing=[archived_row])

    # Act: trigger the sync the list/detail endpoints run before reading.
    paired = await sync_strategies_to_db(session, STRATEGIES_DIR)  # type: ignore[arg-type]

    # Assert:
    # - The archived row stays archived (deleted_at unchanged).
    assert archived_row.deleted_at == archived_stamp
    # - No new row was added for the same file_path.
    new_rows_same_path = [r for r in session.added if r.file_path == str(target_file)]
    assert new_rows_same_path == [], (
        "sync must not create a NEW active row for an archived file_path"
    )
    # - The archived row is NOT returned in ``paired`` so list views hide it.
    archived_in_paired = [row for row, _ in paired if row.id == archived_row.id]
    assert archived_in_paired == [], "archived rows must not surface to list views"
