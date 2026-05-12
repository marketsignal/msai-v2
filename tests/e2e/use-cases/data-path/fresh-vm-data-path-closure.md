# Fresh-VM data-path closure — E2E use cases (graduated)

Graduated 2026-05-12 from PR `fix/fresh-vm-data-path-closure` after pass-3 verify-e2e PASS. These five use cases are the regression net for the 2026-05-12 prod incident — they catch the three env-only bugs that survived four deployment-pipeline slices.

> ARRANGE allowed methods: `/api/v1/instruments/bootstrap` (or the `msai instruments bootstrap` CLI) and `msai ingest stocks` CLI per `.claude/rules/testing.md`. No raw DB writes, no Parquet file injection.

## UC1 — Databento MIC alias resolves to canonical exchange-name

**Intent.** A developer who just ran `msai ingest stocks AAPL` submits a backtest using the printed `instrument_id=AAPL.XNAS` (MIC form) and the backtest completes. This is the literal scenario that 422-ed on prod 2026-05-12.

**Interface:** API.

**Setup.**

- `msai instruments bootstrap --provider databento --symbols AAPL` (outcome `created` or `noop`).
- Bars present for the chosen window (or run `msai ingest stocks AAPL <start> <end>` first).

**Steps.**

- `POST /api/v1/backtests/run` with `instruments=["AAPL.XNAS"]`, `config.instrument_id="AAPL.XNAS"`, `bar_type="AAPL.XNAS-1-MINUTE-LAST-EXTERNAL"`, EMA Cross config.

**Verification.**

- HTTP 201 with a job id (the prod incident returned 422).
- Poll `/api/v1/backtests/{id}/status` until `completed`.
- `/api/v1/backtests/{id}/results` returns a JSON body with `metrics` and `trade_count >= 0`.

**Persistence.** Backtest row appears in `/api/v1/backtests/history` (non-smoke).

## UC2 — Exchange-name alias still works (idempotency / asymmetric registry)

**Intent.** A user who scripted against `AAPL.NASDAQ` (the form `lookup_for_live` documents) keeps working — even when the registry holds only the MIC form.

**Interface:** API.

**Setup.** Same registry row from UC1.

**Steps.** Same as UC1 with `AAPL.NASDAQ` everywhere instead of `AAPL.XNAS`.

**Verification.** HTTP 201 → poll → `completed` → results body has `metrics`.

**Persistence.** New row in history.

## UC3 — Unknown MIC venue fails loud with a `bootstrap`-pointing error

**Intent.** A user who fat-fingers a venue gets an actionable error pointing at the correct CLI (`bootstrap`, not the IB-only `refresh`).

**Interface:** API.

**Steps.** Submit with `instruments=["AAPL.FAKEMIC"]`.

**Verification.**

- HTTP 422.
- `detail` contains `bootstrap`.
- `detail` does NOT contain `instruments refresh` (regression guard against the 2026-05-12 wrong-CLI suggestion).
- `detail` surfaces the unknown venue `FAKEMIC`.

## UC4 — Smoke rows are filtered out of `/backtests/history` by default; `include_smoke=true` shows them

**Intent.** Operator history view stays clean; smoke rows are visible only on opt-in.

**Interface:** API.

**Setup.** Two backtest submissions to `/api/v1/backtests/run`: one with `"smoke": true`, one without.

**Steps.**

- `GET /api/v1/backtests/history` (default).
- `GET /api/v1/backtests/history?include_smoke=true`.

**Verification.**

- Default response: non-smoke backtest in `items`; smoke-tagged one NOT.
- `include_smoke=true` response: both present.
- `total` matches the count delta.

## UC5 — Strategy registry is populated (STRATEGIES_ROOT regression guard)

**Intent.** Backend container with `STRATEGIES_ROOT` correctly wired returns non-empty `/api/v1/strategies/`. This is the assertion that would have caught the 2026-05-12 prod bug at day one.

**Interface:** API.

**Steps.** `GET /api/v1/strategies/`.

**Verification.**

- HTTP 200.
- `items` has length >= 1.
- At least one item has `name == "example.ema_cross"`.
