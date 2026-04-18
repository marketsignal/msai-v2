# PRD Discussion: DB-Backed Strategy Registry + Continuous Futures

**Status:** Complete
**Started:** 2026-04-17
**Closed:** 2026-04-17 (council verdict accepted by user)
**Participants:** User (Pablo), Claude, Codex (research + 2 advisor personas), 5-advisor Council

## Original User Stories

Pulled from `CONTINUITY.md` "Next" list and `docs/plans/2026-04-13-codex-claude-subsystem-audit.md §2`:

> **Phase 2 #5** — DB-backed strategy registry + continuous futures (`InstrumentDefinition` + `.Z.` regex resolution).

> **Audit §2 — Strategy Registry + IB Canonicalization — PORT (P1, M)**
> **Claude:** Sync `TestInstrumentProvider.equity()`, NASDAQ default, no DB persistence, no continuous futures.
> **Codex:** `NautilusInstrumentService` (605 LOC) with dual-provider routing (IB + Databento), `ResolvedInstrumentDefinition` dataclass, DB-backed `InstrumentDefinition` model, continuous futures regex `^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$` (e.g., `ES.Z.5`), Pydantic schema extraction for UI form generation, cached definitions with window-based refresh.
>
> **Port (partial):**
>
> - `ResolvedInstrumentDefinition` dataclass + `InstrumentDefinition` DB model
> - Continuous futures helpers (`_raw_symbol_from_request`, `_resolved_databento_definition`)
> - Multi-provider canonicalization (`canonicalize_live_instruments`, `canonicalize_backtest_instruments`)
> - Config schema extraction (Pydantic `model_json_schema()` + defaults)
>
> **Do NOT port wholesale:** integrate into existing `instruments.py` via async wrapper. Keep Claude's synchronous resolver for simple cases.

## Current State in Claude (from research)

- **`claude-version/backend/src/msai/services/nautilus/instruments.py`** — sync `resolve_instrument()` + `canonical_instrument_id()` wrapping `TestInstrumentProvider.equity(symbol, venue)`. Handles bare tickers (`AAPL`) and dotted IDs (`AAPL.NASDAQ`, `ESM5.XCME`).
- **`claude-version/backend/src/msai/services/nautilus/live_instrument_bootstrap.py`** — a SECOND `canonical_instrument_id()` that does ES front-month roll (`ES.CME` → `ESM6.CME`) using spawn-scoped `today`. IB-convention futures only.
- **No `InstrumentDefinition` table** — every resolve is recomputed per call.
- **No Databento definition path** — `DatabentoClient` exists but no `get_definition()` call path for a `.dbn.zst` definition file.
- **Strategy model is already DB-backed** (`models/strategy.py` is written). What's missing from the "strategy-registry" side of the audit is only the Pydantic → JSON-schema extraction for UI form generation — not the registry itself.

So the "DB-backed strategy **registry**" wording in the CONTINUITY line is slightly misleading — the DB-backed thing to add is really an **instrument** registry (`InstrumentDefinition`). The strategy side is a thin sidecar (schema extraction).

## Research Streams (2026-04-17)

Two independent research streams were run against the Nautilus venv + both codebases. Full outputs preserved separately; key findings extracted here.

### Stream A — Explore agent (Claude-side)

- Confirmed Nautilus `InstrumentProvider` uses in-memory dict (`common/providers.py:48`), no persistence hook on the base class.
- Confirmed IB adapter has native `CONTFUT` → `parse_futures_contract` path (`adapters/interactive_brokers/parsing/instruments.py:319`).
- Confirmed Databento Python adapter has NO continuous-symbol normalization (zero grep hits for `continuous|\.c\.0|\.Z\.`).
- **INCORRECT claim — flagged and overturned below:** Said "ParquetDataCatalog does not persist instruments, only bars." This is wrong (see Stream B + verification).

### Stream B — Codex CLI (independent)

