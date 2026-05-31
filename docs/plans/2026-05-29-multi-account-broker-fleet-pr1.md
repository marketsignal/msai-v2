# Multi-Account Broker Fleet — PR 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the control-plane plumbing for an N-IB-account fleet by running two TradingNodes against two `marin1016test` paper sub-accounts (DUP733214 + DUP733215) through ONE shared IB Gateway, with Databento providing live data and IB doing exec-only, plus account-scoped command routing, halt split, and drain.

**Architecture:** ONE `ib-gateway` container (paper, `marin1016test` login) + TWO TradingNodes connected via distinct `ibg_client_id`s + per-TradingNode `InteractiveBrokersExecClientConfig(account_id=...)`. Databento provides live market data through `DatabentoLiveDataClientFactory`; IB carries order flow only (no IB data client wired). A bidirectional dataset-aware symbology shim translates strategy-visible `<SYM>.IBKR` ↔ Databento native venues at the message-bus boundary. Halt-keys consolidated into a single module with account-scoped + fleet-scoped variants and `reason=` metadata. `GATEWAY_CONFIG` is extended so one `ib_login_key` row can name multiple `account_id`s, and a fail-closed boot check rejects duplicate `ib_login_key`s.

**Tech Stack:** Python 3.12, FastAPI, NautilusTrader 1.223.0 (`nautilus_trader.adapters.databento` + `nautilus_trader.adapters.interactive_brokers`), arq, Redis 7, PostgreSQL 16, IB Gateway (`ghcr.io/gnzsnz/ib-gateway:stable`), Databento Python SDK (live mode via `nautilus_pyo3.DatabentoLiveClient`), pytest, ruff, mypy --strict.

---

## Approach Comparison

The architectural choice was settled across three council rulings persisted in `docs/decisions/multi-account-broker-fleet.md`. This plan does not re-litigate any of them.

| Decision                  | Verdict                                                                                                                                                                                                               | Source                       |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| Production unit           | Container-per-account (Option A). Reject Nautilus multi-venue-in-one-process (Option B) on issue #3176 (no per-venue failure isolation).                                                                              | Council 2026-05-27, ratified |
| Supervisor model          | Per-account supervisors own TradingNode lifetimes. Shared `ProcessManager.handles` is a fleet-wide SPOF; deferred to PR 2.                                                                                            | Council 2026-05-27, ratified |
| Data adapter              | Split data/exec — Databento provides live data; IB carries order flow only.                                                                                                                                           | Addendum 2026-05-28          |
| Symbology                 | Strategy-visible `<SYM>.IBKR`; bidirectional dataset-aware shim translates ↔ native Databento venues.                                                                                                                 | Addendum 2026-05-28          |
| Data-stale halt model     | Per-node detection, fleet-wide action, session-aware freshness, halt-cause metadata. Deferred to PR 1b (gates LVP/HVP graduation).                                                                                    | Addendum 2026-05-28          |
| PR 1 gateway topology     | Shape A — ONE `ib-gateway` container + TWO TradingNodes via distinct `ibg_client_id`s + `account_id` routing. CONDITIONAL APPROVE: PR 1 must explicitly name its scope-of-proof boundary.                             | Addendum 2026-05-29          |
| Multi-container isolation | NOT proven in PR 1. Bound to a hard pre-LVP/HVP graduation drill (Hawk minority report, institutionalized). Before real-money deployment, two distinct TWS logins + two containers + crash isolation must be drilled. | Addendum 2026-05-29          |

## Contrarian Verdict

**COUNCIL** (fired 2026-05-29 during this `/forge-goal` autonomous run; 5 advisors + Codex chairman). Verdict: **CONDITIONAL APPROVE Shape A** with five blocking objections (PRD/addendum amendment, explicit drain-leaves-B-running assertion, `gateway_session_key` propagation through command payload, fail-fast same-`ib_login_key` collision check, synthetic multi-login `GatewayRouter` test). All five objections are encoded as tasks below. Hawk minority report preserved as the pre-LVP/HVP graduation gate in the decision doc; not in PR 1's scope.

---

## Pre-Implementation Verification Spike (gates Phase 4)

**This spike MUST PASS before any task in Phase 4 begins.** If it fails, the worktree retires; the council is re-opened on data-adapter choice.

**Spike:** Confirm that IB Gateway accepts a paper market order on an account with NO active IB market-data subscription.

- [ ] **S1: Operator brings up `ib-gateway` container (paper) under `marin1016test` login** — see compose `broker` profile. Confirm `4004` (host) → `4002` (internal paper) socat proxy is up.
- [ ] **S2: Connect a one-shot Nautilus client to `DUP733213`** (the no-data sub-account from `reference_ib_entitlements.md`). Use `nautilus_trader.adapters.interactive_brokers` with `InteractiveBrokersExecClientConfig(account_id="DUP733213", ibg_client_id=199)` and NO data client. Script at `scripts/spike_ib_orders_without_data.py` (one-shot, throwaway after spike). Place a 1-share MARKET BUY on AAPL. Cancel/close on confirmation.
- [ ] **S3: Verify the fill** — operator (Pablo) confirms 1 paper fill in IB's UI for DUP733213. Record outcome in `docs/research/2026-05-29-spike-ib-orders-without-data.md` (single line: PASS / FAIL).
- [ ] **S4: Decision gate** — if PASS: proceed to Phase 4. If FAIL: STOP. Surface failure as a blocker in `.claude/local/state.md` and pause for human decision (re-council on adapter split).

---

## File Structure

PR 1 modifies these files. Each file is listed with its responsibility; tasks below reference exact files + line ranges.

### Create (new)

- `backend/src/msai/core/halt_keys.py` — Single module of truth for Redis halt keys. Exports `fleet_halt_key()`, `account_halt_key(account_id)`, `halt_cause_key(scope, *, account_id=None)` and a `HaltCause` enum.
- `backend/src/msai/services/symbology_shim.py` — Bidirectional shim between canonical `<SYM>.IBKR` (strategy-visible) and `(dataset, native_symbol, native_venue)` (Databento-native). Outbound: `resolve_for_databento(canonical_id: InstrumentId) -> DatabentoSubscriptionTarget`. Inbound: msgbus event interceptor (callable) that re-tags `Bar` events from native venue to canonical `.IBKR` and preserves provider/dataset/native_venue in event metadata.
- `backend/src/msai/services/nautilus/databento_live_config.py` — Helpers to build `DatabentoDataClientConfig` from project settings + the symbology shim (pre-populated `instrument_ids` in NATIVE venue form, `reconnect_timeout_mins=10` pinned).
- `scripts/spike_ib_orders_without_data.py` — One-shot verification spike script (deleted in Phase 6 after results recorded).

### Modify (existing)

- `backend/src/msai/services/live/gateway_router.py` (54 → ~140 LOC) — Extend `GatewayRouter` to parse a new optional pipe-separated `accounts=A|B|...` segment per entry: `login1:host1:port1:accounts=DUP733214|DUP733215`. Backwards-compatible with the existing `login:host:port` 3-tuple format. Fail-closed at boot if two entries share the same `ib_login_key`. Add `accounts_for(login_key) -> list[str]` accessor.
- `backend/src/msai/live_supervisor/main.py` (l. 63-105) — `handle_command()` extracts `gateway_session_key` (or its successor key — pull from `command.payload["gateway_session_key"]`) and passes it explicitly to `process_manager.spawn(...)`. Verify the START path; this is council blocking objection #12.
- `backend/src/msai/services/live_command_bus.py` (l. 66-76) — Add helper `command_stream_for_account(account_id: str | None) -> str` returning `"msai:live:commands:{account_id}"` when an account is named, else legacy `"msai:live:commands"`. Existing global `LIVE_COMMAND_STREAM` remains, but consumers prefer the account-scoped form.
- `backend/src/msai/api/live.py` (multiple locations) — (a) Replace `_HALT_KEY = "msai:risk:halt"` (l. 105) with `from msai.core.halt_keys import fleet_halt_key, account_halt_key, halt_cause_key, HaltCause`. (b) When `/kill-all` fires, write halt-cause companion keys with `reason=fleet_emergency`. (c) Add new endpoint `POST /api/v1/live/drain/{account_id}` for account-scoped drain. (d) `GET /api/v1/live/status` response includes `ib_login_key`, `account_id`, `ibg_client_id` on each deployment.
- `backend/src/msai/live_supervisor/process_manager.py` (l. 96, 232, 429) — Remove the duplicate `_HALT_KEY` definition; import from `msai.core.halt_keys`. Use account-scoped halt key for per-account halt enforcement.
- `backend/src/msai/services/nautilus/disconnect_handler.py` (l. 60, 219) — Remove the third duplicate `_HALT_KEY` definition; import from `msai.core.halt_keys`.
- `backend/src/msai/services/nautilus/live_node_config.py` (full file restructure, ~688 LOC, surgical edits) — Per-account `TradingNodeConfig` builder. Add `account_id` + `ibg_client_id` parameters. Replace any IB data client wiring with the Databento data client (`DatabentoDataClientConfig`). Use `databento_live_config.build()` to assemble.
- `backend/src/msai/live_supervisor/__main__.py` (l. 70-180 `_build_production_payload_factory`, l. 466-490 main) — Pass `account_id` + `ibg_client_id` through the payload factory into the per-account TradingNode config. Add startup logging that names `(ib_login_key, account_id, ibg_client_id)` for each session.
- `backend/src/msai/cli.py` (the `live status` sub-app) — `msai live status` table includes `ib_login_key`, `account_id`, `ibg_client_id` columns.
- `frontend/src/components/live/strategy-status.tsx:153` — existing live status table component; add `ib_login_key`, `account_id`, `ibg_client_id` columns. `frontend/src/lib/api.ts:293` — typed API client, extend the `LiveDeployment` type. `frontend/src/app/live-trading/page.tsx` — page route, verify no edits required.
- `docker-compose.dev.yml` — `GATEWAY_CONFIG` env now includes accounts list (e.g. `marin1016test:ib-gateway:4002:accounts=DUP733214|DUP733215`); no second `ib-gateway` service. Also adds `GATEWAY_CONFIG: ${GATEWAY_CONFIG:-}` to the backend service env block.

### Test

- `backend/tests/unit/services/live/test_gateway_router.py` — Parser + multi-login + fail-closed.
- `backend/tests/unit/core/test_halt_keys.py` — Key construction + companion-key shape + enum.
- `backend/tests/unit/services/test_symbology_shim.py` — Outbound resolution + inbound re-tagging + audit metadata preservation.
- `backend/tests/unit/services/nautilus/test_databento_live_config.py` — Config builder field set (`instrument_ids` pre-populated, `reconnect_timeout_mins=10` pinned, no IB data client).
- `backend/tests/unit/live_supervisor/test_handle_command_propagation.py` — `gateway_session_key` propagation from `LiveCommand.payload` → `ProcessManager.spawn(...)`.
- `backend/tests/integration/test_gateway_router_synthetic_multi_login.py` — Synthetic multi-login (`_build_production_payload_factory` covers the multi-login branch even in single-login PR 1).
- `backend/tests/integration/api/test_live_drain_account.py` — Account-scoped drain leaves other account running.
- `backend/tests/integration/api/test_kill_all_halt_cause.py` — Fleet halt writes `reason=fleet_emergency` companion keys.
- `backend/tests/integration/api/test_live_status_includes_account_context.py` — Status endpoint shape includes `ib_login_key`, `account_id`, `ibg_client_id`.

---

## Tasks

> **TDD discipline:** Every task follows Red → Green → Refactor → Commit. Each task is self-contained; the dispatched subagent reads this plan, the cited source files, and the linked decision doc + research brief, then executes.

### Task T1: Consolidate halt-keys into `core/halt_keys.py`

**Files:**

- Create: `backend/src/msai/core/halt_keys.py`
- Test: `backend/tests/unit/core/test_halt_keys.py`

Replaces three duplicate `_HALT_KEY = "msai:risk:halt"` definitions (`api/live.py:105`, `live_supervisor/process_manager.py:96`, `services/nautilus/disconnect_handler.py:60`). Adds account-scoped variants + halt-cause metadata schema. Halt-cause is forward-compatible with PR 1b's `data_stale`.

