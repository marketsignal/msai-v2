# PRD — Strategy Config Schema Extraction

**Status:** Ratified 2026-04-20 (council verdict + Pablo ratification)
**Related artifacts:**

- Discussion log: `docs/prds/strategy-config-schema-extraction-discussion.md`
- Research brief: `docs/research/2026-04-20-strategy-config-schema-extraction.md`
- Parent council verdict: this session's chairman output (preserved in `CONTINUITY.md` Workflow block).

## Problem

Operators (Pablo) configure strategies for backtests today by typing raw JSON into a `<Textarea>` at `frontend/src/components/backtests/run-form.tsx:199-207`. Placeholder: `'{ "fast_period": 12, "slow_period": 26 }'`. Zero validation, zero field hints, zero type safety. Every strategy's config shape must be remembered or looked up in the Python source.

## Goal

Replace the JSON textarea with an auto-generated typed form driven by each strategy's Nautilus `StrategyConfig` schema. Backend exposes the schema + defaults + extraction status; frontend renders a narrow form for the supported field-type subset and falls back to JSON when a strategy declares types outside that subset.

## User stories

- **US-001 (primary):** As Pablo, when I open "Run Backtest" and pick a strategy, I see typed fields (int inputs, decimal inputs, string inputs) with labels + defaults pre-filled, instead of an empty JSON textarea. I can submit without typing JSON.
- **US-002:** As Pablo, when I drop a new strategy file into `strategies/`, its form renders automatically on the next `GET /api/v1/strategies/` sync — zero per-strategy frontend work required.
- **US-003 (graceful degrade):** As Pablo, when a strategy uses a config type the form doesn't know how to render, I see the JSON textarea fallback (same UX as today) with a note explaining which types aren't supported yet — nothing is worse than today.
- **US-004 (error path):** As Pablo, when I submit a backtest with an invalid config (e.g. malformed `InstrumentId`), I get a clear field-level error message naming the bad field — not a generic 500.

## In scope

