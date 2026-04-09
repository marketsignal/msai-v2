# Research To Live Flow

For broader context, read this together with:

- [System Topology](./system-topology.md)
- [Module Map](./module-map.md)
- [Data Flows](./data-flows.md)

## Why This Flow Exists

The platform should not let a backtest jump straight into live trading.
Research results must be reviewed, compared, and then promoted into paper
trading in a controlled way.

## Research Flow

### 1. Historical ingest

Historical research starts with Databento:

- equities default to `EQUS.MINI`
- CME futures default to `GLBX.MDP3`
- the default bar schema is `ohlcv-1m`

For each ingest:

1. Databento definitions are downloaded and decoded through Nautilus-aware
   loaders.
2. Real `Instrument` payloads are persisted.
3. Raw bars are written into the local Parquet layer.
4. Ingestion metadata is updated with provider, dataset, schema, symbols, and
   coverage.

### 2. Catalog build

The Nautilus catalog is built from the raw files and the persisted definitions.
This avoids the old failure mode where research backtests silently invented
synthetic instruments that did not match the live lane.

### 3. Research execution

The research engine currently supports:

- parameter sweeps
- rolling walk-forward validation
- expanding walk-forward validation

The output is a JSON report under `data/research/`.

## Phase 3 Research Console

The Research page reads those JSON reports through the backend research API.
It gives the operator:

- report discovery
- detail inspection
- side-by-side comparison for up to three reports
- parameter leaderboard review
- walk-forward window review
- paper promotion draft creation

## Promotion Flow

Promotion intentionally does not auto-start live trading.

The promotion endpoint creates a JSON draft under
`data/research/promotions/` containing:

- source report ID
- selected config
- selected instruments
- strategy ID and name
- selection metadata
- paper-trading target URL

The Live page can then load that draft using `promotion_id` and prefill the
deploy form. This creates an auditable operator checkpoint between research and
paper deployment.

## Why This Is Safer

This design avoids several bad patterns:

- no manual database edits to move a config into paper trading
- no copy/paste from CLI JSON blobs into live deployment forms
- no hidden re-interpretation of research configs during promotion
- no direct jump from backtest optimism to real-money live start

## What Still Needs Improvement

- full experiment persistence beyond file-backed JSON reports
- richer per-run artifact capture for research comparison
- stronger paper-burn-in acceptance gating before promotion beyond operator
  review
- automated Azure staging flow for promoted configs