- [ ] **T1.1: Write the failing test**

```python
# backend/tests/unit/core/test_halt_keys.py
from msai.core.halt_keys import (
    HaltCause,
    account_halt_key,
    fleet_halt_key,
    halt_cause_key,
)


def test_fleet_halt_key_is_canonical_global_string() -> None:
    assert fleet_halt_key() == "msai:risk:halt"


def test_account_halt_key_namespaces_by_account_id() -> None:
    # Critical: keyed by account_id, not ib_login_key. Two sub-accounts
    # under one TWS login MUST have independent halt latches.
    assert account_halt_key("DUP733214") == "msai:risk:halt:account:DUP733214"
    assert account_halt_key("DUP733215") == "msai:risk:halt:account:DUP733215"


def test_account_halt_key_rejects_empty_account_id() -> None:
    import pytest
    with pytest.raises(ValueError, match="account_id must be non-empty"):
        account_halt_key("")


def test_halt_cause_key_fleet_scope() -> None:
    assert halt_cause_key("fleet") == "msai:risk:halt:cause"


def test_halt_cause_key_account_scope() -> None:
    assert (
        halt_cause_key("account", account_id="DUP733214")
        == "msai:risk:halt:account:DUP733214:cause"
    )


def test_halt_cause_enum_values() -> None:
    assert HaltCause.FLEET_EMERGENCY.value == "fleet_emergency"
    assert HaltCause.DATA_STALE.value == "data_stale"  # forward-compat for PR 1b
    assert HaltCause.OPERATOR_DRAIN.value == "operator_drain"
```

- [ ] **T1.2: Run test to verify it fails**

```bash
cd backend && uv run pytest tests/unit/core/test_halt_keys.py -v
```

Expected: collection error (module does not exist).

- [ ] **T1.3: Implement the module**

```python
# backend/src/msai/core/halt_keys.py
"""Redis halt-key namespacing for the live-trading fleet.

Single source of truth — replaces three duplicate ``_HALT_KEY = "msai:risk:halt"``
definitions across api/live.py, live_supervisor/process_manager.py, and
services/nautilus/disconnect_handler.py.

Halt-cause metadata is written as a companion key alongside the latch key so
operators can distinguish a manual ``/kill-all`` panic from a PR-1b data-stale
auto-halt. Per the decision-doc addendum 2026-05-28, reuse the same latch code
path; only the cause-attribution metadata differs.
"""

from __future__ import annotations

from enum import StrEnum

_FLEET_HALT = "msai:risk:halt"


class HaltCause(StrEnum):
    """Distinguishes halt callers so an operator can interpret a halt latch."""

    FLEET_EMERGENCY = "fleet_emergency"  # Manual /kill-all
    OPERATOR_DRAIN = "operator_drain"    # Account-scoped drain
    DATA_STALE = "data_stale"            # PR 1b — Databento freshness gate


def fleet_halt_key() -> str:
    """Return the global fleet-wide halt latch key."""
    return _FLEET_HALT


def account_halt_key(account_id: str) -> str:
    """Return the account-scoped halt latch key for *account_id*.

    PR 1 critical: keyed by ``account_id`` (DUP733214, DUP733215, …),
    NOT by ``ib_login_key``, so that draining one sub-account under a
    shared login does NOT halt the other sub-account (council 2026-05-29
    blocking objection #11 — Codex iter 1 P1-1).
    """
    if not account_id:
        raise ValueError("account_id must be non-empty")
    return f"{_FLEET_HALT}:account:{account_id}"


def halt_cause_key(scope: str, *, account_id: str | None = None) -> str:
    """Return the companion key holding ``HaltCause`` metadata for *scope*."""
    if scope == "fleet":
        return f"{_FLEET_HALT}:cause"
    if scope == "account":
        if not account_id:
            raise ValueError("account_id required when scope='account'")
        return f"{_FLEET_HALT}:account:{account_id}:cause"
    raise ValueError(f"unknown halt scope: {scope!r}")
```

- [ ] **T1.4: Run test to verify it passes**

```bash
cd backend && uv run pytest tests/unit/core/test_halt_keys.py -v
```

Expected: 6 passed.

- [ ] **T1.5: Run lint/types**

```bash
cd backend && uv run ruff check src/msai/core/halt_keys.py tests/unit/core/test_halt_keys.py && uv run mypy src/msai/core/halt_keys.py --strict
```

- [ ] **T1.6: Commit**

```bash
git add backend/src/msai/core/halt_keys.py backend/tests/unit/core/test_halt_keys.py
git commit -m "feat(core): consolidate halt-keys into core/halt_keys.py with account scope + cause metadata"
```

---

### Task T2: Replace duplicate `_HALT_KEY` callsites with the new module

**Files:**

- Modify: `backend/src/msai/api/live.py:105` (delete local `_HALT_KEY`; import from `core.halt_keys`)
- Modify: `backend/src/msai/live_supervisor/process_manager.py:96, 232, 429` (delete local; import from `core.halt_keys`)
- Modify: `backend/src/msai/services/nautilus/disconnect_handler.py:60, 219` (delete local; import from `core.halt_keys`)

Pure refactor; behavior unchanged. All three files now import `fleet_halt_key()` and use it where they previously used the local `_HALT_KEY` constant.

- [ ] **T2.1: Inspect every callsite**

```bash
grep -n "_HALT_KEY" backend/src/msai/api/live.py backend/src/msai/live_supervisor/process_manager.py backend/src/msai/services/nautilus/disconnect_handler.py
```

- [ ] **T2.2: In each file, replace `_HALT_KEY = "msai:risk:halt"` definition with**

```python
from msai.core.halt_keys import fleet_halt_key
```

And replace every read of `_HALT_KEY` with a call to `fleet_halt_key()`.

- [ ] **T2.3: Run the existing test suite to confirm no behavior change**

```bash
cd backend && uv run pytest tests/unit/api/ tests/unit/live_supervisor/ tests/unit/services/nautilus/ -x -q
```

Expected: all green.

- [ ] **T2.4: Run lint/types**

```bash
cd backend && uv run ruff check src/msai/api/live.py src/msai/live_supervisor/process_manager.py src/msai/services/nautilus/disconnect_handler.py && uv run mypy src/msai/ --strict
```

- [ ] **T2.5: Commit**

```bash
git add backend/src/msai/api/live.py backend/src/msai/live_supervisor/process_manager.py backend/src/msai/services/nautilus/disconnect_handler.py
git commit -m "refactor: replace local _HALT_KEY duplicates with core/halt_keys import"
```

---

### Task T3: Extend `/kill-all` to write halt-cause metadata

**Files:**

- Modify: `backend/src/msai/api/live.py` (`/kill-all` handler, around l. 1633-1700)
- Test: `backend/tests/integration/api/test_kill_all_halt_cause.py`

When `/kill-all` fires, write both the latch key (existing behavior) AND a companion `halt_cause_key("fleet")` Redis key holding `HaltCause.FLEET_EMERGENCY` value, plus `detected_at` (ISO-8601 UTC) and `source` ("operator" or the authenticated user identifier). Per the decision-doc addendum 2026-05-28's halt-cause schema.

- [ ] **T3.1: Write the failing integration test**

```python
# backend/tests/integration/api/test_kill_all_halt_cause.py
# Follow the isolated-DB/isolated-Redis pattern from
# backend/tests/integration/test_live_start_endpoints.py (verified
# fixtures at l. 62-119: isolated_postgres_url, isolated_redis_url,
# session_factory, redis_binary, redis_text). The dispatched subagent
# imports/uses those names directly rather than inventing new ones.
# (Codex iter 3 P2)
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_kill_all_writes_fleet_emergency_cause_metadata(
    client: AsyncClient,  # use existing client fixture from test_live_start_endpoints.py
    redis_text,           # text-decoded Redis fixture; auto-flushdb per test
) -> None:
    # redis_text fixture is the AsyncRedis the test should use; auto-flushdb
    # before/after the test (see test_live_start_endpoints.py:108-116).
    # Arrange (extra safety): ensure halt keys are clear
    await redis_text.delete("msai:risk:halt", "msai:risk:halt:cause")

    # Act: fire kill-all
    response = await client.post("/api/v1/live/kill-all")

    # Assert: 200 + latch set + cause set
    assert response.status_code == 200
    assert await redis_text.exists("msai:risk:halt")
    cause_blob = await redis_text.get("msai:risk:halt:cause")
    assert cause_blob is not None
    import json
    cause = json.loads(cause_blob)
    assert cause["reason"] == "fleet_emergency"
    assert "detected_at" in cause
    assert "source" in cause
```

- [ ] **T3.2: Run test to verify it fails (missing cause companion key)**

```bash
cd backend && uv run pytest tests/integration/api/test_kill_all_halt_cause.py -v
```

- [ ] **T3.3: Implement the cause-write in `/kill-all` handler**

In `backend/src/msai/api/live.py`, locate the `/kill-all` handler (`live_kill_all` at `api/live.py:1622`). The real handler uses `claims: dict[str, Any] = Depends(get_current_user)`, `db: AsyncSession = Depends(get_db)`, and `bus: LiveCommandBus = Depends(get_command_bus)` — there is NO `current_user` or `redis_client` (Codex iter 2 P2-3). Redis access goes through `bus._redis` (intentional bus reuse — see the existing call at `api/live.py:1675`). After the existing latch-set call, add:

```python
from msai.core.halt_keys import halt_cause_key, HaltCause
import json
from datetime import datetime, UTC

# user_id is already computed earlier in the handler via _resolve_user_id(db, claims).
cause_payload = {
    "reason": HaltCause.FLEET_EMERGENCY.value,
    "detected_at": datetime.now(UTC).isoformat(),
    "source": str(user_id) if user_id else "operator",
}
await bus._redis.set(  # noqa: SLF001 — intentional bus reuse, matches existing pattern
    halt_cause_key("fleet"),
    json.dumps(cause_payload),
    ex=86400,
)
```

- [ ] **T3.4: Run test to verify it passes**

```bash
cd backend && uv run pytest tests/integration/api/test_kill_all_halt_cause.py -v
```

- [ ] **T3.5: Lint + types + commit**

```bash
cd backend && uv run ruff check src/msai/api/live.py tests/integration/api/test_kill_all_halt_cause.py && uv run mypy src/msai/api/live.py --strict
git add backend/src/msai/api/live.py backend/tests/integration/api/test_kill_all_halt_cause.py
git commit -m "feat(api): /kill-all writes halt-cause companion key with reason=fleet_emergency"
```

---

### Task T4: Extend `GatewayRouter` to accept multi-account login entries + fail-closed on duplicate `ib_login_key`

**Files:**

- Modify: `backend/src/msai/services/live/gateway_router.py` (full file; ~54 → ~140 LOC)
- Test: `backend/tests/unit/services/live/test_gateway_router.py`

