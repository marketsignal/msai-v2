# PRD Discussion: Strategy Config Schema Extraction

**Status:** In Progress
**Started:** 2026-04-20
**Participants:** Pablo, Claude

## Original User Stories

From CONTINUITY.md `Next` list (item #5, pre-PR#37):

> **Strategy config-schema extraction for UI form generation.**
> Skeleton at `docs/plans/2026-04-17-db-backed-strategy-registry.md` §"Strategy Config Schema Extraction + API":
> expose each strategy's Nautilus `StrategyConfig` via `GET /api/v1/strategies/{id}` as `config_schema` + `config_defaults` so a future UI can render forms.

**Scope ratified 2026-04-20 (Pablo):** Option B — full stack. Backend exposes schema + defaults AND frontend ships an auto-generating form component.

## Current State (observed from code)

### Backend

- `DiscoveredStrategy` dataclass (`backend/src/msai/services/strategy_registry.py:53-77`) captures `config_class_name` but NOT the schema or defaults.
- DB model `Strategy` (referenced in `backend/src/msai/api/strategies.py:133-134`) already has `config_schema` and `default_config` columns.
- API schema `StrategyResponse` (`backend/src/msai/schemas/strategy.py:18-20`) already declares both fields as `dict[str, Any] | None`.
- **Gap:** no code populates these fields. They are silently `None` for every existing strategy record.

### Frontend

- Current backtest flow (`frontend/src/components/backtests/run-form.tsx:199-207`) is a raw JSON `<Textarea>` with a string placeholder. Zero validation, zero typed fields, zero auto-complete.
- Only user of the form today is the `RunBacktestForm` dialog on `/backtests`.

### Nautilus

- Strategy config uses `StrategyConfig` (which subclasses `msgspec.Struct`), NOT Pydantic `BaseModel`. Example: `strategies/example/config.py:10-15` defines `EMACrossConfig(StrategyConfig, frozen=True)` with fields `instrument_id: InstrumentId`, `bar_type: BarType`, `fast_ema_period: int = 10`, `slow_ema_period: int = 30`, `trade_size: Decimal = Decimal("1")`.
- `msgspec` ships `msgspec.json.schema(...)` for JSON Schema extraction; needs verification that Nautilus's custom types (`InstrumentId`, `BarType`) round-trip through it.

### Industry patterns (discovery research)

- Established JSON-Schema → React form libraries: `@rjsf/core` (react-jsonschema-form — most common, heavy, Material-default theme), `uniforms`, `@formily/react`. All are generic and theming is work.
- Quant trading products typically ship per-strategy hand-coded forms (Backtrader UI, QuantConnect's textarea). No off-the-shelf pattern.
- Custom mini-renderer with tight shadcn integration aligns with the project's brutal-simplicity principle and dark-mode-first aesthetic.

## Discussion Log

### Round 1 — 2026-04-20

**Claude's questions for Pablo:**

1. **Flow integration priority.** The backtest-run dialog at `run-form.tsx` is the one concrete pain point today (JSON textarea). Scope B naturally extends to the portfolio add-strategy flow. Do you want **both** in this PR, or **backtest-only first** and portfolio add-strategy in a follow-up? (The difference is ~4 frontend tasks vs ~8.)

2. **Nautilus special field types.** `EMACrossConfig` uses `InstrumentId` ("AAPL.NASDAQ" format) and `BarType` ("AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL" format). Two options:
   - **(a) Render as plain text inputs** with a format hint below (e.g. "SYMBOL.VENUE"). Simple; user types the correct string.
   - **(b) Smart typeaheads** — `InstrumentId` becomes a dropdown that queries `/api/v1/market-data/symbols` + venue; `BarType` becomes a composite widget.

   Option (a) ships in 1 day of frontend work; option (b) is 3-4 days and is where every quant UI spends most of its polish budget.

3. **Validation UX on submit.** When the user fills the form and clicks "Run Backtest":
   - **(a) Client-side validate** against the JSON schema → block submit locally with field-level errors.
   - **(b) Submit optimistically** → show server's 422 response as a toast/banner.

   (a) gives better UX but ~200 LOC more; (b) is simpler and reuses the existing server validation path.

4. **Form library choice.**
   - **(a) Custom mini-renderer** — ~300 LOC, only renders the subset we need (int/decimal/string/enum/nullable), 100% shadcn styling, zero npm dep.
   - **(b) `@rjsf/core` with shadcn theme adapter** — full JSON Schema coverage (oneOf, dependencies, etc.), ~100 KB gzip added, needs a custom widget pack to match our dark-mode aesthetic (or lives with Material default).

   My default is (a). (b) makes sense only if you expect operators to author schemas with oneOf/anyOf/allOf soon.

5. **Success metric / ship bar.** What tells us this feature is done beyond "tests pass"?
   - "I can launch a backtest on `EMACrossStrategy` without typing any JSON" — concrete user-visible bar.
   - "Adding a new strategy file to `strategies/` automatically gets a working form in the UI" — stronger bar, requires the discovery path actually populates schema.
   - Something else?

---

_Waiting for Pablo's answers before proceeding to PRD creation._
