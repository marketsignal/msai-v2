# Instruments: symbol readiness answers for dual-provider symbols (provider dead-end regression)

> Graduated 2026-06-06 from `docs/plans/2026-06-06-symbols-readiness-provider-param.md` after verify-e2e PASS (report `tests/e2e/reports/2026-06-06-15-30-symbols-readiness-provider-param.md`).
> Bug pinned: unpinned readiness on a dual-provider symbol returned `422 AMBIGUOUS_INSTRUMENT "pin provider explicitly"` with no `provider` param to comply; the DELETE sibling was an unhandled 500. Read-only UCs — safe for regression.

**Surface coverage decision** (per `CLAUDE.md ## E2E Configuration`, surfaces `[API, CLI, UI]`):

- **API: Covered** — UC1 below. `GET /symbols/readiness` is a public/product API surface (operator/integrator-facing; documented in `how-symbols-work.md` as the Phase-1 read-through surface).
- **CLI: Covered** — UC2 below. The `msai symbols` sub-app (mounted `cli.py:3011`) already exposes `readiness` and `delete` commands that call these endpoints; both gained the optional `--provider [databento|interactive_brokers]` option in this fix so a CLI operator can pin a dual-provider symbol's readiness view and satisfy the destructive-delete disambiguation (code-review iter-1 P2 — the CLI was left behind in the original plan, which predated the discovery that `cli_symbols.py` already wraps these routes).
- **UI: N/A** — the frontend consumes `/symbols/inventory` (`frontend/src/components/live/instrument-readiness-check.tsx:44`), never `/symbols/readiness` (grep clean); inventory already disambiguates per provider and is untouched by this fix.

**UC1 — API: data-readiness answer for a symbol carried by both providers**

```
Actor:         API integrator (operator tooling) checking whether a symbol's historical data
               is ready before scheduling a backtest
Scenario:      Their registry carries SPY under both databento and interactive_brokers (the
               normal state after IB qualification). Yesterday the readiness check dead-ended
               with 422 "pin provider explicitly" — an instruction the API gave no way to
               follow. They need the plain data-readiness answer, and the per-provider view
               when they ask for it.
Interface:     API
Intent:        The integrator asks "is SPY's data ready?" and gets a direct answer, and can
               pin a specific provider's view of the same symbol when they need it.
Setup:         Dev stack up from this worktree. SPY registered under BOTH providers (the
               standing dev-registry state — verify via GET /api/v1/symbols/inventory; if
               absent, register via the documented bootstrap: POST /api/v1/instruments/bootstrap
               for databento + msai instruments refresh --provider interactive_brokers).
               (Do NOT call readiness in Setup — that's the action under test.)
Steps:         1) GET /api/v1/symbols/readiness?symbol=SPY&asset_class=equity   (unpinned)
               2) GET /api/v1/symbols/readiness?symbol=SPY&asset_class=equity&provider=interactive_brokers
Verification:  Step 1 receives 200 — the response includes coverage_status/backtest_data_available
               for SPY plus the resolved provider name (databento, the preferred primary) —
               NOT the former 422 dead-end; the integrator can act on it (schedule the backtest
               or trigger ingest for the missing ranges it names). Step 2 receives 200 with
               provider=interactive_brokers metadata for the SAME symbol and the same
               coverage answer, demonstrating the pin works.
Persistence:   Re-request step 1 after a short delay — same 200 shape, same resolved provider
               (deterministic preference, not a coin flip). A provider value outside the
               registry (e.g. provider=interactive_brokers on a databento-only symbol) still
               returns an explanatory 4xx the integrator can correct.
```

**UC2 — CLI: operator checks a symbol's data-readiness from the shell, then pins a provider**

```
Actor:         Operator at the shell sizing up whether a symbol's historical data is ready
               before kicking off an overnight backtest
Scenario:      Their registry carries SPY under both databento and interactive_brokers (the
               normal post-IB-qualification state). They want a quick readiness answer from
               the terminal without opening the UI, and then the interactive_brokers-specific
               view when they need to confirm live qualification for that provider.
Interface:     CLI
Intent:        The operator asks "is SPY's data ready?" from the CLI and gets a direct answer,
               then pins interactive_brokers to see that provider's view of the same symbol.
Setup:         Dev stack up from this worktree (backend reachable on :8800, MSAI_API_KEY set
               for the CLI). SPY registered under BOTH providers — verify via
               `msai symbols inventory`; if absent, register via the documented bootstrap
               (POST /api/v1/instruments/bootstrap for databento + `msai instruments refresh
               --provider interactive_brokers`). (Do NOT run `msai symbols readiness` in
               Setup — that's the action under test.)
Steps:         1) Run `msai symbols readiness --symbol SPY --asset-class equity`   (unpinned)
               2) Run `msai symbols readiness --symbol SPY --asset-class equity --provider interactive_brokers`
Verification:  Step 1 exits 0 and stdout shows a JSON readiness block whose `provider` field
               reads `databento` (the server's preferred primary) with `registered: true` —
               NOT the former 422 dead-end echoed to stderr; the operator can act on the
               coverage_status/missing_ranges it names. Step 2 exits 0 and stdout shows the
               same block but with `provider: interactive_brokers` for the SAME symbol,
               demonstrating the pin reached the API.
Persistence:   Open a fresh shell and re-run step 1 — stdout shows the same `provider:
               databento` block (deterministic preference, not a coin flip), confirming the
               answer is stable across invocations. Running with `--provider bogus` is
               rejected by the CLI before any call (non-zero exit, choices listed in the
               error), and `--provider interactive_brokers` against a databento-only symbol
               returns a 4xx the operator can read on stderr and correct.
```
