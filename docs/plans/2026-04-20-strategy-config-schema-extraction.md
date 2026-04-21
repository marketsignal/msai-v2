# Strategy Config Schema Extraction — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task.

**Goal:** expose each strategy's Nautilus `StrategyConfig` as JSON Schema + defaults on `GET /api/v1/strategies/{id}`, add server-authoritative validation on `POST /api/v1/backtests/run`, ship a shadcn-native React form that replaces the JSON textarea in the backtest-run flow.

**Architecture:** a shared `msai.services.nautilus.schema_hooks` module provides `schema_hook` + `user_fields_of()` helpers. `strategy_registry.discover_strategies` calls them inside a per-strategy `try/except` and populates `config_schema`, `default_config`, `config_schema_status` on `DiscoveredStrategy`. `StrategyRegistrySyncService` factors DB sync out of `list_strategies` so `get_strategy` can lazy-sync on cache miss. Backtest API path validates incoming config via `StrategyConfig.parse(json_string)` and maps `msgspec.ValidationError` → HTTP 422 with field-level details. Frontend: a `<SchemaForm>` component walks JSON Schema properties to render shadcn `<Input>`/`<Select>`/`<Switch>` widgets; activates only on `config_schema_status === "ready"`; JSON `<Textarea>` remains as fallback.

**Tech stack:** Python 3.12 + msgspec + FastAPI + SQLAlchemy 2.0 (backend); Next.js 15 + React + shadcn/ui + TypeScript (frontend). Existing dev-dep: `msgspec` (via Nautilus), no new Python deps. No new npm deps.

**Council gates this plan honors (8 blocking objections):**

1. Contrarian #1 — msgspec schema fidelity **already verified** in Phase 0 spike (5/5 tests green).
2. Contrarian #2+#3 — parity drift: covered by Task B7 (extend `test_parity_config_roundtrip.py`).
3. Hawk #1+#2 — per-strategy try/except + code_hash memoization: Task B2 + B4.
4. Maintainer #2 — sync coupling: Task B3 extracts `StrategyRegistrySyncService`.
5. Hawk #3 + Maintainer #1 — `config_schema_status` field: Task B1.
6. Hawk #4 — server validation: Task B6.
7. Maintainer #3 — naming: stick with `default_config`. No drift in any task below.
8. Contrarian #4 — scope discipline: Task B7's parity test is a hard merge gate.

---

## Phase 0 — Pre-gate spike (COMPLETE)

- [x] `backend/tests/unit/test_strategy_registry.py::TestMsgspecSchemaFidelitySpike` — 5 tests green (committed alongside this plan). Proves msgspec.json.schema + schema_hook + StrategyConfig.parse round-trip.

---

## Phase 1 — Backend

### Task B1 — `schema_hooks.py` module + `config_schema_status` enum

**Files:**

- Create: `backend/src/msai/services/nautilus/schema_hooks.py`
- Modify: `backend/src/msai/services/strategy_registry.py:53-77` (extend `DiscoveredStrategy` dataclass)
- Modify: `backend/src/msai/schemas/strategy.py:10-24` (add `config_schema_status` field to `StrategyResponse`)
- Modify: `backend/src/msai/models/strategy.py` (add `config_schema_status` column, Alembic migration)
- Test: `backend/tests/unit/test_schema_hooks.py`

**Step 1: Write failing test for `schema_hook`**

```python
# tests/unit/test_schema_hooks.py
import msgspec
import pytest
from nautilus_trader.model.identifiers import InstrumentId, Venue

from msai.services.nautilus.schema_hooks import (
    ConfigSchemaStatus,
    build_user_schema,
    nautilus_schema_hook,
)

def test_schema_hook_maps_instrument_id_with_format_hint():
    out = nautilus_schema_hook(InstrumentId)
    assert out["type"] == "string"
    assert out["x-format"] == "instrument-id"

def test_schema_hook_maps_all_nautilus_id_types():
    # Venue is the minimal case — just a typed string with title
    out = nautilus_schema_hook(Venue)
    assert out == {"type": "string", "title": "Venue"}

def test_schema_hook_raises_on_unknown_type():
    class Unknown: pass
    with pytest.raises(NotImplementedError):
        nautilus_schema_hook(Unknown)

def test_config_schema_status_values():
    assert {s.value for s in ConfigSchemaStatus} == {
        "ready", "unsupported", "extraction_failed", "no_config_class"
    }
```

