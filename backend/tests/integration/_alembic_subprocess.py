"""Shared subprocess helpers for Alembic CLI tests.

Both ``test_alembic_migrations.py`` and migration-specific test files
(e.g. ``test_instrument_cache_migration.py``) need to invoke alembic
against an isolated testcontainer Postgres URL. Extract the helpers
here so they don't drift across test files.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run_alembic(
    database_url: str,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> None:
    """Invoke alembic via the in-venv Python; raise on non-zero exit.

    ``extra_env`` overrides arbitrary pydantic-settings fields (e.g.
    ``STRATEGIES_ROOT``, ``IB_ACCOUNT_ID``) the same way as ``DATABASE_URL``.
    """
    result = run_alembic_raw(database_url, *args, extra_env=extra_env)
    if result.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed (exit {result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def run_alembic_raw(
    database_url: str,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke alembic and return CompletedProcess WITHOUT raising.

    Use for fail-loud branch tests that assert on non-zero exit.
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_backend_root(),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
