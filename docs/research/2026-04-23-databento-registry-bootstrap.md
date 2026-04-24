# Research: Databento registry bootstrap for equities, ETFs & futures

**Date:** 2026-04-23
**Feature:** Databento-backed write path for `instrument_definitions` + `instrument_aliases`; new `POST /api/v1/instruments/bootstrap` + `msai instruments bootstrap` CLI verb; 3 explicit readiness states (`registered` / `backtest_data_available` / `live_qualified`).
**Researcher:** research-first agent

## TL;DR for the plan phase

Five findings actually change design. In priority order:

1. **OQ-3 APPEARS FIXED (high confidence).** The `FuturesContract.to_dict()` signature drift Codex flagged has an explicit dual-path handler at `backend/src/msai/services/nautilus/security_master/parser.py:204-212`. `msai instruments refresh --provider databento --symbols ES.n.0` should no longer hang on that call. **The Phase 4 plan can assume the fix is in place, but MUST run the live `instruments refresh` reproduction on the branch against a running Docker stack as a belt-and-suspenders check** before B9 (US-007 futures path) is marked complete. If reproduction fails, there is a different bug — the `bootstrap` path hits the same parser.
2. **OQ-2 UNKNOWN → conservative policy locks in.** Databento does NOT publicly document per-endpoint rate limits for the Historical REST API. The SDK itself already retries 429s for `Historical.batch.download` only (SDK v0.33.0, 2024-04). `timeseries.get_range` and `metadata.*` do NOT retry on 429 inside the SDK. **Design impact:** the PRD's `tenacity` wrapper + `max_concurrent=3` hard cap is correct and necessary. Don't expect SDK-side resilience.
3. **NEW RISK (beyond the 6 OQs): Nautilus venue-string ambiguity.** `DatabentoDataLoader.from_dbn_file(use_exchange_as_venue=True)` produces the exchange MIC for equities — `XNAS`, `XARC` — not the exchange name (`NASDAQ`, `ARCA`). This will create an IB-vs-Databento venue divergence by design, not by accident: IB resolves AAPL as `AAPL.NASDAQ`, Databento will register it as `AAPL.XNAS`. **This is exactly what the US-009 divergence counter was designed to catch, but the PRD's "100% divergence detection" metric will fire on every single equity bootstrap if IB ever refreshes afterward.** Design must decide: (a) accept it as expected noise and filter by known IB↔Databento pairs, (b) normalize to exchange-name at the Databento bootstrap write boundary (mirror the FX `.` → `/` normalization in `service.py:735-736`), or (c) change the `use_exchange_as_venue` kwarg to `False` for equities — but `False` produces `GLBX` for futures, which breaks futures canonical form. **Recommended: option (b) — add an MIC→exchange-name map in `_upsert_definition_and_alias` for the `databento` provider path so the alias string matches IB's form.** Plan should add this explicitly to the B5/B6 tasks.
4. **OQ-4 DEFENSIVE — advisory lock IS required for the bootstrap path.** Existing `_upsert_definition_and_alias` at `service.py:694-811` uses `INSERT ... ON CONFLICT DO UPDATE` on definitions and an `UPDATE ... SET effective_to = today WHERE effective_to IS NULL` step between the definition upsert and the new alias insert. **Two concurrent bootstrap calls for the same symbol can interleave between the UPDATE and the alias INSERT, producing two active aliases** (both `effective_to IS NULL`) — the `ck_instrument_aliases_effective_window` CHECK does not forbid this, and there is NO unique partial index on `(instrument_uid, provider, effective_to IS NULL)`. The `uq_instrument_aliases_string_provider_from` only blocks same-day same-string-same-provider. The lock is NOT just defensive — it closes a real race. Design must implement `pg_advisory_xact_lock(hashtext(provider || '|' || raw_symbol || '|' || asset_class))` at the top of the transaction.
5. **OQ-5 AMBIGUITY SHAPE is clear.** With `stype_in="raw_symbol"` and `schema="definition"`, Databento returns **one definition record per distinct `instrument_id`** the raw symbol matched in the window. For most equities there is exactly one; for dual-class shares like `BRK.B` (multiple instances in the definition stream) or dual-listed ADRs, multiple records will come back, each with its own `instrument_id`. **The `candidates[]` field list the PRD specifies (raw_symbol, listing_venue, dataset, security_type, databento_instrument_id, description) maps cleanly onto the Nautilus `Instrument` fields the `DatabentoDataLoader` produces** — `raw_symbol`, `id.venue.value` (the MIC), the `dataset` passed in, the Nautilus class name as `security_type`, and the Nautilus `InstrumentId.instrument_id` (UInt32). The PRD's `description` field has no clean Nautilus source — drop it or populate from a follow-up call. **Design impact:** rename `candidates[].description` → `candidates[].asset_class` (from the `asset_class_for_instrument_type` helper) so every field has a deterministic source.

Everything else is either confirmed as the PRD describes, or is low-enough stakes that Phase 4 TDD catches it.

## Libraries Touched

