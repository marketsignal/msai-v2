# Research: Nautilus V2 research integration

**Date:** 2026-10-06
**Feature:** One real strategy discovery → backtest → research/results/report journey across API, CLI and browser, preserving the accepted V1 release.
**Researcher:** research-first agent
**Inputs:** Approved [PRD v1.0](../prds/nautilus-v2-research.md); immutable MSAI base `9d51a633ebeeff9b85ee06b6085df056dbe68458`; `backend/pyproject.toml` and `backend/uv.lock`.

## Evidence boundary and method

This refresh reads official release/package metadata, immutable upstream source and current MSAI call sites. Context7 was queried first for Nautilus and Msgspec; its Nautilus `develop` results are navigation hints, not version evidence. Exact RC6 source takes precedence over rolling documentation. Official web search supplied documentation/changelog checks. No package was installed, no application started, no dependency lock changed, and no trial matrix rerun. Source signatures below are inspected contracts; the [October 5 executable trial](2026-10-05-nautilus-runtime-trial.md) remains separate historical behavior evidence. Research hypotheses require the workflow's independent reproduction before they become acceptance claims.

The fresh PyPI release history and GitHub release list still show **1.231.0 as latest stable and 2.0.0rc6 as newest V2 candidate**. The exact development candidate remains `nautilus-trader==2.0.0rc6`, upstream commit `7b766f8825b2539c5b2ac1375e9d97b41c509edb`. This identifies a reproducible research target; it does not authorize installation into the running release or production/live use.

## Libraries touched

