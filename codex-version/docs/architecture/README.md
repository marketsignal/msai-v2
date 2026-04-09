# Architecture Docs

Date: 2026-04-07
Scope: `codex-version`

This folder documents how the platform is wired today, why the main design
choices were made, and where the production gaps still are.

## Documents

- [System Topology](./system-topology.md)
- [Module Map](./module-map.md)
- [Data Flows](./data-flows.md)
- [Platform Overview](./platform-overview.md)
- [Research To Live Flow](./research-to-live-flow.md)
- [Decision Log](./decision-log.md)

## Recommended Reading Order

For a new developer:

1. [README](/Users/pablomarin/Code/msai-v2/codex-version/README.md)
2. [System Topology](./system-topology.md)
3. [Module Map](./module-map.md)
4. [Data Flows](./data-flows.md)
5. [Platform Overview](./platform-overview.md)
6. [Research To Live Flow](./research-to-live-flow.md)
7. [Decision Log](./decision-log.md)

## Current Intent

The platform is designed as a thin control plane around NautilusTrader:

- NautilusTrader owns market-data normalization, catalog usage, backtests,
  live trading state, execution flow, and broker-facing strategy runtime.
- Interactive Brokers is the live execution venue and live account source.
- Databento is the primary historical research provider for US equities and
  CME futures.
- The MSAI backend and frontend own orchestration, operator workflow, audit,
  promotion, and visibility.

## Status

The system is materially stronger than the original prototype, but it is still
not "error-free" or hedge-fund production certified yet. The main remaining
gaps are:

- broker-connected paper E2E still depends on the IB paper account being active
- Azure deployment hardening and runbooks are still pending
- full broker/account reconciliation on silent-partition cases is not complete
- full order lifecycle durability is still stronger for fills than for all
  broker-side state transitions

These docs should be read together with the roadmap at
[`docs/plans/2026-04-07-research-roadmap.md`](../plans/2026-04-07-research-roadmap.md)
and the Azure rollout recommendation at
[`docs/plans/2026-04-07-azure-rollout-plan.md`](../plans/2026-04-07-azure-rollout-plan.md).

## External References

- [NautilusTrader concepts](https://nautilustrader.io/docs/latest/concepts/)
- [NautilusTrader architecture](https://nautilustrader.io/docs/latest/concepts/architecture/)
- [NautilusTrader message bus](https://nautilustrader.io/docs/latest/concepts/message_bus/)
- [NautilusTrader live trading](https://nautilustrader.io/docs/latest/concepts/live/)
- [NautilusTrader Interactive Brokers integration](https://nautilustrader.io/docs/latest/integrations/ib/)
- [NautilusTrader Databento integration](https://nautilustrader.io/docs/latest/integrations/databento/)
- [Databento examples](https://databento.com/docs/examples)
