````markdown
# Watchlists

Git-tracked YAML manifests that declare MSAI's symbol universe. Each file
is a named watchlist: the filename stem is the watchlist name (e.g.
`core-equities.yaml` → `core-equities`).

## Usage

```bash
msai symbols onboard watchlists/core-equities.yaml
msai symbols status core-equities
```

## Manifest schema

```yaml
name: core-equities # kebab-case, matches filename stem
symbols:
  - { symbol: SPY, asset_class: equity, start: 2021-01-01, end: 2025-12-31 }
  - { symbol: AAPL, asset_class: equity, start: trailing_5y } # expands to (today-5y, today-1d)
  - { symbol: ES.n.0, asset_class: futures, start: 2023-01-01, end: 2025-12-31 }
request_live_qualification: false # default; set true when ready to deploy to IB
```

## Rules

- Every symbol has `start` (ISO date or `trailing_Ny` sugar). `end` is optional; defaults to `today - 1d`.
- `asset_class ∈ {equity, futures, fx, option}` (matches registry taxonomy).
- `trailing_Ny` expands client-side via `dateutil.relativedelta`. The server always sees concrete ISO dates.
- Cross-watchlist dedup: if `SPY` appears in two files, the wider window wins; decision is logged.
- Manifest changes take effect only when `msai symbols onboard <file>` is run. No filesystem watcher.

## Storage

1-minute bars are the canonical storage granularity; 5m/10m/30m/1h/1d aggregate for free at backtest time via Nautilus `BarAggregator`. Don't request per-timeframe — one ingest per symbol is enough.
````
