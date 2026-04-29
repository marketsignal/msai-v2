# Architecture Documentation

Verified against the codebase on 2026-04-09. Every class name, function
signature, config value, and file path in these documents was read from
the source files; nothing is inferred or assumed.

## Reading Order

1. **[Platform Overview](platform-overview.md)** -- What MSAI is, the
   Nautilus/MSAI boundary, capabilities, and Phase 1 vs Phase 2 scope.

2. **[System Topology](system-topology.md)** -- Docker Compose services,
   ports, the `live` profile boundary, inter-container networking.

3. **[Module Map](module-map.md)** -- Directory-by-directory tour of
   `backend/src/msai/` and `frontend/src/`.

4. **[Data Flows](data-flows.md)** -- The five primary data flows with
   ASCII diagrams: ingestion, backtesting, live trading, projection
   pipeline, and WebSocket streaming.

5. **[Live Trading Subsystem](live-trading-subsystem.md)** -- Deep dive
   on the supervisor, subprocess lifecycle, heartbeat, watchdog, and
   four-layer kill switch.

6. **[Nautilus Integration](nautilus-integration.md)** -- Where MSAI
   ends and NautilusTrader begins: config builder, instrument bootstrap,
   IB adapter wiring, cache/message-bus Redis, and the projection
   consumer.

7. **[Decision Log](decision-log.md)** -- Every architectural choice
   with rationale and code references.

## Subsystem Deep Dives — Developer Journey

How to use the system end-to-end across API/CLI/UI. Verified against the codebase on 2026-04-28.

0. **[Developer Journey](00-developer-journey.md)** -- Front-of-house
   narrative + component diagram. Start here.
1. **[How Symbols Work](how-symbols-work.md)** -- Symbol onboarding,
   `instrument_definitions` + `instrument_aliases` registry, daily refresh.
2. **[How Strategies Work](how-strategies-work.md)** -- Authoring Python
   strategies in `strategies/` (git-only Phase 1), `code_hash`/`git_sha`,
   `FailureIsolatedStrategy`, validation.
3. **[How Backtesting Works](how-backtesting-works.md)** -- Single-strategy
   single-symbol backtest: arq → BacktestRunner subprocess → results +
   QuantStats report.
4. **[How Research and Selection Work](how-research-and-selection-works.md)**
   -- Parameter sweeps, walk-forward CV, OOS validation, promotion to
   `GraduationCandidate`.
5. **[How Graduation Works](how-graduation-works.md)** -- 9-stage state
   machine + immutable transition log; the gate from research-winner to
   capital-allocation eligible.
6. **[How Backtest Portfolios Work](how-backtest-portfolios-work.md)** --
   Multi-strategy × multi-symbol allocation of `GraduationCandidate`s,
   per-component fan-out + aggregation.
7. **[How Live Portfolios and IB Accounts Work](how-live-portfolios-and-ib-accounts.md)**
   -- `LivePortfolio → Revision → Deployment` chain, IB account wiring,
   live supervisor, 3-layer idempotency, 4-layer kill-all.
8. **[How Real-Time Monitoring Works](how-real-time-monitoring-works.md)**
   -- WebSocket stream + reconnect hydration, dashboard P&L, alerts,
   halt-flag flow.