- **Corrected Stream A:** `ParquetDataCatalog.write_data()` at `persistence/catalog/parquet.py:294-295` treats `Instrument` as a first-class identifier case — catalogs DO persist Instruments alongside bars. Verified directly.
- **Corrected Stream A on cache DB:** `Cache.add_instrument()` writes to the configured `DatabaseConfig` backing store; `cache_all()` + `load_instrument()` reload on restart (`cache/cache.pyx:1886-1915,1452-1474`; `cache/database.pyx:218-253,340-377,934-950`). So Nautilus already owns Instrument durability when `CacheConfig(database=...)` is configured.
- Venue scheme is NOT standardized across IB + Databento: IB simplified uses `ES.CME` by default or `ES.XCME` with MIC conversion; Databento provider defaults `use_exchange_as_venue=True` (e.g. `ES.<exchange>`) while DBN file loader defaults `use_exchange_as_venue=False` (→ `GLBX`) (`adapters/databento/common.py:25-31`; `adapters/databento/loaders.py:123-157,186-193`; `adapters/interactive_brokers/config.py:128-132,184-195`). MSAI must pick one scheme per asset class.
- Canonical UI-streaming surface is MessageBus topics: `events.order.{strategy_id}`, `events.position.{strategy_id}`, `events.fills.{instrument_id}`, `events.account.{account_id}` (`execution/engine.pyx:851-889,1304-1323,1620-1623`; `portfolio/portfolio.pyx:201-218,472-489,730-754`). Controller hooks are extension actors, not the event surface.
- Continuous ES under IB is `ES.<venue>` (secType=CONTFUT), NOT a `.Z.N` suffix — the suffix is a Databento-side app convention.

### Convergence + divergence summary

| Topic                                                  | Stream A           | Stream B                 | Resolution                                               |
| ------------------------------------------------------ | ------------------ | ------------------------ | -------------------------------------------------------- |
| InstrumentProvider is source-local dict                | ✅                 | ✅                       | Agreed                                                   |
| IB has native CONTFUT                                  | ✅                 | ✅                       | Agreed — `FUT` + `CONTFUT` share `FuturesContract` class |
| Databento has NO continuous-symbol normalization       | ✅                 | ✅                       | Agreed — real gap MSAI must fill                         |
| Nautilus Cache DB persists Instruments across restarts | ❌ "no"            | ✅ "yes when configured" | **B wins** (verified)                                    |
| ParquetDataCatalog persists Instruments                | ❌ "no, only bars" | ✅ "yes, first-class"    | **B wins** (verified at `parquet.py:294-295`)            |
| `find()` is sync warm cache                            | ✅                 | ✅                       | Agreed                                                   |
| Bare `AAPL` is app-level, not Nautilus-core            | ✅                 | ✅                       | Agreed                                                   |

### Key architectural implication

