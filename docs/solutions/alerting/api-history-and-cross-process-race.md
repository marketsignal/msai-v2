# Alerting history API + cross-process write race

## Problem

Two problems rolled into one port from the Codex version:

1. **No history API**: Claude had an SMTP-only `AlertService`. Alerts fired, sometimes emailed, never recorded. The frontend couldn't show a recent-alerts audit trail, and operators couldn't tell whether a specific alert had fired in dev where SMTP is usually unconfigured.
2. **Race on the history file**: The Codex port's naive read-modify-write against a shared `alerts.json` loses records when two worker processes emit alerts simultaneously. Each reads the same old payload, each rewrites. The later write wins; the earlier's record vanishes.

## Root cause

**Problem 1**: `AlertService.send_alert` went straight to SMTP with no persistence layer. Codex had `AlertingService` + `alerting_service` singleton + `GET /api/v1/alerts/` router but no SMTP sender.

**Problem 2**: The `_write_event` pattern `json.loads(read_text()) → mutate → write_text(json.dumps(...))` has two failure modes:

- **Torn-file read**: a reader observing the file mid-`write_text` sees truncated JSON → `JSONDecodeError` → audit returns `[]` silently. Codex inherits this.
- **Lost update**: two processes each read the same prior state, each insert one record, each write back. Only the last write survives; the earlier record is dropped.

## Solution

Merge both: keep Claude's SMTP sender, add Codex's file-backed history and router, and harden the history write for multi-process safety.

1. **Port Codex's surface** (`schemas/alert.py`, `api/alerts.py`, `services/alerting.AlertingService`, `core/config.alerts_path`). Keep the Codex contract: `limit` silently clamped to `[1, 200]`, 200-record rolling cap, newest first, malformed-entries-are-skipped defensive read.
2. **Wire the SMTP path into history**: Claude's `AlertService.send_alert` (and every convenience method) records to `alerting_service` **before** attempting SMTP. A missing SMTP config, empty recipient list, or transport failure still produces an auditable entry.
3. **Make writes multi-process safe**: `_write_event` acquires an exclusive `fcntl.flock` on a `{alerts_path}.lock` sidecar before read-modify-write. `list_alerts` takes the same lock (simple mutex — the audit log is not hot, and matching lock modes avoids writer starvation). Payload is written to a tempfile + `os.replace`'d inside the lock, so a reader always sees either the previous state or the new state, never a torn file.
4. **Remove committed runtime artifact**: `claude-version/data/alerts/alerts.json` was staged in the first draft; removed + `data/alerts/` added to both `.gitignore` files.

## Prevention

- **Cross-process test** (`test_concurrent_writes_across_processes_do_not_lose_records`): spawns 6 `multiprocessing.Process` children emitting simultaneously, asserts all 6 records + the seed survive. Thread-only tests miss this because the GIL masks the race; the multiprocessing test is the real regression guard.
- **Thread test** (`test_concurrent_writes_do_not_lose_records`): 8-thread barrier burst, all 8 records survive. Fast, catches most cases in CI.
- **Regression parametrize** for existing callers (`process_manager.alert_strategy_error`, `disconnect_handler.alert_ib_disconnect`, etc.) asserting the kw-only `level=` parameter didn't break any call site.
- **Auth test** verifies `GET /api/v1/alerts/` rejects unauthenticated requests (401/403).
- **Malformed-entries test** verifies operator hand-edits don't crash the router.
- Add `data/alerts/` to `.gitignore` prevents a future run from re-committing the runtime file.

## Reference

- Claude: `claude-version/backend/src/msai/services/alerting.py`, `claude-version/backend/src/msai/api/alerts.py`
- Codex original: `codex-version/backend/src/msai/services/alerting.py`, `codex-version/backend/src/msai/api/alerts.py` (same race — flagged but not fixed there; Claude's port is stricter)