**Step 2: Implement**

```python
# backend/src/msai/services/nautilus/schema_hooks.py
"""Shared JSON-Schema extractor for Nautilus StrategyConfig subclasses.

Used by strategy discovery to populate ``config_schema`` + ``default_config``
on ``DiscoveredStrategy``. The renderer consumes the schema via
``GET /api/v1/strategies/{id}``.

msgspec.json.schema() raises on custom types without a hook. This module
installs a hook covering every Nautilus identifier type so the schema
serializes as plain strings with format hints for the UI.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

import msgspec


class ConfigSchemaStatus(StrEnum):
    READY = "ready"
    UNSUPPORTED = "unsupported"
    EXTRACTION_FAILED = "extraction_failed"
    NO_CONFIG_CLASS = "no_config_class"


def nautilus_schema_hook(t: type) -> dict[str, Any]:
    # Imports inside the function so modules that never touch Nautilus
    # (e.g. test utilities) don't pay the import cost.
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.identifiers import (
        AccountId, ClientId, ComponentId, InstrumentId, OrderListId,
        PositionId, StrategyId, Symbol, TraderId, Venue,
    )

    if t is InstrumentId:
        return {
            "type": "string",
            "title": "Instrument ID",
            "x-format": "instrument-id",
            "description": "SYMBOL.VENUE",
            "examples": ["AAPL.NASDAQ", "EUR/USD.IDEALPRO"],
        }
    if t is BarType:
        return {
            "type": "string",
            "title": "Bar Type",
            "x-format": "bar-type",
            "description": "INSTRUMENT_ID-STEP-AGGREGATION-PRICE_TYPE-SOURCE",
            "examples": ["AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"],
        }
    if t in (StrategyId, ComponentId, Venue, Symbol, AccountId, ClientId,
             OrderListId, PositionId, TraderId):
        return {"type": "string", "title": t.__name__}
    raise NotImplementedError(f"no schema hook for {t!r}")


def build_user_schema(config_cls: type) -> tuple[dict, dict, ConfigSchemaStatus]:
    """Return (schema, defaults, status) for a StrategyConfig subclass.

    Trims the msgspec-emitted schema to the class's own ``__annotations__``
    so inherited StrategyConfig base-class fields (manage_stop, order_id_tag,
    external_order_claims, ...) do NOT appear in the form.
    """
    try:
        full = msgspec.json.schema(config_cls, schema_hook=nautilus_schema_hook)
    except NotImplementedError:
        return ({}, {}, ConfigSchemaStatus.UNSUPPORTED)
    except Exception:
        return ({}, {}, ConfigSchemaStatus.EXTRACTION_FAILED)

    own_keys = set(config_cls.__annotations__.keys())
    class_def = full["$defs"][config_cls.__name__]
    trimmed_props = {k: v for k, v in class_def["properties"].items() if k in own_keys}
    schema = {"type": "object", "title": class_def.get("title"),
              "properties": trimmed_props,
              "required": [k for k in class_def.get("required", []) if k in own_keys]}
    defaults = {k: v["default"] for k, v in trimmed_props.items() if "default" in v}
    return (schema, defaults, ConfigSchemaStatus.READY)
```

**Step 3: Extend `DiscoveredStrategy`**

Add fields to the dataclass:

```python
config_schema: dict[str, Any] | None = None
default_config: dict[str, Any] | None = None
config_schema_status: str = ConfigSchemaStatus.NO_CONFIG_CLASS.value
```

**Step 4: Alembic migration — add column**

```python
# backend/alembic/versions/xxx_add_config_schema_status_to_strategies.py
def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column("config_schema_status", sa.String(32),
                  nullable=False, server_default="no_config_class"),
    )

def downgrade() -> None:
    op.drop_column("strategies", "config_schema_status")
```

**Step 5: Run tests, commit**

```bash
uv run python -m pytest tests/unit/test_schema_hooks.py tests/unit/test_strategy_registry.py -v
# expect: all green
uv run alembic upgrade head
git add backend/src/msai/services/nautilus/schema_hooks.py \
        backend/src/msai/services/strategy_registry.py \
        backend/src/msai/schemas/strategy.py \
        backend/src/msai/models/strategy.py \
        backend/alembic/versions/xxx_*.py \
        backend/tests/unit/test_schema_hooks.py
git commit -m "feat(strategy-registry): config_schema_status enum + schema_hooks module"
```

---