New `GATEWAY_CONFIG` format: backwards-compatible 3-tuple `login:host:port` PLUS optional 4th segment `:accounts=DUP1|DUP2|...` (pipe-separated; council 2026-05-29 #13 + Codex iter 1 P1-2: pipe avoids colliding with the comma between gateway entries). Parser rejects two entries with the same `ib_login_key`. Adds `accounts_for(login_key)` accessor.

- [ ] **T4.1: Write the failing test**

```python
# backend/tests/unit/services/live/test_gateway_router.py
import pytest

from msai.services.live.gateway_router import GatewayRouter


def test_legacy_3tuple_format_still_parses() -> None:
    r = GatewayRouter("marin1016test:ib-gateway-paper:4004")
    ep = r.resolve("marin1016test")
    assert ep.host == "ib-gateway-paper"
    assert ep.port == 4004
    assert r.accounts_for("marin1016test") == []  # no accounts segment


def test_accounts_segment_parses_pipe_separated() -> None:
    # Grammar fix (Codex iter 1 P1-2): accounts within an entry are
    # pipe-separated to avoid colliding with the comma used between
    # gateway entries. Format: login:host:port:accounts=A1|A2|A3
    r = GatewayRouter(
        "marin1016test:ib-gateway-paper:4004:accounts=DUP733214|DUP733215"
    )
    assert r.accounts_for("marin1016test") == ["DUP733214", "DUP733215"]
    assert r.resolve("marin1016test").host == "ib-gateway-paper"


def test_fail_closed_on_duplicate_login() -> None:
    with pytest.raises(ValueError, match="duplicate ib_login_key 'marin1016test'"):
        GatewayRouter(
            "marin1016test:host-a:4004,marin1016test:host-b:4005"
        )


def test_multi_login_still_works() -> None:
    r = GatewayRouter(
        "marin1016test:ib-gateway-paper:4004:accounts=DUP733214|DUP733215,"
        "mslvp000:ib-gateway-lvp:4003:accounts=U1234567"
    )
    assert r.is_multi_login
    assert r.accounts_for("mslvp000") == ["U1234567"]
    assert sorted(r.login_keys) == ["marin1016test", "mslvp000"]
```

- [ ] **T4.2: Run test to verify the new behaviors fail**

```bash
cd backend && uv run pytest tests/unit/services/live/test_gateway_router.py -v
```

- [ ] **T4.3: Implement the parser extension**

Replace the `GatewayRouter.__init__` body to parse the optional 4th segment. Add an `accounts_for(login_key) -> list[str]` method. After parsing each entry, raise `ValueError("duplicate ib_login_key {key!r}")` if the same `ib_login_key` was already added. Keep the dataclass `GatewayEndpoint` unchanged. Add a parallel `dict[str, list[str]]` for accounts.

```python
# backend/src/msai/services/live/gateway_router.py (key delta)
class GatewayRouter:
    def __init__(self, config_str: str | None = None) -> None:
        self._routes: dict[str, GatewayEndpoint] = {}
        self._accounts: dict[str, list[str]] = {}
        if not config_str:
            return
        for entry in config_str.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) < 3:
                continue  # tolerate malformed entries (existing behavior)
            login_key = parts[0].strip()
            host = parts[1].strip()
            port = int(parts[2].strip())
            accounts: list[str] = []
            for extra in parts[3:]:
                if extra.startswith("accounts="):
                    # Pipe-separated to avoid colliding with the comma used
                    # between gateway entries (Codex iter 1 P1-2).
                    accounts = [
                        a.strip() for a in extra[len("accounts="):].split("|") if a.strip()
                    ]
            if login_key in self._routes:
                raise ValueError(
                    f"duplicate ib_login_key {login_key!r} in GATEWAY_CONFIG"
                )
            self._routes[login_key] = GatewayEndpoint(host=host, port=port)
            self._accounts[login_key] = accounts

    def accounts_for(self, login_key: str) -> list[str]:
        return list(self._accounts.get(login_key, []))
```

- [ ] **T4.4: Run tests to verify they pass**

```bash
cd backend && uv run pytest tests/unit/services/live/test_gateway_router.py -v
```

- [ ] **T4.5: Lint + types + commit**

```bash
cd backend && uv run ruff check src/msai/services/live/gateway_router.py tests/unit/services/live/test_gateway_router.py && uv run mypy src/msai/services/live/gateway_router.py --strict
git add backend/src/msai/services/live/gateway_router.py backend/tests/unit/services/live/test_gateway_router.py
git commit -m "feat(live): GatewayRouter accepts accounts= segment + rejects duplicate ib_login_key"
```

---

### Task T5: Backend startup wiring of GatewayRouter (fail-closed boot)

**Files:**

- Modify: `backend/src/msai/main.py` (startup hook — search for `app.on_event("startup")` or lifespan)
- Modify: `backend/src/msai/live_supervisor/__main__.py:466` (already constructs `GatewayRouter`; ensure the constructor's `ValueError` propagates as a fatal startup error)
- Test: `backend/tests/integration/test_app_startup_gateway_config.py`

When `GATEWAY_CONFIG` is misconfigured (duplicate `ib_login_key`), the backend MUST exit non-zero within 10s of boot. Add an integration test that monkeypatches the env, calls the lifespan/startup hook, and asserts the failure surfaces.

- [ ] **T5.1: Write the failing test against the ACTUAL FastAPI lifespan** (Codex iter 1 P1-6)

The previous draft tested `GatewayRouter(...)` directly — that's a unit test of the parser, not a startup test. The real startup test must exercise the FastAPI lifespan at `backend/src/msai/main.py:224` (the `lifespan` async context manager). Today the lifespan does NOT instantiate `GatewayRouter` — fix that in T5.3 and assert via the lifespan in the test:

```python
# backend/tests/integration/test_app_startup_gateway_config.py
import pytest
from fastapi.testclient import TestClient

from msai.main import app, lifespan


@pytest.mark.asyncio
async def test_startup_fails_closed_on_duplicate_login(monkeypatch) -> None:
    monkeypatch.setenv(
        "GATEWAY_CONFIG",
        "marin1016test:host-a:4004,marin1016test:host-b:4005",
    )
    with pytest.raises(ValueError, match="duplicate ib_login_key"):
        async with lifespan(app):
            pass  # lifespan-enter MUST raise; we should never reach here


@pytest.mark.asyncio
async def test_startup_succeeds_on_valid_config(monkeypatch) -> None:
    monkeypatch.setenv(
        "GATEWAY_CONFIG",
        "marin1016test:ib-gateway-paper:4004:accounts=DUP733214|DUP733215",
    )
    async with lifespan(app):
        # Lifespan-enter completed; the app would now accept traffic. Sufficient
        # for this test — we are NOT exercising endpoints, only boot.
        pass
```

- [ ] **T5.2: Verify both tests fail until lifespan instantiates `GatewayRouter`**

```bash
cd backend && uv run pytest tests/integration/test_app_startup_gateway_config.py -v
```

- [ ] **T5.3: Add `GatewayRouter` instantiation to `backend/src/msai/main.py` lifespan (around l. 224)**

Inside the `lifespan` async context manager, BEFORE the existing `yield`, add:

```python
import os
from msai.services.live.gateway_router import GatewayRouter

# Fail-closed on misconfiguration — duplicate ib_login_key raises ValueError,
# which propagates and crashes the uvicorn process (council 2026-05-29 #13).
app.state.gateway_router = GatewayRouter(os.environ.get("GATEWAY_CONFIG"))
```

Make sure no surrounding `try/except` swallows the `ValueError`.

- [ ] **T5.4: Add a smoke probe in `live_supervisor/__main__.py:466`** — log at INFO level the parsed `(ib_login_key, host, port, accounts)` triples so misconfiguration is human-readable in operator logs.

- [ ] **T5.5: Run tests + commit**

```bash
cd backend && uv run pytest tests/integration/test_app_startup_gateway_config.py -v
git add backend/src/msai/main.py backend/src/msai/live_supervisor/__main__.py backend/tests/integration/test_app_startup_gateway_config.py
git commit -m "feat(startup): fail-closed on duplicate ib_login_key in GATEWAY_CONFIG"
```

---

### Task T6: `gateway_session_key` end-to-end propagation (publisher + handler + existing test stub)

**Files:**

- Modify: `backend/src/msai/api/live.py:1274` (the START-command publisher inside `/start-portfolio`) — publish `gateway_session_key=deployment.ib_login_key` in the START command payload (Codex iter 1 P1-3).
- Modify: `backend/src/msai/live_supervisor/main.py:63-76` (the START handler path) — extract `payload["gateway_session_key"]` and pass to `spawn`.
- Modify: `backend/tests/unit/test_live_supervisor_main.py:155` (existing `_NoopProcessManager.spawn` stub) — accept the new kwarg without breaking existing tests (Codex iter 1 P2-1).
- Test: `backend/tests/unit/live_supervisor/test_handle_command_propagation.py` (new — handler-side propagation).
- Test: extend an existing API-level test (or add a thin one) asserting the START command published by `/start-portfolio` carries `gateway_session_key` matching the deployment's `ib_login_key`.

Council blocking objection #12 + Codex iter 1 P1-3. Today the chain is broken on BOTH ends: the API `/start-portfolio` handler (`api/live.py:1274`) doesn't put `gateway_session_key` in the START command payload, AND `handle_command()` doesn't read it either (`live_supervisor/main.py:63-76`). Either gap means `ProcessManager.spawn` falls through to the DB-row lookup at `process_manager.py:528`, silently degrading per-session startup serialization to global. **PR 1 must fix BOTH ends** (the dispatch evidence from API → command bus → supervisor → spawn must carry the key end-to-end).

- [ ] **T6.1: Write the failing test**

```python
# backend/tests/unit/live_supervisor/test_handle_command_propagation.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from msai.live_supervisor.main import handle_command
from msai.services.live_command_bus import LiveCommand, LiveCommandType


@pytest.mark.asyncio
async def test_handle_command_start_passes_gateway_session_key_when_in_payload() -> None:
    pm = MagicMock()
    pm.spawn = AsyncMock(return_value=True)

    command = LiveCommand(
        entry_id="0-1",
        command_type=LiveCommandType.START,
        deployment_id="deploy-123",
        payload={
            "deployment_slug": "p-A",
            "gateway_session_key": "marin1016test",
        },
        idempotency_key="idem-1",
    )

    ack = await handle_command(command, process_manager=pm)

    assert ack is True
    pm.spawn.assert_awaited_once()
    kwargs = pm.spawn.await_args.kwargs
    assert kwargs.get("gateway_session_key") == "marin1016test"


@pytest.mark.asyncio
async def test_handle_command_start_omits_gateway_session_key_when_absent() -> None:
    # Backwards-compat: payload without the key still works (falls back to deployment row)
    pm = MagicMock()
    pm.spawn = AsyncMock(return_value=True)

    command = LiveCommand(
        entry_id="0-2",
        command_type=LiveCommandType.START,
        deployment_id="deploy-456",
        payload={"deployment_slug": "p-B"},
        idempotency_key="idem-2",
    )

    await handle_command(command, process_manager=pm)

    kwargs = pm.spawn.await_args.kwargs
    assert kwargs.get("gateway_session_key") is None
```

- [ ] **T6.2: Run test to verify it fails**

```bash
cd backend && uv run pytest tests/unit/live_supervisor/test_handle_command_propagation.py -v
```

- [ ] **T6.3a: Modify `handle_command()` in `backend/src/msai/live_supervisor/main.py:63-76`**

```python
if command.command_type is LiveCommandType.START:
    return await process_manager.spawn(
        deployment_id=command.deployment_id,
        deployment_slug=command.payload.get("deployment_slug", ""),
        payload=command.payload,
        idempotency_key=command.idempotency_key,
        gateway_session_key=command.payload.get("gateway_session_key"),
    )
```

- [ ] **T6.3b: Update the existing `_NoopProcessManager.spawn` stub** at `backend/tests/unit/test_live_supervisor_main.py:155` to accept `gateway_session_key: str | None = None` so existing tests continue to pass (Codex iter 1 P2-1).

- [ ] **T6.3c: Publish `gateway_session_key` from `/start-portfolio` in `backend/src/msai/api/live.py:1274`**

Inspect the START-command build path around `api/live.py:1274` (the LiveCommandBus.publish call inside `/start-portfolio`). Add `gateway_session_key=deployment.ib_login_key` to the payload dict. Concretely the new payload includes:

```python
payload = {
    "deployment_slug": deployment.deployment_slug,
    "gateway_session_key": deployment.ib_login_key,  # NEW (Codex iter 1 P1-3)
    # ...existing fields...
}
```

Then add a thin integration test asserting that when `/start-portfolio` succeeds, the LiveCommandBus stream contains a START command whose payload's `gateway_session_key` matches the deployment row's `ib_login_key`.

- [ ] **T6.4: Verify all tests pass + lint/types**

```bash
cd backend && uv run pytest tests/unit/live_supervisor/test_handle_command_propagation.py tests/unit/test_live_supervisor_main.py tests/integration/api/test_start_portfolio_publishes_gateway_session_key.py -v
cd backend && uv run ruff check src/msai/live_supervisor/main.py src/msai/api/live.py && uv run mypy src/msai/ --strict
```

- [ ] **T6.5: Commit**

```bash
git add backend/src/msai/live_supervisor/main.py backend/src/msai/api/live.py backend/tests/unit/live_supervisor/test_handle_command_propagation.py backend/tests/unit/test_live_supervisor_main.py backend/tests/integration/api/test_start_portfolio_publishes_gateway_session_key.py
git commit -m "fix(live): gateway_session_key end-to-end from /start-portfolio through handle_command to spawn"
```

---

### Task T7: Account-scoped command stream helper

**Files:**

- Modify: `backend/src/msai/services/live_command_bus.py` (around l. 66-76)
- Test: `backend/tests/unit/services/test_live_command_bus_account_stream.py`

Add a stream-name helper. Caller-driven; existing global `LIVE_COMMAND_STREAM` stays for backwards-compat during PR 1.

- [ ] **T7.1: Write the failing test** (3-line test asserting the helper exists)
- [ ] **T7.2: Verify fail, add helper:**

```python
def command_stream_for_account(account_id: str | None) -> str:
    if not account_id:
        return LIVE_COMMAND_STREAM
    return f"{LIVE_COMMAND_STREAM}:{account_id}"
```

- [ ] **T7.3: Test + lint + commit.**

**Scope clarification (Codex iter 1 P2-4):** PR 1 lands ONLY the helper. No producer or consumer in PR 1 calls `command_stream_for_account()` — `/start-portfolio` still publishes to the legacy global `LIVE_COMMAND_STREAM`, and the supervisor still consumes from there. The actual switchover happens in PR 2 alongside the per-account-supervisor ownership refactor; deferring is intentional because switching the stream name requires migrating in-flight commands, a coupled change that doesn't belong in PR 1. The plan acknowledges that PR 1 therefore does NOT prove account-scoped command routing — only that the helper exists and is correct. The drill (UC1–UC3) does not depend on account-scoped streams; it depends on account-scoped halt keys (T1) + per-account TradingNode topology (T11). The Hawk's minority report already names the multi-container property as not-proved-here; this is the same posture for account-scoped command routing.

---

### Task T8: Account-scoped drain endpoint

**Files:**

- Modify: `backend/src/msai/api/live.py` (add new endpoint)
- Test: `backend/tests/integration/api/test_live_drain_account.py`

`POST /api/v1/live/drain/{account_id}` drains all deployments matching `account_id` while leaving deployments under other `account_id`s untouched (Codex iter 1 P1-1 — drain MUST key by `account_id`, NOT by shared `ib_login_key`, otherwise two sub-accounts under one TWS login collapse to the same halt latch). Writes `account_halt_key(account_id)` with `halt_cause_key("account", account_id=account_id)` companion holding `HaltCause.OPERATOR_DRAIN`.

- [ ] **T8.1: Write the failing integration test**

Use the existing project fixture conventions (Codex iter 1 P2-2):

- Existing test factory: `backend/tests/integration/_deployment_factory.py:52` (`make_live_deployment`) — use this directly. Do NOT invent a `deployment_factory` fixture.
- Existing auth pattern: tests typically pass the dev `X-API-Key` via test client setup; check `backend/tests/integration/conftest.py` + `backend/tests/integration/api/conftest.py` for the actual client builder. Do NOT invent an `auth_headers` fixture.
- Redis access: tests typically use `redis.asyncio.Redis.from_url(settings.redis_url)` directly. Check `backend/tests/integration/conftest.py` for the conventional fixture (it may be named `redis` or be a fresh-per-test instance).

```python
# backend/tests/integration/api/test_live_drain_account.py
# Follow the isolated-DB/isolated-Redis pattern from
# backend/tests/integration/test_live_start_endpoints.py (verified
# fixtures at l. 62-119). The dispatched subagent reuses those names
# directly. Codex iter 3 P2.
import json
import pytest

from msai.core.halt_keys import account_halt_key, halt_cause_key, HaltCause


@pytest.mark.asyncio
async def test_drain_account_a_leaves_account_b_running(
    client,           # AsyncClient with the test-app override
    session_factory,  # async sessionmaker against isolated_postgres_url
    redis_text,       # AsyncRedis (decode_responses=True), flushed per test
) -> None:
    # Arrange — use the existing make_live_deployment factory from
    # backend/tests/integration/_deployment_factory.py:52. It REQUIRES a
    # session arg (Codex iter 2 P2-2).
    from tests.integration._deployment_factory import make_live_deployment

    async with session_factory() as session:
        dep_a = await make_live_deployment(
            session, ib_login_key="marin1016test", account_id="DUP733214"
        )
        dep_b = await make_live_deployment(
            session, ib_login_key="marin1016test", account_id="DUP733215"
        )
        await session.commit()

    # Act
    resp = await client.post(f"/api/v1/live/drain/{dep_a.account_id}")
    assert resp.status_code == 200

    # Assert — A drained, B alive. LiveDeploymentInfo uses ``status``,
    # NOT ``state`` (backend/src/msai/schemas/live.py:46). Codex iter 2 P2-2.
    status = (await client.get("/api/v1/live/status")).json()
    rows = {d["account_id"]: d for d in status["deployments"]}
    assert rows["DUP733214"]["status"] in {"draining", "stopped"}
    assert rows["DUP733215"]["status"] == "running"

    # Assert — account-scoped halt + cause companion present, NOT fleet-wide
    assert await redis_text.exists(account_halt_key("DUP733214"))
    assert not await redis_text.exists(account_halt_key("DUP733215"))
    assert not await redis_text.exists("msai:risk:halt")  # fleet key MUST NOT be set
    cause = json.loads(
        await redis_text.get(halt_cause_key("account", account_id="DUP733214"))
    )
    assert cause["reason"] == HaltCause.OPERATOR_DRAIN.value
```

- [ ] **T8.2: Verify fail; implement the endpoint**

In `api/live.py`, add a handler near `/kill-all` (around l. 1633). The handler:

1. Looks up active `LiveDeployment` rows where `account_id == path_arg`.
2. For each row, send a STOP command on the LiveCommandBus (existing pattern).
3. Set `account_halt_key(deployment.account_id)` in Redis (keyed by `account_id` — NOT by shared `ib_login_key` — per the T1 helper signature and Codex iter 1 P1-1) with `halt_cause_key("account", account_id=deployment.account_id)` companion holding `HaltCause.OPERATOR_DRAIN`.

- [ ] **T8.3: Test + lint + commit.**

---

### Task T9: Bidirectional symbology shim

**Files:**

- Create: `backend/src/msai/services/symbology_shim.py`
- Test: `backend/tests/unit/services/test_symbology_shim.py`

**Outbound** (strategy → Databento subscription): `async resolve_for_databento(canonical: InstrumentId, *, session: AsyncSession, as_of_date: date) -> DatabentoSubscriptionTarget(dataset, native_symbol, native_venue)`. Uses the existing async `lookup_for_live` function at `backend/src/msai/services/nautilus/security_master/live_resolver.py:447`, whose verified signature is `async def lookup_for_live(symbols, *, as_of_date, session, provider="interactive_brokers")` returning `list[ResolvedInstrument]` order-preserved (Codex iter 3 P1 + iter 5 residual). Each item is a `ResolvedInstrument` (verified shape at `live_resolver.py:127`): `canonical_id: str`, `asset_class: AssetClass`, `contract_spec: dict[str, Any]`, `effective_window: tuple[date, date | None]`. **`ResolvedInstrument` does NOT have a `raw_symbol` attribute** (Codex iter 2 P1-3) — `raw_symbol` is on the underlying `InstrumentDefinition` row, accessed inside the resolver. **PR 1 scope is equities-only** (Codex iter 5 P2): for AAPL/SPY/etc., `contract_spec["symbol"]` correctly carries the trading symbol. **Futures are explicitly out of scope** for the shim in PR 1 because `contract_spec["symbol"]` carries the root (`"ES"`) — NOT the active contract (`"ESM6"`) — and the resolver's contract-binding for futures lives in `canonical_id`/alias, not the dict. Futures shim semantics deferred to a follow-up PR.

**Inbound** (Databento bar → strategy-visible bar): `retag_inbound_bar(bar: Bar, *, audit_metadata_sink: Callable[[dict[str, Any]], None] | None = None) -> Bar` returning a Bar whose `instrument_id.venue == Venue("IBKR")`. The `audit_metadata_sink`, if provided, is called with a dict `{provider, dataset, native_venue, native_symbol, original_instrument_id, ts_event}` so audit info isn't lost in the re-tag. Same signature used by `SymbologyShimActor` in T11 (Codex iter 6 P2 — signatures synchronised across T9 and T11).

This shim sits OUTSIDE the Nautilus Databento adapter (research brief §2 — `venue_dataset_map` is a dataset alias, not a venue rename).

- [ ] **T9.1: Write the failing test (ASYNC — matches the implementation's async signature, Codex iter 2 P1-3)**

```python
# backend/tests/unit/services/test_symbology_shim.py
from datetime import date

import pytest

from msai.services.symbology_shim import (
    DatabentoSubscriptionTarget,
    resolve_for_databento,
    retag_inbound_bar,
)


@pytest.fixture
def registry_session_with_aapl_es():
    # Project-local fixture; uses the existing security_master test fixtures
    # (see backend/tests/integration/conftest_symbol_onboarding.py + the
    # security_master test fixture conventions). Yields an AsyncSession with
    # AAPL (asset_class=equity, raw_symbol=AAPL) and ES (asset_class=future,
    # raw_symbol=ES, with appropriate contract_spec) registered.
    ...


@pytest.mark.asyncio
async def test_outbound_resolves_aapl_to_dbeq(registry_session_with_aapl_es) -> None:
    from nautilus_trader.model.identifiers import InstrumentId
    target = await resolve_for_databento(
        InstrumentId.from_str("AAPL.IBKR"),
        session=registry_session_with_aapl_es,
        as_of_date=date(2026, 5, 29),
    )
    assert isinstance(target, DatabentoSubscriptionTarget)
    assert target.dataset == "DBEQ.BASIC"
    assert target.native_symbol == "AAPL"
    assert target.native_venue == "XNAS"


@pytest.mark.asyncio
async def test_outbound_futures_raises_not_implemented(registry_session_with_aapl_es) -> None:
    # PR 1 scope is equities-only (Codex iter 5 P2 + iter 6 P1-1). Futures
    # raise NotImplementedError until the contract_spec ambiguity is fixed.
    from nautilus_trader.model.identifiers import InstrumentId
    with pytest.raises(NotImplementedError, match="equities only"):
        await resolve_for_databento(
            InstrumentId.from_str("ES.IBKR"),
            session=registry_session_with_aapl_es,
            as_of_date=date(2026, 5, 29),
        )


def test_inbound_retag_preserves_dataset_in_metadata() -> None:
    # Build a Bar with native venue XNAS, run through retag_inbound_bar,
    # assert the returned Bar has venue=IBKR and the audit-metadata dict
    # has provider="databento", dataset="DBEQ.BASIC", native_venue="XNAS".
    # (sync — retag_inbound_bar is a pure transformation on already-loaded events,
    # no DB I/O required)
    ...
```

- [ ] **T9.2: Verify fail. Implement the shim against the ACTUAL security_master API**

Codex iter 1 P1-4 + Claude review: the `SecurityMaster` class lives at `backend/src/msai/services/nautilus/security_master/service.py:206` (NOT at `msai.services.security_master`). The existing live-resolution surface is `lookup_for_live` / `ResolvedInstrument` at `backend/src/msai/services/nautilus/security_master/live_resolver.py:126`, and the registry's instrument-definitions schema uses `raw_symbol` + `asset_class` + `provider` columns (NOT `native_symbol` / `asset_class.value` — those were hallucinated). The subagent for T9 MUST:

1. Read `backend/src/msai/services/nautilus/security_master/service.py:206-450` (SecurityMaster class signature + resolve/resolve_for_backtest methods).
2. Read `backend/src/msai/services/nautilus/security_master/live_resolver.py:126` and the surrounding `lookup_for_live` function to learn the canonical live-side resolution shape and what `ResolvedInstrument` carries.
3. Read `backend/src/msai/services/nautilus/security_master/registry.py` for the `instrument_definitions`/`instrument_aliases` lookup primitives.
4. Read `backend/src/msai/services/nautilus/security_master/specs.py:77` to see `InstrumentSpec` (the input type to `resolve`).
5. Read `backend/src/msai/services/nautilus/security_master/venue_normalization.py` to see how canonical `.IBKR` venue stripping works today.

Then implement the shim using THOSE primitives — typically by calling `lookup_for_live(...)` (which returns `list[ResolvedInstrument]` order-preserved per `live_resolver.py:453` — NOT a mapping; Codex iter 4 P2), then mapping `asset_class` → Databento `(dataset, native_venue)` via the table below. The shim sits at `backend/src/msai/services/symbology_shim.py` (sibling to the other services modules; NOT inside the nautilus/security_master subdirectory because the shim is broader than the registry — it's the boundary translator between strategy canonical IDs and Databento native).

```python
# backend/src/msai/services/symbology_shim.py (shape — finalize against the real API)
from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.model.identifiers import InstrumentId, Venue

# Verified imports (Codex iter 3 P1):
from datetime import date
from sqlalchemy.ext.asyncio import AsyncSession

# IMPORTANT: AssetClass is defined in live_resolver.py:102, NOT specs.py.
# Members: EQUITY, FUTURES (note plural), FX, OPTION, CRYPTO.
from msai.services.nautilus.security_master.live_resolver import (
    AssetClass,
    lookup_for_live,
    ResolvedInstrument,  # has: canonical_id, asset_class, contract_spec, effective_window
)

# AssetClass enum member → Databento (dataset, native_venue).
# PR 1 scope is EQUITIES ONLY (Codex iter 5 P2 + iter 6 P1-1).
# Futures/option/fx entries kept commented for the follow-up PR; resolve_for_databento
# raises NotImplementedError for any asset_class except EQUITY.
_ASSET_CLASS_TO_DATABENTO: dict[AssetClass, tuple[str, str]] = {
    AssetClass.EQUITY: ("DBEQ.BASIC", "XNAS"),
    # AssetClass.FUTURES: ("GLBX.MDP3", "GLBX"),  # deferred — contract_spec['symbol'] is root, not active contract
    # AssetClass.OPTION:  ("OPRA.PILLAR", "OPRA"),  # deferred per addendum
    # AssetClass.FX:      TBD — not in PR 1 scope
}


@dataclass(frozen=True)
class DatabentoSubscriptionTarget:
    dataset: str
    native_symbol: str
    native_venue: str


async def resolve_for_databento(
    canonical: InstrumentId,
    *,
    session: AsyncSession,
    as_of_date: date,
) -> DatabentoSubscriptionTarget:
    """Map a strategy-canonical ``<SYM>.IBKR`` to Databento ``(dataset, symbol, venue)``.

    Uses ``lookup_for_live`` (the existing async live-resolution function at
    ``live_resolver.py:447``). The shim consumes that result and maps
    ``AssetClass`` to the Databento dataset + native venue.
    """
    if canonical.venue != Venue("IBKR"):
        raise ValueError(
            f"resolve_for_databento expects canonical .IBKR venue; got {canonical.venue}"
        )
    raw_symbol_key = str(canonical.symbol)  # e.g. "AAPL" from "AAPL.IBKR"
    # lookup_for_live returns list[ResolvedInstrument] order-preserved per
    # live_resolver.py:453 (Codex iter 3 P1 fix).
    resolved_items: list[ResolvedInstrument] = await lookup_for_live(
        [raw_symbol_key],
        as_of_date=as_of_date,
        session=session,
    )
    resolved = resolved_items[0]
    # PR 1 scope: equities only (Codex iter 5 P2). Reject any non-equity asset
    # class explicitly with a clear error pointing at the deferred work, so
    # callers fail fast instead of silently emitting bad subscriptions.
    if resolved.asset_class != AssetClass.EQUITY:
        raise NotImplementedError(
            f"resolve_for_databento PR 1 scope is equities only; got "
            f"asset_class={resolved.asset_class!r} for {canonical}. Futures "
            f"shim semantics deferred to a follow-up PR (contract_spec['symbol']"
            f" carries the root, not the active contract — bridge work required)."
        )
    if resolved.asset_class not in _ASSET_CLASS_TO_DATABENTO:
        raise ValueError(
            f"no Databento dataset for asset_class={resolved.asset_class!r}"
        )
    dataset, native_venue = _ASSET_CLASS_TO_DATABENTO[resolved.asset_class]
    # contract_spec["symbol"] is filled by the resolver at live_resolver.py:332/:352
    return DatabentoSubscriptionTarget(
        dataset=dataset,
        native_symbol=str(resolved.contract_spec["symbol"]),
        native_venue=native_venue,
    )
    #   raw_symbol = resolved.raw_symbol
    #
    # Then map asset_class_str → (dataset, native_venue) via _ASSET_CLASS_TO_DATABENTO.
    ...
```

The subagent fills in the `...` body using the real `security_master` API. If `lookup_for_live` is async, this function is async (note `async def`).

```python
from typing import Any, Callable

def retag_inbound_bar(
    bar,
    *,
    audit_metadata_sink: Callable[[dict[str, Any]], None] | None = None,
) -> Any:  # returns nautilus_trader.model.data.Bar
    """Re-tag a Databento Bar's venue suffix to IBKR; optionally emit audit.

    Implementation note: Nautilus ``Bar`` is frozen. Reconstruct with a new
    ``bar_type`` whose ``instrument_id.venue == Venue("IBKR")``. When
    ``audit_metadata_sink`` is provided, the callable receives a dict of
    ``{provider, dataset, native_venue, native_symbol, original_instrument_id,
    ts_event}``. T11 wires this via ``SymbologyShimActor`` whose handler passes
    a default sink that pushes to the project's audit log.
    """
    ...
```

- [ ] **T9.3: Test + lint + commit.**

---

### Task T10: Databento live-config builder

**Files:**

- Create: `backend/src/msai/services/nautilus/databento_live_config.py`
- Test: `backend/tests/unit/services/nautilus/test_databento_live_config.py`

Helper that builds `DatabentoDataClientConfig` for a given list of strategy-visible `<SYM>.IBKR` IDs. Resolves each via the symbology shim (Task T9), pre-populates `instrument_ids` in NATIVE venue form (e.g. `AAPL.XNAS`), pins `reconnect_timeout_mins=10`.

- [ ] **T10.1: Write the failing tests for BOTH halves of the split builder (Codex iter 4 P1 + iter 5 residual)**

```python
# backend/tests/unit/services/nautilus/test_databento_live_config.py
from datetime import date

import pytest

from msai.services.nautilus.databento_live_config import (
    ResolvedDatabentoTargets,
    build_databento_data_client_config,    # SYNC — subprocess path
    resolve_databento_targets,             # ASYNC — supervisor path
)


# --- Group A: async resolver (supervisor path) ---

@pytest.mark.asyncio
async def test_resolver_returns_native_ids_and_venue_dataset_map(
    registry_session_with_aapl_spy,
) -> None:
    targets = await resolve_databento_targets(
        canonical_ids=["AAPL.IBKR", "SPY.IBKR"],
        session=registry_session_with_aapl_spy,
        as_of_date=date(2026, 5, 29),
    )
    assert isinstance(targets, ResolvedDatabentoTargets)
    assert set(targets.native_instrument_ids) == {"AAPL.XNAS", "SPY.XNAS"}
    assert targets.venue_dataset_map == {"XNAS": "DBEQ.BASIC"}


# --- Group B: sync builder (subprocess path; no DB) ---

def test_config_pins_reconnect_timeout_to_10_min() -> None:
    config = build_databento_data_client_config(
        native_instrument_ids=["AAPL.XNAS", "SPY.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        api_key="dbn-test-key",
    )
    assert config.reconnect_timeout_mins == 10


def test_config_pre_populates_native_instrument_ids() -> None:
    config = build_databento_data_client_config(
        native_instrument_ids=["AAPL.XNAS", "SPY.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        api_key="dbn-test-key",
    )
    native_ids = {str(iid) for iid in config.instrument_ids}
    assert "AAPL.XNAS" in native_ids
    assert "SPY.XNAS" in native_ids


def test_config_includes_authoritative_venue_dataset_map() -> None:
    # Codex iter 4 P2-2: builder MUST populate venue_dataset_map so Nautilus
    # uses our authoritative dataset choice rather than defaulting via the
    # publisher lookup.
    config = build_databento_data_client_config(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        api_key="k",
    )
    assert config.venue_dataset_map == {"XNAS": "DBEQ.BASIC"}


def test_config_uses_exchange_as_venue_default() -> None:
    config = build_databento_data_client_config(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        api_key="k",
    )
    assert config.use_exchange_as_venue is True
```

- [ ] **T10.2: Verify fail. Implement the builder.**

**Codex iter 4 P1 + P2-2: SPLIT the builder into an async resolver (supervisor path) and a sync config builder (subprocess path).** The TradingNode subprocess at `trading_node_subprocess.py:675` calls `node_factory(payload)` synchronously without a DB session — so the Databento resolution MUST happen earlier in the supervisor's async `_build_production_payload_factory` (which has DB access), and the pre-resolved `(native_ids, venue_dataset_map)` flow through the START command payload. Additionally, T10 must populate `venue_dataset_map` from the shim's outputs so Nautilus actually USES our shim's dataset choice rather than defaulting (Codex iter 4 P2-2).

```python
# backend/src/msai/services/nautilus/databento_live_config.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from nautilus_trader.adapters.databento.config import DatabentoDataClientConfig
from nautilus_trader.model.identifiers import InstrumentId
from sqlalchemy.ext.asyncio import AsyncSession

from msai.services.symbology_shim import resolve_for_databento


@dataclass(frozen=True)
class ResolvedDatabentoTargets:
    """Already-resolved targets passed through the START command payload.

    Built in the supervisor's async path; consumed in the subprocess's
    sync path. Serializable to JSON via __dict__ (str-valued fields only)
    because the START command payload is a JSON-able dict at the bus level.
    """

    native_instrument_ids: list[str]  # e.g. ["AAPL.XNAS", "SPY.XNAS"]
    venue_dataset_map: dict[str, str]  # e.g. {"XNAS": "DBEQ.BASIC"}


async def resolve_databento_targets(
    *,
    canonical_ids: list[str],
    session: AsyncSession,
    as_of_date: date,
) -> ResolvedDatabentoTargets:
    """Async: resolve canonical .IBKR symbols → Databento native targets.

    Runs in the supervisor's _build_production_payload_factory (which has
    an AsyncSession). The result is serialized into the START command
    payload as ``native_instrument_ids`` + ``venue_dataset_map``.
    """
    native_ids: list[str] = []
    venue_to_dataset: dict[str, str] = {}
    for canonical_str in canonical_ids:
        target = await resolve_for_databento(
            InstrumentId.from_str(canonical_str),
            session=session,
            as_of_date=as_of_date,
        )
        native_ids.append(f"{target.native_symbol}.{target.native_venue}")
        # Authoritative: bind native_venue → dataset so Nautilus uses our
        # shim's choice rather than the default publisher lookup (Codex iter 4 P2-2).
        venue_to_dataset[target.native_venue] = target.dataset
    return ResolvedDatabentoTargets(
        native_instrument_ids=native_ids,
        venue_dataset_map=venue_to_dataset,
    )


def build_databento_data_client_config(
    *,
    native_instrument_ids: list[str],
    venue_dataset_map: dict[str, str],
    api_key: str,
) -> DatabentoDataClientConfig:
    """Sync: assemble the Databento client config from pre-resolved targets.

    Runs in the TradingNode subprocess (which has NO DB session). All
    resolution happens upstream in resolve_databento_targets.
    """
    return DatabentoDataClientConfig(
        api_key=api_key,
        instrument_ids=[InstrumentId.from_str(s) for s in native_instrument_ids],
        venue_dataset_map=venue_dataset_map,
        reconnect_timeout_mins=10,  # PR 1 pins explicitly (council 2026-05-29)
        use_exchange_as_venue=True,
    )
```

Update T10.1 tests to cover BOTH halves: an async test for `resolve_databento_targets` (DB-backed); a sync test for `build_databento_data_client_config` (pure-function, no fixtures). The async test should assert `venue_dataset_map={"XNAS": "DBEQ.BASIC"}` (the shim's authoritative dataset choice).

- [ ] **T10.3: Test + lint + commit.**

---

### Task T11: Per-account `TradingNodeConfig` builder (data=Databento, exec=IB exec-only)

**Files:**

- Modify: `backend/src/msai/services/nautilus/live_node_config.py` (surgical edits, ~688 LOC)
- Modify: `backend/src/msai/services/nautilus/trading_node_subprocess.py` (the subprocess entrypoint that consumes the config)
- Test: `backend/tests/unit/services/nautilus/test_live_node_config_split_topology.py`

The per-account config builder now produces a `TradingNodeConfig` whose `data_clients={DATABENTO_CLIENT_KEY: DatabentoDataClientConfig(...)}` and `exec_clients={IB_VENUE.value: InteractiveBrokersExecClientConfig(account_id, ibg_client_id, ...)}`. **Use the existing `IB_VENUE.value` constant from the codebase** — currently `"INTERACTIVE_BROKERS"` per `backend/src/msai/services/nautilus/live_node_config.py:445` (Codex iter 1 P2-3). Existing tests at `backend/tests/unit/test_live_node_config.py:278` already assert against `IB_VENUE.value` — do NOT break those. Similarly for `DATABENTO`: use the existing constant `DATABENTO_CLIENT_KEY` if defined elsewhere in the codebase, or import directly from `nautilus_trader.adapters.databento.constants` (the canonical Nautilus constant). No IB data client. Asserts at build time that no IB data client is wired.

**`ibg_client_id` source-of-truth (Codex iter 1 P1-5):** today `live_node_config.py:179` derives `ibg_client_id` privately from the deployment_slug hash. The /live/status endpoint + CLI + UI (Task T14) need to surface this value, but neither `LiveDeployment` nor `LiveNodeProcess` persists it. The cheapest fix is to **extract the derivation into a shared helper** at `backend/src/msai/services/nautilus/ibg_client_id.py` (e.g., `derive_ibg_client_id(deployment_slug: str) -> int`), then call it from both `live_node_config.py:179` (where the value is consumed) AND from a new `/live/status` serializer field (where the value is surfaced). Persisting the value on `LiveDeployment` is an alternative — but it adds a migration, so a deterministic re-derivation helper is preferred for PR 1. The subagent for T11 creates the helper and migrates the private derivation; the subagent for T14 consumes it from the status path.

**Symbology shim production hook — TWO-WAY bridge (Codex iter 5 P1 + iter 6 P1-2 + iter 7 P1).** The shim has TWO responsibilities, not just inbound retagging:

- **Outbound subscription hook (Codex iter 7 P1).** Strategies subscribe to `AAPL.IBKR-1-MINUTE-LAST-EXTERNAL`. The Nautilus data-engine routes that subscription to the data client that owns the `IBKR` venue — but our Databento client owns `XNAS`/`GLBX`/etc. (the native venues from `venue_dataset_map`). Without an outbound bridge, the subscription never reaches Databento and no bars ever arrive. The shim actor's `on_start()` MUST eagerly subscribe to the NATIVE bar types corresponding to the configured canonical universe (passed via config as `canonical_to_native_bar_types: dict[str, str]` — e.g. `{"AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"}`). Concretely the actor calls `self.subscribe_bars(BarType.from_str(native_str))` for each value in that map, which routes through Nautilus's data-engine to the Databento client. The native subscriptions are what actually cause Databento to stream bars.
- **Inbound retagging.** When native bars arrive on the bus (because of the outbound subscriptions above), the actor's handler calls `retag_inbound_bar(bar, audit_metadata_sink=self._audit_sink)` and publishes the result on the canonical `.IBKR` topic where the strategy's bus subscription picks it up.

Together: actor subscribes native at on_start → Databento streams native bars → actor receives + retags to canonical → strategy receives canonical bars. UC1 only works end-to-end if BOTH halves are wired.

**Concrete deliverables (no prose-only assertions):**

1. Create `backend/src/msai/services/symbology_shim_actor.py` defining a `SymbologyShimActor` subclass of `nautilus_trader.common.actor.Actor` (or whatever Nautilus 1.223.0 calls the "lightweight component with bus access" primitive — the subagent reads `nautilus_trader/common/actor.py` to confirm). Config schema:
   ```python
   class SymbologyShimActorConfig(ActorConfig):
       canonical_to_native_bar_types: dict[str, str]   # outbound: "AAPL.IBKR-1-MIN-LAST-EXTERNAL" -> "AAPL.XNAS-1-MIN-LAST-EXTERNAL"
       venue_dataset_map: dict[str, str]               # inbound audit: native_venue -> dataset
   ```
   Behavior:
   - `on_start()`: iterates `canonical_to_native_bar_types.values()` and calls `self.subscribe_bars(BarType.from_str(...))` for each. The data-engine routes those to the Databento client (which has `XNAS` etc. in `venue_dataset_map`).
   - On bar event (native): looks up canonical from the inverse map, calls `retag_inbound_bar(bar, audit_metadata_sink=self._audit_sink)`, publishes on the canonical bar topic.
2. Register the actor in `TradingNodeConfig.actors=[ImportableActorConfig(actor_path="msai.services.symbology_shim_actor:SymbologyShimActor", config_path="msai.services.symbology_shim_actor:SymbologyShimActorConfig", config={"canonical_to_native_bar_types": <map>, "venue_dataset_map": <the resolved map>})]`. Note: `TradingNodeConfig.actors` is `list[ImportableActorConfig]` — assert on `actor_path/config_path/config`, NOT on a raw actor instance.
3. The supervisor's payload factory (T12) builds `canonical_to_native_bar_types` from the strategy universe + the resolved `native_instrument_ids` mapping. This is a NEW field in the START command payload, passed alongside `native_instrument_ids` and `venue_dataset_map`.
4. THREE failing tests in T11.1 (see code block below): (a) `test_symbology_shim_actor_is_wired` — `actor_path` in config with `canonical_to_native_bar_types` + `venue_dataset_map` injected; (b) `test_symbology_shim_actor_subscribes_native_bars_on_start` — when `on_start()` is invoked, the actor calls `subscribe_bars` for each native bar_type; (c) `test_symbology_shim_actor_retags_bar_to_ibkr` — synthetic XNAS Bar published on the bus reaches a subscriber on the canonical `.IBKR` topic.

The dispatched subagent reads `nautilus_trader/common/actor.py` for the actual `Actor` API + `MessageBus.subscribe`/`MessageBus.publish` for the bus surface; if `Actor` isn't the right primitive, the subagent uses whatever Nautilus 1.223.0 provides for "transform events on the bus" (alternatives: subclass `LiveDataEngine` and add a pre-publish hook). The dispatch plan adds the new file under T11's `Writes`.

- [ ] **T11.1: Write the failing test — `build_per_account_trading_node_config` is SYNC (Codex iter 4 P1).** The subprocess's `node_factory(payload)` at `trading_node_subprocess.py:675` is synchronous and cannot await; native_ids + venue_dataset_map come pre-resolved via the payload. Test fixture is plain in-memory dicts (no DB). **Two test groups:** (1) data_clients / exec_clients shape; (2) actors list includes `SymbologyShimActor` with the venue_dataset_map injected.

```python
# backend/tests/unit/services/nautilus/test_live_node_config_split_topology.py
from datetime import date

import pytest

from msai.services.nautilus.live_node_config import build_per_account_trading_node_config


def test_data_clients_contains_only_databento() -> None:
    config = build_per_account_trading_node_config(
        account_id="DUP733214",
        ibg_client_id=210,
        ib_login_key="marin1016test",
        native_instrument_ids=["AAPL.XNAS"],   # pre-resolved by supervisor
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
        databento_api_key="k",
        ib_host="ib-gateway",
        ib_port=4002,
    )
    # DATABENTO_CLIENT_KEY is the existing Nautilus constant
    # from nautilus_trader.adapters.databento.constants.
    from nautilus_trader.adapters.databento.constants import DATABENTO_CLIENT_ID
    assert set(config.data_clients.keys()) == {str(DATABENTO_CLIENT_ID)}


def test_exec_clients_contains_only_ib_with_account_id() -> None:
    config = build_per_account_trading_node_config(
        account_id="DUP733215",
        ibg_client_id=215,
        ib_login_key="marin1016test",
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
        databento_api_key="k",
        ib_host="ib-gateway",
        ib_port=4002,
    )
    # Codex iter 1 P2-3: existing code uses IB_VENUE.value
    # ("INTERACTIVE_BROKERS"), not the string "IB".
    from msai.services.nautilus.live_node_config import IB_VENUE  # adjust import if symbol moved
    assert set(config.exec_clients.keys()) == {IB_VENUE.value}
    ib_cfg = config.exec_clients[IB_VENUE.value]
    assert ib_cfg.account_id == "DUP733215"
    assert ib_cfg.ibg_client_id == 215


def test_symbology_shim_actor_is_wired() -> None:
    # Codex iter 6 P1-2: assert SymbologyShimActor lands in actors list.
    # TradingNodeConfig.actors is list[ImportableActorConfig]; check the
    # actor_path / config_path / config fields, NOT a raw actor instance.
    config = build_per_account_trading_node_config(
        account_id="DUP733214", ibg_client_id=214,
        ib_login_key="marin1016test", native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
        databento_api_key="k", ib_host="ib-gateway", ib_port=4002,
    )
    actor_paths = [a.actor_path for a in config.actors]
    assert any("SymbologyShimActor" in p for p in actor_paths)
    shim_cfg = next(a for a in config.actors if "SymbologyShimActor" in a.actor_path).config
    assert shim_cfg["venue_dataset_map"] == {"XNAS": "DBEQ.BASIC"}
    assert shim_cfg["canonical_to_native_bar_types"] == {
        "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
    }


def test_symbology_shim_actor_subscribes_native_bars_on_start(monkeypatch) -> None:
    # Codex iter 7 P1 (load-bearing): on_start MUST subscribe to NATIVE
    # bar types so the Databento data client streams them. Without this,
    # no bars ever arrive and the inbound retag test never gets input
    # in production. The dispatched subagent reads nautilus_trader/common/
    # actor.py to confirm the real Actor lifecycle method name.
    from msai.services.symbology_shim_actor import SymbologyShimActor, SymbologyShimActorConfig
    from nautilus_trader.model.data import BarType

    subscribed: list[BarType] = []
    actor = SymbologyShimActor(
        config=SymbologyShimActorConfig(
            canonical_to_native_bar_types={
                "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
            },
            venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        )
    )
    monkeypatch.setattr(actor, "subscribe_bars", subscribed.append)
    actor.on_start()  # or whatever Nautilus calls

    assert any(
        str(bt) == "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL" for bt in subscribed
    )


@pytest.mark.asyncio
async def test_symbology_shim_actor_retags_bar_to_ibkr() -> None:
    # Codex iter 6 P1-2 (load-bearing): when a native XNAS Bar arrives on
    # the bus, the actor's handler MUST republish it as .IBKR so the
    # strategy receives it.
    from msai.services.symbology_shim_actor import SymbologyShimActor
    bus = _build_test_message_bus()
    actor = SymbologyShimActor(config=_actor_config(
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
    ))
    actor.register_base(bus)

    received: list[Any] = []
    bus.subscribe("data.bars.AAPL.IBKR-1-MINUTE-LAST-EXTERNAL", received.append)

    synthetic_native_bar = _build_synthetic_bar("AAPL.XNAS", price=200.0)
    bus.publish("data.bars.AAPL.XNAS-1-MINUTE-LAST-EXTERNAL", synthetic_native_bar)

    assert len(received) == 1
    assert str(received[0].bar_type.instrument_id.venue) == "IBKR"


def test_two_accounts_get_distinct_ibg_client_ids() -> None:
    _bar_types = {"AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"}
    cfg_a = build_per_account_trading_node_config(
        account_id="DUP733214", ibg_client_id=214,
        ib_login_key="marin1016test", native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        canonical_to_native_bar_types=_bar_types,
        databento_api_key="k", ib_host="ib-gateway", ib_port=4002,
    )
    cfg_b = build_per_account_trading_node_config(
        account_id="DUP733215", ibg_client_id=215,
        ib_login_key="marin1016test", native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        canonical_to_native_bar_types=_bar_types,
        databento_api_key="k", ib_host="ib-gateway", ib_port=4002,
    )
    from msai.services.nautilus.live_node_config import IB_VENUE
    assert (
        cfg_a.exec_clients[IB_VENUE.value].ibg_client_id
        != cfg_b.exec_clients[IB_VENUE.value].ibg_client_id
    )
```

- [ ] **T11.2: Verify fail. Implement the SYNC builder in `live_node_config.py`.**

The exact edits depend on the existing 688-line structure of `live_node_config.py`. The dispatched subagent should:

1. Read the file in full.
2. Identify the existing `build_*_trading_node_config` function(s).
3. Either extend them with `account_id` + `ibg_client_id` + `native_instrument_ids` + `venue_dataset_map` + `canonical_to_native_bar_types` params and the data-client swap, OR add a new SYNC `build_per_account_trading_node_config(...)` that subsumes them and migrate callers in `__main__.py`. The builder MUST pass `canonical_to_native_bar_types` into the `ImportableActorConfig` for `SymbologyShimActor`; without it, on_start has no native bar types to subscribe (Codex iter 8 P1 — T12 propagation residual of iter 7).
4. Import + use the SYNC half of T10's split builder (`build_databento_data_client_config`).
5. Use `InteractiveBrokersExecClientConfig` (no IB data client).
6. Use `DatabentoLiveDataClientFactory` registration for the data-client side.
7. **DO NOT call any async DB-resolution code in this builder** — the subprocess at `trading_node_subprocess.py:675` (`node_factory(payload)`) is synchronous and cannot await (Codex iter 4 P1). Resolution of canonical IDs to native IDs happens upstream in the supervisor's async payload factory (T12).

- [ ] **T11.3: Test + lint + commit.**

---

### Task T12: Migrate callers to per-account builder + supervisor-side Databento resolution

**Files:**

- Modify: `backend/src/msai/live_supervisor/__main__.py` (`_build_production_payload_factory`, l. 70-180) — async path; call `resolve_databento_targets` from T10 and embed `native_instrument_ids` + `venue_dataset_map` + `canonical_to_native_bar_types` into the START payload.
- Modify: `backend/src/msai/services/nautilus/trading_node_subprocess.py` (the subprocess entrypoint, `node_factory(payload)` at l. 675) — sync path; pass `native_instrument_ids` + `venue_dataset_map` + `canonical_to_native_bar_types` from payload to the sync `build_per_account_trading_node_config`.

Wire `account_id` + `ibg_client_id` + `native_instrument_ids` + `venue_dataset_map` + `canonical_to_native_bar_types` through the payload factory → subprocess chain (Codex iter 8 P1 — `canonical_to_native_bar_types` is what makes the SymbologyShimActor's outbound subscription path actually fire on_start). `ibg_client_id` allocation rule: derive from `deployment_slug` hash (the shared helper from T11's `derive_ibg_client_id`).

The async/sync boundary lives at the supervisor's payload factory:

- **Supervisor (async, has DB session)**: calls `await resolve_databento_targets(canonical_ids, session=..., as_of_date=...)`; builds `canonical_to_native_bar_types` from each Strategy's subscribed bar_types (canonical `.IBKR-...`) zipped with their native equivalents (replace the venue suffix per the shim's mapping); embeds all three (`native_instrument_ids`, `venue_dataset_map`, `canonical_to_native_bar_types`) in the payload dict.
- **Subprocess (sync, no DB session)**: receives the resolved targets in the payload, calls sync `build_per_account_trading_node_config(native_instrument_ids=payload["native_instrument_ids"], venue_dataset_map=payload["venue_dataset_map"], canonical_to_native_bar_types=payload["canonical_to_native_bar_types"], ...)`.

This split is the Codex iter 4 P1 + iter 8 P1 fix.

- [ ] **T12.1-T12.5: Standard TDD steps** — failing tests against `_build_production_payload_factory`'s output payload (assert it contains `account_id`, `ibg_client_id`, `native_instrument_ids`, `venue_dataset_map`, AND `canonical_to_native_bar_types`); a separate failing test asserts the subprocess's `node_factory(payload)` propagates `canonical_to_native_bar_types` into `build_per_account_trading_node_config`'s output `config.actors[…SymbologyShimActor…].config["canonical_to_native_bar_types"]`; implement; verify; lint; commit.

---

### Task T13: Synthetic multi-login `GatewayRouter` integration test

**Files:**

- Create: `backend/tests/integration/test_gateway_router_synthetic_multi_login.py`

Council 2026-05-29 #14. Even though PR 1 runs single-login (`marin1016test`), the `_build_production_payload_factory()` multi-login branch (`__main__.py:171`) must have coverage. Synthetic test: construct a `GatewayRouter` with two logins (`marin1016test`, `mslvp000`), feed it through `_build_production_payload_factory` with deployments under both, assert the factory uses the resolved `(host, port)` for each rather than process-wide `settings.ib_host/port`.

- [ ] **T13.1: Write the test** (no need to mock IB — just assert the factory's resolution choice).
- [ ] **T13.2: Verify it passes against the current code.** This test is verification — `_build_production_payload_factory` already implements the branch; the test prevents regression.
- [ ] **T13.3: Lint + commit.**

---

### Task T14: Account context on operator surfaces

**Files (paths corrected per Codex iter 1 P2-5):**

- Modify: `backend/src/msai/api/live.py` (`/live/status` response shape)
- Modify: `backend/src/msai/cli.py:557` (the `live status` sub-app — currently uses plain `typer.echo`, NOT a Rich table — append the new columns to the echoed output or migrate to a Rich table if desired, BUT keep within scope: minimum change is the echoed-string columns)
- Modify: `frontend/src/components/live/strategy-status.tsx:153` (the existing live status table component)
- Modify: `frontend/src/lib/api.ts:293` (the typed API client — add `ib_login_key`, `account_id`, `ibg_client_id` to the LiveDeployment type)
- Modify: `frontend/src/app/live-trading/page.tsx` (the page route — likely no change if it consumes `strategy-status.tsx`; verify)
- Test: `backend/tests/integration/api/test_live_status_includes_account_context.py`
- Test (frontend): UI changes are exercised by UC5 (verify-e2e Phase 5.4 + graduated Playwright spec Phase 6.2c)

Add fields `ib_login_key`, `account_id`, `ibg_client_id` to each row of `/live/status`. Source-of-truth for `ibg_client_id` is the helper from T11 (`backend/src/msai/services/nautilus/ibg_client_id.py`, `derive_ibg_client_id(deployment_slug)`); the status serializer calls it on each row. Add corresponding fields to `msai live status` output (typer.echo lines — keep change minimal). Add the same columns to the existing UI table at `strategy-status.tsx:153`.

- [ ] **T14.1: Failing integration test for the API** — assert the response includes `ib_login_key`, `account_id`, `ibg_client_id` for each deployment row; the `ibg_client_id` matches `derive_ibg_client_id(deployment.deployment_slug)`.
- [ ] **T14.2: Implement.** API: include the columns in the existing serializer at `backend/src/msai/api/live.py` (find `/live/status` handler and its response builder; add the three fields). CLI: update `cli.py:557` to include the new fields in the existing typer.echo line. Frontend: add three columns to `strategy-status.tsx:153` table; add three fields to `LiveDeployment` (or analogous) type at `frontend/src/lib/api.ts:293`.
- [ ] **T14.3: Test + lint + commit.**

---

### Task T15: Docker compose + env updates

**Files:**

- Modify: `docker-compose.dev.yml` (only `GATEWAY_CONFIG` value — no second service)
- Modify: `.env.example` (if exists) — document new `GATEWAY_CONFIG` format with `accounts=` segment.

Update the existing `live-supervisor` service env `GATEWAY_CONFIG` to `marin1016test:ib-gateway:4002:accounts=DUP733214|DUP733215` (Codex iter 2 P1-5: compose service is `ib-gateway`, not `ib-gateway-paper`; internal paper port is 4002 per `docker-compose.dev.yml:235`). **Also add `GATEWAY_CONFIG: ${GATEWAY_CONFIG:-}` to the `backend` service env block** at `docker-compose.dev.yml:98-118` (Codex iter 2 P1-4) — T5 makes the backend's lifespan instantiate `GatewayRouter`, so the backend container needs the env. Without this, the fail-closed boot check is dead. Do NOT add a second `ib-gateway` service — per the 2026-05-29 council Shape A ruling, PR 1 uses one container.

- [ ] **T15.1: Edit `docker-compose.dev.yml`** — update the `live-supervisor` service's `GATEWAY_CONFIG` env value AND add `GATEWAY_CONFIG: ${GATEWAY_CONFIG:-}` to the `backend` service's `environment` block (Codex iter 2 P1-4).
- [ ] **T15.2: Bring up the stack from the worktree** (per `feedback_compose_must_cycle_from_worktree.md`) and verify backend boots cleanly:

```bash
cd .worktrees/multi-account-broker-fleet
docker compose -f docker-compose.dev.yml up -d --build
curl -sf http://localhost:8800/health
docker compose -f docker-compose.dev.yml logs backend | grep "GATEWAY_CONFIG"
```

- [ ] **T15.3: Commit.**

---

## Dispatch Plan

| Task ID | Depends on  | Writes (concrete file paths)                                                                                                                                                                                                                                                                                               |
| ------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T1      | —           | `backend/src/msai/core/halt_keys.py`, `backend/tests/unit/core/test_halt_keys.py`                                                                                                                                                                                                                                          |
| T2      | T1          | `backend/src/msai/api/live.py`, `backend/src/msai/live_supervisor/process_manager.py`, `backend/src/msai/services/nautilus/disconnect_handler.py`                                                                                                                                                                          |
| T3      | T1, T2      | `backend/src/msai/api/live.py`, `backend/tests/integration/api/test_kill_all_halt_cause.py`                                                                                                                                                                                                                                |
| T4      | —           | `backend/src/msai/services/live/gateway_router.py`, `backend/tests/unit/services/live/test_gateway_router.py`                                                                                                                                                                                                              |
| T5      | T4          | `backend/src/msai/main.py`, `backend/src/msai/live_supervisor/__main__.py`, `backend/tests/integration/test_app_startup_gateway_config.py`                                                                                                                                                                                 |
| T6      | T1, T2, T3  | `backend/src/msai/live_supervisor/main.py`, `backend/src/msai/api/live.py`, `backend/tests/unit/live_supervisor/test_handle_command_propagation.py`, `backend/tests/unit/test_live_supervisor_main.py`, `backend/tests/integration/api/test_start_portfolio_publishes_gateway_session_key.py`                              |
| T7      | —           | `backend/src/msai/services/live_command_bus.py`, `backend/tests/unit/services/test_live_command_bus_account_stream.py`                                                                                                                                                                                                     |
| T8      | T1, T2, T3  | `backend/src/msai/api/live.py`, `backend/tests/integration/api/test_live_drain_account.py`                                                                                                                                                                                                                                 |
| T9      | —           | `backend/src/msai/services/symbology_shim.py`, `backend/tests/unit/services/test_symbology_shim.py`                                                                                                                                                                                                                        |
| T10     | T9          | `backend/src/msai/services/nautilus/databento_live_config.py`, `backend/tests/unit/services/nautilus/test_databento_live_config.py`                                                                                                                                                                                        |
| T11     | T9, T10     | `backend/src/msai/services/nautilus/live_node_config.py`, `backend/src/msai/services/nautilus/trading_node_subprocess.py`, `backend/src/msai/services/symbology_shim_actor.py`, `backend/tests/unit/services/nautilus/test_live_node_config_split_topology.py`, `backend/tests/unit/services/test_symbology_shim_actor.py` |
| T12     | T6, T11     | `backend/src/msai/live_supervisor/__main__.py`, `backend/src/msai/services/nautilus/trading_node_subprocess.py`                                                                                                                                                                                                            |
| T13     | T4, T5, T11 | `backend/tests/integration/test_gateway_router_synthetic_multi_login.py`                                                                                                                                                                                                                                                   |
| T14     | T11, T12    | `backend/src/msai/api/live.py`, `backend/src/msai/cli.py`, `frontend/src/components/live/strategy-status.tsx`, `frontend/src/lib/api.ts`, `frontend/src/app/live-trading/page.tsx`, `backend/tests/integration/api/test_live_status_includes_account_context.py`                                                           |
| T15     | T4, T5, T11 | `docker-compose.dev.yml`, `.env.example`                                                                                                                                                                                                                                                                                   |

**Scheduling note (per `superpowers:subagent-driven-development` discipline):**

- Parallel-eligible at start: T1, T4, T7, T9 (no shared `Writes`, no `Depends on`). T6 is NOT parallel-eligible at start anymore — it now touches `backend/src/msai/api/live.py` (Codex iter 1 P1-3 fix added the `/start-portfolio` publish step) so it must serialize with T2/T3/T8/T14 on that file.
- T11 is the convergence point; T12/T13/T14 fan out from it.
- `backend/src/msai/api/live.py` is touched by T2, T3, T6, T8, T14 — these MUST serialize via the dispatch order: T2 → T3 → T6 → T8 → T14. (T6 lands between T3 and T8 because T6 needs T2's `fleet_halt_key` import already present and T3's halt-cause companion patterns already in place when it adds the `gateway_session_key` publish snippet.)
- `backend/src/msai/live_supervisor/__main__.py` is touched by T5, T12 — serialize T5 then T12.
- `backend/src/msai/services/nautilus/trading_node_subprocess.py` is touched by T11 and T12 — serialize T11 then T12.

Default concurrency cap: 3 subagents. Sequential mode is acceptable for any subagent that fails or needs full file context.

---

## E2E Use Cases (Phase 3.2b)

### Surface coverage decision

Project type per `CLAUDE.md ## E2E Configuration`: **fullstack** (API primary, UI secondary). The project also exposes a CLI (`msai`) — per the project's "API-first, CLI-second, UI-third" ordering rule. Surfaces evaluated for PR 1:

- **API:** **Covered.** UC1, UC2, UC3 below.
- **CLI:** **Covered.** UC4 below.
- **UI:** **Covered.** UC5 below.

No surface omitted.

---

### UC1 — Operator deploys to two paper sub-accounts and observes a fill in each

**Actor:** Operator (Pablo) running the dev stack from the worktree.

**Scenario:** The operator has the dev stack up with the new shared-gateway topology and the symbology shim wired. They want to confirm that one API call per account can deploy a portfolio and that each account independently places a paper order against IB without the two interfering.

**Interface:** API (curl/httpx against `http://localhost:8800/api/v1/`).

**Intent:** The operator places one portfolio on account A (DUP733214) and one on account B (DUP733215), both routing through the same IB Gateway under `marin1016test`, and confirms each account submits an independent paper order whose fill is visible in IB's UI and reconciled into the MSAI DB.

**Setup:** Authenticated session via `X-API-Key: $MSAI_API_KEY`. Dev stack up from the worktree (docker compose dev profile + `broker` profile). DUP733214 and DUP733215 confirmed clean (no prior open positions). Verification spike (S1-S4 above) already PASS.

**Steps:**

1. `POST /api/v1/live/start-portfolio` with `portfolio_id=P-A` and `account_id=DUP733214` — receive 201.
2. `POST /api/v1/live/start-portfolio` with `portfolio_id=P-B` and `account_id=DUP733215` — receive 201.
3. Wait up to 60 seconds for each strategy's first bar event + paper market order.
4. `GET /api/v1/live/status` and `GET /api/v1/live/trades`.

**Verification:** The operator receives 201 on both starts; subsequent `GET /api/v1/live/status` lists two deployments scoped to their own `account_id` AND `ibg_client_id` with the same `ib_login_key="marin1016test"`; `GET /api/v1/live/trades` includes one fill per account, each tagged with its `account_id`; IB's web UI shows one paper fill in DUP733214 and one in DUP733215.

**Persistence:** Re-request `GET /api/v1/live/trades` 30 seconds later — both fills still present with the same `account_id` scope and same broker trade IDs.

---

### UC2 — Account-scoped drain leaves the other account running

**Actor:** Operator running the dev stack.

**Scenario:** The operator is mid-drill: both deployments are live, both have filled. They want to take account A offline for maintenance without touching account B.

**Interface:** API.

**Intent:** The operator drains account A through the account-scoped drain endpoint and confirms A's TradingNode exits cleanly while B continues to receive bars and could accept new orders.

**Setup:** UC1 must have completed successfully. Both deployments are in `running` state. Authenticated session via `X-API-Key`.

**Steps:**

1. `POST /api/v1/live/drain/DUP733214` — receive 200.
2. `GET /api/v1/live/status` (poll every 5s for up to 30s).
3. Confirm via `docker logs <backend>` that the supervisor log lines for B's TradingNode continue to show heartbeats AFTER A's TradingNode has exited.

**Verification:** The drain response includes the drained `account_id` and the new state (`draining` → `stopped`); `/live/status` shows A's deployment transitioning to `stopped` while B's stays `running`; B's TradingNode log timestamps after A's exit confirm it kept receiving bars; Redis has the account-scoped halt latch `msai:risk:halt:account:DUP733214` set (and its `:cause` companion holds `operator_drain`); the account-scoped key for B (`msai:risk:halt:account:DUP733215`) is NOT set, and the fleet key (`msai:risk:halt`) is NOT set (Codex iter 1 P1-1 — keying by account_id, not by shared ib_login_key, is what makes the drill provable).

**Persistence:** 30s after drain, `GET /api/v1/live/status` still shows A `stopped` and B `running`; B's broker positions remain unchanged.

---

### UC3 — Fleet emergency halt sets `reason=fleet_emergency` and stops both accounts

**Actor:** Operator under simulated panic.

**Scenario:** The operator has both deployments running and discovers a problem outside the system. They want to halt the entire fleet immediately and have the halt cause clearly recorded so a future investigation can distinguish this manual halt from a future automatic data-stale halt.

**Interface:** API.

**Intent:** The operator fires `/kill-all` and confirms both TradingNodes exit, both Redis halt keys are set, and the halt-cause companion key reads `reason=fleet_emergency` with operator attribution.

**Setup:** Two deployments running (UC1 setup). Authenticated session.

**Steps:**

1. `POST /api/v1/live/kill-all` — receive 200.
2. `GET /api/v1/live/status` (poll for up to 30s).
3. Inspect Redis: `redis-cli GET msai:risk:halt:cause`.

**Verification:** Both deployments transition to halted; `/live/status` reports `halted`/`stopped` status for both deployments (Codex iter 3 P2: T14 does NOT add halt-cause to status response — cause attribution lives in Redis only for PR 1); `msai:risk:halt:cause` JSON contains `{"reason": "fleet_emergency", "detected_at": "...", "source": "..."}`; no `data_stale` reason in Redis (PR 1b territory).

**Persistence:** 30s after halt, the halt key is still present in Redis (TTL 24h) and the cause companion key still reads `fleet_emergency`; `GET /api/v1/live/status` still shows both deployments halted.

---

### UC4 — `msai live status` shows account-scoped rows

**Actor:** Operator using the CLI from the dev VM.

**Scenario:** The operator is debugging the drill from a terminal and wants to see at a glance which account each live deployment is bound to.

**Interface:** CLI.

**Intent:** The operator runs `msai live status` and sees lines naming `ib_login_key`, `account_id`, and `ibg_client_id` for each live deployment, so they can correlate logs to accounts without opening the UI.

**Setup:** UC1 must have completed successfully (two deployments running). Operator has shell access to the backend container OR has `MSAI_API_KEY` exported locally.

**Steps:**

1. Run `msai live status` (no flags).
2. Inspect the echoed output.

**Verification:** stdout shows lines (one per deployment, via the existing `typer.echo` at `backend/src/msai/cli.py:557` — NOT a Rich table; Codex iter 3 P2). Each line includes `account_id`, `ib_login_key`, `ibg_client_id`, and the existing `status` field (LiveDeploymentInfo uses `status`, NOT `state` — `backend/src/msai/schemas/live.py:46`). Two deployment lines: DUP733214 and DUP733215, both with `ib_login_key=marin1016test` and distinct `ibg_client_id` values. Exit code 0.

**Persistence:** Re-run `msai live status` 30s later — same two lines, same account values; the `status` field may have advanced, but `(ib_login_key, account_id, ibg_client_id)` are stable.

---

### UC5 — `/live/status` page renders account context

**Actor:** Authenticated dashboard user (Pablo logged in to `http://localhost:3300`).

**Scenario:** The operator is watching the drill from the browser and wants to see the same per-account information the API and CLI surface.

**Interface:** UI.

**Intent:** The signed-in operator navigates to `/live/status` and sees each live deployment row labeled with its `ib_login_key`, `account_id`, and `ibg_client_id`, so they can correlate the page to the API and CLI views.

**Setup:** Authenticated session (signed in via the existing MSAL flow OR the dev `X-API-Key` bypass if present). UC1 must have completed.

**Steps:**

1. Navigate to `http://localhost:3300/live-trading`.
2. Inspect the deployments table.

**Verification:** The page reads two deployment rows; each row has visible cells naming the IB login (`marin1016test`), the account ID (DUP733214 or DUP733215), and the client ID (a small integer). Clicking a row opens the deployment detail view (existing behavior) and the detail header repeats the same account context.

**Persistence:** Reload `/live-trading` — both rows still present with the same labels; refresh after the drill's drain step (UC2) — A's row reflects the new `stopped` state but its account labels are unchanged.

---

### Negative UC — Boot fails fast on duplicate `ib_login_key`

**Actor:** Operator misconfiguring `GATEWAY_CONFIG`.

**Scenario:** While editing compose, the operator accidentally adds a duplicate `marin1016test` entry to `GATEWAY_CONFIG`. They want the stack to refuse to start with a clear log line, rather than silently picking one of the duplicates.

**Interface:** API + CLI (boot path).

**Intent:** The operator brings up the stack with a known-bad `GATEWAY_CONFIG` and confirms the backend exits non-zero within 10 seconds with a log line that names the duplicate key.

**Setup:** Worktree dev stack down. Edit `docker-compose.dev.yml` (or pass `GATEWAY_CONFIG=marin1016test:host-a:4004,marin1016test:host-b:4005` as an env override).

**Steps:**

1. `docker compose -f docker-compose.dev.yml up backend` (do NOT detach so we see logs).
2. Observe backend stdout/stderr.
3. Restore the correct compose config + restart.

**Verification:** Backend container exits with non-zero status within 10s of boot; the log line includes the literal string `duplicate ib_login_key 'marin1016test'`. After fixing the config, the stack boots normally.

**Persistence:** N/A — boot-time validation; the misconfiguration never persists state.

---

## Self-Review (per writing-plans skill)

- **Spec coverage:** All 11 PRD deliverables are mapped to tasks T1-T15. The 5 council 2026-05-29 blocking objections map: #10 (PRD amendment — handled in this PR's scope), #11 (drain-leaves-B-running — UC2 + T8), #12 (`gateway_session_key` propagation — T6), #13 (fail-fast same-`ib_login_key` — T4 + T5), #14 (synthetic multi-login test — T13).
- **Placeholder scan:** Tasks T9, T11, T12 use ellipsis `...` in code blocks for very long files where the exact edit pattern depends on reading the file in full at dispatch time. The text around each `...` describes what the subagent should produce. Acceptable for files > 500 LOC.
- **Type consistency:** `gateway_session_key` parameter name matches between T6 (passing through) and `ProcessManager.spawn` signature already at `process_manager.py:175`. `HaltCause` enum values match across T1, T3, T8. `DatabentoSubscriptionTarget` is the only data class introduced in T9; T10 + T11 consume it without renaming. `build_per_account_trading_node_config` signature consistent across T11 + T12.

---

## Execution handoff

Plan complete and saved to `docs/plans/2026-05-29-multi-account-broker-fleet-pr1.md`. Default execution under `/forge-goal` autonomous mode is **subagent-driven** per `superpowers:subagent-driven-development` — fresh subagent per task with two-stage review between tasks.

After this plan goes through Phase 3.3 plan-review loop (Claude + Codex, iterating until P0/P1/P2 clean), Phase 4 begins with the verification spike (S1-S4) and then dispatches tasks per the schedule above.
