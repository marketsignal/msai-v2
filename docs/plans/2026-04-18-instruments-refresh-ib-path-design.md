# Design: `msai instruments refresh --provider interactive_brokers`

**Status:** Approved
**Created:** 2026-04-18
**Author:** Claude + Pablo
**PRD:** [`docs/prds/instruments-refresh-ib-path.md`](../prds/instruments-refresh-ib-path.md)
**Discussion log:** [`docs/prds/instruments-refresh-ib-path-discussion.md`](../prds/instruments-refresh-ib-path-discussion.md) (council verdict in Round 2)
**Research brief:** [`docs/research/2026-04-18-instruments-refresh-ib-path.md`](../research/2026-04-18-instruments-refresh-ib-path.md)

---

## 1. Scope

Complete the deferred `--provider interactive_brokers` branch of `msai instruments refresh`. Today that branch hard-fails at `claude-version/backend/src/msai/cli.py:747-756` because `Settings` lacks IB-wiring fields. This design ships:

1. Three new `Settings` fields (connect timeout, request timeout, instrument client_id)
2. A new shared module `services/nautilus/ib_port_validator.py` that deduplicates the paper/live mode guard currently inlined in three places
3. A real implementation of the IB branch replacing the deferral stub
4. Unit tests + an opt-in live-paper smoke test

All other design choices are pre-settled:

- 6 design Qs answered by a 5-advisor engineering council + Codex chairman — see discussion log Round 2.
- 4 design-changing library findings resolved by the research-first agent against NautilusTrader 1.223.0 venv source — see research brief findings #1-4.
- Validator extraction + placement (`services/nautilus/` not `core/`) validated by Codex — see conversation history 2026-04-18.

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  msai instruments refresh --symbols AAPL,ES                     │
│                           --provider interactive_brokers        │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  PREFLIGHT (no IB connection yet)                               │
│    • parse/strip/dedupe --symbols                               │
│    • reject empty list                                          │
│    • reject symbols ∉ PHASE_1_PAPER_SYMBOLS                     │
│    • validate_port_account_consistency(ib_port, ib_account_id)  │
│    • log resolved (host, port, account_prefix, client_id)       │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  CONNECT                                                        │
│    MessageBus + Cache + LiveClock                               │
│        ↓                                                        │
│    get_cached_ib_client(host, port, client_id, request_timeout) │
│        ↓                                                        │
│    client.start() → schedules _start_async                      │
│        ↓                                                        │
│    asyncio.wait_for(client._is_client_ready.wait(),             │
│                     timeout=ib_connect_timeout_seconds)         │
│        ├── TimeoutError → CLI error with operator hint          │
│        └── ready → proceed                                      │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  QUALIFY                                                        │
│    provider = get_cached_interactive_brokers_instrument_provider│
│    qualifier = IBQualifier(provider)   [existing wrapper]       │
│    sm = SecurityMaster(qualifier=qualifier, db=session)         │
│    await sm.resolve_for_live(symbols)                           │
│        → per-symbol provider.get_instrument(contract)           │
│        → upsert instrument_definitions + instrument_aliases     │
│    await session.commit()                                       │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  TEARDOWN (always runs — try/finally)                           │
│    client.stop()              [sync, schedules _stop_async]     │
│    await client._stop_async() [drains so IB releases slot]      │
│    await session.close()                                        │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
            Exit 0 on full success
            Non-zero on any per-symbol or phase failure