| Library | Our locked version | Latest stable available | Relevant delta / implication | Official version source, accessed 2026-10-06 |
| --- | --- | --- | --- | --- |
| NautilusTrader | 1.223.0; manifest `nautilus_trader[ib]>=1.222.0` | 1.231.0; V2 prerelease 2.0.0rc6 | V2 changes Python imports/configuration, catalog/wrangler, component registration, reports and live lifecycle. | [PyPI metadata](https://pypi.org/pypi/nautilus-trader/json), [GitHub releases](https://github.com/nautechsystems/nautilus_trader/releases) |
| Msgspec | 0.20.0, transitive and undeclared by MSAI | 0.22.0 | V2 no longer supplies it; MSAI still imports it directly. V2 configs are not V1 Msgspec structs. | [PyPI](https://pypi.org/pypi/msgspec/json) |
| PyArrow | 23.0.1; manifest `>=22.0.0` | 25.0.1 | Native V2 wrangler expects Arrow IPC/native decimal schema, replacing V1 DataFrame processing. No upgrade established as necessary. | [PyPI](https://pypi.org/pypi/pyarrow/json) |
| Pandas | 2.3.3; manifest `>=2.2.0` | 3.0.6 | Native reports still expose DataFrames; keep date/return units explicit. No Pandas upgrade is needed for RC6. | [PyPI](https://pypi.org/pypi/pandas/json) |
| QuantStats | 0.0.81; manifest `>=0.0.81` | 0.0.86 | Existing HTML report consumes a daily return series; rendering does not select or certify its economic basis. No upgrade required. | [PyPI](https://pypi.org/pypi/quantstats/json) |

Latest availability is informational, not a recommendation to upgrade every package. The prior full RC6 resolution retained PyArrow 23.0.1, Pandas 2.3.3 and QuantStats 0.0.81. See the [dependency addendum](2026-10-05-nautilus-upstream-trial-addendum.md#follow-up-complete-application-lock-resolution).

## NautilusTrader: release and platform facts

The GitHub release is `prerelease=true`, published **2026-10-05 03:03:18 UTC**; its annotated tag object `d76a9032a7f747825e7fd65664bb47844bce32f9` resolves to the commit above. PyPI exact RC6 metadata declares Python `>=3.12,<3.15`, no mandatory runtime dependencies and only the `visualization` extra. The old `ib` extra is absent. Exact CPython 3.12 Linux x86_64 artifact:

```text
nautilus_trader-2.0.0rc6-cp312-cp312-manylinux_2_34_x86_64.whl
sha256 9b4002a7bf5e6399c51073039b740ccf3ca7a1e2584ff72c7d479f03eaa9658d
```

Pinned installation guidance supports Python 3.12–3.14 and Ubuntu 22.04+ with glibc 2.35+. That documented tested-platform floor is distinct from the artifact's manylinux 2.34 tag. Upstream discourages RCs for production/live real capital. Unpinned `--pre` can select a different release/development artifact; retain exact wheel/lock identity. The October 5 complete Python 3.12.15 Linux/amd64 build and native equity fixture passed; that is historical packaging proof, not current application startup.

Sources, accessed 2026-10-06: [exact RC6 metadata](https://pypi.org/pypi/nautilus-trader/2.0.0rc6/json), [release](https://github.com/nautechsystems/nautilus_trader/releases/tag/v2.0.0rc6), [annotated tag](https://api.github.com/repos/nautechsystems/nautilus_trader/git/tags/d76a9032a7f747825e7fd65664bb47844bce32f9), [pinned installation guide](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/docs/getting_started/installation.md).

**Design impact:** Pin RC6 deliberately; preserve other resolved dependencies where compatible, make direct application requirements explicit, and verify the intended Linux environment. Keep the baseline's data/state separate.

**Test implication:** Resolve the complete backend, inspect the exact installed engine/Python/platform, and start the API plus every affected worker/service in the bounded candidate environment. A wheel import/build cannot replace startup or user-journey acceptance.

## Strategy discovery, configuration and callbacks

RC6 exports `Strategy` and `StrategyConfig` from `nautilus_trader.trading`; `nautilus_trader.config` reexports `StrategyConfig` and `ImportableStrategyConfig`. Native model IDs, Bar/BarType/Price/Quantity/Currency/Equity are exported by `nautilus_trader.model`. The old `trading.config`, `trading.strategy`, `model.data`, `model.identifiers` paths are not a supported V2 migration contract.

The native `StrategyConfig` is subclassable with an explicit constructor. A user subclass declares keyword arguments and assigns its own attributes; it is not the V1 `class Config(StrategyConfig, frozen=True)` annotation-only Msgspec model. The PyO3 base constructor accepts native base fields plus `**_kwargs` and validates its own fields (including order-ID tags and market-exit settings). Its permissive extra-key absorption does **not** establish user-field validation or typo rejection. The official pattern calls `super().__init__()` in the subclass; the base object is initially constructed through the inherited native constructor. The inspected stubs/source do not provide V1 `.parse()`, JSON schema or Msgspec field introspection.

`ImportableStrategyConfig(strategy_path: str, config_path: str, config: dict)` remains native. `BacktestEngineConfig` no longer accepts a `strategies` list; high-level setup builds the node and registers the constructed/importable strategy using its run-config ID. Preserve the actual user strategy, not a substitute native example.

MSAI's [registry](../../backend/src/msai/services/strategy_registry.py) currently imports V1 Strategy/StrategyConfig and requires `.parse` when finding configs; [schema hooks](../../backend/src/msai/services/nautilus/schema_hooks.py) introspect `msgspec.json.schema` and own `__annotations__`; [backtest API](../../backend/src/msai/api/backtests.py) uses `config_cls.parse`. The existing [EMA config](../../strategies/example/config.py) is annotation-only/frozen and its [strategy](../../strategies/example/ema_cross.py) depends on V1 imports plus the MSAI risk mixin. All are concrete integration boundaries.

RC6 changes EMA warm-up to mean seeding. It also changes some event names (notably `on_quote_tick` → `on_quote`); the prior failure wrapper did not observe its intended failure under V2. `cancel_all_orders(..., strategy_only=True)` protects sibling strategies by default; broad cancellation remains execution-client scoped. Do not infer strategy parity from similar P&L or successful import.

Sources, accessed 2026-10-06: [config exports](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/python/nautilus_trader/config/__init__.py), [StrategyConfig binding and validation](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/crates/trading/src/python/strategy.rs#L104), [trading stubs](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/python/nautilus_trader/trading/__init__.pyi#L1000), [official strategy patterns](https://nautilustrader.io/docs/latest/concepts/strategies/), [release changes](https://github.com/nautechsystems/nautilus_trader/releases/tag/v2.0.0rc6).

**Design impact:** The smallest user-schema/validation adapter needs an explicit supported field contract, preserving defaults, required fields, typed IDs/Decimal and useful errors before native construction. Native base-field validation cannot replace that contract. No general dual-engine/config framework is justified.

**Test implication:** Discover the actual EMA module, render its schema/defaults, validate and execute the same typed configuration. Reject malformed IDs, missing required fields, invalid periods/size and unsupported fields before queueing; preserve governance and helper/config hashing behavior. Compare callback counts, EMA values and initialization, signal/first-order/fill timestamps, and stop/cancel behavior with the preserved baseline.

## Catalog, instruments and Arrow wrangler

RC6 exposes `ParquetDataCatalog` and `BarDataWrangler` directly under `nautilus_trader.persistence`. Supported inspected signatures include:

```python
BarDataWrangler(bar_type: str, price_precision: int, size_precision: int)
wrangler.process_record_batch_bytes(data: bytes)  # Arrow IPC stream
ParquetDataCatalog(base_path: str, storage_options=None, batch_size=None,
                   compression=None, max_row_group_size=None)
catalog.write_instruments(instruments)
catalog.write_bars(data, start=None, end=None, skip_disjoint_check=False)
catalog.query_bars(identifiers=None, start=None, end=None, where_clause=None)
```

This is **not** `BarDataWrangler(bar_type=BarType, instrument=...)` plus `.process(DataFrame)`, nor generic `catalog.write_data`. Native Arrow bars use ordered non-null OHLCV Decimal128(38,16) columns, followed by `ts_event` and `ts_init`; canonical storage timestamps are nanosecond UTC timestamps, while decoding also accepts its UInt64 nanosecond representation. The wrangler builds the bar-type/precision metadata itself and decodes native record batches through an IPC stream reader. Float OHLCV columns or a single `timestamp` column are not that native schema. Native `Bar(bar_type, open, high, low, close, volume, ts_event, ts_init)` construction is also supported and was exercised in the prior trial; source inspection alone does not establish the new MSAI Arrow conversion.

`Equity`'s inspected native constructor takes required `instrument_id`, `raw_symbol`, `currency`, `price_precision`, `price_increment`, `ts_event`, `ts_init`; optional `isin`, `lot_size`, quantity/price bounds, `margin_init`, `margin_maint`, `tick_scheme`, `info`. Fees belong to venue fee models; maker/taker instrument fields are absent. MSAI's [instrument resolver](../../backend/src/msai/services/nautilus/instruments.py) still imports V1 TestInstrumentProvider, and its [catalog builder](../../backend/src/msai/services/nautilus/catalog_builder.py) still uses V1 wrangler/write/query calls. The source-owned raw-Parquet bridge remains a real application need; native catalog maintenance does not provide the product's raw input selection/provenance policy.

Sources, accessed 2026-10-06: [persistence stubs](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/python/nautilus_trader/persistence/__init__.pyi), [wrangler](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/crates/persistence/src/python/wranglers/bar.rs), [bar Arrow schema](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/crates/serialization/src/arrow/bar.rs#L35), [decimal/timestamp schema](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/crates/serialization/src/arrow/mod.rs#L126), [Equity constructor](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/crates/model/src/python/instruments/equity.rs#L41).

**Design impact:** Use native instruments/catalog operations and a bounded raw-to-native adapter. Regenerate a candidate catalog in a separate namespace; do not reuse/purge the baseline's only catalog. Retain minute-equity scope honestly rather than certifying other assets/intervals.

**Test implication:** Check typed Arrow conversion or supported native Bar construction against exact prices/volume/precision/timestamps, order/count, first/last boundary and readback. Exercise unchanged-source reuse and changed-source rebuild without sibling/baseline damage. Missing/corrupt/unsupported input must fail usefully. Timestamp convention and inclusive dates must be checked separately from session completeness.

## BacktestNode, native reports, equity and costs

The native high-level sequence is `BacktestNode([run_config])`, `node.build()`, `node.add_strategy_from_config(run_config.id, importable_config)` (or `add_strategy`), `node.run()`. `BacktestDataConfig` uses `data_type=NautilusDataType.Bar` with native `InstrumentId` selectors, catalog path and timestamp bounds; V1 `data_cls` is absent. Retain `raise_exception=True`: source warns that disabled exceptions can skip failed configs/omit results. An empty result is not automatically a successful empty experiment.

V2 does not expose the old Python engine/trader through `get_engine`. With `dispose_on_completion=False`, use native `generate_orders_report(id)`, `generate_fills_report(id)`, `generate_positions_report(id)` and `generate_account_report(id, venue=None, account_id=None)`, supplying an account/venue selector. `get_engine_cache(id)` and `get_engine_portfolio(id)` expose supported inspection. Dispose only after extracting results. `BacktestResult.returns_series` is a timestamp→float dictionary; inspect return basis rather than substituting it mechanically for historical analytics.

Native portfolio exposes `equity(venue=None, account_id=None)` and `snapshots(account_id)` with typed account identity. Native cash equity can include open-position marks; margin equity includes unrealized P&L. Missing prices/common currency/snapshot coverage can alter availability and return selection. The previous RC6 wheel returned 0.28% for two same-day snapshots although pinned prose says two UTC dates are needed; preserve that documented/source/runtime distinction.

Use explicit native venue fee/fill configuration even for zero costs. RC6 exports `FixedFeeModel(commission: Money, charge_commission_once=...)`, `MakerTakerFeeModel(maker_rate: Decimal, taker_rate: Decimal, overrides=...)`, and `ProbabilisticFillModel(prob_fill_on_limit, prob_slippage, random_seed=...)` under `nautilus_trader.execution`. Native venue accepts currency, leverage, execution/liquidity assumptions and fee/fill models; omitted leverage is not safely equivalent to the intended economics. MSAI currently seeds MARGIN venues with $1 million and no explicit leverage/fee/fill model, and extracts realized account balances through V1 engine/trader access.

Sources, accessed 2026-10-06: [backtest stubs](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/python/nautilus_trader/backtest/__init__.pyi), [node methods and failure behavior](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/crates/backtest/src/python/node.rs), [portfolio stubs](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/python/nautilus_trader/portfolio/__init__.pyi), [native portfolio semantics](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/docs/concepts/portfolio.md), [fee/fill stubs](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/python/nautilus_trader/execution/__init__.pyi), [current MSAI runner](../../backend/src/msai/services/nautilus/backtest_runner.py).

**Design impact:** Preserve native fills/accounts/equity and explicit assumptions with the existing product outputs. Reconcile before changing accounting basis; keep historical `realized_account_balance` results and unknown values intact. A native marked-equity series and realized balances have different meanings.

**Test implication:** Reuse the tiny $10,000/10-share example: buy $100, sell $103, $2 total fees, $28 net, $10,028 ending capital, 0.28% account return. Add an open-position check if marked equity is exposed. Assert engine exceptions and missing reports cannot become zero-metric success. Check native report index/identity/Decimal serialization and API/CLI/browser units, inclusive dates and reload.

## Msgspec and data/reporting dependencies

**Msgspec:** The direct imports remain in schema hooks, backtest validation, projection consumer and cold position reader. RC6 no longer brings Msgspec into a clean backend environment. Declare the required application dependency or remove each actual dependency deliberately; supplying Msgspec does not restore Nautilus V1 paths or `.parse`. Official Msgspec supports schema generation/conversion for supported typed models and extension hooks; it cannot infer arbitrary native user-config field semantics. Latest 0.22 changes `gc=False`+weakref behavior, and 0.21 makes replace call `__post_init__`; neither is established as a trigger in this feature. Avoid an unrelated upgrade solely to recover availability. Sources, accessed 2026-10-06: [API/conversion](https://jcristharif.com/msgspec/api.html), [exact changelog](https://github.com/msgspec/msgspec/blob/0.22.0/docs/changelog.md), [metadata](https://pypi.org/pypi/msgspec/json). Test clean dependency install, schema/defaults, typed decode/validation and safe error envelopes.

**PyArrow:** IPC stream writers and `decimal128(precision, scale)` are supported; use an explicit schema matching the selected native contract. Standard Pandas inference produces a different schema. No PyArrow version increase is required by RC6 metadata. Sources, accessed 2026-10-06: [IPC writer](https://arrow.apache.org/docs/python/generated/pyarrow.ipc.new_stream.html), [Decimal128](https://arrow.apache.org/docs/python/generated/pyarrow.decimal128.html), [metadata](https://pypi.org/pypi/pyarrow/json). Test decimal precision, UTC nanoseconds, invalid/null columns and multi-batch conversion; do not infer raw-data/session quality from valid Arrow.

**Pandas:** Preserve the resolved 2.3.3 behavior where compatible. Pandas 3 changes default string inference and copy-on-write; no broad upgrade is justified for this slice. Sources, accessed 2026-10-06: [3.0 changes](https://pandas.pydata.org/docs/whatsnew/v3.0.0.html), [metadata](https://pypi.org/pypi/pandas/json). Test native DataFrame index→records normalization and UTC/daily return handling. No new Pandas abstraction is needed.

**QuantStats:** Its official `reports.html` signature takes returns/benchmark and compounded/annualization settings; its documentation describes daily return input. Keep the currently resolved package unless a concrete incompatibility appears. Sources, accessed 2026-10-06: [official report source](https://github.com/ranaroussi/quantstats/blob/main/quantstats/reports.py), [metadata](https://pypi.org/pypi/quantstats/json). Test the persisted report against the same reconciled daily series and supported signed/authenticated delivery, including report reload and useful refusal if unavailable. A tearsheet proves rendering, not economics or alpha.

## Minimum application startup/live boundary

Research startup can pull live/vendor code before any live command: [live dependencies](../../backend/src/msai/api/live_deps.py) import [PositionReader](../../backend/src/msai/services/nautilus/projection/position_reader.py), which imports removed V1 cache/serializer APIs and Msgspec; [Databento client](../../backend/src/msai/services/data_sources/databento_client.py) imports the old loader path at module scope. The prior Linux probe reproduced catalog/live/cold-reader/Databento import failures; successful runner-module import did not execute deferred calls.

Current RC6 has native IB/Databento adapters and Redis backing, but replacing those imports does not complete live migration. Native-only Redis-backed nodes use blocking `run`; cache-backed `run_async` is rejected. Hosted custom Python clients cannot use cache backing, and node ownership/stop/disposal differ. Live typed events/topics/cache representation and failure wrappers remain incompatible as recorded in the [addendum](2026-10-05-nautilus-upstream-trial-addendum.md#most-material-trial-contracts).

Sources, accessed 2026-10-06: [pinned Python lifecycle](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/docs/concepts/python.md), [cache/lifecycle guide](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/docs/how_to/configure_live_trading.md), [migration guide](https://github.com/nautechsystems/nautilus_trader/blob/7b766f8825b2539c5b2ac1375e9d97b41c509edb/MIGRATION_V2.md).

**Design impact:** A coherent bounded research mode must fail explicitly before any unsupported order-bearing live operation or broker connection. Isolate optional imports only where that expresses the supported scope; do not turn import failure into permissive risk behavior or silently disabled safeguards. Keep the accepted V1 live runtime/code/state independent.

**Test implication:** Real API/worker startup, CLI entrypoint import, safe read behavior and explicit order-bearing live refusal under candidate mode, with no broker/vendor I/O. Default/running V1 behavior must remain preserved. This does not certify live lifecycle, event projection, cache recovery, account routing or broker execution.

## Retained failures, unknowns and next proof

- Same-bar shared cash still raises negative-cash failure on RC6; staggered cash refusal passes only the exercised case. All three trial versions admit the exercised aggregate-margin overcommitment. Explicit per-order caps do not certify aggregate affordability.
- RC6's second simultaneous sell differs by one tick despite zero slippage probability and liquidity-consumption control. Its cause remains unproved. Do not explain it as a validated cost assumption.
- No real MSAI strategy/config/catalog/API/CLI/browser journey on V2 has passed yet. This refresh does not change that status or establish source-to-wheel equivalence for new Arrow/schema adapter code.
- Existing market/raw/catalog availability in the new isolated environment has not been established by this research. Documented synthetic seed data can arrange a labeled synthetic journey; it cannot be described as current vendor-market completeness.
- Account-equity return fallback and same-day snapshots require exact candidate behavior checks. Native report column presence is not complete financial reconciliation.
- Copying baseline code alone cannot recover incompatible state. Keep original raw data/catalog/cache and saved results untouched; prove compatible code plus state recovery and historical readback within the approved slice.
- Data completeness, realistic broker costs, immutable experiments, independent final validation, shared-capital portfolios and live-capital readiness remain open.

## Not researched further

FastAPI/Pydantic, SQLAlchemy/asyncpg/Alembic, arq/Redis job semantics, Entra/PyJWT, Typer and frontend libraries are existing product interfaces, with no upstream API/version migration established by this narrow engine change. Preserve their locks and contracts; expand focused research only if design changes them. Native Redis/live restrictions are covered above, while broker/vendor SDK operation, entitlements, ingestion, cloud deployment and actual live migration are out of scope. The completed October 5 matrix is reused, not repeated.
