# PRD Discussion: `msai instruments refresh --provider interactive_brokers`

**Status:** In Progress
**Started:** 2026-04-18
**Participants:** User (Pablo), Claude

## Original User Stories

From CONTINUITY.md "Next" section (post PR #32 deferred items):

> **Deferred from PR #32** — 2 of the original 3 items still open:
>
> 1. `msai instruments refresh` for plain symbols (Databento path works; IB path skipped).
> 2. Live path wiring onto registry (`/api/v1/live/start-portfolio` still uses closed-universe `canonical_instrument_id()`).

This PRD scopes **item #1 only** — the deferred IB branch of the existing `msai instruments refresh` CLI. Item #2 is a follow-up PR.

## Pre-Existing Design (from 2026-04-17 plan)

`docs/plans/2026-04-17-db-backed-strategy-registry.md:2250-2370` ships a detailed skeleton for this exact path, deferred at merge time because `Settings` lacked the fields needed to build an `IBQualifier`. The skeleton leaves `qualifier = ...` as a placeholder and names the imports (`get_cached_ib_client`, `get_cached_interactive_brokers_instrument_provider`, `InteractiveBrokersInstrumentProviderConfig`).

The code path today (`cli.py:747-756`) immediately `_fail`s on `--provider interactive_brokers` with a clear deferral message.

## Discovery Findings

- **IBQualifier wrapper** already exists at `security_master/ib_qualifier.py:157` (thin async adapter over Nautilus provider). The missing piece is the provider-factory chain, not the adapter.
- **Provider-config builder** already exists at `live_instrument_bootstrap.py:260-305` (`build_ib_instrument_provider_config`) — builds an `InteractiveBrokersInstrumentProviderConfig` from a list of symbols. Reusable.
- **Settings** already have `ib_host` + `ib_port` with env aliases (`IB_HOST`/`IB_GATEWAY_HOST`, `IB_PORT`/`IB_GATEWAY_PORT_PAPER`). The paper/live split is operator-driven via `IB_PORT=4002` vs `IB_PORT=4001`, not two separate fields.
- **Client-ID reservation** per Nautilus gotcha #3: two TradingNodes with the same `ibg_client_id` silently disconnect each other. The skeleton reserves `999` as out-of-band for this CLI, outside the live-subprocess range (100-200).
- **Existing example in CLAUDE.md** shows expected invocation: `uv run msai instruments refresh --symbols AAPL,ES --provider interactive_brokers`

## Discussion Log

### Round 1 — Questions to User

Most of the design is pre-specified in the 2026-04-17 plan skeleton. These questions target the remaining ambiguities:

**Q1: Settings field names — confirm.**
Plan to add:

- `ib_request_timeout_seconds: float = 30.0` (env: `IB_REQUEST_TIMEOUT_SECONDS`)
- `ib_instrument_client_id: int = 999` (env: `IB_INSTRUMENT_CLIENT_ID`) — out-of-band reservation for the CLI, never used by live subprocess
  Any preferred names/defaults, or lock in these?

**Q2: Paper vs live port selection.**
The existing `ib_port` is already operator-set via env. Current plan: the CLI just reads `settings.ib_port` — if an operator wants to pre-warm against live instead of paper, they set `IB_PORT=4001` before running the CLI. No new flag.
Alternative: add `--ib-port {paper,live}` flag that selects between `4002`/`4001` literals. Which do you want?

**Q3: Integration test — skip or gate on live IB?**
Unit tests (per plan step 1, line 2263) fully mock the IB factory chain. Do we also want a `pytest.mark.ib_paper` integration test that actually runs the CLI against the running paper IB Gateway (port 4002, account `DU...`)? Opt-in via env var, skipped in CI.
Default recommendation: yes, one smoke test — paper account, 1-2 common symbols (AAPL + ES). Catches the "fictional API" class of bugs from the batch-3 memory entry.

**Q4: Symbol scope for first ship.**
Plan skeleton mocks `AAPL,ES`. Do we need support for FX pairs (`EURUSD`) and options on day one, or is equity+futures enough? (FX works via the existing `spec_to_ib_contract` FOREX branch; options were explicitly non-goal'd in PR #32.)
Default: equity + futures only (matches PR #32 non-goal for options).

**Q5: Error UX when IB Gateway is down.**
Today when the live subprocess can't reach the gateway, it takes ~60s to time out with a cryptic connection-refused. For a CLI run by a human, I want a fast-fail: try to connect with a short timeout (3-5s), and on failure print a clear operator-hint: `"IB Gateway not reachable at {host}:{port}. Start it first: docker compose ... up ib-gateway-paper"`.
Acceptable, or do you prefer honoring the full `ib_request_timeout_seconds`?

**Q6: Success criteria — how do we know this is done?**
Proposal:

- Unit tests (mocked factory) green
- `msai instruments refresh --symbols AAPL --provider interactive_brokers` succeeds against the running paper IB Gateway (manual verification)
- A row appears in `instrument_definitions` + at least one row in `instrument_aliases` for AAPL
- Subsequent `/api/v1/backtests/run` on an AAPL strategy resolves via the warm registry (no cold-miss synthesis path hit)
  Additional criteria you want captured?

---

## Round 2 — Council Verdict (2026-04-18)

5-advisor council (Simplifier, Scalability Hawk, Pragmatist, Contrarian, Maintainer) + Codex chairman (xhigh) synthesized answers to all 6 questions. Resolved answers below. 2 APPROVE + 3 CONDITIONAL; chairman adopted the load-bearing CONDITIONAL blockers, overruled scope-creep additions.

**A1 (Settings fields) — SPLIT timeouts.** Add THREE settings (not two):

- `ib_connect_timeout_seconds: int = 5` — gateway-reachability probe (env `IB_CONNECT_TIMEOUT_SECONDS`)
- `ib_request_timeout_seconds: int = 30` — post-connect qualification round-trip; `int` to match Nautilus's `request_timeout_secs` signature (env `IB_REQUEST_TIMEOUT_SECONDS`)
- `ib_instrument_client_id: int = 999` — out-of-band CLI reservation (env `IB_INSTRUMENT_CLIENT_ID`)

Rationale: a single timeout conflates two operator-visible failures (dead-gateway hang vs per-symbol slowness). Also surface the resolved `client_id` in CLI help text + every preflight log so the reservation is explicit, not hidden.

**A2 (Paper/live port) — reuse `IB_PORT`, but ADD CLI-side preflight.** No new flag. The CLI must NOT rely on the live-supervisor's `_validate_port_account_consistency` (that guard fires at subprocess build time, not at `msai instruments refresh` time). Extract or reuse the existing validator (`live_node_config.py:178`) so the CLI refuses to connect when `settings.ib_port` ↔ `settings.ib_account_id` mismatch (gotcha #6). Print the resolved `(host, port, account_prefix, client_id)` tuple before connecting.

**A3 (Testing) — BOTH layers.** Mocked unit tests for control flow + error copy, PLUS opt-in `pytest.mark.ib_paper` smoke gated on env var (skipped in CI). Smoke MUST re-run refresh twice to exercise Nautilus's cached client/provider layer — catches the "wrapper against fictional API" failure class from MEMORY.md.

**A4 (Scope) — closed universe honesty.** Day-1 scope = what `resolve_for_live` actually supports: `AAPL`, `MSFT`, `SPY`, `EUR/USD`, `ES`. FX is IN (already live-verified). Docs/help text must name the exact symbols, NOT "equity + futures." Anything outside the list returns a clear error pointing at the canonical list.

**A5 (IB-down UX) — fast-fail 3-5s on connect; then honor full request timeout.** Error hint must name: `ib_host`, `ib_port`, `ib_account_id`, `ib_instrument_client_id`, and mention the paper/live mismatch trap. Example: `"IB Gateway not reachable at {host}:{port} within {connect_timeout}s. Check: (a) gateway container running, (b) IB_PORT matches IB_ACCOUNT_ID prefix (DU* → paper 4002, U* → live 4001), (c) IB_INSTRUMENT_CLIENT_ID={N} not colliding with an active subprocess."`

**A6 (Success criteria) — expanded.**

1. Unit tests green (mocked factory)
2. Non-zero exit on any per-symbol qualification failure (partial success is still a failure)
3. Idempotent re-run: running the CLI twice with the same symbols produces NO duplicate alias-window rows in `instrument_aliases` and NO new `instrument_definitions` rows on the second call
4. Live-paper smoke covers >1 symbol shape (equity + future minimum; FX if feasible)
5. Proof the warm path works: after refresh, `SecurityMaster.resolve_for_live(symbols)` returns without touching IB (verified in smoke test, not just "rows exist")
6. Explicit `client.disconnect()` teardown path, verified by a second CLI invocation within 60s that does NOT collide with a zombie `client_id=999`

## Missing Evidence (carried into plan)

- Confirm `client.disconnect()` is sufficient to leave Nautilus cached client/provider state clean across CLI re-invocations → verify during implementation against actual venv source.
- Choose 30s vs 60s for post-connect qualification timeout → codex-version precedent is 60s; default to 30s for CLI (faster fail), bump to 60s only if real smoke shows flaky contract qualifications.
- Cleanest place to share `_validate_port_account_consistency` between live-node-config and the CLI → options: (a) extract to a new small helper module, (b) rehouse inside `core/config.py` as a validator method. Decide during plan phase.

## Status

**Complete — ready for PRD creation.** Next step: `/prd:create instruments-refresh-ib-path`.
