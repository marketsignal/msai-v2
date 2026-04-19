# PRD: `msai instruments refresh --provider interactive_brokers`

**Version:** 1.0
**Status:** Draft
**Author:** Claude + Pablo
**Created:** 2026-04-18
**Last Updated:** 2026-04-18

---

## 1. Overview

Complete the deferred `--provider interactive_brokers` branch of the existing `msai instruments refresh` CLI. Today, invoking that branch hard-fails with a deferral message (`cli.py:747-756`) because `Settings` lacks three IB-wiring fields. This PRD ships those fields, a short-lived Nautilus IB factory chain, a CLI-side port/account preflight, and a paper-gateway smoke test — closing deferred item #2 from PR #32. The Databento branch of the same CLI already works; this PR brings the IB branch to parity so operators can pre-warm the instrument registry for closed-universe symbols before deploying live strategies.

## 2. Goals & Success Metrics

### Goals

- **Primary:** Operators can run `msai instruments refresh --symbols AAPL,ES,EUR/USD --provider interactive_brokers` against a running paper IB Gateway and see `instrument_definitions` + `instrument_aliases` rows written for every requested symbol.
- **Secondary:** Close a known follow-up item from PR #32 without expanding scope into live-path wiring (deferred item #1) or into instruments outside the existing `resolve_for_live` closed universe.

### Success Metrics

