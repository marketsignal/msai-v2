# E2E Use Cases — Strategy Config Schema Extraction

**Feature:** strategy-config-schema-extraction
**Branch:** feat/strategy-config-schema-extraction
**Drafted:** 2026-04-20
**Graduation target:** `tests/e2e/use-cases/strategies/config-schema-form.md` (this file, when passing).
**Council ratified scope:** narrowed Scope B (backend-only + backtest-run flow frontend; no portfolio add-strategy, no smart typeaheads).

## Pre-flight

- `GET /health` returns 200.
- `GET /api/v1/auth/me` with `X-API-Key: msai-dev-key` returns 200.
- Alembic head is `w1r2s3t4u5v6` (migration applied).
- `strategies/example/ema_cross.py` + `strategies/example/config.py` present on disk.
- For UC-SCS-002's UI submit step to complete: at least one Databento-provider registry row must exist. Warm via `docker exec msai-claude-backend /app/.venv/bin/python -c "from msai.cli import app; app()" instruments refresh --provider databento --symbols ES.n.0` (requires `DATABENTO_API_KEY`).

---

## UC-SCS-001 — Extracted schema surfaces on GET /api/v1/strategies/{id}

**Interface:** API (fullstack — UI variant covered by UC-SCS-002).

**Intent:** When a discovered strategy has a valid Nautilus `StrategyConfig`, its JSON Schema and defaults are exposed so the frontend can render a form without per-strategy code.

**Setup:** Clean DB + example strategies dir. `GET /api/v1/strategies/` once to trigger discovery sync.

**Steps:**

1. `GET /api/v1/strategies/` with `X-API-Key: msai-dev-key` → 200. Capture the strategy row with `strategy_class == "EMACrossStrategy"`; record its `id`.
2. `GET /api/v1/strategies/{id}` → 200.

**Verification:**

- `body.config_schema_status == "ready"`.
- `body.config_schema.type == "object"`.
- `body.config_schema.properties` keys include `fast_ema_period`, `slow_ema_period`, `trade_size`, `instrument_id`, `bar_type`.
- `body.config_schema.properties` **does not** include `manage_stop`, `order_id_tag`, `external_order_claims`, or any other inherited `StrategyConfig` base-class field.
- `body.config_schema.properties.fast_ema_period == {"type": "integer", "default": 10}`.
- `body.config_schema.properties.instrument_id["x-format"] == "instrument-id"`.
- `body.default_config.fast_ema_period == 10` and `body.default_config.slow_ema_period == 30`.

**Persistence:** Second `GET /api/v1/strategies/{id}` returns the same payload (idempotent; `code_hash` memoization skips recompute).

---

## UC-SCS-002 — Run a backtest without typing JSON

**Interface:** UI (backtest flow — primary user story US-001 in the PRD).

**Intent:** Pablo picks `EMACrossStrategy` in the Run Backtest dialog, sees typed fields with defaults pre-filled, fills instruments + dates, and submits — no JSON typing anywhere.

**Setup:** Dev stack up. Auth via `X-API-Key` fixture or dev-mode. At least one Databento-provider registry row pre-warmed — this file uses `ES.n.0` because `msai instruments refresh --provider databento --symbols ES.n.0` is the canonical seed path and it's the only one that doesn't require IB Gateway.

**Steps:**

1. Navigate to `http://localhost:3300/backtests`.
2. Click "Run Backtest".
3. In the dialog, select "example.ema_cross" from the Strategy dropdown.
4. Observe the Configuration section below the dates: typed form fields (`Fast Ema Period = 10`, `Slow Ema Period = 30`, `Trade Size = 1`) — NOT a JSON textarea. The `instrument_id` and `bar_type` fields are NOT shown (they're injected backend-side from the `Instruments` top-level input).
5. Fill "Instruments" = `ES.n.0`. Leave the form defaults for all other fields.
6. Click "Run Backtest".

**Verification:**

- Dialog closes (success path — no 422 error banner).
- Backtest appears in the list on the `/backtests` page within ~2 seconds with status `pending`, `running`, or `failed` (failure is expected if Databento price data for ES.n.0 isn't cached locally — that's a worker-side concern, not a form-submit concern).
- `GET /api/v1/backtests/history` total count increased by 1; the new row's `strategy_id` matches the selected EMACrossStrategy UUID.
- Note: `GET /api/v1/backtests/history` does NOT return `config` inline; to assert the backend-injected `instrument_id` / `bar_type`, look at the worker log line `[INFO] backtest_running config={...}` OR add a backtest-detail endpoint in a follow-up PR.

**Persistence:** Reload `/backtests` — the new backtest row persists on refresh.

---

## UC-SCS-003 — 422 inline field error on malformed instrument ID

**Interface:** API (UI variant is parallel — the RunBacktestForm consumes the same 422 envelope into `fieldErrors` state).

**Intent:** When the submitted config fails `StrategyConfig.parse()`, the server returns 422 with a structured `details[].field` payload identifying the bad field, and the frontend surfaces the message inline under the field.

**Setup:** Same as UC-SCS-001.

**Steps:**

1. `POST /api/v1/backtests/run` with `X-API-Key: msai-dev-key`, body:

   ```json
   {
     "strategy_id": "<the EMACrossStrategy id>",
     "config": { "fast_ema_period": 5 },
     "instruments": ["this_is_not_a_valid_instrument"],
     "start_date": "2025-01-01",
     "end_date": "2025-01-15"
   }
   ```

   (The instrument resolution will fail first — this tests the existing 422 path that the validation helper runs AFTER.)

2. `POST /api/v1/backtests/run` with a VALID instrument but malformed config:
   ```json
   {
     "strategy_id": "<id>",
     "config": { "instrument_id": "garbage" },
     "instruments": ["AAPL.NASDAQ"],
     "start_date": "2025-01-01",
     "end_date": "2025-01-15"
   }
   ```

**Verification:**

- Step 1 → 422 with a `detail` string (existing instrument-resolve path unchanged).
- Step 2 → 422 with `detail.error.code == "VALIDATION_ERROR"` and `detail.error.details[0].field` containing `instrument_id`. The `detail.error.details[0].message` quotes the msgspec error message (`Error parsing 'InstrumentId' from 'garbage': missing '.' separator ...`).

**Persistence:** No backtest row should have been created for either request — verify `GET /api/v1/backtests/history` total count is unchanged.

---

## UC-SCS-004 — Status enum disambiguates empty-schema causes (regression guard)

**Interface:** API.

**Intent:** `config_schema_status` distinguishes "no config class", "unsupported type", "extraction failed", and "ready" — preventing the frontend from silently dropping into JSON textarea fallback for the wrong reason.

**Setup:** Same.

**Steps:**

1. `GET /api/v1/strategies/` → 200.
2. Inspect every row's `config_schema_status`.

**Verification:**

- Every row has a `config_schema_status` field with value ∈ `{ "ready", "unsupported", "extraction_failed", "no_config_class" }`.
- `EMACrossStrategy`'s row has status `"ready"`.
- Any row with `status != "ready"` has `config_schema == null` and `default_config == null` (or `default_config == {}`).

**Persistence:** N/A — read-only.