| Library                         | Our Version                                                                        | Latest Stable                                             | Breaking Changes                                                    | Source                                                                                                                                                |
| ------------------------------- | ---------------------------------------------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `databento` (Historical SDK)    | `>=0.43.0`                                                                         | `0.75.0` (2026-04-08, PyPI) · `0.76.0` on GH (2026-04-21) | Many, but none in our call paths                                    | [CHANGELOG](https://github.com/databento/databento-python/blob/main/CHANGELOG.md) · [PyPI](https://pypi.org/project/databento/) (accessed 2026-04-23) |
| `nautilus_trader[ib]`           | `>=1.222.0`                                                                        | `1.223.0` current pin                                     | None for `DatabentoDataLoader.from_dbn_file` kwargs                 | [Databento adapter docs](https://nautilustrader.io/docs/latest/integrations/databento/) (accessed 2026-04-23)                                         |
| `tenacity`                      | **NOT DIRECT DEP** (transitive via nautilus — present at runtime but not declared) | `9.1.4` (2026-02-07)                                      | 9.x stable API; async-native                                        | [PyPI](https://pypi.org/project/tenacity/) · [GitHub](https://github.com/jd/tenacity) (accessed 2026-04-23)                                           |
| `fastapi`                       | `>=0.133.0`                                                                        | ≥0.133 current pin                                        | None affecting 207 Multi-Status                                     | [207 pattern reference](https://oneuptime.com/blog/post/2026-02-02-rest-bulk-api-partial-success/view) (accessed 2026-04-23)                          |
| `sqlalchemy[asyncio]` + asyncpg | `>=2.0.36`                                                                         | 2.0.x                                                     | None for `pg_advisory_xact_lock` via `func.pg_advisory_xact_lock()` | [asyncpg advisory lock](https://medium.com/@n0mn0m/postgres-advisory-locks-with-asyncio-a6e466f04a34) (accessed 2026-04-23)                           |
| `prometheus_client` primitives  | via MSAI `Histogram` / Counter wrappers                                            | n/a — internal                                            | n/a                                                                 | Reuse existing `trading_metrics.py` + PR #41 `Histogram` pattern                                                                                      |
| `typer`                         | `>=0.15.0`                                                                         | 0.15.x                                                    | None                                                                | Reuse existing CLI sub-app pattern in `cli.py`                                                                                                        |

## Per-Library Analysis

### databento (Historical Python SDK)

**Versions:** ours=`>=0.43.0` (pinned in `pyproject.toml:30`) — the resolved lockfile version is what actually runs; latest on PyPI=`0.75.0` (2026-04-08), latest on GH=`0.76.0` (2026-04-21) but not yet on PyPI.

**Breaking changes since `0.43.0` that MIGHT touch our bootstrap path:**

| Version | Date       | Change                                                         | Affects bootstrap?                                                         |
| ------- | ---------- | -------------------------------------------------------------- | -------------------------------------------------------------------------- |
| 0.48.0  | 2025-01-21 | "Added new dataset `EQUS.MINI`"                                | Yes — relevant fallback if XNAS.ITCH is unentitled                         |
| 0.55.0  | 2025-05-29 | DBNv3 delivered via `databento-dbn 0.35.0`                     | Low risk — we decode via Nautilus loader, which tracks its own DBN version |
| 0.62.0  | 2025-08-19 | Removed ability to directly instantiate most enums from `int`  | No — we don't construct DBN enums                                          |
| 0.65.0  | 2025-11-11 | **Removed Python 3.9** support                                 | No — we're Python 3.12/3.13                                                |
| 0.67.0  | 2025-12-02 | `map_symbols` default flipped to `True` for `batch.submit_job` | No — we use `timeseries.get_range`, not `batch.submit_job`                 |
| 0.68.0  | 2025-12-09 | Python 3.14 support                                            | No — our pin is `>=3.12,<3.14`                                             |

**Deprecations:** none affecting bootstrap — the method we use (`timeseries.get_range`) has been stable since its rename from `timeseries.stream` in v0.8.0 (2023-03).

**Exception hierarchy** (verified against [common/error.py](https://raw.githubusercontent.com/databento/databento-python/main/databento/common/error.py)):

```
BentoError (Exception)
  BentoHttpError          # attrs: http_status, http_body, json_body, message, headers, request_id
    BentoServerError      # 500-series
    BentoClientError      # 400-series (includes 401, 403, 422, 429 — all same class)
BentoWarning (Warning)
  BentoDeprecationWarning
```

**Design impact:**

- Retry policy must key on `http_status`, not on exception class — `BentoClientError` covers both 401 (never retry) and 429 (retry). `tenacity`'s `retry_if_exception()` with a custom predicate is the right primitive:

  ```python
  def _should_retry(exc: BaseException) -> bool:
      return isinstance(exc, BentoServerError) or (
          isinstance(exc, BentoClientError) and getattr(exc, "http_status", None) == 429
      )
  ```

- Our current client in `databento_client.py:83-87` wraps all SDK exceptions in a generic `RuntimeError(f"... failed for {symbol} ...: {exc}")` — **that swallows `http_status`**. Plan must introduce a narrower error type in the bootstrap path so the retry policy and the `DATABENTO_UNAUTHORIZED` / `DATABENTO_RATE_LIMITED` / `DATABENTO_UPSTREAM_ERROR` envelope codes can distinguish them. Don't modify `fetch_bars` / `fetch_definition_instruments` signatures — add a new adapter or expose the `BentoHttpError` to the caller.

**Rate limits (OQ-2):** Databento does NOT publicly document per-endpoint rate limits for the Historical REST API. What IS documented:

- [Connection limits — Live API](https://databento.com/docs/api-reference-live/basics/connection-limits) (accessed 2026-04-23) — applies to Live, not Historical.
- SDK v0.33.0 (2024-04-16) added auto-retry on 429 for `Historical.batch.download` + `Historical.batch.download_async` ONLY. NOT for `timeseries.get_range`, NOT for `metadata.*`. (Source: [CHANGELOG](https://github.com/databento/databento-python/blob/main/CHANGELOG.md), accessed 2026-04-23.)

**Test implication:** unit-test the retry wrapper against a mock that raises `BentoClientError(http_status=429)` then `BentoClientError(http_status=429)` then succeeds — assert the retry counter fires twice + `outcome="rate_limited_recovered"`. Also assert `BentoClientError(http_status=401)` does NOT retry — immediate `unauthorized` outcome.

**Sources:**

1. [databento-python CHANGELOG](https://github.com/databento/databento-python/blob/main/CHANGELOG.md) — accessed 2026-04-23
2. [databento-python common/error.py](https://raw.githubusercontent.com/databento/databento-python/main/databento/common/error.py) — accessed 2026-04-23
3. [PyPI databento](https://pypi.org/project/databento/) — accessed 2026-04-23

---

### nautilus_trader Databento loader (1.222+)

**Versions:** ours=`>=1.222.0` (lockfile version is what runs; current main has 1.223 references in research/backlog).

**Signature confirmed** (verified on develop branch loaders.py):

```python
def from_dbn_file(
    self,
    path: PathLike[str] | str,
    instrument_id: InstrumentId | None = None,
    price_precision: int | None = None,
    as_legacy_cython: bool = True,
    include_trades: bool = False,
    use_exchange_as_venue: bool = False,          # ← default is False!
    bars_timestamp_on_close: bool = True,
    skip_on_error: bool = False,
) -> list[Data]: ...
```

Our code in `databento_client.py:169-173` passes `as_legacy_cython=False, use_exchange_as_venue=True`. **Both are non-default overrides.** Rationale is documented in `databento_client.py:162-167`:

> `use_exchange_as_venue=True` is a per-call kwarg of `DatabentoDataLoader.from_dbn_file` […]. Setting it ensures CME futures emit venue='CME' not 'GLBX' — keeps registry canonical alias in exchange-name form.

**Problem for equities (NEW RISK, not in the 6 OQs):** the Nautilus Databento adapter docs (https://nautilustrader.io/docs/latest/integrations/databento/) confirm:

- `XNAS.ITCH` equities → `Venue("XNAS")` (ISO 10383 MIC), NOT `Venue("NASDAQ")`.
- `ARCX.PILLAR` ETFs → `Venue("XARC")`, NOT `Venue("ARCA")`.
- `GLBX.MDP3` CME futures with `use_exchange_as_venue=True` → `Venue("CME")` (the parsed exchange field), NOT `Venue("GLBX")`.

So `use_exchange_as_venue=True` FIXES the futures case (as our comment claims) but **equities still come out as MIC, not exchange-name**. IB resolves AAPL as `AAPL.NASDAQ` (exchange name). The US-009 divergence counter will fire on **every single** equity `bootstrap → IB refresh` pair.

**Design impact — this changes the plan.** Three options:

| Option                                                          | Effort | Consequence                                                                                                                                                                                                                               |
| --------------------------------------------------------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| (a) Accept divergence as noise                                  | Low    | Violates PRD §2 metric "100% of IB/Databento venue mismatches logged + counted" — a noisy counter dilutes the signal for real mismatches (legitimate ETF venue migrations).                                                               |
| (b) **Normalize MIC→exchange-name at Databento write boundary** | Low    | Add a small dict `{"XNAS": "NASDAQ", "XARC": "ARCA", "BATS": "BATS", …}` in `_upsert_definition_and_alias` when `provider="databento"`. Alias string is rewritten before insert. Mirrors FX `.→/` normalization already at lines 735-736. |
| (c) Flip `use_exchange_as_venue=False` for equities             | Medium | Would give `GLBX` for futures, breaking the existing `ES.Z.N → ESM6.CME` canonical form. Requires per-asset-class loader config.                                                                                                          |

**Recommended: option (b)** — explicit, minimal, reversible. Plan task B5 (or wherever `_upsert_definition_and_alias` is extended for the bootstrap provider) adds the MIC↔name map. Unit-tested with AAPL, SPY, QQQ via live probes (see Test implication below). Implicitly, this means the **alias string the bootstrap writes is the same one IB would write** — divergence counter fires ONLY when the actual listing venue changes (e.g., ETF migrates ARCA→BATS), which is what the metric is for.

**Test implication:** parametrize a test over known equities + their expected (MIC, exchange-name) pairs. Assert the stored alias is exchange-name. Then assert an IB refresh for the same symbol is a `noop` under the outcome semantics (US-005), not an `alias_rotated`. WITHOUT the normalization, a Phase 5.4 E2E test exercising US-005 + US-009 together WILL fail.

**Equity decoding support:** `DatabentoDataLoader.from_dbn_file` IS documented to decode equity definitions (stock class `K`) into Nautilus `Equity` instances (source: https://nautilustrader.io/docs/latest/integrations/databento/). No known issues found with equity decoding in recent CHANGELOGs. But we have no in-tree test of equity `.definition.dbn.zst` decoding today — Phase 4 must add one before the B5/B6 implementation tests land.

**Sources:**

1. [NautilusTrader Databento adapter docs (latest)](https://nautilustrader.io/docs/latest/integrations/databento/) — accessed 2026-04-23
2. [NautilusTrader loaders.py (develop)](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/nautilus_trader/adapters/databento/loaders.py) — accessed 2026-04-23
3. [Databento Nasdaq symbology blog post](https://databento.com/blog/nasdaq-symbology) — accessed 2026-04-23 (documents the synthetic instrument_id change for XNAS.ITCH that shipped 2023-11-28)

---

### tenacity

**Versions:** ours=**not explicitly declared in `pyproject.toml`**; latest=9.1.4 (2026-02-07).

**IMPORTANT — the PRD/discussion states "tenacity already in pyproject.toml via transitive dependencies".** Grepping confirms tenacity is NOT in `backend/pyproject.toml`. It MAY be present transitively (nautilus_trader or one of its deps), but relying on that is fragile. **The plan MUST explicitly add `tenacity>=9.1.0` to `[project]` dependencies** — otherwise a fresh `uv sync` can drop it whenever an upstream dep removes its own tenacity use.

**Breaking changes:** tenacity 8.x → 9.x was a cleanup release (Python 3.9+ required; no API changes in the retry/wait/stop primitives we'd use). Stable.

**Recommended pattern for the bootstrap wrapper** (from official docs):

```python
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

async def _with_retry(coro_factory):
    async for attempt in AsyncRetrying(
        retry=retry_if_exception(_should_retry),
        wait=wait_exponential(multiplier=1, min=1, max=9),  # 1s, 3s, 9s
        stop=stop_after_attempt(3),
        reraise=True,
    ):
        with attempt:
            return await coro_factory()
```

The PRD US-008 specifies "3 attempts, exponential backoff (1s, 3s, 9s)" — `wait_exponential(multiplier=1, min=1, max=9)` matches exactly.

**Design impact:** use `AsyncRetrying` context manager, not the `@retry` decorator. The decorator form obscures the per-symbol retry count that feeds `msai_databento_api_calls_total{outcome=...}`. The context-manager form gives us the `attempt.retry_state.attempt_number` to emit telemetry on success vs retry-after-failure.

**Test implication:** use `tenacity.stop_after_attempt(...)` with a mocked async factory that raises twice then succeeds. Assert the counter increments once with `outcome="rate_limited_recovered"` (when N>1 attempts succeeded) and once with `outcome="success"` (when N=1).

**Sources:**

1. [tenacity PyPI](https://pypi.org/project/tenacity/) — accessed 2026-04-23
2. [tenacity GitHub README](https://github.com/jd/tenacity) — accessed 2026-04-23

---

### FastAPI 207 Multi-Status

**Versions:** ours=`>=0.133.0`. No breaking changes affecting 207.

**Pattern** (verified against FastAPI docs + the batch-API 207 reference):

```python
from fastapi.responses import JSONResponse

@router.post("/instruments/bootstrap")
async def bootstrap(body: BootstrapRequest) -> JSONResponse:
    results = await run_bootstrap(body)
    # HTTP 200 when every symbol succeeded; 207 otherwise.
    status = 200 if all(r.outcome != "failed" for r in results) else 207
    return JSONResponse(
        status_code=status,
        content=BootstrapResponse(results=results, summary=_summarize(results)).model_dump(mode="json"),
    )
```

FastAPI's default `response_model=...` + `status_code=...` pattern forces a single status code at decorator time — it can't toggle 200/207 at runtime. **The endpoint must return `JSONResponse` directly with the status code computed after processing.** This is the same pattern PR #41 uses for `/report-token` error shapes (`_error_response` helper at `api/backtests.py`).

**Design impact:** `BootstrapResponse` stays declared as a Pydantic model for OpenAPI/schema generation but the handler itself returns `JSONResponse`. The OpenAPI `responses` dict can document 200 AND 207 as separate schemas — FastAPI supports this via the `responses=` kwarg on `@router.post`.

**Test implication:** two separate FastAPI TestClient tests — one asserting `resp.status_code == 200` on all-success, one asserting `resp.status_code == 207` on a mocked-half-failure. Both must share the same response envelope shape (PRD §4 US-001).

**Sources:**

1. [FastAPI additional responses docs](https://fastapi.tiangolo.com/advanced/additional-responses/) — accessed 2026-04-23
2. [REST bulk API partial-success pattern](https://oneuptime.com/blog/post/2026-02-02-rest-bulk-api-partial-success/view) — accessed 2026-04-23
3. [RFC 4918 §11.1](https://datatracker.ietf.org/doc/html/rfc4918#section-11.1) — 207 Multi-Status definition

---

### PostgreSQL `pg_advisory_xact_lock` via SQLAlchemy 2.0 async

**Versions:** ours=SQLAlchemy 2.0.36+ / asyncpg 0.30.0+ / Postgres 16. None of these are changing.

**Pattern:**

```python
from sqlalchemy import func, select

lock_key = hash((provider, raw_symbol, asset_class)) & 0x7FFFFFFFFFFFFFFF  # pg wants signed 64-bit
await db.execute(select(func.pg_advisory_xact_lock(lock_key)))
# … then the existing close-previous-alias + INSERT alias_stmt …
```

The lock is released automatically at `COMMIT` / `ROLLBACK`. No explicit release needed. It's CORRECT to take it inside `_upsert_definition_and_alias` because that method's only caller is an outer `session.begin()` (the API handler uses the request-scoped session).

**OQ-4 RESOLUTION — the lock IS required, not just defensive:**

Re-reading `_upsert_definition_and_alias` at `service.py:694-811`, the race is:

1. Tx A: `INSERT ... ON CONFLICT DO UPDATE` on `instrument_definitions` → gets `instrument_uid`.
2. Tx B: same, gets the same `instrument_uid` (row-level lock on the existing row).
3. Tx A: `UPDATE instrument_aliases SET effective_to = today WHERE instrument_uid = X AND provider = P AND effective_to IS NULL AND alias_string != new`.
4. **Tx B reads the alias table before A commits** — the row A is about to close is STILL `effective_to IS NULL` from B's snapshot.
5. Tx B: same UPDATE (no-op if same alias) or closes a different alias row.
6. Tx A: `INSERT INTO instrument_aliases (effective_from = today, alias_string = new_A) ON CONFLICT DO NOTHING`.
7. Tx B: `INSERT INTO instrument_aliases (effective_from = today, alias_string = new_B)` — if `new_B != new_A`, this succeeds because the unique constraint is `(alias_string, provider, effective_from)`, not on the "one active alias" invariant.
8. **Result: two rows with `effective_to IS NULL` for the same `(instrument_uid, provider)`.**

The existing `uq_instrument_aliases_string_provider_from` does NOT protect against this. Neither does any CHECK. The caller code at `service.py:312` then picks arbitrarily: `next((a for a in idef.aliases if a.effective_to is None), None)` — whichever the SQLAlchemy selectinload happened to put first.

**Council Blocking Constraint #2 was right.** The lock MUST be added. It's not defensive — the race window is real and MSAI's only previous mitigation was "one operator, one terminal, one IB refresh at a time," which breaks the moment a CLI call and an API call overlap.

**Design impact:**

- Add `await db.execute(select(func.pg_advisory_xact_lock(lock_key)))` as the FIRST statement of `_upsert_definition_and_alias`, before the definition upsert. Use a stable 64-bit hash (`hashtext` in Postgres or Python's `hash()` masked to signed 64-bit).
- Add a test that simulates the race (two concurrent asyncio tasks calling `_upsert_definition_and_alias` for the same `(provider, raw_symbol, asset_class)` with different alias strings) and asserts exactly one row has `effective_to IS NULL`. The test uses `asyncio.gather` + two separate sessions against the testcontainer Postgres.

**Test implication:** this is a race test, which is always flaky under CI. Use a `pre_upsert_barrier` fixture + event loop barrier to make the race deterministic — see the pattern in `test_auto_heal_lock.py` for Redis-based races.

**Known caveat with asyncpg:** do NOT pack the advisory-lock call with the definition upsert in a single `execute()` — asyncpg rejects multi-statement SQL in a prepared statement (see https://github.com/langchain-ai/langchain-postgres/issues/86). Two separate `db.execute(...)` calls. Our current code already does this, so no change.

**Sources:**

1. [Postgres Advisory Locks with Asyncio (Medium)](https://medium.com/@n0mn0m/postgres-advisory-locks-with-asyncio-a6e466f04a34) — accessed 2026-04-23
2. [expobrain/sqlalchemy-asyncpg-advisory-lock-int64](https://github.com/expobrain/sqlalchemy-asyncpg-advisory-lock-int64) — accessed 2026-04-23 (canonical int64 lock-key pattern for Python)
3. [asyncpg multi-statement limitation](https://github.com/langchain-ai/langchain-postgres/issues/86) — accessed 2026-04-23

---

## OQ-by-OQ resolution map

### OQ-1: Databento plan entitlements for `XNAS.ITCH`, `XNYS.PILLAR`, `ARCX.PILLAR`, `GLBX.MDP3`

**Status: UNVERIFIED — live probe required.** This research brief cannot verify entitlements without running a curl against Pablo's API key, which requires a shell on the worktree and access to `$DATABENTO_API_KEY`. The research-first agent does not have shell + env access.

**Hand-off to the plan phase:** before Phase 4 TDD starts, run:

```bash
curl -sH "Authorization: Bearer $DATABENTO_API_KEY" \
  "https://hist.databento.com/v0/metadata.list_datasets" | jq .
```

(API key is in `.env` symlinked into the worktree per CLAUDE.md.) Expect JSON array; presence of `XNAS.ITCH`, `XNYS.PILLAR`, `ARCX.PILLAR`, `GLBX.MDP3` answers the question. If `metadata.list_datasets` returns ALL datasets (not entitlement-filtered), fall back to `POST /v0/metadata.get_cost` on a 1-second window for each dataset — returns a cost estimate on entitled datasets and a 403 on unentitled.

**Design impact if any one is MISSING:**

- `XNAS.ITCH` missing → fallback to `EQUS.MINI` (added in SDK 0.48.0) for equities, OR scope-down US-001 to ETFs+futures only.
- `ARCX.PILLAR` missing → SPY/QQQ won't bootstrap; DOA for the PRD's demo symbols → pivot to IB Gateway bootstrap (council minority report).
- `XNYS.PILLAR` missing → bare NYSE stocks (IBM, KO) fail; amend PRD to "NASDAQ + ARCA only in v1".
- `GLBX.MDP3` missing → ES.n.0 (US-007) fails; scope futures out of v1.

**This is the one OQ that Phase 2 research cannot fully answer without a credential. Plan phase MUST treat it as a pre-Phase-4 gate.**

---

### OQ-2: Real Databento rate limits

**Status: UNKNOWN (vendor-silent).** As above — Databento does NOT publish per-endpoint rate limits for the Historical REST API. The SDK itself retries only on `Historical.batch.download` (not on `timeseries.get_range` or `metadata.*`).

**Design impact:** the PRD's `max_concurrent=3` hard cap + tenacity 3-attempt (1s/3s/9s) retry is the correct conservative default. Observability via `msai_databento_api_calls_total{endpoint,outcome}` is the ONLY way to detect if Pablo actually hits the limit in production — and when he does, we'll know what the real number is from the counter.

**Test implication:** unit-test the retry policy against mocked SDK calls raising `BentoClientError(http_status=429)`, not against a live API.

---

### OQ-3: `BE-01` `FuturesContract.to_dict()` drift status

**Status: FIXED (high confidence).** Verified by reading `backend/src/msai/services/nautilus/security_master/parser.py:171-212` on the current worktree branch. The fix is:

```python
def nautilus_instrument_to_cache_json(instrument: Instrument) -> dict[str, Any]:
    # ... docstring explains pyo3 vs Cython signatures ...
    try:
        result: dict[str, Any] = instrument.to_dict()
        return result
    except TypeError:
        # Cython path — staticmethod-style signature requires the instance explicitly.
        fallback: dict[str, Any] = instrument.to_dict(instrument)
        return fallback
```

This is the exact drift Codex flagged. The dual-path handler covers BOTH pyo3-backed (Databento loader) and Cython-backed (IB adapter + `TestInstrumentProvider`) instrument classes.

**Design impact:** US-007 (futures E2E) can proceed as specified. The Phase 4 plan does NOT need to fix BE-01 — only verify it. CONTINUITY can close the BE-01 known-issues tracker item.

**Test implication:** Phase 5.4 E2E for US-007 MUST reproduce the original failing command against the live Docker stack:

```bash
docker compose -f docker-compose.dev.yml up -d
./scripts/restart-workers.sh
uv run msai instruments refresh --provider databento --symbols ES.n.0 --start 2024-01-01 --end 2024-01-02
```

If this hangs or errors, BE-01 is NOT fixed (or a second drift was introduced). Do NOT skip this probe — the integration-test suite CAN'T catch a drift in the pyo3-returns-from-Databento path because the tests use `TestInstrumentProvider` (Cython).

---

### OQ-4: Advisory-lock necessity

**Status: REQUIRED (not defensive).** See the per-library "PostgreSQL advisory lock" section above. The race is real, present today, and only unexercised because single-operator serializes calls naturally. Opening a new HTTP `bootstrap` endpoint surfaces it.

**Design impact:** `pg_advisory_xact_lock(hash((provider, raw_symbol, asset_class)))` at the top of `_upsert_definition_and_alias`. Add to the existing method — don't duplicate the helper for the bootstrap path.

**Test implication:** race test with `asyncio.gather` + two sessions + a barrier. See the "asyncpg caveat" note above.

---

### OQ-5: `fetch_definition_instruments` ambiguity behavior

**Status: ANSWERED (from Nautilus loader + Databento schema contract).**

With `stype_in="raw_symbol"` and `schema="definition"`, the Databento response is a stream of `InstrumentDefMsg` (DBN v2) or `InstrumentDefMsgV3` (DBN v3, SDK ≥0.55) records — ONE PER DISTINCT `instrument_id` per symbol mapping over the window. For:

- **Unambiguous equities (AAPL, SPY, QQQ):** exactly one record — one `instrument_id` in `XNAS.ITCH` for each.
- **Dual-class shares (BRK.B, BF.B):** multiple records — one per share class, each with its own `instrument_id`. `BRK.B` on XNAS.ITCH has a single `instrument_id` BUT if the raw_symbol matches across datasets (not our case — we pass `dataset` kwarg), multiple would come back.
- **Dual-listed ADRs:** same ticker across multiple datasets → N records if we query across datasets. We pin one dataset per bootstrap call, so N=1 per dataset.
- **Continuous futures (`ES.n.0`) on GLBX.MDP3 with `stype_in="continuous"`:** the resolver synthesizes ONE front-month record per day in the window; our existing `_resolve_databento_continuous` collapses to the most recent.

**Design impact — the PRD's `candidates[]` field list needs one change:**

| PRD field                 | Nautilus source                     | Comment                                                                                                                                                                                                            |
| ------------------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `raw_symbol`              | `instrument.raw_symbol.value`       | Always present                                                                                                                                                                                                     |
| `listing_venue`           | `instrument.id.venue.value`         | This is the MIC per our decoder — see normalization note in Nautilus section                                                                                                                                       |
| `dataset`                 | Passed in by caller                 | Always present                                                                                                                                                                                                     |
| `security_type`           | `instrument.__class__.__name__`     | e.g. `"Equity"`, `"FuturesContract"`                                                                                                                                                                               |
| `databento_instrument_id` | `instrument.id.instrument_id` (int) | **Verify** — may be on a different attribute; see test implication below                                                                                                                                           |
| `~~description~~`         | **No Nautilus field**               | **DROP from candidates** OR populate from a follow-up `reference.corporate_actions` call (cost + latency, not worth v1). Recommend rename to `asset_class: str` from our `asset_class_for_instrument_type` helper. |

**Test implication:** write a spike test in `tests/integration/test_databento_ambiguity_spike.py` that calls `fetch_definition_instruments("BRK.B", start, end, dataset="XNAS.ITCH", ...)` against the live API, counts the returned records, and inspects field shapes. Run this BEFORE Phase 4 B3/B4 implementation. If BRK.B actually returns one record on XNAS.ITCH (not multiple), the PRD can simplify — ambiguity emerges across datasets but not within one, and the `candidates[]` contract changes.

**Fallback position:** if the spike reveals `fetch_definition_instruments` always returns ≤1 per symbol per dataset, the PRD's US-004 ambiguity path still has a job — it fires when a user queries without `exact_ids` and the dataset wasn't fully specified (operator passed `asset_class_override="equity"` but the symbol matches across XNAS.ITCH AND DBEQ.BASIC, for instance).

---

### OQ-6: Where does 5m/10m/30m aggregation happen?

**Status: ANSWERED — aggregation happens in Nautilus engine, NOT in MSAI ingest or auto-heal.**

**Evidence:**

- Databento publishes OHLCV natively at `1s / 1m / 1h / 1d` intervals (source: https://databento.com/docs/schemas-and-data-formats/ohlcv). No 5m/10m/30m native.
- `backend/src/msai/services/backtests/auto_heal.py` (the orchestrator that runs when a backtest fails with MISSING_DATA) only enqueues an ingest job via `enqueue_ingest(...)` — doesn't resample bars.
- `backend/src/msai/services/data_sources/databento_client.py:37-100` (`fetch_bars`) returns the schema-native timeframe the caller asks for (default: `ohlcv-1m`). No downstream resampling.
- NautilusTrader's `BacktestEngine` has a built-in `BarAggregator` that composes higher-timeframe bars from a lower-timeframe feed on-the-fly during backtest execution (source: https://nautilustrader.io/docs/latest/concepts/backtesting/). The strategy subscribes to `5-MINUTE-LAST-EXTERNAL` (or similar) and the engine aggregates from the loaded 1m catalog automatically.

**Design impact:** PRD US-003's claim "auto-heal handles bars without code changes" HOLDS for 5m/10m/30m — the strategy subscribes to the aggregated timeframe, the engine reads 1m from the catalog, aggregates at run-time. No MSAI ingest/resample code to change. The acceptance demo in the PRD is correct — prove 1m E2E + one aggregated smoke (engine proves aggregation, no MSAI code to test).

**Test implication:** Phase 5.4 UC-BDR-003 (or whatever the aggregated-timeframe UC is numbered) should subscribe to `5-MINUTE-LAST-EXTERNAL` bars on AAPL and assert the on-bar handler fires ≈ 1/5 the frequency of a 1-minute run. Don't test the resampler itself — that's Nautilus territory.

**Sources:**

1. [Databento OHLCV schema](https://databento.com/docs/schemas-and-data-formats/ohlcv) — accessed 2026-04-23
2. [NautilusTrader Backtesting concepts](https://nautilustrader.io/docs/latest/concepts/backtesting/) — accessed 2026-04-23

---

## Not Researched (with justification)

- **`pandas`, `pyarrow`, `asyncpg`** — bootstrap doesn't touch data-frame / Parquet / direct SQL paths; the SDK returns `Instrument` objects and we use SQLAlchemy ORM everywhere. Covered by existing test coverage.
- **`prometheus_client`** — reusing the existing `Histogram` + Counter primitives from PR #41. Pattern locked in.
- **`structlog`** — unchanged; reuse the established `get_logger` pattern.
- **`httpx`** — Databento SDK has its own HTTP client (`requests`-based, not httpx). We don't touch httpx from this path.
- **`ib_async` / `nautilus_trader[ib]` IB adapter** — this feature explicitly does NOT touch IB. US-009 references an existing IB refresh path that stays unchanged.
- **`msal` / `PyJWT` auth** — reusing existing JWT + `X-API-Key` middleware.

## Open Risks

1. **OQ-1 unresolved.** Plan phase must run the entitlements probe before Phase 4 begins. If any of the 4 required datasets are unentitled, the PRD scope shrinks or pivots. Pre-Phase-4 gate; do not skip.
2. **Nautilus venue MIC vs exchange-name divergence (finding #3).** The "Recommended option (b)" normalization WILL show up in divergence telemetry on the first few IB refreshes if not implemented. Plan phase must commit to one option explicitly in the task list.
3. **`use_exchange_as_venue=True` kwarg behavior for equities is NOT visible in the Nautilus loader docstring.** Docstring says "actual exchanges or GLBX" — ambiguous for equities. Phase 4 MUST add an integration test that downloads a real `.definition.dbn.zst` for AAPL on XNAS.ITCH and asserts the decoded venue is the MIC value `XNAS` (not `NASDAQ`). If the actual behavior is different, finding #3 above is wrong and the plan changes.
4. **Tenacity not a declared dep.** Plan must add `tenacity>=9.1.0` to `[project].dependencies` in `pyproject.toml` as explicit Task B0 (or before B1).
5. **`databento_instrument_id` attribute access unverified.** The Nautilus `Instrument.id` is an `InstrumentId` struct. Whether `instrument.id.instrument_id` is the numeric Databento instrument_id or a Nautilus-synthetic ID needs a 10-minute spike before committing the `candidates[].databento_instrument_id` field in the response envelope.
6. **Equity definition decoding in `DatabentoDataLoader` has no in-tree test today.** The Nautilus loader IS documented to support equities, but MSAI has no fixture exercising it. Phase 4 MUST add a fixture + test — real `.definition.dbn.zst` committed under `backend/tests/fixtures/databento/` or generated on the fly with a tiny stub. Without this, the FIRST time Pablo runs `bootstrap AAPL` live is the first integration test.
7. **Race test flakiness (OQ-4).** Postgres advisory-lock race tests are structurally flaky. Use a deterministic barrier pattern — don't rely on `asyncio.sleep(0.1)` to provoke the race. Follow the `test_auto_heal_lock.py` style.
8. **Dataset-pinning decision.** PRD mentions `asset_class_override` but does NOT specify which dataset maps to which asset class. Plan must enumerate:
   - `equity` → `XNAS.ITCH` (NASDAQ-listed) OR `XNYS.PILLAR` (NYSE-listed)? Or both sequentially? The operator probably doesn't know which listing venue applies to a random ticker.
   - `etf` → `ARCX.PILLAR`? Or include XNAS.ITCH (many ETFs list on NASDAQ)?
   - `future` → `GLBX.MDP3` only in v1.
     The dispatching heuristic (what happens when operator passes `AAPL` with no `asset_class_override`) needs a decision — default to `XNAS.ITCH` first, fall through to `XNYS.PILLAR` on miss? Or query both and pick the one that returned a record? This is a UX + correctness question the PRD leaves unspecified.
9. **`exact_ids` type safety.** PRD says `exact_ids: {[symbol]: int}` maps to Databento `instrument_id`. `instrument_id` IS an integer (UInt32 in DBN), but Pydantic must validate it as `int` ≥ 0 — not `str`. Plan must pin the type.
10. **Payload size observability on bootstrap.** PR #41 introduced `msai_backtest_results_payload_bytes` histogram for response size. This endpoint's response grows O(N symbols) — at batch size 50 the JSON could be 10-20 KB. Plan should add `msai_registry_bootstrap_response_bytes` histogram for parity with the PR #41 pattern.

---

## Findings → OQ traceback

| Finding                                          | Answers OQ | Design-changing?              | Task anchor in plan                                              |
| ------------------------------------------------ | ---------- | ----------------------------- | ---------------------------------------------------------------- |
| BE-01 fixed (parser.py:204-212)                  | OQ-3       | Yes — US-007 can proceed      | B9 test must include live `instruments refresh` reproduction     |
| Databento rate limits are undocumented           | OQ-2       | No — PRD default is correct   | B7 retry policy (3 attempts, 1s/3s/9s)                           |
| Venue divergence (MIC vs exchange-name)          | —          | **YES** — new decision needed | B5 — add MIC→exchange-name map in `_upsert_definition_and_alias` |
| Advisory lock is required (not defensive)        | OQ-4       | No — PRD already mandates     | B6 `_upsert_definition_and_alias` top-of-transaction             |
| `fetch_definition_instruments` returns N records | OQ-5       | Minor — rename `description`  | B3 response schema: rename `candidates[].description`            |
| 5m/10m/30m aggregated by Nautilus engine         | OQ-6       | No — US-003 holds             | UC-BDR-003 covers aggregated-timeframe smoke                     |
| Plan entitlements unverified                     | OQ-1       | BLOCKS Phase 4                | Pre-Phase-4 gate: live `metadata.list_datasets` probe            |
| Tenacity not a declared dep                      | —          | Yes — plan adds B0 dep bump   | B0 `pyproject.toml` addition                                     |
| No in-tree equity-definition test fixture        | —          | Yes — plan adds fixture       | B2 (or wherever fixture setup lands)                             |
| Dataset-pinning UX undefined                     | —          | Yes — needs decision          | B3 or B4 — define dispatch heuristic for `asset_class_override`  |

---

## Sources — consolidated list

1. [databento-python CHANGELOG (GitHub)](https://github.com/databento/databento-python/blob/main/CHANGELOG.md) — accessed 2026-04-23
2. [databento-python common/error.py (raw, main branch)](https://raw.githubusercontent.com/databento/databento-python/main/databento/common/error.py) — accessed 2026-04-23
3. [databento on PyPI](https://pypi.org/project/databento/) — accessed 2026-04-23
4. [NautilusTrader Databento adapter docs (latest)](https://nautilustrader.io/docs/latest/integrations/databento/) — accessed 2026-04-23
5. [NautilusTrader adapters/databento/loaders.py (develop)](https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/nautilus_trader/adapters/databento/loaders.py) — accessed 2026-04-23
6. [NautilusTrader Backtesting concepts (BarAggregator)](https://nautilustrader.io/docs/latest/concepts/backtesting/) — accessed 2026-04-23
7. [Databento OHLCV schema](https://databento.com/docs/schemas-and-data-formats/ohlcv) — accessed 2026-04-23
8. [Databento Nasdaq symbology blog](https://databento.com/blog/nasdaq-symbology) — accessed 2026-04-23
9. [tenacity on PyPI](https://pypi.org/project/tenacity/) — accessed 2026-04-23
10. [tenacity on GitHub](https://github.com/jd/tenacity) — accessed 2026-04-23
11. [tenacity documentation (readthedocs)](https://tenacity.readthedocs.io/en/latest/) — accessed 2026-04-23
12. [FastAPI additional responses docs](https://fastapi.tiangolo.com/advanced/additional-responses/) — accessed 2026-04-23
13. [REST bulk API partial-success pattern (OneUptime blog)](https://oneuptime.com/blog/post/2026-02-02-rest-bulk-api-partial-success/view) — accessed 2026-04-23
14. [Postgres Advisory Locks with Asyncio (Medium)](https://medium.com/@n0mn0m/postgres-advisory-locks-with-asyncio-a6e466f04a34) — accessed 2026-04-23
15. [expobrain/sqlalchemy-asyncpg-advisory-lock-int64](https://github.com/expobrain/sqlalchemy-asyncpg-advisory-lock-int64) — accessed 2026-04-23
16. [asyncpg multi-statement limitation discussion](https://github.com/langchain-ai/langchain-postgres/issues/86) — accessed 2026-04-23
17. [Databento API reference navigation](https://databento.com/docs/api-reference-historical) — accessed 2026-04-23 (nav-only; specific method pages returned stub content)
18. In-tree evidence read:
    - `backend/src/msai/services/nautilus/security_master/parser.py:171-212` (OQ-3 fix)
    - `backend/src/msai/services/nautilus/security_master/service.py:694-811` (\_upsert_definition_and_alias race)
    - `backend/src/msai/services/data_sources/databento_client.py:102-174` (fetch_definition_instruments current shape)
    - `backend/src/msai/services/backtests/auto_heal.py:1-200` (OQ-6 — no resample code)
    - `backend/src/msai/models/instrument_alias.py` (CHECK + uniqueness constraints)
    - `backend/src/msai/models/instrument_definition.py` (CHECK + uniqueness constraints)
    - `backend/pyproject.toml` (tenacity NOT declared; `databento>=0.43.0` pinned)