| Metric              | Target                                                                              | How Measured                                                                               |
| ------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Unit-test pass      | 100% green on mocked factory tests                                                  | `uv run pytest tests/unit/test_cli_instruments_refresh.py`                                 |
| Paper-gateway smoke | Passes against running paper IB Gateway for ≥2 symbol shapes (equity + future)      | `RUN_PAPER_E2E=1 uv run pytest -m ib_paper tests/e2e/test_instruments_refresh_ib_smoke.py` |
| Idempotency         | Second invocation writes 0 new rows, bumps 0 alias-window records                   | Assertion inside the paper smoke test                                                      |
| Warm-path proof     | Post-refresh `SecurityMaster.resolve_for_live(symbols)` returns without touching IB | Smoke test asserts no IB request traffic on the warm call                                  |
| Dead-gateway UX     | CLI fails within `ib_connect_timeout_seconds` (default 5s) with named hint          | Manual: stop IB Gateway → run CLI → measure wall-clock + read error text                   |
| Client-ID collision | CLI invocation while live subprocess is active does NOT disconnect the live node    | Manual: deploy a strategy → run CLI → verify live subprocess stays connected (gotcha #3)   |

### Non-Goals (Explicitly Out of Scope)

- ❌ Live-path wiring (`/api/v1/live/start-portfolio` still using closed-universe `canonical_instrument_id()`) — that's PR #32 deferred item #1, a separate PR.
- ❌ Options resolution — PR #32 non-goal'd options; no change here.
- ❌ Support for symbols outside the current `resolve_for_live` closed universe (AAPL, MSFT, SPY, EUR/USD, ES). Unknown symbols return a clear error referencing the canonical list.
- ❌ Multi-login gateway routing (PR #30's `GatewayRouter` / `ib_login_key` concept). The CLI uses the single `(ib_host, ib_port)` pair from Settings.
- ❌ Automated CI run against real IB Gateway — the opt-in `pytest.mark.ib_paper` smoke is gated on an env var and never fires in default CI.
- ❌ Structured metrics / dashboards for CLI runs. Stderr + exit code are the observability surface.
- ❌ Retry logic for transient IB errors. The whole batch fails; operator re-runs.
- ❌ Rename of `ib_request_timeout_seconds` to `ib_instrument_request_timeout_seconds` (Maintainer suggestion, overruled by chairman — split connect/request timeouts already provide clarity).

## 3. User Personas

### Operator (sole persona)

- **Role:** Developer or ops engineer running the MSAI CLI on a machine with IB Gateway reachable (either `localhost:4002` for paper or a Docker-compose gateway container).
- **Permissions:** Shell access to the backend container; env-var control over `IB_HOST`, `IB_PORT`, `IB_ACCOUNT_ID`, `IB_INSTRUMENT_CLIENT_ID`, `IB_CONNECT_TIMEOUT_SECONDS`, `IB_REQUEST_TIMEOUT_SECONDS`, `DATABENTO_API_KEY`.
- **Goals:** Pre-warm the instrument registry for a new symbol batch before deploying a strategy, so the live subprocess never hits a cold-miss resolution path at bar-event time.

## 4. User Stories

### US-001: Pre-warm IB registry for closed-universe symbols (happy path)

**As an** operator
**I want** to run `msai instruments refresh --symbols AAPL,ES --provider interactive_brokers` against the running paper IB Gateway
**So that** subsequent live deployments resolve those symbols from the warm registry without re-qualifying at bar-event time.

**Scenario:**

```gherkin
Given IB Gateway is running on port 4002 with account DUP733211
  And Settings.ib_port=4002, Settings.ib_account_id="DUP733211"
  And the instrument_definitions table has no row for AAPL
When the operator runs `msai instruments refresh --symbols AAPL,ES --provider interactive_brokers`
Then the CLI prints the resolved "(host, port, account_prefix, client_id)" tuple before connecting
 And the CLI connects to IB Gateway within ib_connect_timeout_seconds (5s)
 And it qualifies AAPL → InstrumentId AAPL.NASDAQ via InteractiveBrokersInstrumentProvider.get_instrument
 And it qualifies ES → InstrumentId ESM6.CME (front-month) via the same path
 And it upserts one row per symbol into instrument_definitions
 And it upserts one row per symbol into instrument_aliases with effective_to = NULL
 And it calls client.disconnect() before exit
 And the process exits with code 0
```

**Acceptance Criteria:**

- [ ] CLI connects within `ib_connect_timeout_seconds` and honors `ib_request_timeout_seconds` for per-qualification round-trip
- [ ] Both `instrument_definitions` and `instrument_aliases` have rows for each requested symbol
- [ ] `client.disconnect()` is called in a `try/finally` so cleanup runs even on mid-batch failure
- [ ] Exit code is 0 on full success
- [ ] Preflight log line names `ib_host`, `ib_port`, `ib_account_id`, `ib_instrument_client_id` (so the operator can grep `docker logs` if something fails)

**Edge Cases:**

| Condition                                     | Expected Behavior                                                                            |
| --------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Empty `--symbols` list                        | Exit non-zero with `"no symbols provided"` (matches Databento path at cli.py:744-745)        |
| Whitespace-wrapped symbol (`" AAPL "`)        | Stripped then qualified                                                                      |
| Duplicate symbols in the list                 | Deduped before calling IB (so we don't charge 2× the rate limit)                             |
| Mid-batch qualification failure (symbol #2/5) | Exit non-zero; rows for symbols already qualified are committed (idempotent re-run recovers) |

**Priority:** Must Have

---

### US-002: Idempotent re-run

**As an** operator
**I want** running the refresh command twice with the same symbols to be a no-op on the second call
**So that** operational runbooks can include refresh as a safe pre-deploy step without worrying about row duplication or alias-window corruption.

**Scenario:**

```gherkin
Given the operator has already run `msai instruments refresh --symbols AAPL --provider interactive_brokers`
  And AAPL has exactly one row in instrument_definitions
  And AAPL has exactly one active alias row in instrument_aliases (effective_to = NULL)
When the operator runs the same command within 60 seconds
Then the CLI still connects and qualifies AAPL
 And NO new row is added to instrument_definitions for AAPL
 And NO new alias row is added (the existing row still has effective_to = NULL)
 And the process exits with code 0
```

**Acceptance Criteria:**

- [ ] Idempotency is verified in the paper-gateway smoke test, not just asserted
- [ ] No race between "close previous active alias" and "insert new alias" on identical re-run (refer to PR #32 post-merge fix `8f5f943` for the contract)

**Edge Cases:**

| Condition                                                     | Expected Behavior                                                                |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Second call runs before `client.disconnect()` fully completes | CLI retries the IB connect transparently; no collision on `client_id=999`        |
| Second call with a subset of the original symbols             | Unchanged symbols are no-op; dropped symbols remain in registry (no auto-delete) |

**Priority:** Must Have

---

### US-003: Fast-fail when IB Gateway is unreachable

**As an** operator
**I want** the CLI to fail within 3-5 seconds when IB Gateway is not running, with a hint naming the exact env vars to check
**So that** I don't waste ~30s per invocation wondering whether the command is hung.

**Scenario:**

```gherkin
Given IB Gateway is NOT running on the configured host/port
When the operator runs `msai instruments refresh --symbols AAPL --provider interactive_brokers`
Then the CLI attempts connect for ib_connect_timeout_seconds (default 5)
 And it prints an error naming ib_host, ib_port, ib_account_id, ib_instrument_client_id
 And the error suggests: "(a) gateway container running, (b) IB_PORT matches IB_ACCOUNT_ID prefix, (c) IB_INSTRUMENT_CLIENT_ID={N} not colliding"
 And the process exits with a non-zero code within wall-clock 6s
```

**Acceptance Criteria:**

- [ ] Connect-phase timeout is `ib_connect_timeout_seconds` (default 5), NOT `ib_request_timeout_seconds` (30)
- [ ] Error message names all four env vars plus the three diagnostic buckets (container / port-account match / client-id collision)
- [ ] No partial rows are written to the registry on a failed connect

**Edge Cases:**

| Condition                                        | Expected Behavior                                                                           |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| Gateway accepts connection but never replies     | Per-symbol qualification honors `ib_request_timeout_seconds` and then fails that one symbol |
| Gateway connection succeeds then drops mid-batch | Error surfaces on the failing symbol; exit non-zero; rows already committed stay committed  |

**Priority:** Must Have

---

### US-004: Block on port/account mismatch (gotcha #6 preflight)

**As an** operator
**I want** the CLI to refuse to connect if `IB_PORT`/`IB_ACCOUNT_ID` are mis-paired
**So that** I never accidentally pre-warm the registry from a live account when I meant paper (or vice-versa).

**Scenario:**

```gherkin
Given Settings.ib_port=4001 (live port)
  And Settings.ib_account_id="DUP733211" (paper-prefixed account — "DU")
When the operator runs `msai instruments refresh --symbols AAPL --provider interactive_brokers`
Then the CLI raises a ValueError BEFORE attempting any IB connection
 And the error names the exact mismatch ("port 4001 expects non-paper account; got DUP733211")
 And the process exits non-zero
```

**Acceptance Criteria:**

- [ ] CLI preflight uses the SAME port/account validator used by the live subprocess (`_validate_port_account_consistency` at `live_node_config.py:178`) — extracted to a shared helper if needed
- [ ] Validator covers both raw IB ports (4001, 4002) and socat-proxy ports (4003, 4004) — see `live_node_config.py:76-77`
- [ ] No IB connection attempt is made on mismatch (no wasted `client_id=999` slot)

**Edge Cases:**

| Condition                                   | Expected Behavior                                     |
| ------------------------------------------- | ----------------------------------------------------- |
| Unknown port (e.g. 4005)                    | Validator rejects with clear "unknown IB port" error  |
| Account id with leading/trailing whitespace | Normalized before validation (mirrors subprocess fix) |

**Priority:** Must Have

---

### US-005: Clean disconnect on exit (no zombie client_id)

**As an** operator
**I want** a second CLI invocation within 60s of the first to NOT collide with a leftover `client_id=999` connection
**So that** my pre-deploy runbook can re-run refresh without manual wait time.

**Scenario:**

```gherkin
Given the first `msai instruments refresh` invocation has just exited successfully
When the operator runs the command again within 60 seconds
Then the second CLI connects to IB Gateway without "client_id already in use" error
 And no log line in IB Gateway's diagnostic output mentions "disconnected by new connection"
```

**Acceptance Criteria:**

- [ ] The CLI wraps the full IB session in `try/finally` and calls `client.disconnect()` in the `finally` block
- [ ] Post-exit verification: the Nautilus cached-client/provider globals are cleared or recreatable before the next invocation succeeds
- [ ] The smoke test exercises this by running refresh twice in succession (not just once)

**Edge Cases:**

| Condition                                  | Expected Behavior                                                                                                           |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| First invocation killed with SIGKILL       | Gateway eventually releases the slot; second run may fail once with clear hint to wait; not a blocker — operator can re-run |
| First invocation exited with network error | Disconnect still runs in `finally`; no zombie                                                                               |

**Priority:** Should Have (one manual "wait 30s then retry" is acceptable for the rare SIGKILL case; clean exit path must work always)

---

### US-006: Reject unknown symbols with a clear pointer to the closed universe

**As an** operator
**I want** the CLI to reject symbols outside the current `resolve_for_live` closed universe with a message naming the supported symbols
**So that** I don't accidentally think the refresh command silently skipped a symbol.

**Scenario:**

```gherkin
Given Settings are configured for paper trading
When the operator runs `msai instruments refresh --symbols NVDA,TSLA --provider interactive_brokers`
Then the CLI rejects the batch BEFORE connecting to IB Gateway
 And the error lists supported symbols: "AAPL, MSFT, SPY, EUR/USD, ES"
 And the error notes these are the symbols resolve_for_live currently supports
 And the process exits non-zero
```

**Acceptance Criteria:**

- [ ] Unknown-symbol rejection fires in preflight (before burning the `client_id=999` slot)
- [ ] Error message names the exact closed-universe list (sourced from a single place, not duplicated)
- [ ] Mixed-validity input (one known + one unknown) rejects the entire batch with the unknown name in the error

**Edge Cases:**

| Condition                                       | Expected Behavior                                                                           |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Symbol with different casing (`aapl`)           | Treated as unknown unless the closed-universe list normalizes casing (decide in plan phase) |
| Symbol with dotted venue suffix (`AAPL.NASDAQ`) | Accepted if the bare root is in the closed universe; stripped to `AAPL` before qualifying   |

**Priority:** Must Have

---

## 5. Technical Constraints

### Known Limitations

- **Closed universe only:** Day-1 scope is the 5-symbol set `resolve_for_live` supports (`AAPL`, `MSFT`, `SPY`, `EUR/USD`, `ES`). Expansion requires separate work to generalize `canonical_instrument_id()` and `SecurityMaster.resolve_for_live`.
- **Single gateway:** Ignores PR #30's multi-login `GatewayRouter` — CLI uses the single `(ib_host, ib_port)` from Settings.
- **Sequential qualification:** IB's `reqContractDetails` is rate-limited (≤50 msg/sec); `IBQualifier.qualify_many` iterates sequentially (see `ib_qualifier.py:196-210`). ~100-symbol batches will take ~2 seconds; 1000-symbol batches are outside current scale.
- **No retry:** Transient IB errors fail the batch; operator re-runs.

### Dependencies

- **Requires (already shipped):**
  - `IBQualifier` wrapper at `ib_qualifier.py:157` (PR #32 shipped the async adapter)
  - `build_ib_instrument_provider_config` at `live_instrument_bootstrap.py:260`
  - `SecurityMaster.resolve_for_live` at `security_master/service.py` (PR #32)
  - `instrument_definitions` + `instrument_aliases` tables (PR #32)
  - `_validate_port_account_consistency` at `live_node_config.py:178` (needs extraction or shared-helper import)
- **Blocked by:** Nothing — all building blocks are on `main`.

### Integration Points

- **Interactive Brokers Gateway:** TCP socket at `settings.ib_host:settings.ib_port`. Must match `settings.ib_account_id` prefix per gotcha #6.
- **NautilusTrader IB adapter:** `nautilus_trader.adapters.interactive_brokers.factories.get_cached_ib_client` + `get_cached_interactive_brokers_instrument_provider` (signatures to be VERIFIED against `.venv/lib/python3.12/site-packages/nautilus_trader/adapters/interactive_brokers/factories.py` during implementation — MEMORY.md rule).
- **Postgres:** Writes via `SecurityMaster._upsert_definition_and_alias` using the CLI's own `async_session_factory` session (commit at end of successful batch).

## 6. Data Requirements

### New Data Models

None. This PR writes to existing tables (`instrument_definitions`, `instrument_aliases`) using the existing `SecurityMaster` service.

### Settings Additions (`core/config.py`)

| Field                        | Type  | Default | Env Alias                    | Notes                                                                                                           |
| ---------------------------- | ----- | ------- | ---------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `ib_connect_timeout_seconds` | `int` | `5`     | `IB_CONNECT_TIMEOUT_SECONDS` | Wall-clock budget for the IB Gateway TCP + client-ready probe                                                   |
| `ib_request_timeout_seconds` | `int` | `30`    | `IB_REQUEST_TIMEOUT_SECONDS` | Per-symbol qualification round-trip; `int` to match Nautilus signature                                          |
| `ib_instrument_client_id`    | `int` | `999`   | `IB_INSTRUMENT_CLIENT_ID`    | Out-of-band reservation; must NOT collide with live-node derived IDs (100-200 range). Printed in preflight log. |

### Data Validation Rules

- `ib_port` × `ib_account_id` — validated via reused `_validate_port_account_consistency` before any IB connection.
- `--symbols` — comma-split, whitespace-stripped, deduped, then checked against the closed-universe allow-list.

### Data Migration

None.

## 7. Security Considerations

- **Authentication:** CLI is operator-only (shell access implies authorization). No JWT/auth layer needed.
- **Authorization:** The CLI has full write access to `instrument_definitions` / `instrument_aliases` via its DB connection — scope matches other `msai` CLI commands (`ingest`, `live-status`, etc.).
- **Data Protection:** No PII. IB account id (`DU...` paper prefix) is logged to stderr — acceptable since logs stay on the operator's machine/container.
- **Secrets:** `DATABENTO_API_KEY` is only read by the databento branch; this branch doesn't touch it. IB Gateway auth is handled by the gateway process itself, not the CLI.
- **Audit:** Every successful refresh writes `lifecycle_state` + `refreshed_at` on the `instrument_definitions` row per the PR #32 schema — that IS the audit trail. No additional structured audit needed.

## 8. Open Questions

> Carried from the council's "Missing Evidence" section — to be resolved during the implementation plan phase.

- [ ] Is `client.disconnect()` sufficient to leave Nautilus's cached client/provider globals clean across re-invocations, or do we need an explicit `clear()` on the adapter's global registries? (Verify against venv source in Phase 2 research.)
- [ ] `ib_request_timeout_seconds = 30` vs `60` (codex-version uses 60). Ship with 30 (faster fail at CLI), bump to 60 only if paper smoke surfaces flakiness.
- [ ] Where to house the shared port/account validator so the CLI can use it without pulling subprocess-only deps: (a) extract a tiny helper module next to `live_node_config.py`, (b) rehouse inside `core/config.py` as a Settings method, or (c) duplicate a 10-line copy with a pointer comment. Decide in Phase 3.2 plan.
- [ ] Closed-universe list — single source of truth. Options: (a) constant in `live_instrument_bootstrap.py` (current location of `phase_1_paper_symbols`), (b) new constant in `security_master/specs.py`. The CLI preflight and the live subprocess should import from the same module.

## 9. References

- **Discussion Log:** [`docs/prds/instruments-refresh-ib-path-discussion.md`](./instruments-refresh-ib-path-discussion.md)
- **Pre-existing skeleton:** [`docs/plans/2026-04-17-db-backed-strategy-registry.md`](../plans/2026-04-17-db-backed-strategy-registry.md) §"Task 13: Add `msai instruments refresh` CLI" (lines 2250-2370) — original deferred plan
- **Related PRDs:**
  - [`docs/prds/db-backed-strategy-registry.md`](./db-backed-strategy-registry.md) — PR #32, the parent feature
- **Nautilus rules:** [`.claude/rules/nautilus.md`](../../.claude/rules/nautilus.md) — gotchas #3 (client_id collision), #5 (silent start_async), #6 (port/account mismatch), #20 (disconnect cleanup)
- **Codex-version precedent:** `codex-version/backend/src/msai/services/nautilus/instrument_service.py:411-420` — the working IB instrument service to read for API shape

---

## Appendix A: Revision History

| Version | Date       | Author         | Changes                                                  |
| ------- | ---------- | -------------- | -------------------------------------------------------- |
| 1.0     | 2026-04-18 | Claude + Pablo | Initial PRD after 5-advisor council + chairman synthesis |

## Appendix B: Approval

- [ ] Product Owner (Pablo) approval
- [ ] Ready for technical design (Phase 2 research + Phase 3 brainstorming)
