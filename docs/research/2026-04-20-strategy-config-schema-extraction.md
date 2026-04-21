# Research Brief — Strategy Config Schema Extraction

**Date:** 2026-04-20
**Author:** Claude (per /new-feature workflow Phase 2)
**Status:** Complete — all gate criteria met

## Purpose

Verify the technical viability of extracting JSON Schema from Nautilus `StrategyConfig` subclasses so the frontend can auto-generate a typed form for the backtest-run flow (replacing the raw JSON textarea at `frontend/src/components/backtests/run-form.tsx:199-207`). Research driven by the 2026-04-20 engineering council verdict (`docs/prds/strategy-config-schema-extraction-discussion.md`). The council Contrarian OBJECTed that msgspec behavior on Nautilus-native types was unverified and required a pre-implementation spike.

## Libraries & APIs investigated

### `msgspec` (Nautilus's serialization layer)

**Version:** pinned via Nautilus 1.223.0 transitive dep.

**Findings:**

| Call                                        | Behavior                                                                                                                                                              | Sources                                                                                                                   |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `msgspec.json.schema(T)` (no `schema_hook`) | Raises `TypeError` on any custom class (e.g. `InstrumentId`, `BarType`, `StrategyId`). Does NOT silently degrade to `type: string`.                                   | Verified live 2026-04-20, `.venv/lib/python3.12/site-packages/msgspec/_json_schema.py:428`                                |
| `msgspec.json.schema(T, schema_hook=fn)`    | Succeeds when `fn(t) -> dict` returns valid JSON Schema for each custom type encountered. Produces `$defs[ClassName]`, properties with defaults, anyOf for nullables. | Verified live 2026-04-20 on `EMACrossConfig`; see `tests/unit/test_strategy_registry.py::TestMsgspecSchemaFidelitySpike`. |
| `msgspec.structs.fields(T)`                 | Returns **all** fields including 17 inherited `StrategyConfig` base-class fields (`strategy_id`, `order_id_tag`, `manage_stop`, etc.).                                | Verified live.                                                                                                            |
| `T.__annotations__`                         | Returns **only** the class's own declarations (5 user-defined fields for `EMACrossConfig`).                                                                           | Verified live.                                                                                                            |
| Primitive type output: `int`                | `{"type": "integer", "default": <val>}`                                                                                                                               | Verified live.                                                                                                            |
| Primitive type output: `Decimal`            | `{"type": "string", "format": "decimal", "default": "<val>"}`                                                                                                         | Verified live.                                                                                                            |
| Primitive type output: `bool`               | `{"type": "boolean", "default": <val>}`                                                                                                                               | Verified live.                                                                                                            |
| `T \| None` (nullable) output               | `{"anyOf": [{<type>}, {"type": "null"}], "default": null}`                                                                                                            | Verified live.                                                                                                            |

**Design impact:** backend extractor must (a) install a `schema_hook` covering Nautilus ID types, (b) trim `$defs[ClassName].properties` via `T.__annotations__` keys to hide inherited plumbing, (c) handle the `TypeError` path as "unsupported" status instead of crashing discovery.

**Test implication:** 5 spike tests land at `backend/tests/unit/test_strategy_registry.py::TestMsgspecSchemaFidelitySpike`. Full discovery path must gain a per-strategy `try/except` around `msgspec.json.schema()` so a new unsupported type can't break `/strategies` listing.

### `nautilus_trader.trading.config.StrategyConfig`

**Findings:**

| Call                                          | Behavior                                                                                                                                                        | Sources                   |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| `EMACrossConfig.parse(json_string)`           | Accepts string payloads; converts `"AAPL.NASDAQ"` → `InstrumentId`, `"AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"` → `BarType`, `"2.5"` → `Decimal`.                    | Verified live 2026-04-20. |
| `.parse()` on bad InstrumentId                | Raises `msgspec.ValidationError: Error parsing 'InstrumentId' from 'garbage': missing '.' separator ... at $.instrument_id`. Field path usable for 422 display. | Verified live.            |
| `.parse()` on minimum payload (defaults only) | Fills `fast_ema_period=10`, `slow_ema_period=30`, `trade_size=Decimal("1")`, `strategy_id=None`.                                                                | Verified live.            |

