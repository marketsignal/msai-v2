# structlog test-pollution: capture_logs() returns empty in full pytest suite

**Date:** 2026-05-09
**Branch where surfaced:** `feat/prod-compose-deployable` (caught by post-PR verify-app)
**Symptom files:** `backend/tests/unit/test_backtest_job.py::test_materialize_series_payload_*`

## Symptom

Tests that use `structlog.testing.capture_logs()` to assert log events PASS in isolation but FAIL when the full `pytest tests/` suite runs:

```
matches = [
    entry for entry in captured
    if entry.get("event") == "backtest_series_materialized" and entry.get("log_level") == "info"
]
assert matches, f"missing backtest_series_materialized INFO log; got {captured!r}"
# AssertionError: got []  ← captured is empty even though the log line shows up in stdout
```

## Root cause

`backend/src/msai/core/logging.py:setup_logging()` calls `structlog.configure(... cache_logger_on_first_use=not is_test)`. When `environment != "test"`, caching is `True`. The `BoundLoggerLazyProxy` returned by `structlog.get_logger()` captures the cache-mode setting at proxy-creation time and, on first method call, freezes the processor chain on the instance.

`structlog.testing.capture_logs()` works by replacing `_CONFIG["processors"]` with a `[LogCapture()]` chain. For loggers created with cache=False, this swap takes effect on the next method call. **For loggers already created with cache=True and already called once, the processor chain is frozen — capture_logs's swap is invisible to them.**

`backend/tests/conftest.py` imports `from msai.main import app`, which triggers `setup_logging(settings.environment)` at line 53 of main.py. **`settings.environment` defaults to `"development"`** when no `ENVIRONMENT` env var is set. Local pytest runs typically don't set the env var (CI does); module-level loggers created via `structlog.get_logger(__name__)` during conftest import are bound with `cache_logger_on_first_use=True`.

The flake's specific reproducer:

- `tests/e2e/` and `tests/integration/` run first in the default pytest discovery order. They exercise code paths that trigger first-method calls on production loggers (e.g., `tests/integration/test_backtest_job_auto_heal.py` invokes the `backtest_job` module's logger). These loggers cache the dev/prod processor chain.
- `tests/unit/test_backtest_job.py` runs much later. By then, the `backtest_job` logger is already cached. `capture_logs()` swaps `_CONFIG["processors"]` but the cached logger ignores it. `captured` stays `[]`. Test fails.
- Run the test in isolation (no e2e/integration before it): the logger has not yet been called, so `capture_logs()`'s swap takes effect when the test invokes the logger for the first time. Test passes.

## Fix

Set `ENVIRONMENT=test` BEFORE any `msai.*` import in `backend/tests/conftest.py`:

```python
import os
os.environ.setdefault("ENVIRONMENT", "test")  # MUST be before any msai.* import

# ... rest of imports follow with # noqa: E402
```

`setup_logging()` then sees `is_test=True`, configures structlog with `cache_logger_on_first_use=False`, and every `BoundLoggerLazyProxy` resolves processors from `_CONFIG` at every method call. `capture_logs()` works regardless of which loggers have already been bound or called.

`os.environ.setdefault` (not `os.environ[...] =`) lets explicit overrides win — useful for tests that intentionally run in dev/prod mode.

## Why this didn't surface earlier

CI sets `ENVIRONMENT=test` in the job env, so the flakes never fired in CI. They only fired on local `pytest` runs (which is exactly when verify-app runs in a developer's worktree). The flakes were probably present for as long as the conftest has imported msai.main; they only became visible when verify-app reported them on this branch.

## Prevention

When adding any module-level Pydantic settings validator that reads `environment`, OR any structlog configuration that varies by `environment`, ensure tests guarantee the right env-var BEFORE the import path that consumes it. The conftest's first action should be `os.environ.setdefault(...)` for every env var that gates module-import-time behavior.

## Related solution

`docs/solutions/deployment/pydantic-config-validators-fire-on-import.md` covers a related (production-mode) variant of the same import-time-side-effect class of bug, where `REPORT_SIGNING_SECRET` validation fires for every container that imports `msai.core.config.settings`.