```

### Design rationale (the non-obvious bits)

- **Why `asyncio.wait_for(client._is_client_ready.wait(), ...)` instead of `client.wait_until_ready(timeout=...)`** — research brief finding #1: Nautilus 1.223.0's `wait_until_ready` silently swallows `TimeoutError` and only logs, which would give a "dead gateway looks ready" false-negative. The internal `_is_client_ready` event is the correct signal; we own the timeout ourselves.
- **Why `await client._stop_async()` in teardown** — research brief finding #2: `client.stop()` is a Cython `cpdef void` that _schedules_ `_stop_async` as a task rather than executing synchronously. Without awaiting the inner coroutine, the TCP disconnect races process exit and IB Gateway holds the `client_id=999` slot past 60s — breaking US-005.
- **Why a pytest fixture clearing factory globals** — research brief finding #3: `IB_CLIENTS` / `IB_INSTRUMENT_PROVIDERS` / `GATEWAYS` dicts have no `.clear()` method on the factory module itself. Production is unaffected (each CLI invocation is a fresh process), but unit tests reusing the same Python process would see stale cached clients. Autouse fixture clears them explicitly.
- **Why extract the validator to `services/nautilus/ib_port_validator.py`** — there are currently THREE copies of the paper/live mode policy: `live_node_config.py:177-215` (private helper), `live_supervisor/__main__.py:162-191` (inlined open-coded), and the CLI would make a fourth. `core/` is cross-cutting plumbing; `services/nautilus/` is where broker-specific conventions already live (see `live_node_config.py`, `live_instrument_bootstrap.py`, `ib_qualifier.py`).

## 3. Components

### NEW files

| Path                                                                     | Purpose                                                                                                                                                                                    | Est. LOC |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------- |
| `claude-version/backend/src/msai/services/nautilus/ib_port_validator.py` | Extracted validator + 3 constants. Exports `validate_port_account_consistency(port, account_id)` + `validate_port_vs_paper_trading(port, paper_trading)` (for the supervisor inline case). | ~50      |
| `claude-version/backend/tests/unit/test_ib_port_validator.py`            | Combinatorial coverage of the validator.                                                                                                                                                   | ~80      |
| `claude-version/backend/tests/e2e/test_instruments_refresh_ib_smoke.py`  | Opt-in `pytest.mark.ib_paper` smoke gated on `RUN_PAPER_E2E=1`. Runs refresh twice.                                                                                                        | ~120     |

### MODIFIED files

| Path                                                                    | Change                                                                           | Net LOC |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------------- | ------- |
| `claude-version/backend/src/msai/core/config.py`                        | Add 3 Settings fields with `AliasChoices` env aliases                            | +20     |
| `claude-version/backend/src/msai/services/nautilus/live_node_config.py` | Import validator from new module; delete local helper + 3 constants              | -30     |
| `claude-version/backend/src/msai/live_supervisor/__main__.py`           | Replace inlined policy (lines 162-191) with validator call                       | -25     |
| `claude-version/backend/src/msai/cli.py`                                | Replace `--provider interactive_brokers` deferral (lines 747-756) with real impl | +120    |
| `claude-version/backend/tests/unit/test_cli_instruments_refresh.py`     | Add 4 IB-branch parametrized cases                                               | +150    |
| `claude-version/backend/tests/unit/test_live_node_config.py`            | Update imports; remove validator tests (moved to new file)                       | -20     |
| `claude-version/backend/tests/unit/test_live_supervisor_main.py`        | Retarget to validate via the new module                                          | ~0      |
| `claude-version/backend/tests/unit/conftest.py` (or new)                | Autouse fixture clearing `IB_CLIENTS`/`IB_INSTRUMENT_PROVIDERS`/`GATEWAYS`       | +15     |

**Net:** +260 LOC new code, -75 LOC deduplication, ~0 mechanical test-wire updates. All changes within `claude-version/backend/`.

### Closed-universe source of truth

`services/nautilus/live_instrument_bootstrap.py::PHASE_1_PAPER_SYMBOLS` already defines the 5-symbol allow-list (AAPL, MSFT, SPY, EUR/USD, ES). The CLI preflight imports from there — no duplication.

## 4. Settings additions

```python
# In claude-version/backend/src/msai/core/config.py

ib_connect_timeout_seconds: int = Field(
    default=5,
    validation_alias=AliasChoices("IB_CONNECT_TIMEOUT_SECONDS"),
    description="Wall-clock budget for the IB Gateway TCP connection + "
                "client-ready probe. Applies to `msai instruments refresh` and "
                "any short-lived one-shot IB client. Distinct from "
                "`ib_request_timeout_seconds` so a dead gateway fails fast "
                "(~5s) while slow individual qualifications still honor 30s.",
)

ib_request_timeout_seconds: int = Field(
    default=30,
    validation_alias=AliasChoices("IB_REQUEST_TIMEOUT_SECONDS"),
    description="Post-connect per-request timeout for IB contract "
                "qualification (reqContractDetails round-trip). `int` matches "
                "Nautilus `get_cached_ib_client(request_timeout_secs=...)` "
                "signature.",
)