**Design impact:** `StrategyConfig.parse(json_string)` is the canonical server-authoritative round-trip path. Backend `POST /api/v1/backtests/run` should pass the user's submitted `config` dict through `json.dumps` → `<Config>.parse(json_str)` to validate; catch `msgspec.ValidationError` and return 422 with the field path attached (per `.claude/rules/api-design.md` error format).

**Test implication:** extending `backend/tests/integration/test_parity_config_roundtrip.py` to prove omitted-default + backend-injected-field normalization holds across backtest / portfolio / paper / live paths — per the council's Blocking Objection #2.

### Nautilus identifier types (`InstrumentId`, `BarType`, `StrategyId`, `Venue`, …)

**Findings:**

- All are Rust-backed frozen classes (cython/pyo3 layer). No Python-level `__annotations__`; hence msgspec cannot introspect them without help.
- Inherited base-class field `external_order_claims: list[InstrumentId] | None` means the schema_hook must also cover nested occurrences (msgspec recurses through generics).
- Known complete list of Nautilus ID types: `InstrumentId`, `BarType`, `StrategyId`, `ComponentId`, `Venue`, `Symbol`, `AccountId`, `ClientId`, `OrderListId`, `PositionId`, `TraderId`. Covered by the spike's `_nautilus_schema_hook`.

**Design impact:** the `schema_hook` belongs in a shared module (`backend/src/msai/services/nautilus/schema_hooks.py` is the obvious home) so `strategy_registry.py` and future API layers can share it. Future-proof: any new Nautilus ID type gets a one-line addition.

**Test implication:** spike already covers the 2 types that need format hints (`InstrumentId`, `BarType`). A parametrized test over the full `nautilus_id_types` tuple is the right regression guard at integration time.

## Open Risks

1. **Complex field types beyond the covered subset.** `Annotated[int, msgspec.Meta(gt=0)]` and `TimeInForce` (a `flag` enum) appear on the base class. The spike doesn't assert renderer behavior for these — the mini-renderer's "unsupported type → JSON textarea fallback" is the escape hatch, but we should log when it fires.
2. **msgspec default mutation risk.** `EMACrossConfig.fast_ema_period: int = 10` renders as `{"default": 10}`. If the form materializes defaults into the user's submitted payload (instead of letting the backend re-resolve them), the stored `default_config` bytes diverge from the backtest's effective config. The form MUST submit only user-edited fields.
3. **Frontend bundle impact.** Custom renderer (~300 LOC) adds ≤ 5 KB gzip; no new npm dep. Acceptable.
4. **Graduation parity (Contrarian Blocking Objection #2).** `test_parity_config_roundtrip.py` currently xfails on `manage_stop` / `order_id_tag` injection. The UI form must NOT expose those fields, and backend must continue injecting them identically across backtest/portfolio/live. Verified by extending the parity test — blocker for merge per council.
5. **code_hash memoization edge case.** If two strategies share the same `code_hash` (unlikely but possible for zero-content edits), the JSONB cache key collides. Acceptable — SHA256 of non-trivial files makes this astronomically unlikely; will revisit if Pablo lands a deterministic-hash test strategy.

## Conclusion

**Spike verdict: PASS.** All three council-required properties hold:

- (a) `msgspec.json.schema(..., schema_hook=...)` produces usable JSON Schema.
- (b) `StrategyConfig.parse(json_string)` is the round-trip of record with field-level error paths.
- (c) User-defined fields are distinguishable from inherited base plumbing.

**Scope B proceeds as ratified** (narrowed per chairman): Q1=backtest-only, Q2=plain-text+hint, Q3=server 422 (authoritative), Q4=custom mini-renderer, Q5=ship bar requires `config_schema_status` field + parity test extension + JSON fallback on unsupported types.

Next step: write the implementation plan invoking all 8 chairman blocking-objection resolution plans.