The codex-version's approach (msgpack-serialize the `Instrument` payload into JSONB + round-trip via `instrument_from_payload`) **partially reinvents** Nautilus's own cache DB + catalog persistence. If MSAI wires `CacheConfig(database=...)` (gotcha #7 in our own `nautilus.md`) and writes Instruments into the Parquet catalog, Nautilus handles durability natively.

The **real gaps** Nautilus leaves — and what MSAI's Postgres table should own — are:

1. Raw-symbol → canonical-InstrumentId alias (e.g. `"AAPL"` → `"AAPL.XNAS"`)
2. Provider provenance (which source resolved this + when)
3. Continuous-futures roll policy (e.g. third-Friday quarterly)
4. Lifecycle state (staged, active, retired)
5. Databento `.Z.N` / `.c.0` synthesis (Python-side gap)

## Discussion Log

### Round 1 — 8 questions asked by Claude

Claude surfaced 8 questions on scope, persistence, continuous-futures symbology, sync/async boundary, wiring points, Databento fetch, fallback, and migration. User declined to answer them directly ("I really cannot answer these questions unless we look at Nautilus and how it does it") and requested independent research streams.

### Round 2 — Research

Two independent streams were run in parallel:

- **Stream A — Explore agent** (Claude, codebase + Nautilus venv)
- **Stream B — Codex CLI** (independent research from first principles)

Key convergence + divergence summarized in the "Research Streams" section above. Stream B overturned two of Stream A's claims (both verified against source):

- Nautilus Cache DB DOES persist Instruments across restarts when `CacheConfig(database=...)` is configured
- `ParquetDataCatalog` DOES persist Instruments as first-class (`parquet.py:294-299`)

**Architectural implication surfaced:** codex-version's 605-LOC `NautilusInstrumentService` partially reinvents Nautilus's own cache DB + catalog persistence. MSAI's own table should be a thin control plane, not a copy of `Instrument` payloads.

### Round 3 — User answers on 2 remaining questions

User accepted Claude's recommendations for 6 of the 8 questions and answered the remaining 2:

- **Q1 (scope):** keep Pydantic config-schema extraction IN this PR — no defer.
- **Q on venue scheme:** ask council.

### Round 4 — 5-advisor Council

Standalone council invoked. Personas tailored per user's request: (1) Cross-Vendor Data Engineer (Codex), (2) Maintainer / Existing-Code Steward (Claude), (3) Contrarian / Simplifier (Codex), (4) Nautilus-First Architect (Claude), (5) UX / Operator (Claude). Chairman = Codex xhigh.

**Verdict tally:**

- Maintainer: APPROVE Option B (exchange-name) — 398 occurrences / 69 files makes migration expensive
- Nautilus-First Architect: APPROVE Option B + factual correction (Databento emits `GLBX`, not `XCME`)
- UX/Operator: APPROVE Option B (conditional on normalizing current split-brain)
- Contrarian/Simplifier: APPROVE **third option** (stable logical PK + aliases)
- Cross-Vendor Data Engineer: CONDITIONAL on third option (stable master PK + alias table)

**Chairman synthesis:** Hybrid — third option at schema layer, Option B at runtime alias layer. Durable PK = `instrument_uid` (UUID); runtime canonical alias = exchange-name (`AAPL.NASDAQ`, `ES.CME`, `EURUSD.IDEALPRO`); listing_venue and routing_venue as separate columns to survive future IB options work.

**Preserved minority (Codex advisors):** registry should NOT key on venue-string even if exchange-name wins the runtime debate — both Codex advisors independently made this argument.

Chairman verdict file: `/tmp/msai-research/council/chairman-verdict.md`
Raw advisor outputs: `/tmp/msai-research/council/advisor-*.md`

### Round 5 — User accepts hybrid + Claude resolves Missing Evidence

User accepted the council's hybrid recommendation, corrected one fact (**no Polygon in the MSAI stack** — Databento is backtest-only + optional future real-time; IB handles live real-time + execution), and tasked Claude with researching the 4 Missing Evidence items.

**Missing Evidence resolutions:**

1. **IB options listing vs routing venue (Polygon-question reframed).** IB adapter emits `SMART` (routing) as `InstrumentId.venue` for options; drops listing exchange from the ID. `OptionContract` class has one venue field. BUT `contract.primaryExchange` (listing) IS preserved in `contract_details.info` on the provider side. **Council's listing/routing split stands** — IB options alone force it, no Polygon needed.

2. **Current split-brain extent.** Much smaller than feared: runtime code uses `.CME` consistently (`live_instrument_bootstrap.py` constructs `ESM6.CME`). `.XCME` survives in ~7 source-file docstrings/examples + `security_master/specs.py:21-22` canonical-format doc + 26 test fixtures + defensive input normalization at `live_instrument_bootstrap.py:147` ("legacy MIC accepted for input only"). MSAI's own Parquet storage is partitioned by **symbol** not venue — venue string does not appear in on-disk path. Split-brain normalization = docstring/example/test-fixture cleanup, NOT a disk rewrite.

3. **Parquet partition rename feasibility.** Nautilus `ParquetDataCatalog` has no built-in rename; venue change = read-old/write-new/delete-old offline rewrite. Mitigated by #2 — MSAI's raw data is symbol-partitioned, not venue-partitioned. Nautilus catalog (if used for backtest) is regenerable from raw data + instrument lookup, so wipe + regenerate is sufficient.

4. **`CacheConfig(database=postgres|redis)` under both schemes.** Cache key = `f"{_INSTRUMENTS}:{instrument_id.to_str()}"` at `cache/database.pyx:583`. Venue string IS part of the key. Changing `ES.XCME` → `ES.CME` invalidates cache entries; on first boot after normalization, cache misses → Nautilus re-resolves from IB/Databento → re-writes under new keys. No data-loss risk; one-time warm-up cost. Redis and Postgres backends use the same key format.

## Refined Understanding

### Personas

- **Pablo (primary):** sole operator, portfolio-manager, strategy author, on-call engineer. Reads UI + logs + DB directly. All UI strings must survive 3am drills.

### User Stories (Refined)

- **US-001:** As a strategy author, I declare `instruments=["ES", "AAPL"]` in my strategy config and the registry resolves each to the correct `InstrumentId` per context (live IB → `AAPL.NASDAQ` / `ESM6.CME`; backtest Databento → `AAPL.NASDAQ` / `ES.Z.5.GLBX`-normalized).
- **US-002:** As the portfolio-manager, I deploy a portfolio to a second IB account and the registry lookup is warm-cache sync — no IB round-trip during spawn.
- **US-003:** As the operator, I read a log line `Fill AAPL.NASDAQ BUY 1 @ 261.33` and can immediately verify against IB TWS without mental translation.
- **US-004:** As a risk engineer, I query `SELECT * FROM instrument_definition WHERE listing_venue = 'CBOE'` and get all options listed on CBOE regardless of whether IB routes them via SMART.
- **US-005:** As the ingestion CLI, I pre-warm the registry with `msai instruments refresh --symbols ES,NQ,AAPL,SPY` without requiring a live IB connection for static symbols already known.

### Non-Goals

- Wholesale migration to MIC-codes (`XNAS`, `XCME`) now — deferred until a future vendor forces it. Alias table makes this a column migration, not a schema rewrite.
- Polygon integration (not planned — correction from user).
- Multi-tenant isolation — single-user platform.
- UI form generation for strategy configs — schema extraction ships in this PR, but UI consumption is future work.

### Key Decisions

- **Primary key:** `instrument_uid` (UUID). Never venue-string.
- **Runtime canonical:** exchange-name aliases (`AAPL.NASDAQ`, `ES.CME`, `EURUSD.IDEALPRO`, `<localSymbol>.SMART` for options).
- **Listing vs routing venue:** separate columns from day one.
- **Split-brain normalization:** bundled into this PR.
- **Nautilus primitive delegation:** wire `CacheConfig(database=redis)` so Nautilus owns `Instrument` payload durability. MSAI's table is control-plane metadata (alias, provider provenance, roll policy, lifecycle), NOT a copy of Nautilus payloads.
- **Continuous futures:** Databento `.Z.N` regex helper ported from codex-version; IB `ES.CME`→`ESM6.CME` roll stays as-is (already verified working in PR #23).
- **Pydantic config-schema extraction:** small sidecar on `StrategyRegistry` (`model_json_schema()` + defaults). API exposes it; UI consumption is future work.
- **Fallback:** live-start fails loud on provider miss + no cached row; backtest fails loud if no Databento definition row.
- **Migration strategy:** lazy (empty table at ship, populate on next `/live/start` or ingest); one-shot `msai instruments refresh` CLI for explicit pre-warming. Seed rows for known continuous-futures symbols included in migration.

### Open Questions (Remaining — to resolve during implementation)

- [ ] Actual count of mixed-format rows in `live_deployment_strategy` (need docker stack up to query).
- [ ] Whether codex-version's msgpack `instrument_data` JSONB column is needed at all once `CacheConfig(database=redis)` is wired — strong hypothesis: NO (Nautilus handles payload durability), but verify with an integration test.
- [ ] Whether `Databento loader use_exchange_as_venue=True` actually emits `CME`/`NYMEX`/`CBOT` in MSAI's ingestion path end-to-end (test in a spike before locking).

**Next step:** Run `/prd:create db-backed-strategy-registry` to lock these decisions into a formal PRD.
