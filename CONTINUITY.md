# CONTINUITY

## Goal

First real backtest — ingest market data and run EMA Cross strategy on real AAPL/SPY data.

## Workflow

| Field   | Value |
| ------- | ----- |
| Command | none  |

## Done

- Hybrid merge PR#3 merged (2026-04-13): 18 tasks, 99 files, ~15K lines
- Docker Compose parity PR#4 merged (2026-04-13): 12 gaps fixed, all 10 containers running
- IB Gateway connected: 6 paper sub-accounts verified (DFP733210 + DUP733211-215, ~$1M each)
- Databento API key configured

## Now

Data ingestion — ingest real AAPL/SPY historical data via Databento, then run first backtest.

## Next

1. Ingest AAPL + SPY minute bars (2024-01-01 to 2025-01-01)
2. Run first backtest with EMA Cross strategy on real data
3. Verify results in UI + QuantStats report
4. Test live accounts (test-lvp, msai-mv-0)
5. US-2.4, US-5.3, US-8.3
