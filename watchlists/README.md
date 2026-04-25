# Watchlists

Git-tracked YAML manifests that declare MSAI's symbol universe. Each file
is a named watchlist whose `watchlist_name` is the canonical handle the
API + CLI use to refer to it.

## Usage

```bash
msai symbols onboard --manifest watchlists/core-equities.yaml
msai symbols status <run_id>
```

## Manifest schema

```yaml
watchlist_name: core-equities # kebab-case, must match ^[a-z0-9-]+$
symbols:
  - { symbol: SPY, asset_class: equity, start: 2021-01-01, end: 2025-12-31 }
  - { symbol: AAPL, asset_class: equity, window: trailing_5y } # expands to (today-5y, today-1d)
  - { symbol: ES.n.0, asset_class: futures, start: 2023-01-01, end: 2025-12-31 }
```

## Rules

- Top-level keys: `watchlist_name`, `symbols`. Anything else is rejected.
- Per-symbol keys: `symbol`, `asset_class`, `start`, `end`, `window`.
- `asset_class ∈ {equity, futures, fx, option}` (matches registry taxonomy).
  Note: v1 cost estimator only prices `equity` + `futures`; `fx` and `option`
  return HTTP 422 `UNPRICEABLE_ASSET_CLASS`.
- A symbol entry must use either explicit `start` + `end` OR `window:` sugar,
  not both.
- `window: trailing_5y` is the only sugar value supported in v1; it expands
  client-side via `dateutil.relativedelta` to `(today-5y, today-1d)`. The
  server always sees concrete ISO dates.
- `request_live_qualification` is a request-time flag, NOT a manifest field;
  pass it through the API/CLI when calling `onboard`.
- Cross-watchlist dedup: when the same `(symbol, asset_class)` appears in
  multiple manifests passed in the same merge, the wider window wins;
  decision is logged.
- Manifest changes take effect only when `msai symbols onboard --manifest <file>`
  is run. No filesystem watcher.

## Storage

1-minute bars are the canonical storage granularity; 5m/10m/30m/1h/1d
aggregate for free at backtest time via Nautilus `BarAggregator`. Don't
request per-timeframe — one ingest per symbol is enough.
