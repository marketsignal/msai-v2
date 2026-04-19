# Research: instruments-refresh-ib-path

**Date:** 2026-04-18
**Feature:** Complete the deferred `msai instruments refresh --provider interactive_brokers` CLI branch in MSAI v2 (claude-version/).
**Researcher:** research-first agent

## Libraries Touched

| Library                                      | Our Version    | Latest Stable   | Breaking Changes                                                                                                                                                                      | Source                                                                                                   |
| -------------------------------------------- | -------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| nautilus-trader[ib]                          | 1.223.0        | 1.224.0         | Only between 1.222→1.223 (ibapi 10.43 upgrade, `request_timeout_secs` consolidation, `fetch_all_open_orders` in cache key). None between 1.223→1.224 affecting IB adapter public API. | [GH release 1.223](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.223.0) (2026-04-18) |
| pydantic                                     | 2.10+ (pinned) | n/a for this PR | N/A — `AliasChoices` unchanged                                                                                                                                                        | venv source (2026-04-18)                                                                                 |
| pydantic-settings                            | 2.13.1         | n/a for this PR | N/A — already in use at `core/config.py`                                                                                                                                              | `uv.lock:1770-1772` (2026-04-18)                                                                         |
| typer                                        | 0.24.1         | n/a for this PR | N/A — `Option`/`BadParameter` pattern already in use across `cli.py`                                                                                                                  | `uv.lock:2212-2214` (2026-04-18)                                                                         |
| ibapi (transitive via `nautilus_trader[ib]`) | 10.43          | 10.43           | 1.223 bumped to 10.43; no direct use from MSAI code                                                                                                                                   | GH release notes (2026-04-18)                                                                            |

---

## Per-Library Analysis

### nautilus-trader — `get_cached_ib_client` factory

**Versions:** ours=1.223.0, latest=1.224.0

**Verified signature** (venv source `nautilus_trader/adapters/interactive_brokers/factories.py:47-58`):

```python
def get_cached_ib_client(
    loop: asyncio.AbstractEventLoop,
    msgbus: MessageBus,
    cache: Cache,
    clock: LiveClock,
    host: str = "127.0.0.1",
    port: int | None = None,
    client_id: int = 1,
    dockerized_gateway: DockerizedIBGatewayConfig | None = None,
    fetch_all_open_orders: bool = False,
    request_timeout_secs: int = 60,
) -> InteractiveBrokersClient:
```

- Plan skeleton's kwargs names are all CORRECT. `request_timeout_secs` (not `request_timeout_seconds`) is the Nautilus name.
- Cache key is `(host, port, client_id)` — see `factories.py:120`. The `fetch_all_open_orders` flag mutates on existing client (`factories.py:139-140`) — PR #3441 (1.223).
- `request_timeout_secs: int` — must be `int`, not `float`. Matches the plan's `ib_request_timeout_seconds: int = 30`.
- The factory auto-calls `client.start()` the first time (`factories.py:134`). A subsequent call for the same `(host, port, client_id)` returns the cached client without restarting it.

**Sources:**

1. venv source `.../nautilus_trader/adapters/interactive_brokers/factories.py:47-142` — read 2026-04-18 (pinned 1.223.0 is ground truth)
2. [NautilusTrader 1.223.0 release notes](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.223.0) — accessed 2026-04-18

**Design impact:** The CLI should call `get_cached_ib_client(loop, msgbus, cache, clock, host=..., port=..., client_id=..., request_timeout_secs=settings.ib_request_timeout_seconds)` with `dockerized_gateway=None` (MSAI runs gateway as a separate compose service, not docker-in-docker). `fetch_all_open_orders` stays default False (CLI does not place orders).

