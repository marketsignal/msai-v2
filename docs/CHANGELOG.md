# Changelog

All notable changes to msai-v2 will be documented in this file.

## [Unreleased]

### Added

- Initial project setup with Claude Code configuration
- 2026-04-16: ES futures canonicalization pipeline (PR #23) — `canonical_instrument_id()`, `phase_1_paper_symbols()` as a function with fresh front-month per call, `exchange_local_today()` helper on America/Chicago, `TradingNodePayload.spawn_today_iso` threading so supervisor + subprocess agree on the same quarterly contract across midnight-on-roll-day spawns. 28 new unit tests in `test_live_instrument_bootstrap.py` (39 total). Branch `fix/es-contract-spec`.

### Changed

- 2026-04-16: Live-supervisor now canonicalizes user-facing instrument ids before passing to strategy config — e.g., `ES.CME` → `ESM6.CME` for futures, identity for stocks/ETF/FX. Overwrites stale explicit `instrument_id` / `bar_type` only when the root symbol changes (futures rollover), preserving operator aggregation choices on stocks/FX.

### Fixed

- 2026-04-16: ES deployments producing zero bar events (drill 2026-04-15 failure mode, PR #23) — root cause was an instrument-id mismatch between the user-facing `ES.CME` bar subscription and the concrete `ESM6.CME` instrument Nautilus registers after IB resolves `FUT ES 202606`. Now canonicalized at the supervisor. Live-verified: subscription succeeds against paper IB Gateway with no "instrument not found" error. Also caught a `.XCME` (ISO MIC) vs `.CME` (IB_SIMPLIFIED native) venue bug in an earlier iteration that would have shipped without the live e2e test. NOTE: bars still don't fire due to a separate IB-side issue — `DUP733213` lacks a real-time CME market-data subscription (confirmed via `ib_async.reqMktData` → IB error 354). Same entitlement gap killed AAPL/MSFT/SPY bars in the drill. Requires operator action at `broker.ibkr.com → Market Data Subscription Manager` — not code.

### Removed

---

## Format

Each entry should include:

- Date (YYYY-MM-DD)
- Brief description
- Related issue/PR if applicable
