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