**Test implication:** Unit test that the factory is called with these exact kwargs (mock the factory, assert on mock call args). Integration/paper-smoke test will exercise the real factory but should clear `IB_CLIENTS` between tests (see Open Risk #1 below).

---

### nautilus-trader — `get_cached_interactive_brokers_instrument_provider` factory

**Versions:** ours=1.223.0, latest=1.224.0

**Verified signature** (venv source `factories.py:145-149`):

```python
def get_cached_interactive_brokers_instrument_provider(
    client: InteractiveBrokersClient,
    clock: LiveClock,
    config: InteractiveBrokersInstrumentProviderConfig,
) -> InteractiveBrokersInstrumentProvider:
```

- Cache key is `((client._host, client._port, client._client_id), hash(config))` — `factories.py:175-176`. Two calls with the same client + same config hash return the SAME provider instance.
- Takes 3 positional args — no `loop`, `msgbus`, `cache` pass-through needed (provider only needs the client + clock).

**Sources:**

1. venv source `.../factories.py:145-182` — read 2026-04-18
2. [Nautilus IB integration docs](https://nautilustrader.io/docs/nightly/integrations/ib/) — accessed 2026-04-18

**Design impact:** The CLI passes `InteractiveBrokersInstrumentProviderConfig(symbology_method=SymbologyMethod.IB_SIMPLIFIED)` — NO `load_contracts` is needed for the CLI since we're calling `provider.get_instrument(contract)` on-demand per symbol, not bulk-loading at startup (that pattern is for `TradingNode` init via `InteractiveBrokersDataClientConfig`). The CLI does NOT need to replicate `build_ib_instrument_provider_config()` — that helper is live-path-only.

**Test implication:** Verify the CLI uses a config WITHOUT `load_contracts` (empty/None), so the provider lazy-loads on the per-symbol `get_instrument()` path. Avoids a 60s startup hang loading contracts we haven't listed.

---

### nautilus-trader — `InteractiveBrokersClient.wait_until_ready`

**Versions:** ours=1.223.0

**Verified signature** (venv source `client/client.py:362-376`):

```python
async def wait_until_ready(self, timeout: int = 300) -> None:
    try:
        if not self._is_client_ready.is_set():
            await asyncio.wait_for(self._is_client_ready.wait(), timeout)
    except TimeoutError as e:
        self._log.error(f"Client is not ready: {e}")
```

**CRITICAL GOTCHA — P0-risk finding:** `wait_until_ready` **silently swallows `TimeoutError`** and only logs. It does NOT re-raise. The codex-version call `await client.wait_until_ready(timeout=settings.ib_request_timeout_seconds)` (their `instrument_service.py:379`) would return normally on a dead gateway and subsequent `get_instrument()` would hang for the full `request_timeout_secs` per symbol.

Similarly, `_connect()` at `connection.py:45-99` catches `ConnectionError`, `TimeoutError`, `asyncio.CancelledError`, and `Exception` and only logs — it does NOT re-raise either. `_start_async` at `client.py:198` has `while not self._is_ib_connected.is_set():` — a dead gateway loop-retries forever unless `IB_MAX_CONNECTION_ATTEMPTS` is set.

**Sources:**

1. venv source `.../client/client.py:362-376` and `.../client/connection.py:45-99` — read 2026-04-18
2. venv source `.../client/client.py:181-234` — `_start_async` — read 2026-04-18

**Design impact:** The CLI MUST NOT trust `wait_until_ready` alone for the "is the gateway actually reachable" check. After `wait_until_ready(timeout=settings.ib_connect_timeout_seconds)` returns, the CLI must explicitly test `client._is_ib_connected.is_set()` (or better, `client._is_client_ready.is_set()`) and fail-fast with the named-env-vars error message from US-003 if not connected. The plan's assumption of `await wait_until_ready(...)` as a connect fence is **insufficient** — it needs a post-call assertion.

Alternative pattern: the CLI can wrap the `wait_until_ready` call in its own `asyncio.wait_for(..., timeout=ib_connect_timeout_seconds)` PLUS an explicit readiness probe after. Simpler still: use `asyncio.wait_for(client._is_client_ready.wait(), timeout=ib_connect_timeout_seconds)` directly and catch `TimeoutError` ourselves, instead of going through the Nautilus wrapper that swallows it.

**Test implication:** Test that a dead-gateway (factory raises `ConnectionRefusedError` from `asyncio.open_connection`, OR event never sets) produces a non-zero exit within `ib_connect_timeout_seconds`, NOT just a log line. Cover BOTH failure modes: (a) TCP connect fails, (b) TCP connects but the managedAccounts message never arrives (`_is_ib_connected` never sets).

---

### nautilus-trader — client disconnect + cached-registry teardown (US-005)

**Versions:** ours=1.223.0

**Verified flow** (venv source `client/client.py:275-308` + `connection.py:112-125`):

- `InteractiveBrokersClient` inherits `Component` (Cython) which has `cpdef void stop()` — **SYNCHRONOUS**.
- `Component.stop()` triggers `self._stop()` (a sync callable), which in `InteractiveBrokersClient._stop` does `self._create_task(self._stop_async())` — this **schedules** the stop but does NOT await it.
- `_stop_async()` cancels 4 async tasks and calls `self._eclient.disconnect()` (sync ibapi call that shuts the TCP socket).
- There is a `_disconnect()` async method at `connection.py:112-125` but it's intended for internal reconnect flows — the public shutdown path is `client.stop()`.

**Globals are NOT cleared**:

```python
# factories.py:42-44
GATEWAYS: dict[tuple, DockerizedIBGateway] = {}
IB_CLIENTS: dict[tuple, InteractiveBrokersClient] = {}
IB_INSTRUMENT_PROVIDERS: dict[tuple, InteractiveBrokersInstrumentProvider] = {}
```

There is **NO `clear()`** function, no `invalidate()`, no `close_all_clients()` on the factory module. Within a single Python process, a re-invocation with the same `(host, port, client_id)` returns the SAME cached `InteractiveBrokersClient` instance — even if `.stop()` was called on it.

**Good news:** The CLI is a short-lived `typer` process. Each `msai instruments refresh` invocation is a fresh Python interpreter → fresh module state → empty `IB_CLIENTS` dict. The 60s re-run test in US-005 actually tests IB Gateway's view of the client_id, NOT Nautilus caching. IB Gateway releases the client_id slot as soon as the TCP socket closes via `_eclient.disconnect()`.

**In-process re-use risk**: If the CLI ever calls `refresh` twice in one process (unit tests!), the second invocation returns a cached, possibly-stopped client. Tests must monkeypatch or manually `IB_CLIENTS.clear()` between cases.

**Sources:**

1. venv source `.../client/client.py:275-308` — read 2026-04-18
2. venv source `.../adapters/interactive_brokers/factories.py:42-44, 120-142` — read 2026-04-18
3. [Nautilus IB docs](https://nautilustrader.io/docs/nightly/integrations/ib/) — accessed 2026-04-18 (confirms `client.disconnect()` / `client.stop()` releases IB Gateway slot)

**Design impact:**

1. The CLI's `try/finally` block MUST call `client.stop()` (sync) in the `finally`. Then the operator's next `msai instruments refresh` invocation (new process) starts with a fresh factory state and IB Gateway has already released the `client_id=999` slot.
2. Since `stop()` is sync-but-async-scheduled, the CLI should also **`await asyncio.sleep(0)` or `await client._stop_async()`** after `client.stop()` to ensure the disconnect actually completes before process exit. Preferred: call `client._stop_async()` directly from the async teardown since we already have an event loop and it's the actual shutdown coroutine `client.stop()` schedules.
3. Unit tests that re-instantiate the CLI function in one process MUST do `from nautilus_trader.adapters.interactive_brokers.factories import IB_CLIENTS, IB_INSTRUMENT_PROVIDERS, GATEWAYS; IB_CLIENTS.clear(); IB_INSTRUMENT_PROVIDERS.clear(); GATEWAYS.clear()` between tests (or use a `pytest` fixture that wraps this).
4. Plan Open Question #1 ("Is `client.disconnect()` sufficient...") — ANSWER: yes, PROVIDED the CLI is a separate process per invocation. No explicit factory-`clear()` needed across process boundaries.

**Test implication:**

- Unit test: assert `client.stop()` (or `_stop_async`) is called in the `finally` block even when `get_instrument()` raises mid-batch.
- Unit test: provide a pytest fixture that clears the three factory globals after each test.
- Paper smoke: run `msai instruments refresh` twice in succession with `time.sleep(5)` between, assert both exit 0 AND the second invocation's stderr doesn't contain "client id already in use" or IB error 326.

---

### nautilus-trader — `MessageBus` + `Cache` + `LiveClock` construction

**Versions:** ours=1.223.0

**Verified signatures** (venv source):

- `Cache()` — `cache/cache.pyx:98-106` — accepts `(database=None, config=None)`, both optional. Calling `Cache()` with no args is valid.
- `MessageBus(TraderId, Clock, ...)` — `common/component.pyx:2227-2236` — requires `trader_id` and `clock` positional; other args optional.
- `LiveClock()` — no required args.

**The codex-version precedent** (`codex-version/backend/src/msai/services/nautilus/instrument_service.py:64-66`) uses exactly:

```python
self._clock = LiveClock()
self._cache = Cache()
self._message_bus = MessageBus(TraderId(settings.nautilus_trader_id), self._clock)
```

**Sources:**

1. venv source `.../cache/cache.pyx:98-106` — read 2026-04-18
2. venv source `.../common/component.pyx:2227-2236` — read 2026-04-18
3. codex-version `backend/src/msai/services/nautilus/instrument_service.py:62-66` — read 2026-04-18

**Design impact:**

- Use a sentinel trader_id like `TraderId("MSAI-CLI-REFRESH-001")` (Nautilus format: `NAME-NNN`, max 3-digit numeric suffix). NOT `_derive_trader_id(deployment_slug)` — that's live-path only.
- The `loop` passed to `get_cached_ib_client` should be obtained via `asyncio.get_running_loop()` inside the `async def _run()` nested function (matches codex `instrument_service.py:410`). Do NOT call `asyncio.get_event_loop()` — deprecated in 3.12, see MEMORY.md "uvloop policy" note.

**Test implication:** Construction is a simple wiring step. Unit tests can mock the entire factory chain. No separate test needed for the construction pattern itself.

---

### nautilus-trader — `InteractiveBrokersInstrumentProviderConfig` schema

**Versions:** ours=1.223.0

**Verified fields** (venv source `adapters/interactive_brokers/config.py:88-189`):

```python
class InteractiveBrokersInstrumentProviderConfig(InstrumentProviderConfig, frozen=True):
    symbology_method: SymbologyMethod = SymbologyMethod.IB_SIMPLIFIED
    load_contracts: frozenset[IBContract] | None = None
    build_options_chain: bool | None = None
    build_futures_chain: bool | None = None
    min_expiry_days: int | None = None
    max_expiry_days: int | None = None
    cache_validity_days: int | None = None
    # (plus: load_ids, convert_exchange_to_mic_venue, symbol_to_mic_venue, pickle_path, filter_sec_types)
```

**All field names MSAI currently uses at `live_instrument_bootstrap.py:301-305`** (`symbology_method`, `load_contracts`, `cache_validity_days`) are still present in 1.223. No renames between 1.222→1.223.

**Sources:**

1. venv source `.../adapters/interactive_brokers/config.py:88-193` — read 2026-04-18
2. [Nautilus 1.223 release](https://github.com/nautechsystems/nautilus_trader/releases/tag/v1.223.0) — accessed 2026-04-18

**Design impact:** No impact — our current usage is aligned. The CLI reuses `SymbologyMethod.IB_SIMPLIFIED` (same default as live path).

**Test implication:** Standard coverage sufficient. No new tests needed specifically for the config schema.

---

### nautilus-trader — `InteractiveBrokersInstrumentProvider.get_instrument` return shape

**Versions:** ours=1.223.0

**Verified behavior** (venv source `providers.py:118-149`):

```python
async def get_instrument(self, contract: IBContract) -> Instrument | None:
    if self._is_filtered_sec_type(contract.secType):
        self._log.warning(...)
        return None
    contract_id = contract.conId
    instrument_id = self.contract_id_to_instrument_id.get(contract_id)
    if instrument_id:
        instrument = self.find(instrument_id)
        if instrument is not None:
            return instrument
    # ... non-cached path ...
    instrument_ids = await self.load_with_return_async(contract)
    if instrument_ids is None:
        self._log.error(...)
        raise ValueError(f"Instrument not found for contract {contract}")
    instrument = self.find(instrument_ids[0])
    if instrument is None:
        raise ValueError(f"Instrument not found for contract {contract}")
    return instrument
```

- Returns `Instrument` on success.
- Returns `None` ONLY if `contract.secType` is in `filter_sec_types` (warn + return).
- Raises `ValueError` on "not found" (load failed).

The existing `IBQualifier.qualify` at `ib_qualifier.py:182-194` correctly handles both paths (treats `None` as ValueError with its own message). The assumption in `IBQualifier` is verified.

**Sources:**

1. venv source `.../adapters/interactive_brokers/providers.py:118-149` — read 2026-04-18
2. Existing `claude-version/backend/src/msai/services/nautilus/security_master/ib_qualifier.py:182-194` — read 2026-04-18

**Design impact:** No impact — `IBQualifier.qualify` already handles the contract correctly. The CLI wires to `IBQualifier.qualify_many` which delegates to `qualify`.

**Test implication:** The CLI unit tests should cover both provider return paths:

1. `provider.get_instrument(contract)` returns an `Instrument` → successful upsert.
2. `provider.get_instrument(contract)` raises `ValueError` → batch fails mid-way, `finally` runs cleanup, exit non-zero. Rows already committed stay committed (PRD edge case in US-001).

---

### pydantic.AliasChoices (for new Settings fields)

**Versions:** ours=pydantic 2.10+ / pydantic-settings 2.13.1

**Verified usage** (venv source `pydantic/__init__.py:24, 119, 288`):

- `AliasChoices` is exported from the top-level `pydantic` namespace (not `pydantic.aliases`) — matches the existing MSAI usage pattern at `core/config.py:74`.
- No signature change vs. the version MSAI originally adopted.

**Sources:**

1. venv source `.../pydantic/__init__.py:24, 119, 288` — read 2026-04-18
2. Existing `claude-version/backend/src/msai/core/config.py:72-78` (working `AliasChoices` usage) — read 2026-04-18

**Design impact:** No impact — use the same `AliasChoices` pattern that already wires `IB_HOST`/`IB_GATEWAY_HOST`. New fields per the PRD:

```python
ib_connect_timeout_seconds: int = Field(default=5, validation_alias=AliasChoices("IB_CONNECT_TIMEOUT_SECONDS"))
ib_request_timeout_seconds: int = Field(default=30, validation_alias=AliasChoices("IB_REQUEST_TIMEOUT_SECONDS"))
ib_instrument_client_id: int = Field(default=999, validation_alias=AliasChoices("IB_INSTRUMENT_CLIENT_ID"))
```

**Test implication:** Existing `tests/unit/test_config_ib_env.py` is the precedent for env-var alias testing. Add parallel test cases for the 3 new fields (each env var maps to the right field, defaults apply when unset).

---

### typer — CLI option handling

**Versions:** ours=typer 0.24.1

**Verified usage** (existing cli.py line 694-724):

- `typer.Option(..., "--symbols", help=...)` — required option pattern, ellipsis sentinel.
- `typer.Option("default", "--flag", help=...)` — optional with default.
- `typer.BadParameter(msg)` — surfaces as red error to stderr, exit code 2.
- `_fail(msg)` helper in cli.py — calls `typer.echo(..., err=True)` + `raise typer.Exit(1)`.

**Sources:**

1. Existing `claude-version/backend/src/msai/cli.py:694-772` — read 2026-04-18
2. typer 0.24.1 pinned via uv.lock — no known breaking changes from the 0.15 declared in pyproject; `uv sync` resolves to 0.24.1 which is backward-compatible for `Option`/`BadParameter`/`Typer` public API.

**Design impact:** No impact. The IB branch of `instruments_refresh` replaces the `_fail("The `--provider interactive_brokers` path is deferred...")` block at `cli.py:747-756` with the actual wiring. Existing error helpers (`_fail`, `typer.BadParameter`) are reused.

**Test implication:** Standard coverage sufficient. Existing cli tests cover Option parsing.

---

### Interactive Brokers API — `reqContractDetails` pacing

**Versions:** IB TWS API 10.43 (via `nautilus_trader[ib]`)

**Verified behavior**:

- **Global** pacing limit: ≤50 msg/sec across ALL API traffic for a given client_id (not specific to `reqContractDetails`).
- TWS 9.76+ introduced automatic throttling (`SetConnectOptions("+PACEAPI")`) — requests over 50/sec get queued internally rather than disconnecting the client.
- Nautilus's `InteractiveBrokersClient._await_request` at `client.py:522+` already respects pacing internally.
- The existing `IBQualifier.qualify_many` iterates **sequentially** (`ib_qualifier.py:196-210`) to avoid pacing spikes.

**Sources:**

1. [TWS API Order Limitations](https://interactivebrokers.github.io/tws-api/order_limitations.html) — accessed 2026-04-18
2. [TWS API Historical Limitations](https://interactivebrokers.github.io/tws-api/historical_limitations.html) — accessed 2026-04-18
3. [MultiCharts pacing reference](https://www.multicharts.com/trading-software/index.php?title=Interactive_Brokers_Pacing_Violation) — accessed 2026-04-18

**Design impact:** No impact — MSAI already serializes qualification in `IBQualifier.qualify_many`. For the closed-universe (5 symbols), we are **well under** 50/sec — a 5-symbol batch takes ~5 round-trips × ~100–500ms each = comfortably within limits. The PRD's 100-symbol estimate (~2 seconds) matches reality.

**Test implication:** Paper smoke test should log the wall-clock time for a 2-symbol batch (AAPL + ES per success metric) as a sanity check (target: <5s including connect/disconnect).

---

### Interactive Brokers — short-lived connect/qualify/disconnect pattern

**Verified behavior**:

- Connect time on local IB Gateway: typically 1-3 seconds including TCP handshake, protocol version exchange, and `managedAccounts` response.
- Same `client_id` within 60s of a clean disconnect: **IB Gateway releases the slot immediately** upon TCP close; no 60s cooldown for clean exits.
- Same `client_id` after a SIGKILL (no TCP FIN): IB Gateway waits ~30-60s for its own keepalive to detect the dead socket, THEN the slot is reusable. This is IB Gateway behavior, not Nautilus.
- Alternative option seen in docs (`HistoricInteractiveBrokersClient`) exists but (a) calls `init_logging` as a side effect that pollutes the CLI process's logger, (b) has no `disconnect` method implemented in the pinned version. **Do NOT use `HistoricInteractiveBrokersClient` for this CLI.**

**Sources:**

1. [Nautilus IB integration docs](https://nautilustrader.io/docs/nightly/integrations/ib/) — accessed 2026-04-18
2. venv source `.../adapters/interactive_brokers/historical/client.py:59-181` — read 2026-04-18

**Design impact:**

1. Stick with the `get_cached_ib_client` + `get_cached_interactive_brokers_instrument_provider` path (matches codex-version precedent and is consistent with the live wiring).
2. US-005's "60s re-run test" will pass on clean-exit paths. The SIGKILL edge case documented in US-005 ("First invocation killed with SIGKILL → operator may need to retry") is accurate — acceptable per PRD.

**Test implication:** Include a timing assertion on the smoke test: clean re-run within 10s must succeed.

---

## nautilus.md Gotchas Status Check

| Gotcha                                                                             | Original concern                                                        | Status in 1.223.0                                                                                                                       | Impact on this PR                                                                                                                                                                                                                            |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **#3** — Two TradingNodes with same `ibg_client_id` silently disconnect each other | Live subprocess uses derived ids 100-200 range; we pick 999 out-of-band | Unchanged — issue is IB Gateway, not Nautilus                                                                                           | Design impact: `ib_instrument_client_id=999` default must not overlap live-node derivation space (100-200 range per `_derive_client_id` at `live_node_config.py:126-153`). Safe.                                                             |
| **#5** — `await node.start_async()` returns successfully even if IB connect failed | Silent failure mode                                                     | Unchanged — applies to `TradingNode`, but the same pattern leaks into `InteractiveBrokersClient` (see `wait_until_ready` finding above) | Design impact: CLI MUST do explicit post-connect readiness check. Matches finding above.                                                                                                                                                     |
| **#6** — IB Gateway port 4002 (paper) + live account_id fails silently             | Port/account mismatch                                                   | Unchanged — Nautilus does not validate this combo                                                                                       | Design impact: CLI preflight MUST call `_validate_port_account_consistency(port, account_id)` at `live_node_config.py:177` BEFORE any IB connect. Matches US-004.                                                                            |
| **#20** — `dispose()` not called → Rust logger + sockets leak                      | Leak on abnormal exit                                                   | Unchanged — the Rust logger is a process-wide singleton                                                                                 | Design impact: Short-lived CLI process exits naturally → OS closes everything. Manual `dispose()` not strictly required. BUT: `client.stop()` is still needed to cleanly disconnect from IB Gateway so the client_id slot releases (US-005). |

---

## Not Researched (with justification)

- **Database migration patterns** — PRD Section 6 ("New Data Models: None"). No tables added.
- **Async Postgres/SQLAlchemy patterns** — existing, working. CLI reuses `async_session_factory` and `SecurityMaster._upsert_definition_and_alias`.
- **Polygon.io / Databento client APIs** — out of scope per PRD Section 2 non-goals. Databento branch already ships.
- **Options resolution** — explicit non-goal per PRD. `IBQualifier.qualify` does not use the options/futures-chain path.
- **Multi-login gateway routing / `GatewayRouter`** — explicit non-goal per PRD (PR #30 separate scope).
- **`InstrumentId.from_str` / `parse_instrument`** — these are internals of `get_instrument`; `IBQualifier` already handles them. No new MSAI-side wiring.
- **FastAPI router / request lifecycle** — CLI is standalone, not an API route.

---

## Open Risks

1. **Nautilus factory globals are process-wide and never cleared.** Within a single Python process (i.e. pytest running multiple CLI invocations via an in-process helper), re-invocation returns the cached client even after `.stop()`. **Mitigation:** pytest fixture that clears `IB_CLIENTS`, `IB_INSTRUMENT_PROVIDERS`, `GATEWAYS` between tests. Do not use `monkeypatch.setattr` on the module globals — they're `dict` references used elsewhere; actual `.clear()` is needed. This is NOT a production risk (CLI is a fresh process per invocation) but WILL cause flaky unit tests if ignored.

2. **`wait_until_ready` silently swallows `TimeoutError`.** Calling it alone as a connect fence produces a "dead gateway looks ready" bug. The CLI MUST either (a) post-call assert `client._is_ib_connected.is_set()`, or (b) call `asyncio.wait_for(client._is_client_ready.wait(), timeout=...)` directly. The codex-version precedent does not show this explicit assertion — **verify before copying the codex pattern verbatim**.

3. **`InteractiveBrokersClient._start_async` is a retry loop without bounded attempts** unless `IB_MAX_CONNECTION_ATTEMPTS` env var is set. Without that env var, a dead gateway → client loop-retries forever in the background task. The CLI's `wait_until_ready(timeout=5)` bounds the WAIT but does not stop the retry task. On CLI shutdown, `client.stop()` cancels the loop via task cancellation. **Mitigation:** explicitly set `IB_MAX_CONNECTION_ATTEMPTS=1` (or `2`) via env in the CLI before constructing the client, OR rely on `client.stop()` to cancel the task on the finally path (acceptable).

4. **`client.stop()` is synchronous-scheduled, not synchronous-blocking.** `cpdef void stop()` returns immediately after scheduling `_stop_async` as a task. A too-quick process exit after `client.stop()` can leave the TCP close mid-flight, delaying IB Gateway's client_id release. **Mitigation:** after `client.stop()`, the CLI should `await asyncio.sleep(0.5)` or (better) call `await client._stop_async()` directly to block until disconnect completes. This is the critical US-005 contract.

5. **`request_timeout_secs` vs `ib_request_timeout_seconds` vs `connection_timeout` naming confusion.** Nautilus uses:
   - `request_timeout_secs: int` on `InteractiveBrokersClient.__init__` and the factory — per-request round-trip.
   - `connection_timeout` on `InteractiveBrokersDataClient.__init__` (distinct from request timeout) — connect handshake.
     The CLI bypasses `InteractiveBrokersDataClient` entirely (we use the raw `InteractiveBrokersClient`), so `connection_timeout` does not apply. Our `ib_connect_timeout_seconds` (5s default) is the CLI-side `asyncio.wait_for` budget wrapping the `_is_client_ready.wait()` — NOT a Nautilus parameter. Keep this distinction clear in variable naming AND in the preflight-log banner so operators don't grep for `connection_timeout` in Nautilus logs (won't find it).

6. **1.224.0 exists but we are pinned at 1.223.0.** The 1.224 delta has 3 IB bugfixes (BarType/str comparison, historical bar crash, contract details parsing) and NO breaking changes. **Not blocking** — this PR can ship on 1.223. Consider a separate maintenance bump in a follow-up if operators hit any of the fixed bugs.

---

## Summary for Design Phase

**Locked-in API shape** (verified against venv source, not hallucinated):

```python
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersInstrumentProviderConfig,
    SymbologyMethod,
)
from nautilus_trader.adapters.interactive_brokers.factories import (
    IB_CLIENTS,  # for test teardown
    IB_INSTRUMENT_PROVIDERS,  # for test teardown
    get_cached_ib_client,
    get_cached_interactive_brokers_instrument_provider,
)
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId

loop = asyncio.get_running_loop()
clock = LiveClock()
cache = Cache()
msgbus = MessageBus(TraderId("MSAI-CLI-REFRESH-001"), clock)

client = get_cached_ib_client(
    loop=loop,
    msgbus=msgbus,
    cache=cache,
    clock=clock,
    host=settings.ib_host,
    port=settings.ib_port,
    client_id=settings.ib_instrument_client_id,
    request_timeout_secs=settings.ib_request_timeout_seconds,
)
try:
    # Connect fence — DO NOT rely on wait_until_ready alone.
    await asyncio.wait_for(
        client._is_client_ready.wait(),
        timeout=settings.ib_connect_timeout_seconds,
    )
    provider = get_cached_interactive_brokers_instrument_provider(
        client,
        clock,
        InteractiveBrokersInstrumentProviderConfig(
            symbology_method=SymbologyMethod.IB_SIMPLIFIED,
        ),
    )
    qualifier = IBQualifier(provider)
    instruments = await qualifier.qualify_many(specs)
    # ... upsert via SecurityMaster ...
finally:
    client.stop()  # schedules _stop_async
    await client._stop_async()  # block until disconnect completes (or inline equivalent)
```

**Gaps vs. plan skeleton at `docs/plans/2026-04-17-db-backed-strategy-registry.md:2321-2335`:**

- Skeleton likely omits the "wait_until_ready silently swallows" gotcha → must adjust the connect fence
- Skeleton likely omits the "stop() is async-scheduled" gotcha → must adjust the teardown
- Skeleton's import names appear correct (`get_cached_ib_client`, `get_cached_interactive_brokers_instrument_provider`) — confirmed against venv

**Test surface to ensure:**

- Unit: factory-chain mock (asserts exact kwargs), stop-in-finally (asserts cleanup runs on mid-batch qualify failure), pydantic-alias env var round-trip (3 new fields), closed-universe rejection before connect (US-006), port/account preflight (US-004).
- Integration (paper-gateway smoke): happy-path qualify AAPL + ES, idempotent re-run, dead-gateway fast-fail within `ib_connect_timeout_seconds`, back-to-back re-run 5s apart succeeds (US-005), wall-clock <10s for 2-symbol batch.
