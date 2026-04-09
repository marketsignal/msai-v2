# Research Platform Roadmap

Date: 2026-04-07
Scope: US equities + CME futures research, backtesting, and promotion to paper/live.

## Goal

Build a research and trading platform where:

- historical data is reproducible and easy to refresh
- strategies can be backtested repeatedly with parameter sweeps
- walk-forward validation reduces overfitting risk
- promising configurations can be promoted into paper trading safely
- live trading remains centered on NautilusTrader + Interactive Brokers

## Principles

- Use NautilusTrader for market-data normalization, catalog usage, backtests, live state, and execution flow where possible.
- Use Interactive Brokers as the execution venue and account source of truth in live trading.
- Use Databento as the primary historical research provider for US equities and CME futures.
- Treat every research result as provisional until it survives out-of-sample and paper trading.

## Phase 1

Databento-first historical data foundation for US equities and CME futures.

### Deliverables

- Historical ingestion chooses Databento by default for `equities` and `futures`.
- Request-time provider overrides remain possible for development and migration.
- Datasets and schemas are explicit instead of hidden inside asset-class conditionals.
- Raw bars are written to `data/parquet`, then converted into Nautilus catalog format on demand.
- Ingestion metadata records provider, dataset, schema, symbols, time range, and per-symbol coverage.
- Ingestion failures fail honestly when Databento credentials or datasets are missing.

### Acceptance Criteria

- Ingest `AAPL` and `MSFT` from Databento US equities into raw Parquet.
- Ingest CME futures symbols from Databento `GLBX.MDP3` into raw Parquet.
- Build the Nautilus catalog for those instruments without manual intervention.
- Surface empty/failed ingests clearly through API, CLI, and status metadata.

## Phase 2

Parameter sweeps and walk-forward validation.

### Deliverables

- Experiment model and storage for batches of backtests.
- Parameter-grid execution with result aggregation.
- Walk-forward validation windows:
  - rolling train/test
  - expanding train/test
- Comparison metrics:
  - sharpe
  - sortino
  - max drawdown
  - total return
  - win rate
  - trade count
- Stability/out-of-sample reporting to reduce overfit selection.

### Acceptance Criteria

- Run a strategy over a grid of parameters.
- Compare in-sample and out-of-sample performance by configuration.
- Rank configurations by both return and stability.

## Phase 3

Research comparison UI and promotion workflow.

### Deliverables

- Experiment list and status page.
- Side-by-side strategy comparison view.
- Parameter leaderboard and walk-forward summary.
- Backtest report explorer.
- “Promote to paper” control path with audit trail.

### Acceptance Criteria

- Compare at least three strategies from the UI.
- Drill into top configurations and inspect reports/trades.
- Promote a selected configuration into paper trading without manual database edits.

## Initial Strategy Set

1. US equities intraday mean reversion
2. CME futures intraday mean reversion
3. CME futures breakout / trend

## Dependencies

- Databento Standard subscription
- `DATABENTO_API_KEY` set in local/dev/prod environment
- Interactive Brokers paper environment for later promotion checks

## Current Order of Execution

1. Phase 1 data ingestion foundation
2. First equity and futures backtests using the new ingest path
3. Phase 2 experiment runner and walk-forward engine
4. Phase 3 comparison UI and promotion workflow