### Task B2 — per-strategy try/except in `discover_strategies`

**Files:**

- Modify: `backend/src/msai/services/strategy_registry.py:138-183`
- Test: extend `backend/tests/unit/test_strategy_registry.py::TestDiscoverStrategies`

**Step 1: Failing test — broken config class does NOT break discovery**

```python
def test_discover_strategies_broken_config_does_not_poison_list(tmp_path: Path) -> None:
    # Two strategy files: one clean, one with a *Config that msgspec can't serialize
    ...
    results = discover_strategies(tmp_path)
    assert len(results) == 2
    statuses = {r.name: r.config_schema_status for r in results}
    assert statuses["good.strategy"] == "ready"
    assert statuses["bad.strategy"] == "extraction_failed"
```

**Step 2: Implement**

In `discover_strategies`, after `config_cls = _find_config_class(module)`, call `build_user_schema(config_cls)` inside `try/except Exception as exc: log.warning("strategy_schema_extraction_failed", ...)`. Status defaults to `NO_CONFIG_CLASS` when `config_cls is None`.

**Step 3: Run test, commit.**

---

### Task B3 — `StrategyRegistrySyncService` (decouple list ↔ detail)

**Files:**

- Create: `backend/src/msai/services/strategy_registry_sync.py`
- Modify: `backend/src/msai/api/strategies.py:41-100` (list) and `:105-137` (detail)
- Test: `backend/tests/integration/test_strategy_registry_sync.py`

**Step 1: Failing test — `get_strategy` works on a cold DB (no prior list call)**

```python
async def test_get_strategy_works_without_prior_list_call(client, session_factory):
    async with session_factory() as session:
        strategy_id = await _bootstrap_example_strategy(session)
    r = await client.get(f"/api/v1/strategies/{strategy_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["config_schema_status"] == "ready"
    assert "fast_ema_period" in body["config_schema"]["properties"]
```

**Step 2: Implement**

`StrategyRegistrySyncService.sync()` encapsulates the current `list_strategies` upsert loop. Both `list_strategies` and `get_strategy` call it (the detail endpoint calls with `filter_by_id=strategy_id` so it only hits the filesystem once for the requested row).

**Step 3: Run test, commit.**

---

### Task B4 — `code_hash` memoization in sync

**Files:**

- Modify: `backend/src/msai/services/strategy_registry_sync.py`
- Modify: `backend/src/msai/models/strategy.py` (add index on `code_hash`)
- Test: `backend/tests/integration/test_strategy_registry_sync.py::test_sync_is_idempotent`

**Step 1: Failing test — sync doesn't recompute schema when `code_hash` unchanged**

```python
async def test_sync_skips_extraction_when_code_hash_unchanged(mocker):
    spy = mocker.spy(schema_hooks, "build_user_schema")
    await sync_service.sync()
    await sync_service.sync()  # second call, same filesystem
    assert spy.call_count == 1  # extraction only ran once per strategy
```

**Step 2: Implement**

In the sync loop: if `existing_row.code_hash == discovered.code_hash`, skip `build_user_schema()` and keep the persisted `config_schema` / `default_config` / `config_schema_status`. Only recompute when hash changes.

**Step 3: Run test, commit.**

---

### Task B5 — wire populated schema through API response

**Files:**

- Modify: `backend/src/msai/services/strategy_registry_sync.py` (write to DB)
- Modify: `backend/src/msai/api/strategies.py:84-100` (read `status` field)
- Modify: `backend/src/msai/schemas/strategy.py:10-24` (add `config_schema_status`)
- Modify: `frontend/src/lib/api.ts:112-124` (extend TS interface)
- Test: `backend/tests/unit/test_strategies_api.py` — assert schema + status on `GET /strategies/{id}`

**Step 1-3: standard TDD.**

---

### Task B6 — server-side validation on `POST /api/v1/backtests/run`

**Files:**

- Modify: `backend/src/msai/api/backtests.py` (POST handler)
- Modify: `backend/src/msai/services/backtest_service.py` (or wherever config is read)
- Test: `backend/tests/unit/test_backtests_api.py::test_run_backtest_rejects_bad_instrument_id_with_422`

**Step 1: Failing test**

```python
async def test_run_backtest_rejects_bad_instrument_id_with_422(client, graduated_strategy_id):
    r = await client.post("/api/v1/backtests/run", json={
        "strategy_id": str(graduated_strategy_id),
        "config": {"instrument_id": "garbage", "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"},
        "instruments": ["AAPL.NASDAQ"],
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
    })
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert any(d["field"].endswith("instrument_id") for d in err["details"])
```