- Backend: populate `config_schema` + `default_config` on `StrategyResponse` (fields already declared — `backend/src/msai/schemas/strategy.py:18-19`).
- Backend: new `config_schema_status` enum field: `"ready" | "unsupported" | "extraction_failed" | "no_config_class"`.
- Backend: per-strategy `try/except` around extraction in `strategy_registry.py` so one bad config can't poison discovery.
- Backend: memoize extracted schema by `code_hash`, persist to the existing JSONB columns.
- Backend: shared `schema_hook` module for Nautilus ID types (`InstrumentId`, `BarType`, `StrategyId`, `Venue`, `Symbol`, `AccountId`, `ClientId`, `OrderListId`, `PositionId`, `TraderId`, `ComponentId`).
- Backend: server-side authoritative validation on `POST /api/v1/backtests/run` via `StrategyConfig.parse(json_string)`. Catches `msgspec.ValidationError`, returns 422 with `details: [{field, message}]` per `.claude/rules/api-design.md`.
- Backend: refactor so `GET /api/v1/strategies/{id}` doesn't depend on `GET /api/v1/strategies/` side-effects (Maintainer Blocking Objection #2).
- Backend: extend `test_parity_config_roundtrip.py` so omitted defaults + backend-injected fields normalize identically across backtest / portfolio / paper / live (Contrarian Blocking Objection #2).
- Frontend: custom mini-renderer component (~300 LOC, shadcn-native, zero npm dep) supporting `integer` / `string` / `boolean` / `decimal` / `enum` / `nullable` fields + the `x-format: instrument-id` and `x-format: bar-type` hints.
- Frontend: renderer activates only when `config_schema_status === "ready"`; otherwise render the current JSON textarea with the status message.
- Frontend: submit optimistically, surface 422 field-level errors inline.
- Frontend: replace only the backtest-run flow's textarea. Research launch form + strategy defaults editor keep their textareas (out of scope).

## Out of scope

- Smart typeaheads for `InstrumentId` / `BarType` backed by the registry (deferred — needs a `/instruments` search endpoint that doesn't exist yet).
- Portfolio add-strategy frontend flow (no matching UI exists today; ship backend pipeline first).
- Research launch form (`frontend/src/components/research/launch-form.tsx`) and strategy defaults editor (`frontend/src/app/strategies/[id]/page.tsx`) JSON textareas.
- Client-side JSON Schema validation library (e.g. `ajv`). No council consensus; server remains authoritative.
- `@rjsf/core` or similar third-party form libraries. Custom renderer chosen.
- Dependent/conditional fields, `oneOf` / `anyOf` / `allOf` JSON-Schema constructs, array-of-object editing.
- Deleting the JSON textarea component; it stays as fallback.

## Acceptance criteria (ship bar — council-ratified)

A ship is valid iff all of:

1. `GET /api/v1/strategies/{strategy_id}` returns `config_schema` (dict), `default_config` (dict), `config_schema_status` (enum) for the example `EMACrossStrategy`. `config_schema` has the 5 user fields (no inherited plumbing).
2. `POST /api/v1/backtests/run` with a payload containing `config={"instrument_id": "garbage", "bar_type": "..."}` returns 422 with `error.details[0].field == "instrument_id"`.
3. In the UI: opening "Run Backtest" for `EMACrossStrategy` shows typed fields prepopulated with defaults. Submitting without editing succeeds without typing any JSON.
4. If the backend reports `config_schema_status == "unsupported"` for a strategy, the UI falls back to the JSON textarea with an inline message.
5. A discovered strategy file with a syntax error in its `*Config` class does NOT cause the `GET /api/v1/strategies/` call to fail — that strategy row surfaces with `config_schema_status == "extraction_failed"`.
6. `backend/tests/integration/test_parity_config_roundtrip.py` still passes (xfails remain xfail — we do not change normalization semantics). New parity test: `test_omitted_defaults_normalize_identically_backtest_and_portfolio` passes.
7. A new strategy file dropped into `strategies/` gets a working form automatically on the next discovery sync. No per-strategy frontend code.

## Non-goals worth calling out

- Changing any DB hashing (live deployment identity, portfolio composition hash).
- Changing what gets persisted into `live_portfolio_revision_strategies.config` or `backtests.config`.
- Exposing `manage_stop`, `order_id_tag`, `external_order_claims`, or any of the 17 inherited `StrategyConfig` base-class fields in the form.

## Risks & mitigations

| Risk                                                               | Mitigation                                                                                                                |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| `msgspec.json.schema()` errors on a novel Nautilus type            | Per-strategy try/except emits `extraction_failed` status + structured log; UI falls back to textarea. No discovery crash. |
| Form materializes defaults and perturbs config bytes stored on run | Submit only user-edited fields; backend's `StrategyConfig.parse()` re-resolves defaults at run time (existing path).      |
| Inherited base-class fields leak into the form                     | Extractor trims schema to `Config.__annotations__.keys()` before persistence.                                             |
| Hot-path performance degradation on `/strategies` listing          | Memoize extraction by `code_hash`; persist to JSONB; only recompute when `code_hash` changes.                             |
| `config_schema=None` ambiguity ("no class" vs "extraction failed") | `config_schema_status` enum field disambiguates.                                                                          |
| Naming drift (`default_config` vs `config_defaults`)               | Stick with `default_config` (already declared in schema + ORM + api.ts). Do NOT introduce `config_defaults`.              |
| Live/backtest parity drift on new fields                           | Pre-merge: extend `test_parity_config_roundtrip.py`. Test is a hard gate.                                                 |
| Detail endpoint depends on list endpoint's sync side-effect        | Factor sync into a reusable backend path (`StrategyRegistrySyncService` or similar).                                      |

## Success metrics

- Primary: Pablo runs a backtest on `EMACrossStrategy` without typing any JSON. **Observable in Phase 5.4 E2E.**
- Secondary: any new strategy file auto-populates a working form. **Observable by adding a second example strategy in a follow-up PR.**
- Counter-metric: no change in `test_parity_config_roundtrip.py` xfail count. **Enforced by Phase 5.3 verify.**

## Open questions (none blocking — resolved by council)

All blocking objections from the council (8 total) have resolution plans in the research brief and will be tracked as individual tasks in the implementation plan.
