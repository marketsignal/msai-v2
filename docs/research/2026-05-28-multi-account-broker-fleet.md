# Research: multi-account-broker-fleet (PR 1 — split-topology proof + 2-account paper drill)

**Date:** 2026-05-28
**Feature:** Prove Databento-live-data + IB-exec-only split topology on 2 paper IB accounts (DUP733214/DUP733215), with per-account drain and explicit fleet emergency halt.
**Researcher:** research-first agent
**Authoritative decision:** [`docs/decisions/multi-account-broker-fleet.md`](../decisions/multi-account-broker-fleet.md) — council 2026-05-27 + addendum 2026-05-28.

This brief researches the external libraries / APIs the PR 1 implementation will touch. It does **not** revisit the architectural decision — the council + addendum already locked the topology. The job here is to confirm the assumptions the addendum bakes in are true on the pinned package versions, and surface anything that changes the plan.

---

## Libraries Touched

| Library                                                     | Our Version                                       | Latest Stable                          | Breaking Changes                                                                                                     | Source                                                                                                                    |
| ----------------------------------------------------------- | ------------------------------------------------- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `nautilus_trader[ib]`                                       | `>=1.222.0` (verified 1.223.0 in venv)            | 1.228.0 beta (develop), 1.227.x stable | None breaking for this work; several IB/Databento improvements (see #2)                                              | [RELEASES.md](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/RELEASES.md) — accessed 2026-05-28 |
| `databento` (Python SDK)                                    | `>=0.43.0` (only used by us for historical today) | 0.78.0 (2026-05-12)                    | Live client gained `heartbeat_interval_s`, `reconnect_policy`, `add_reconnect_callback`, `is_connected()` since 0.43 | [databento-python GitHub](https://github.com/databento/databento-python) — accessed 2026-05-28                            |
| `ghcr.io/gnzsnz/ib-gateway` (Docker)                        | `stable` (10.45.1g + IBC 3.23.0)                  | `stable` (same channel)                | None — `stable` is rolling                                                                                           | [gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker) — accessed 2026-05-28                             |
| Redis (halt-key patterns; internal)                         | 7-alpine (compose)                                | 7.x                                    | N/A                                                                                                                  | [docker-compose.dev.yml:79](../../docker-compose.dev.yml#L79) — accessed 2026-05-28                                       |
| `ib_async` / `ibapi` (transitive via `nautilus_trader[ib]`) | pinned by Nautilus extra                          | n/a                                    | N/A — exec-only path never calls market-data APIs                                                                    | venv source (see #3)                                                                                                      |

Frontend (`frontend/package.json`): treated as low priority per the prompt; PR 1 surfaces account context on existing endpoints but does not reorganise the UI. No new frontend dependencies expected for PR 1.

---

## Per-Library Analysis

### 1. Databento Python SDK — live mode

**Versions:** ours pinned `databento>=0.43.0`; latest 0.78.0 (May 12, 2026).

**Where MSAI uses it today.** `backend/src/msai/services/data_sources/databento_client.py:121,189` uses `databento.Historical(...)` only — `.timeseries.get_range(...)` for OHLCV historical pulls. **MSAI does not use `databento.Live` anywhere.** The addendum's "wire Databento for live" is genuinely new code for PR 1.

**Important — Nautilus does not call the Python SDK in the live path.** `nautilus_trader/adapters/databento/data.py:166` ([venv source](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/data.py)) wires `nautilus_pyo3.DatabentoLiveClient` — a Rust-side client. The Python `databento.Live` class is **not** in the Nautilus live path. So most of the Python SDK's live surface (`heartbeat_interval_s`, `add_reconnect_callback`, `is_connected()`, `ReconnectPolicy`) is **not directly available to MSAI** through the Nautilus adapter.

**What Nautilus exposes to MSAI (verified from venv).** The pyo3-wrapped client at [`nautilus_pyo3.pyi:7322-7351`](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/core/nautilus_pyo3.pyi) is:

```
class DatabentoLiveClient:
    def __init__(self, key, dataset, publishers_filepath, use_exchange_as_venue,
                 bars_timestamp_on_close=True, reconnect_timeout_mins=None): ...
    @property
    def key(self) -> str: ...
    @property
    def dataset(self) -> str: ...
    def is_running(self) -> bool: ...
    def is_closed(self) -> bool: ...
    def subscribe(self, schema, instrument_ids, stype_in=None, start=None, snapshot=False) -> dict: ...
    def start(self, callback, callback_pyo3) -> Awaitable[None]: ...
    def close(self) -> None: ...
```

There is **no** Python-side hook for: heartbeat interval, per-message system messages, reconnect callbacks, last-event timestamps, or "is connected" beyond `is_running()` / `is_closed()`. Reconnect behaviour is controlled entirely by the `reconnect_timeout_mins` arg passed at construction (configurable via `DatabentoDataClientConfig.reconnect_timeout_mins`, default 10 minutes — `nautilus_trader/adapters/databento/config.py:71`). Nautilus internally translates Databento `SystemMsg`/`ErrorMsg` into log lines, not into events the MSAI strategy or supervisor can observe.

**Recommended pattern for PR 1.** Wire `DatabentoDataClientConfig(api_key=settings.databento_api_key, instrument_ids=[...], venue_dataset_map={...})` into `TradingNodeConfig.data_clients`. Set `reconnect_timeout_mins=10` (the default) for PR 1 — indefinite retries are explicitly warned against in the integration docs. Do **not** depend on Python-SDK-side heartbeat/stale APIs; they are inaccessible from this code path.

**Recommended pattern for PR 1b (the deferred safety follow-up).** Per-account/per-node/per-required-feed freshness has to be measured by **MSAI code observing bar event arrivals** — wall-clock now() minus the last bar's `ts_event` for each (account, instrument_id, bar_type) tuple, against a session-aware expected interval. The freshness signal cannot be sourced from the Nautilus `DatabentoLiveClient` wrapper. PR 1b will need either (a) a new MSAI service that subscribes to the message bus and tracks per-instrument freshness, or (b) an upstream fork/patch to expose heartbeat/last-event from `nautilus_pyo3.DatabentoLiveClient`. **(a) is far cheaper and is what the PRD assumes.**

**Sources:**

1. [databento-python GitHub README](https://github.com/databento/databento-python) — accessed 2026-05-28 (latest 0.78.0, 101 releases)
2. [Databento Live client source — WebFetch of `databento/live/client.py`](https://github.com/databento/databento-python/blob/main/databento/live/client.py) — accessed 2026-05-28 (constructor signature: `Live(key, gateway, port, ts_out, heartbeat_interval_s, reconnect_policy, slow_reader_behavior, compression)`; methods include `is_connected()`, `add_reconnect_callback()` — none of which Nautilus surfaces)
3. [Databento Live API reference — Live client](https://databento.com/docs/api-reference-live/client/live) — accessed 2026-05-28
4. [Databento System Messages](https://databento.com/docs/api-reference-live/basics/system-messages) — accessed 2026-05-28 (`SystemMsg`, `ErrorMsg`, `SymbolMappingMsg`)
5. venv: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/core/nautilus_pyo3.pyi:7322-7351` (verified Python signature of `DatabentoLiveClient`)
6. venv: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/data.py:166` (wires `nautilus_pyo3.DatabentoLiveClient`)
7. venv: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/config.py:53-71` (full `DatabentoDataClientConfig` field set)
8. MSAI: `backend/src/msai/services/data_sources/databento_client.py:121` (historical-only use today)

**Design impact (PR 1).** Stale-detection cannot piggyback on the Databento Python SDK's heartbeat or reconnect callbacks — Nautilus hides them. PR 1 wires Databento live cleanly via `DatabentoDataClientConfig` with `reconnect_timeout_mins=10`; PR 1b must build freshness tracking on top of bar/tick arrivals into the strategy/data engine, not at the Nautilus client boundary. The PRD's deferral of data-stale to PR 1b is correct.

**Test implication.** PR 1 unit tests for live-data wiring should assert `DatabentoDataClientConfig` is constructed with the expected `api_key`, `instrument_ids`, `venue_dataset_map`, and `reconnect_timeout_mins`. Integration tests should NOT try to assert "client is connected" via SDK surface — the only Nautilus-observable signal is `node.is_running` + actual bar arrivals on the message bus. Drill verification is a manual count: did each account observe ≥1 bar from Databento during the drill window?

---

### 2. Nautilus DatabentoDataClient + DatabentoLiveDataClientFactory

**Versions:** ours `nautilus_trader[ib]>=1.222.0`, venv resolves to 1.223.0; latest 1.228.0 beta on develop. Stable line currently at 1.227.x.

**Verified classes (venv source).**

- [`nautilus_trader/adapters/databento/data.py:88`](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/data.py) — `class DatabentoDataClient(LiveMarketDataClient)` — the runtime client.
- [`nautilus_trader/adapters/databento/factories.py:114`](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/factories.py) — `class DatabentoLiveDataClientFactory(LiveDataClientFactory)` — passed to `TradingNode.add_data_client_factory("DATABENTO", DatabentoLiveDataClientFactory)`.
- [`nautilus_trader/adapters/databento/config.py:20-71`](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/config.py) — `class DatabentoDataClientConfig(LiveDataClientConfig, frozen=True)`.

**Full `DatabentoDataClientConfig` field set** (verified in venv at `config.py:20-71`):

```python
api_key: str | None = None              # falls back to DATABENTO_API_KEY env
http_gateway: str | None = None
live_gateway: str | None = None
use_exchange_as_venue: bool = True      # IMPORTANT — see symbology shim below
timeout_initial_load: float | None = 15.0
mbo_subscriptions_delay: float | None = 3.0
bars_timestamp_on_close: bool = True
instrument_ids: list[InstrumentId] | None = None
parent_symbols: dict[str, set[str]] | None = None
venue_dataset_map: dict[str, str] | None = None
reconnect_timeout_mins: int | None = 10
```

**Keying in TradingNodeConfig.** Nautilus convention (per integration docs + factory contract): `data_clients={DATABENTO: DatabentoDataClientConfig(...)}` where `DATABENTO = "DATABENTO"` (constant exported from `nautilus_trader.adapters.databento.constants`). One key per registered factory; one `DatabentoDataClient` instance per TradingNode.

**How subscriptions route by instrument_id.venue (CRITICAL for the symbology shim).** `nautilus_trader/adapters/databento/data.py:368` resolves `dataset = self._loader.get_dataset_for_venue(instrument_id.venue)` for **every** subscribe call. The dataset is required — it's how Databento knows whether to talk to `DBEQ.BASIC` vs `GLBX.MDP3` vs `OPRA.PILLAR`. If a strategy subscribes to `AAPL.IBKR`, the loader will:

- Look up `Venue("IBKR")` in its publishers map. **IBKR is not a Databento publisher.** Without a `venue_dataset_map={"IBKR": "DBEQ.BASIC"}` override, the loader raises `ValueError: No Databento dataset for venue 'IBKR'` ([loaders.py:115](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/loaders.py#L115)).
- Even with `venue_dataset_map={"IBKR": "DBEQ.BASIC"}`, the **outbound subscription** would tell Databento to subscribe to `AAPL.IBKR` on dataset `DBEQ.BASIC`, but Databento's native symbol is `AAPL` on publisher `XNAS` — the symbology has to match the publisher's symbol space, not the strategy's canonical `.IBKR` suffix.

This forces the shape of the symbology shim:

- **Option A — `use_exchange_as_venue=True` (default).** Databento publishes bars with native venue suffixes (`AAPL.XNAS`, `ESH4.GLBX`, etc.). The shim must:
  - On the outbound path, translate strategy `BarType("AAPL.IBKR-1-MINUTE-LAST-EXTERNAL")` → the Databento subscription using native symbol+venue, AND publish bars onto the message bus tagged with the canonical `.IBKR` venue suffix that strategies actually subscribed to. Concretely: the shim is an interceptor on the message bus that consumes `Bar` events with native venue (`AAPL.XNAS`) and re-publishes them with the strategy-visible venue (`AAPL.IBKR`), preserving provider/dataset/native-venue in event metadata for audit.
- **Option B — `use_exchange_as_venue=False`.** Bars arrive tagged with a single Databento publisher-style venue. Strategies still subscribed to `.IBKR` — same gap, just smaller. Still requires a re-publish step.

**Neither option lets you pretend `.IBKR` is the Databento venue.** The shim sits OUTSIDE the adapter (in MSAI code), as the addendum's "compatibility shim of decision (a)" states. The `venue_dataset_map` field on `DatabentoDataClientConfig` is a **dataset alias**, not a venue rename — it cannot collapse `XNAS` → `IBKR`.

**Subscription pre-loading.** `DatabentoDataClient._connect()` ([data.py:193](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/data.py#L193)) only loads instruments for entries pre-populated in `config.instrument_ids`. Dynamic loading mid-flight is supported (`_ensure_subscribed_for_instrument`, data.py:366) but is slow + serial — match Nautilus gotcha #11 + the project's "Pre-load every instrument at startup" architectural rule.

**Reconnect behavior.** [Integration docs](https://nautilustrader.io/docs/latest/integrations/databento/) (accessed 2026-05-28 via WebFetch): default `reconnect_timeout_mins=10` uses exponential backoff capped at 60s. Indefinite (`None`) caps backoff at 10 minutes but explicitly warns it "can mask persistent configuration or authentication issues." Subscriptions are automatically restored after reconnect.

**Scheduled maintenance gotcha.** Databento restarts gateways every Sunday (UTC times vary by dataset — CME at 09:30 UTC). Live nodes should expect a short disconnect-then-reconnect window. Combined with the reconnect-storm scenario in blocking objection #9, this is an expected event PR 1b's stale-detection must tolerate.

**Recent release notes (1.224 → 1.228 develop).** Verbatim from [RELEASES.md](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/RELEASES.md):

- 1.227: "Fixed Databento market data price precision preservation (#4002)", "Fixed Databento MBP10 panic on undefined depth levels (#4046)", "Fixed Databento decoder gaps on dbn 0.58 wire data: skip `'I'` (Index) classes and map new stat types".
- 1.228 beta: "Added Databento `set_price_precision` and `get_price_precisions` methods on the data loader and historical client".

None of these is breaking for our use; the precision fix in 1.227 is worth picking up.

**Sources:**

1. [Nautilus Databento integration docs](https://nautilustrader.io/docs/latest/integrations/databento/) — accessed 2026-05-28 (via WebFetch of `develop/docs/integrations/databento.md`)
2. [Nautilus RELEASES.md (raw)](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/RELEASES.md) — accessed 2026-05-28
3. venv: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/databento/{data.py:88,166,193,368, factories.py:114, config.py:20-71, loaders.py:93-117}`

**Design impact.** Three concrete plan changes for PR 1:

1. **The symbology shim must sit in MSAI code, not in adapter config.** `venue_dataset_map` is a venue→dataset alias, not a venue rename. The shim is a message-bus interceptor that translates strategy `.IBKR` subscriptions to native `(dataset, symbol, native_venue)` outbound, and re-tags inbound bars from native venue back to `.IBKR` for strategy consumption — preserving provider/dataset/native-venue in event metadata for audit (addendum §Symbology).
2. **`DatabentoDataClientConfig.instrument_ids` must be pre-populated at TradingNode-config build time** with the native-venue InstrumentIds (e.g., `AAPL.XNAS`), derived from the strategy's `.IBKR` universe through the instrument registry + asset-class routing rules. No dynamic loading on the critical path (Nautilus gotcha #11 + architectural rule #2).
3. **Pin `reconnect_timeout_mins=10` explicitly** in the live-config builder (it's the default, but make the choice explicit to lock it against a future Nautilus default change). Do not pass `None`.

**Test implication.** Three test surfaces:

1. **Symbology shim unit tests:** parametrized over equities (`AAPL.IBKR` → `(DBEQ.BASIC, AAPL, XNAS)`), futures (`ES.IBKR` → `(GLBX.MDP3, ESH4, GLBX)`), and options (deferred to post-equities per PR 1 scope but the shim shape should be ready). Each direction (outbound subscription resolution + inbound bar re-tagging) gets its own table.
2. **TradingNode boot integration test:** assert `data_clients` keyed `"DATABENTO"` with a `DatabentoDataClientConfig` carrying the populated `instrument_ids` list. Fail-closed at startup if any strategy-subscribed `.IBKR` instrument cannot be resolved to a native (`dataset, symbol, venue`) tuple (blocking objection #6).
3. **No mock for `nautilus_pyo3.DatabentoLiveClient`.** It's Rust-side; mocking it is fragile. Live integration is verified by the drill (Acceptance Criteria step 4 — observe ≥1 bar in each account's path).

---

### 3. Interactive Brokers exec-only mode in Nautilus (the assumption that must not be wrong)

**Versions:** ours `nautilus_trader[ib]>=1.222.0`, venv resolves to 1.223.0.

**Goal:** confirm `InteractiveBrokersExecClientConfig` works in a `TradingNodeConfig` whose `data_clients` does NOT include an IB data client, without any hard dependency on `InteractiveBrokersDataClient` being instantiated in the same node.

**Source-verified conclusion: yes, exec-only is supported. No hard dependency on a data client.**

**Evidence (venv source).**

1. [`adapters/interactive_brokers/execution.py`](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/execution.py) — searched the entire file for `_data_client`, `data_client`, `market_data`, `MarketDataType`, `reqMktData`. **Zero matches.** The execution client neither imports nor references the data client.
2. [`adapters/interactive_brokers/client/connection.py`](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/client/connection.py) — searched for `reqMktData`, `subscribe.*data`, `market_data`. **Zero matches.** The shared `InteractiveBrokersClient` connection layer never auto-subscribes to market data. `_start_async` only opens the socket and waits for `managedAccounts` ([client/client.py:196-227](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/client/client.py#L196)).
3. `reqMktData` lives entirely in `adapters/interactive_brokers/client/market_data.py` (lines 295, 341, 1186). The exec path never imports this module.
4. The factories ([factories.py:258-342](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/factories.py#L258)) construct exec and data clients **independently** through `get_cached_ib_client`. The shared IB client is created lazily on first call to `get_cached_ib_client` from either factory; if only the exec factory ever runs, only the exec-friendly subset is exercised.
5. The exec factory **does** need `get_cached_interactive_brokers_instrument_provider` (factories.py:309), which uses `reqContractDetails`. **`reqContractDetails` is allowed by IB without a market-data subscription** (it's contract metadata, not quotes). [providers.py:316,379,536,604,653](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/providers.py) — all paths go through `client.get_contract_details(contract)`, never `reqMktData`.

**However: there are still real assumptions to validate.**

- **`InteractiveBrokersExecClientConfig.ibg_client_id`.** Each (host, port, client_id) tuple is keyed by `get_cached_ib_client` ([factories.py:120](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/factories.py#L120)). Two TradingNodes against the same IB Gateway with the same `client_id` → Nautilus gotcha #3 (silent disconnect). With two gateway containers per the PRD, this is naturally avoided, but the `IB_CLIENT_ID=30` / `IB_DATA_CLIENT_ID=101` / `IB_EXEC_CLIENT_ID=102` triad in `docker-compose.dev.yml` ([live-supervisor service env, lines 214-216](../../docker-compose.dev.yml#L214)) is global today — PR 1 must scope these per gateway-session-key so the second gateway gets a non-colliding id.
- **`InteractiveBrokersExecClientConfig.account_id`** is mandatory ([execution.py via factories.py:316-319](../../backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/factories.py#L316)). The factory asserts on it. The two paper accounts (DUP733214/DUP733215) ride under one `marin1016test` TWS login — confirm `account_id` selects within the login (IB advisor accounts surface multiple `DU*` sub-accounts under a single login; the existing repo memory `reference_ib_accounts.md` notes this).
- **Reconciliation.** `LiveExecEngineConfig(reconciliation=True)` reconciles open orders + positions on every restart (Nautilus gotcha #10 + #19). In exec-only mode this still works — reconciliation queries IB for orders/fills, not market data. **No change needed.**
- **The "no IB data client" decision means `DELAYED_FROZEN` workaround is irrelevant.** `IB_MARKET_DATA_TYPE` env in `docker-compose.dev.yml:114,218` is dead config in the new topology; safe to leave for backwards-compat but can be dropped in the future.

**IB API itself (the documentary backstop, not Nautilus).** IB Gateway accepts order submission on accounts without active market-data subscriptions — only quote-streaming requires the subscription. The PRD's US-5 verification spike on DUP733213 (the known no-data sub-account from `reference_ib_entitlements.md`) is the empirical proof; this research is the documentary backstop. Static analysis confirms no Nautilus-side code path forces a `reqMktData` call along the order submission flow.

**Sources:**

1. venv: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/execution.py` (full read, lines 1-200 inspected + grep for data-client refs returned zero matches)
2. venv: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/client/connection.py` (grep for reqMktData/market_data returned zero matches)
3. venv: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/client/client.py:181-234` (`_start` / `_start_async` — only opens socket + waits for managedAccounts)
4. venv: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/factories.py:258-342` (exec factory independent of data factory)
5. venv: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/config.py:246-290` (`InteractiveBrokersExecClientConfig` full field set)
6. venv: `backend/.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/providers.py:316,379,536,604,653` (uses `get_contract_details` / `reqContractDetails`, never `reqMktData`)
7. [Nautilus Databento integration docs](https://nautilustrader.io/docs/latest/integrations/databento/) — accessed 2026-05-28 — explicitly mentions pairing "Databento data with Interactive Brokers execution"

**Design impact.** The exec-only assumption is sound at the Nautilus level. Plan changes are minor + already in the decision-doc list:

1. Per-gateway-session `ibg_client_id` allocation — the existing globals (`IB_CLIENT_ID=30`, `IB_EXEC_CLIENT_ID=102`) need to become per-account (e.g., `IB_EXEC_CLIENT_ID_DUP733214=102`, `IB_EXEC_CLIENT_ID_DUP733215=112`). Conflicts with the existing single-account env vars; the migration is additive (new env vars) — drop the old when PR 3 introduces the `BrokerAccount` entity.
2. **No IB data client in the new TradingNodeConfig.** The existing `InteractiveBrokersDataClientFactory` registration must NOT be invoked. Drop the `data_clients={IB: ...}` line; replace with `data_clients={DATABENTO: ...}`. Keep `exec_clients={IB: InteractiveBrokersExecClientConfig(...)}`.
3. The `IB_MARKET_DATA_TYPE` env variable becomes dead config; leave for backwards-compat for one release, then prune.

**Test implication.**

1. **Unit test** the live-config builder: a `TradingNodeConfig` constructed via the per-account factory has `data_clients` keyed `"DATABENTO"` only (no `"IB"`), and `exec_clients` keyed `"IB"` (or whatever the IB constant is). Negative test: assert a misconfiguration that adds IB to `data_clients` fails fast at boot (blocking objection #6).
2. **Integration spike (the PRD's US-5):** place one paper market order on `DUP733213` via an IB Gateway running exec-only (no IB data subscription). This is the empirical falsification; if it fails, PR 1 is cancelled per PRD §5 step 0. Static analysis here predicts it will pass.

---

### 4. Nautilus split data/exec adapter examples

**Versions:** 1.223.0 in venv; integration docs from develop branch.

**Looked for:** any example in `nautilus_trader/examples/` or the integration docs that wires `data_clients={"X": ...}` and `exec_clients={"Y": ...}` across providers.

**Finding.** The Nautilus Databento integration doc (accessed 2026-05-28) **does** mention the split topology: "you can pair [Databento] with a sandbox for simulated execution, or match Databento data with Interactive Brokers execution." However, **no code example** in the doc shows the concrete `TradingNodeConfig(data_clients={DATABENTO: ...}, exec_clients={IB: ...})` shape. WebSearch returned no third-party blog posts or repos with a working example either; the closest was a Medium tutorial on Databento+Nautilus using SIM execution, not IB.

This makes PR 1 the first (publicly visible) implementation of this exact topology in MSAI's stack. **It is a first-class supported topology per the docs, but it is unprecedented in the wild** — MSAI's reference implementation is novel.

**Sources:**

1. [Nautilus Databento integration docs](https://nautilustrader.io/docs/latest/integrations/databento/) — accessed 2026-05-28
2. WebSearch query `"nautilus_trader" "data_clients" "exec_clients" different providers example databento interactive_brokers split` — accessed 2026-05-28 (zero working code examples found)

**Design impact.** The plan should NOT rely on any third-party example to validate the topology. Source-verified contract checks at the `TradingNodeConfig` build step are the only available correctness signal short of the drill itself.

**Test implication.** Pre-drill integration test: instantiate the per-account `TradingNodeConfig` builder, assert the produced `data_clients` dict has only `DATABENTO`, the `exec_clients` dict has only the IB venue key, and the `instrument_provider` configs differ between the two. **Do not** assume any prior Nautilus user has trodden this path safely; over-test the boundaries.

---

### 5. `ghcr.io/gnzsnz/ib-gateway:stable` — running two containers simultaneously

**Version:** `stable` (10.45.1g + IBC 3.23.0 at last check); rolling channel. Current dev compose pins `image: ghcr.io/gnzsnz/ib-gateway:stable` ([docker-compose.dev.yml:237](../../docker-compose.dev.yml#L237)).

**What MSAI does today.** One `ib-gateway` container behind the `broker` compose profile. Internal ports 4001 (live) / 4002 (paper); host-side `socat` proxy on 4003 (live) / 4004 (paper). Volume: `./data/ib-gateway:/home/ibgateway/tws_settings`.

**Constraints for running a second container.**

1. **TWS_USERID per container.** Each container needs its own `TWS_USERID` / `TWS_PASSWORD`. For PR 1, **both DUP733214 and DUP733215 ride under one `marin1016test` advisor login** (per PRD §6 + addendum's "second KV TWS secret pair deferred to LVP/HVP graduation drill"). This means **both containers use the same TWS_USERID/PASSWORD** in PR 1 — and IB's "EXISTING_SESSION_DETECTED" behaviour kicks in: only one TWS session per login is allowed at a time.
2. **EXISTING_SESSION_DETECTED_ACTION.** Current compose sets `EXISTING_SESSION_DETECTED_ACTION: ${IB_EXISTING_SESSION_ACTION:-primary}` ([docker-compose.dev.yml:263](../../docker-compose.dev.yml#L263)). `primary` means "claim the session, kick the other one out." With two containers logging in with the same credentials, **they will fight for the session** and only one will hold IB Gateway at a time. **This is the PR 1 architectural mismatch that needs resolution.**
3. **Volume per container.** `tws_settings` is per-login; reusing one host directory between two containers (same login) is probably fine but should be tested. If KV secrets diverge in PR 2/3, each gets its own settings dir.
4. **Port mapping per container.** Each needs unique host ports. Proposal in PRD §6: existing `4004→4002` for A, new `4006→4002` for B (and `4005→4001` / `4007→4001` for live, when applicable).
5. **2FA.** `RELOGIN_AFTER_TWOFA_TIMEOUT: yes` + `TWOFA_TIMEOUT_ACTION: restart` is set globally ([docker-compose.dev.yml:261-262](../../docker-compose.dev.yml#L261)). Two simultaneous 2FA prompts on one phone is a real failure mode in prod; for the PR 1 paper drill, paper trading does not require 2FA on every restart so this is moot until PR 3's per-account real-money credentials.

**Critical question for the plan.** Can two IB Gateway containers with the **same TWS login** both be active simultaneously, serving two different `account_id`s through the API?

**Searched discussions** ([gnzsnz/ib-gateway-docker Discussion #245 — Troubleshooting guide](https://github.com/gnzsnz/ib-gateway-docker/discussions/245), Discussion #334) — none addresses this directly. The IB platform itself permits one TWS session per username; the API client_id distinguishes multiple API connections within one TWS session.

**The likely-correct topology for PR 1 (CONTRADICTS THE PRD'S "TWO CONTAINERS" PROPOSAL):** with **one TWS login**, run **ONE** ib-gateway container, and connect TWO TradingNodes to it with different `ibg_client_id`s. Each `InteractiveBrokersExecClientConfig(account_id="DUP733214", ibg_client_id=N)` vs `(account_id="DUP733215", ibg_client_id=M)` will route to the right sub-account via IB's `account_id` field on `placeOrder`.

This **collapses the PR 1 topology to one IB Gateway container + two TradingNodes**, not two gateway containers. The PRD's stated topology assumes two containers, but with shared credentials, IB will not let both be active. This is **the biggest finding** of this research brief.

**Sources:**

1. [gnzsnz/ib-gateway-docker repo](https://github.com/gnzsnz/ib-gateway-docker) — accessed 2026-05-28
2. [Discussion #245 troubleshooting](https://github.com/gnzsnz/ib-gateway-docker/discussions/245) — accessed 2026-05-28
3. MSAI: `docker-compose.dev.yml:235-283` — current single-gateway service definition
4. MSAI memory: `reference_ib_accounts.md`, `reference_ib_entitlements.md` (in research notes — not re-read here)

**Design impact.** **The PR 1 plan needs a council-question or plan revision on gateway topology under one TWS login.** Two viable shapes:

- **Shape A — single ib-gateway container, two TradingNodes connected via different `ibg_client_id`s.** Works under one TWS login. Loses some isolation (one Gateway crash takes both accounts down) but matches IB's session model. Validates the `gateway_session_key` propagation work at the supervisor layer — `gateway_session_key` becomes 1:1 with `(host, port)` and `account_id` distinguishes the two TradingNodes.
- **Shape B — two ib-gateway containers under two TWS logins.** Requires the second KV TWS secret pair. The addendum explicitly **defers** the second KV secret pair to LVP/HVP graduation. So Shape B is not viable for PR 1's "deferred KV" constraint.

Shape A is the operationally-correct PR 1 topology. The PRD's drill steps still pass — two account-scoped deployments, drain one, fleet halt — but the "second `ib-gateway` container" wording in PRD §6 is incorrect for the chosen credential model. **Recommend surfacing this gap in the design phase before writing the plan.**

**Test implication.** Drill steps 5-6 (drain A, fleet halt) gain a subtle test: with one gateway container shared, draining account A's TradingNode must NOT close account B's connection. Shape A makes this a real test — assert via `docker logs ib-gateway` that the surviving client_id is still subscribed after A's TradingNode exits.

---

### 6. Redis halt-key patterns (internal — mostly known)

**Current state.** `_HALT_KEY = "msai:risk:halt"` is fleet-wide. Verified at:

- `backend/src/msai/api/live.py:105` — definition
- `backend/src/msai/live_supervisor/process_manager.py:96` — supervisor-side definition (duplicated string, not imported)
- `backend/src/msai/services/nautilus/disconnect_handler.py:60` — disconnect handler also defines its own copy
- `backend/src/msai/api/live.py:1675-1682` — write path sets `_HALT_KEY:set_by` + `_HALT_KEY:set_at` companion keys

**Recommended PR 1 patterns.**

1. **Account-scoped halt:** `msai:risk:halt:{ib_login_key}` (or `:{account_id}`). Account drain sets the account-scoped key; the supervisor's halt-check reads both keys.
2. **Fleet-emergency halt:** keep `msai:risk:halt` semantics but ADD halt-cause metadata in companion keys: `msai:risk:halt:cause` (e.g., `fleet_emergency`, future `data_stale`), `msai:risk:halt:detected_at`, `msai:risk:halt:source` (for PR 1b's data-stale, this is the account/node/dataset/symbol that stalled).
3. **Halt cause must be carried, not inferred** (addendum §"Data-stale auto-halt"). Operators must be able to distinguish a manual `/kill-all` from a future auto data-stale halt — same latch, distinct cause.

Confirm Redis patterns we already use: `SET key value EX seconds` (✅ used at `live.py:1675` with `ex=86400`), `EXISTS key`, `DELETE key`. No new Redis primitives required. The `_HALT_KEY` string is defined in 3 places — **PR 1 should consolidate to a single module** (e.g., `msai/core/halt_keys.py` exporting `fleet_halt_key()`, `account_halt_key(login_key)`, `halt_cause_key()`).

**Sources:**

1. MSAI: `backend/src/msai/api/live.py:105,1675-1682,1878-1880`
2. MSAI: `backend/src/msai/live_supervisor/process_manager.py:96,232,429`
3. MSAI: `backend/src/msai/services/nautilus/disconnect_handler.py:60,219`

**Design impact.** Halt key namespacing is purely internal — no external library research needed. The plan should explicitly add a halt-keys consolidation module + carry halt-cause metadata from PR 1's first commit (so PR 1b can wire data-stale on top without a schema migration of in-flight halts).

**Test implication.** Unit-test fixture: parametrize over halt scenarios — (a) account-scoped drain sets only account key, fleet key absent, (b) fleet emergency sets fleet key with `cause=fleet_emergency`, (c) recovery (`/resume`) clears keys in the correct order (account first, then fleet — so operator can verify both are clear before re-enabling).

---

## Not Researched (with justification)

- **arq / Redis Streams / consumer-group internals** — already covered extensively elsewhere in the project. `LiveCommandBus` ([backend/src/msai/services/live_command_bus.py](../../backend/src/msai/services/live_command_bus.py)) has stable internal patterns; PR 1's namespacing work changes the stream name, not the bus semantics.
- **`ib_async` / `ibapi`** — transitive via `nautilus_trader[ib]`. The exec-only path never calls `ib_async` directly; everything goes through `InteractiveBrokersClient.placeOrder` and the parsing layer in `adapters/interactive_brokers/parsing/execution.py`. No external research needed.
- **Frontend libraries** — PR 1 surfaces account context on existing endpoints but does not change the UI. No frontend research scope.
- **OPRA / options chain loading** — explicitly out of scope per PRD §2 + addendum Codex P2 #9 (deferred to OPRA capacity spike). Nautilus gotcha #12 applies when this lands; not for PR 1.
- **Databento partial-stall / single-symbol-stall detection** — PR 1b scope per the addendum.
- **Databento Python SDK live mode directly** — Nautilus wraps it via `nautilus_pyo3.DatabentoLiveClient`. MSAI never calls `databento.Live` directly through the Nautilus path. Heartbeat/reconnect surface inaccessible by design.
- **Two-VM split** — deferred per council blocking objection #8 + PRD §2 non-goals.

---

## Open Risks

1. **(BIGGEST) The PRD's "two ib-gateway containers" topology is incompatible with one TWS login.** IB allows one TWS session per username; two containers with the same `TWS_USERID` will fight for the session via `EXISTING_SESSION_DETECTED_ACTION`. The addendum defers second-KV secret pair to LVP/HVP graduation, so PR 1 must either (a) collapse to one gateway container + two TradingNodes routed by `ibg_client_id` + `account_id` (Shape A above), or (b) accept the second-KV-pair operator prerequisite for PR 1 (contradicting the addendum's revised prerequisites). **This needs a design-phase decision before writing the plan.** Shape A is recommended; the PRD's drill checklist still works with it, the gateway-session-key propagation work still proves out, and the second-container topology can land in PR 3 alongside the per-account KV credentials.

2. **No Nautilus-side hook for Databento heartbeat / stale-detection.** Confirmed by reading `nautilus_pyo3.pyi:7322-7351`. PR 1b's freshness signal must be built in MSAI on top of bar-event arrival timestamps from the message bus; it cannot be sourced from the Nautilus `DatabentoLiveClient` wrapper. The PRD's deferral is correct, but the PR 1b plan should not assume an SDK-level signal exists.

3. **Symbology shim shape is constrained — `venue_dataset_map` is a dataset alias, not a venue rename.** Strategies must subscribe to `.IBKR`; Databento publishes on native venues (`XNAS`/`GLBX`/`OPRA`); the shim has to live in MSAI code as a message-bus interceptor + subscription router. The addendum's "bidirectional dataset-aware symbology shim" is the right shape, but the plan must specify (a) WHERE the shim lives (msgbus subscriber + an outbound subscription resolver?), (b) HOW provider/dataset/native-venue metadata are preserved on `Bar` events (current Nautilus `Bar` doesn't carry arbitrary metadata — may need a parallel audit log).

4. **No public reference implementation of Databento-data + IB-exec split in Nautilus 1.22x.** Integration docs mention the topology is supported but provide no code sample. MSAI's PR 1 is the first implementation. Hidden incompatibilities (e.g., reconciliation behaviour when the cache has bars under a different venue suffix than the exec client expects) may only surface during the drill — budget for one drill iteration to land on an integration gotcha not visible from static analysis.

5. **`reconnect_timeout_mins=10` default = up to 10 minutes blind during a Databento reconnect storm.** Acceptable for PR 1 paper, **must be re-evaluated for real money in PR 1b** (the data-stale auto-halt is what makes a 10-minute reconnect tolerable). Lock the value explicitly in PR 1's live-config builder so a future Nautilus default change doesn't shift the safety budget.

6. **`InstrumentId.venue == "IBKR"` is hardcoded across MSAI today.** Search `backend/src/msai` for `IBKR`/`Venue("IBKR")` before writing the plan — the symbology shim must remain consistent with existing strategy/registry code, NOT diverge from it. (Not researched in detail — the addendum says it's "compatibility shim" shape, but the plan needs an audit of where canonical IDs flow.)

---

## Pointer to PR 1b research (NOT performed here)

Per the prompt, the following are deferred to PR 1b's own research brief:

- Databento partial-stall / single-symbol-stall detection patterns (how do you tell that ONE symbol's bars stopped while others flow?).
- OPRA capacity / throttling — explicitly deferred per Codex P2 #9.
- Per-node freshness observability libraries (Prometheus exporter shape? Custom metrics on the msgbus? Reuse the existing `core/metrics.py`?).
- Multi-failure-mode test harness shape (simulating Databento disconnect, stale-timestamp, partial-dataset stall, single-symbol stall, reconnect storm — what's the minimal mock surface that's faithful to the Rust-side `DatabentoLiveClient` behaviour?).