**Step 2: Implement**

Load the strategy's `*Config` class via `load_strategy_class`. Serialize the request `config` dict to JSON. Call `ConfigCls.parse(json_str)`. On `msgspec.ValidationError`: extract field path from `str(exc)`, return 422 per `api-design.md` error format.

**Step 3: Run test + existing suite, commit.**

---

### Task B7 — extend parity roundtrip test (Contrarian Blocking Objection #2)

**Files:**

- Modify: `backend/tests/integration/test_parity_config_roundtrip.py`

**Step 1: New test**

```python
async def test_omitted_defaults_normalize_identically_backtest_and_portfolio():
    # Submit a backtest with config={"instrument_id": "AAPL.NASDAQ", "bar_type": "..."}
    # (fast_ema_period omitted → default=10)
    # Submit the same strategy to a portfolio revision
    # Assert the persisted Backtest.config and LivePortfolioRevisionStrategy.config
    # are byte-identical after normalization, so composition_hash stays stable.
    ...
```

Per council: this is a **hard merge gate**. If it xfails, we degrade to "backend only, no frontend renderer" — per the minority-report fallback.

**Step 2: Run. Must pass. Commit.**

---

## Phase 2 — Frontend

### Task F1 — `<SchemaForm>` mini-renderer

**Files:**

- Create: `frontend/src/components/strategies/schema-form.tsx`
- Create: `frontend/src/components/strategies/schema-form.types.ts` (narrow JSON-Schema subset type)
- Test: `frontend/src/components/strategies/schema-form.test.tsx` (if a test runner is configured; otherwise rely on E2E)

**Step 1: Types**

```typescript
// schema-form.types.ts
export type SchemaField =
  | { type: "integer"; default?: number; title?: string; description?: string }
  | { type: "number"; default?: number; title?: string; description?: string }
  | {
      type: "string";
      default?: string;
      format?: "decimal";
      "x-format"?: "instrument-id" | "bar-type";
      title?: string;
      description?: string;
      examples?: string[];
    }
  | { type: "boolean"; default?: boolean; title?: string; description?: string }
  | {
      anyOf: SchemaField[];
      default?: unknown;
      title?: string;
      description?: string;
    }; // nullable

export interface ObjectSchema {
  type: "object";
  title?: string;
  properties: Record<string, SchemaField>;
  required?: string[];
}
```

**Step 2: Renderer (shadcn-native)**

```tsx
// schema-form.tsx
"use client";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import type { ObjectSchema, SchemaField } from "./schema-form.types";

export function SchemaForm({
  schema,
  value,
  onChange,
  errors,
}: {
  schema: ObjectSchema;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  errors?: Record<string, string>;
}) {
  return (
    <div className="space-y-3">
      {Object.entries(schema.properties).map(([key, field]) => (
        <FieldRenderer
          key={key}
          name={key}
          field={field}
          value={value[key]}
          onChange={(v) => onChange({ ...value, [key]: v })}
          error={errors?.[key]}
        />
      ))}
    </div>
  );
}

function FieldRenderer({ name, field, value, onChange, error }: ...) {
  // Dispatch on field.type / anyOf / x-format.
  // integer/number → <Input type="number" />
  // boolean → <Switch />
  // string + format=decimal → <Input /> with validation
  // string + x-format=instrument-id/bar-type → <Input /> + placeholder from examples
  // anyOf([T, null]) → T widget + nullable checkbox
  // Unknown → <Textarea /> fallback (mini-renderer's safety net)
}
```

**Step 3: Snapshot test with the spike's schema output.**

Commit.

---

### Task F2 — integrate `<SchemaForm>` into `run-form.tsx`

**Files:**

- Modify: `frontend/src/components/backtests/run-form.tsx:199-207`
- Modify: `frontend/src/app/backtests/page.tsx` — fetch `config_schema` + `default_config` from `/api/v1/strategies/{id}` when strategy selection changes
- Test: manual smoke; E2E covers it in Phase 5.4

**Step 1: Data flow**

When `selectedStrategy` changes, fetch `/api/v1/strategies/{id}` and set local state `{schema, defaults, status}`. Seed form state from `defaults`.

**Step 2: Conditional render**