ib_instrument_client_id: int = Field(
    default=999,
    validation_alias=AliasChoices("IB_INSTRUMENT_CLIENT_ID"),
    description="Out-of-band IB client_id reserved for `msai instruments "
                "refresh`. MUST NOT collide with live-subprocess derived IDs "
                "(100-200 range). Surfaced in CLI help + every preflight log "
                "so the reservation is explicit, not hidden. Nautilus gotcha "
                "#3: concurrent clients on the same client_id silently "
                "disconnect each other.",
)
```

## 5. Error handling

| Phase     | Failure                                           | CLI exit          | User-facing message                                                                                                                                                                                                                                                 |
| --------- | ------------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Preflight | Empty `--symbols`                                 | non-zero          | `"no symbols provided"` (mirrors Databento branch at cli.py:744-745)                                                                                                                                                                                                |
| Preflight | Unknown symbol                                    | non-zero          | `"symbol {X} not in PHASE_1_PAPER_SYMBOLS (AAPL, MSFT, SPY, EUR/USD, ES). resolve_for_live only supports this closed universe today."`                                                                                                                              |
| Preflight | Port/account mismatch                             | non-zero          | `ValueError` from `validate_port_account_consistency` (e.g., `"port 4001 (live) + account DUP733211 (paper prefix); set IB_PORT=4002 or change IB_ACCOUNT_ID"`)                                                                                                     |
| Connect   | `TimeoutError` after `ib_connect_timeout_seconds` | non-zero          | `"IB Gateway not reachable at {host}:{port} within {N}s. Check: (a) gateway container running, (b) IB_PORT matches IB_ACCOUNT_ID prefix (DU/DF* → paper 4002/4004, U* → live 4001/4003), (c) IB_INSTRUMENT_CLIENT_ID={M} not colliding with an active subprocess."` |
| Qualify   | `provider.get_instrument` returns None            | non-zero          | `IBQualifier.qualify` raises `ValueError` with the contract spec; propagated to CLI                                                                                                                                                                                 |
| Qualify   | Mid-batch connection drop                         | non-zero          | Exception propagates; DB session rolled back; rows already committed stay committed (idempotent recovery on re-run)                                                                                                                                                 |
| Teardown  | `_stop_async` errors                              | preserve original | Logged as warning via `msai.core.logging.get_logger(__name__)`; does NOT override the original exit code                                                                                                                                                            |

## 6. Testing strategy

### Unit tests (mocked, always run in CI)

**`test_ib_port_validator.py`** — 6+ cases:

- paper port + paper prefix (DU / DF / DFP) → pass
- paper port + live prefix (U…) → raise
- live port + paper prefix → raise
- live port + live prefix → pass
- unknown port (e.g. 4005) → raise
- whitespace-padded account ID → stripped then validated

**`test_cli_instruments_refresh.py::*_ib_branch*`** — 4 parametrized cases using Typer's `CliRunner` with `get_cached_ib_client` + `get_cached_interactive_brokers_instrument_provider` + `SecurityMaster.resolve_for_live` mocked:

- Happy path: AAPL + ES → exit 0, session.commit called
- Unknown symbol: NVDA → exit non-zero, no IB connect attempt
- Port mismatch: IB_PORT=4001 + account DU… → exit non-zero, validator fires before connect
- Dead gateway: mock `_is_client_ready.wait()` to hang → `TimeoutError` + exit non-zero with operator hint text

### Integration test (opt-in, skipped in CI)

**`test_instruments_refresh_ib_smoke.py`** gated on `RUN_PAPER_E2E=1`:

```python
@pytest.mark.ib_paper
@pytest.mark.skipif(not os.getenv("RUN_PAPER_E2E"), reason="opt-in paper smoke")
async def test_refresh_aapl_and_es_twice():
    # Run #1: fresh registry → assert rows written for AAPL + ES
    # Run #2 (within 60s): assert NO new rows in instrument_definitions
    #                       assert NO new rows in instrument_aliases (effective_to stays NULL)
    # Warm check: SecurityMaster.resolve_for_live(["AAPL"]) returns without
    #             touching IB (verified via mock instrumentation on the qualifier)
```

### Factory-globals fixture

```python
# In tests/unit/conftest.py (or tests/unit/test_cli_instruments_refresh.py)

@pytest.fixture(autouse=True)
def _clear_ib_factory_globals():
    """Nautilus 1.223.0 factory caches `IB_CLIENTS`/`IB_INSTRUMENT_PROVIDERS`/
    `GATEWAYS` as module-level dicts with no `.clear()` method. Clear them
    between tests so one test's cached client doesn't leak into the next.
    Production is unaffected (each CLI invocation is a fresh process)."""
    yield
    from nautilus_trader.adapters.interactive_brokers import factories
    factories.IB_CLIENTS.clear()
    factories.IB_INSTRUMENT_PROVIDERS.clear()
    factories.GATEWAYS.clear()
```

## 7. Out of scope (reiteration from PRD non-goals)

- Live-path wiring (`/api/v1/live/start-portfolio`) — separate follow-up PR
- Options resolution — non-goal from PR #32
- Symbols outside the 5-symbol closed universe
- Multi-login gateway routing (PR #30's `GatewayRouter`)
- Automated CI runs against real IB Gateway
- Retry logic for transient IB errors
- Rename of `ib_request_timeout_seconds` to `ib_instrument_request_timeout_seconds` (Maintainer suggestion, overruled by chairman)
- Structured metrics / dashboards for CLI runs

## 8. Open items carried to the implementation plan

1. **Bare `root` handling for `AAPL.NASDAQ`-style input:** PRD US-006 says "accepted if the bare root is in closed universe." Decide during Phase 4 whether to strip venue suffix in preflight or require bare symbols only.
2. **Casing normalization for symbols:** `aapl` vs `AAPL` — decide during Phase 4. Default: case-sensitive match (simpler; operators typically upper-case).
3. **`IB_MAX_CONNECTION_ATTEMPTS` env var vs `client.stop()` cancellation:** research brief finding #4 gives two options. Default: rely on `client.stop()` in the connect-failure except block (simpler, no env-var dance). If smoke test shows the retry-forever loop leaks, switch to setting the env var pre-client-construction.
4. **Commit cadence:** `SecurityMaster.resolve_for_live` currently calls `session.flush()` per upsert. CLI commits once at the end of the batch. If a mid-batch failure is frequent in practice, we may add per-symbol commit — decide after first smoke run.

## 9. Approval

- [x] Product Owner (Pablo) approval — 2026-04-18
- [x] Ready for implementation plan — proceed to `/superpowers:writing-plans`