```tsx
{status === "ready" && schema ? (
  <SchemaForm schema={schema} value={formState} onChange={setFormState} errors={fieldErrors} />
) : (
  <>
    <Textarea value={configJson} onChange={(e) => setConfigJson(e.target.value)} ... />
    {status !== "ready" && (
      <p className="text-xs text-muted-foreground">
        Auto-form unavailable for this strategy ({status}). Edit JSON directly.
      </p>
    )}
  </>
)}
```

**Step 3: Submit path**

Build request body from `formState` (schema path) OR parsed `configJson` (fallback path). POST to `/api/v1/backtests/run`. On 422: parse `error.details[]` → populate `fieldErrors`.

**Step 4: Commit.**

---

## Phase 3 — E2E use cases

Stage two use cases in the plan file and graduate after Phase 5.4 passes.

### UC-SC-001 — Run backtest without typing JSON (happy path)

- **Intent:** Pablo picks the EMACrossStrategy, sees the form, submits with defaults.
- **Interface:** API + UI (fullstack).
- **Setup:** seed `strategies/example/ema_cross.py` (already present). Ensure a graduated `Strategy` DB row exists for it.
- **Steps (API):** `GET /api/v1/strategies/{id}` — assert `config_schema_status == "ready"`, `properties.fast_ema_period.default == 10`. `POST /api/v1/backtests/run` with `config = default_config` dict — assert 201.
- **Steps (UI):** navigate to `/backtests`. Click "Run Backtest". Select "EMA Cross". Form renders with `fast_ema_period=10` pre-filled. Fill dates. Click Run. See job enqueued toast.
- **Verification:** `GET /api/v1/backtests/{id}/status` returns `pending` or `running`. UI shows row in backtest history.
- **Persistence:** reload page. Backtest row persists.

### UC-SC-002 — Unsupported-config fallback

- **Intent:** a strategy with an exotic config type falls back to the JSON textarea gracefully.
- **Interface:** API + UI.
- **Setup:** drop a fixture strategy `strategies/_test/exotic_config.py` whose `*Config` declares a type `schema_hook` doesn't cover (e.g. a custom dataclass).
- **Steps (API):** `GET /api/v1/strategies/` — assert list returns both strategies (no 500). The exotic one has `config_schema_status == "unsupported"` and `config_schema == {}`.
- **Steps (UI):** select the exotic strategy — see JSON textarea + info banner "Auto-form unavailable for this strategy (unsupported)".
- **Verification:** can still submit valid JSON and get a backtest enqueued.
- **Persistence:** N/A.

### UC-SC-003 — 422 with field-level error on bad config

- **Intent:** submitting malformed config surfaces inline field errors.
- **Interface:** API + UI.
- **Steps (API):** `POST /api/v1/backtests/run` with `config = {"instrument_id": "garbage", ...}` — assert 422 with `error.details[0].field` containing `instrument_id`.
- **Steps (UI):** in the form, type `garbage` into Instrument ID field. Click Run. Inline red error appears under the field reading the msgspec message.

---

## Phase 4 — Quality gates

See `.claude/commands/new-feature.md` Phase 5. Standard loops:

1. Code review (Codex + PR Toolkit) in parallel until clean.
2. Simplify (3-agent parallel).
3. `verify-app` subagent (full test suite, ruff, mypy --strict).
4. `verify-e2e` subagent (3 UCs above).
5. E2E regression (run use cases already in `tests/e2e/use-cases/`).
6. Graduate UCs to `tests/e2e/use-cases/strategy-config-schema.md`.
7. Commit + push + PR + (after merge) delete branch + worktree.

No live-trading drill required — this feature does not touch the live path.

---

## Sequencing & rough budget

| Phase     | Tasks                                      | Budget                           |
| --------- | ------------------------------------------ | -------------------------------- |
| 0         | Spike (done)                               | ~1h (complete)                   |
| 1         | B1–B7                                      | ~1 day                           |
| 2         | F1–F2                                      | ~0.5 day                         |
| 3         | E2E authoring (already in plan)            | negligible (reused in Phase 5.4) |
| 4         | Code review + simplify + verify + E2E + PR | ~0.5 day                         |
| **Total** |                                            | **~2 days wall-clock**           |

Backend tasks can be done end-to-end before any frontend task starts (clean contract). Within backend: B1 (schema_hooks + status enum + dataclass) unlocks B2–B5 in parallel; B6 (API validation) and B7 (parity test) depend on B1's status field.
