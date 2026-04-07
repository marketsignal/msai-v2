# Changelog

All notable changes to msai-v2 will be documented in this file.

## [Unreleased]

### Added

- Initial project setup with Claude Code configuration
- **2026-04-06** — Architecture review of Claude vs Codex against the NautilusTrader canonical system architecture diagram. Two parallel reviews (Claude Explore agent + Codex CLI) reached the same verdict: neither version is close to a hedge-fund production platform. Claude 4/10 (toy on the live side, well-engineered backtest), Codex 5/10 (real exec path, untested). See `docs/plans/2026-04-06-architecture-review.md`.
- **2026-04-06** — Started `feat/claude-nautilus-production-hardening` worktree. Goal: production-harden the Claude version to use real Nautilus end-to-end with real IB live trading.
- **2026-04-06** — Deep Nautilus technical reference (`docs/nautilus-reference.md`, 60KB, 10 sections, 20 production gotchas). Every claim cited to `nautilus_trader/...:line`. Auto-loaded short-form gotchas list at `.claude/rules/nautilus.md` plus auto-memory `nautilus-gotchas.md`.
- **2026-04-06** — Nautilus natives audit (`docs/nautilus-natives-audit.md`). For each Tier-1 production blocker, identifies what Nautilus already provides natively vs what we have to build. Bottom line: Nautilus is 60-70% of a production platform — our job is the 30% glue, not reinventing.
- **2026-04-06** — Phased implementation plan v2 (`docs/plans/2026-04-06-claude-nautilus-production-hardening.md`, 5 phases, ~47 task subsections). Incorporates Codex review of v1 (1 P0 + 9 P1 + 3 P2 fixed) and the Nautilus natives audit (deletes ~6 reinventing tasks, simplifies Phase 4 dramatically). Codex re-reviewed v2: REJECTED with 2 new P0 + 7 P1 around container topology and process ownership. v3 pending design discussion with operator.
- **2026-04-06** — Phased implementation plan v3 (~2560 lines, +834/−245 vs v2). Addresses Codex v2 re-review (2 P0 + 7 P1 fixed) around container topology and process ownership. Key architectural shifts: dedicated `live-supervisor` Docker service (not arq-hosted), heartbeat-only liveness (no cross-container PID probing), deterministic `trader_id`/`order_id_tag` derived from `deployment_id`, `stream_per_topic = False` for single deterministic Redis stream per trader, Redis pub/sub per deployment for multi-uvicorn-worker WebSocket fan-out, FastAPI uses Nautilus `Cache` Python API (no raw Redis key reads), `StrategyConfig.manage_stop = True` replaces custom `on_stop` flatten, parity harness redesigned (determinism + config round-trip + intent contract, no TradingNode-vs-IB-paper), restart-continuity via `BacktestNode` run twice, `RiskAwareStrategy` uses `self.portfolio.account()/total_pnl()/net_exposure()`. Pending Codex v3 re-review.
- **2026-04-07** — Phased implementation plan v4 (~3540 lines, +~1000 vs v3). Addresses Codex v3 re-review (3 P0 + 5 P1 + 2 P2 fixed). Topology and ownership are directionally correct from v3; v4 fixes Redis Streams semantics, idempotency, the readiness gate, and the identity/schema model. Key changes: stable `deployment_slug` (16 hex chars, decoupled from `live_deployments.id`) so Phase 4 state reload actually works across restarts; Redis Streams PEL recovery via explicit `XAUTOCLAIM` (un-ACKed messages do NOT auto-redeliver); idempotency at three layers (DB partial unique index + supervisor pre-spawn check + ACK-only-on-success + HTTP `Idempotency-Key` header); post-start health check before writing `status="ready"` (kernel.start_async returning is not proof of readiness); new task 1.1b for `live_deployments` schema migration (adds slug/trader_id/account_id/message_bus_stream/instruments_signature columns); `live_node_processes.pid` nullable, `building` added to status enum; PositionReader rewritten to use in-memory `ProjectionState` (fast path) + ephemeral per-request `Cache` (cold-start fallback) — no more drift-prone long-lived Caches; correct portfolio API (`total_pnls(venue)`/`net_exposures(venue)` plurals for venue-level aggregates); restart-continuity test runs against testcontainers Redis (not invented on-disk KV-store); Phase 4 Scenario D dropped (non-deterministic, replaced by deterministic BacktestNode-twice test); supervisor keeps `dict[deployment_id, mp.Process]` handle map for instant exit detection via `reap_loop` (heartbeat is the cross-restart fallback only); kill switch is push-based (supervisor SIGTERM + persistent halt flag + strategy mixin defense in depth — < 5 second latency). Pending Codex v4 re-review.

### Changed

- **2026-04-06** — Plan Phase 1 architecture: live trading subprocess no longer hosted by FastAPI. Now spawned by a worker-side supervisor consuming a Redis command stream. Killing the FastAPI container has zero effect on running trading subprocesses (FastAPI never owned them).
- **2026-04-06** — Plan deletes custom `RiskEngine` subclass (kernel can't use it), replaces with `RiskAwareStrategy` mixin reading from Nautilus Cache + Portfolio.
- **2026-04-06** — Plan deletes `PositionSnapshotCache` (Nautilus Cache with `CacheConfig.database = redis` already does this).
- **2026-04-06** — Plan crash recovery dramatically simplified to orphaned-process detection only — reconciliation, state persistence, position restore are all automatic via `LiveExecEngineConfig.reconciliation = True` and `NautilusKernelConfig.load_state/save_state = True`.

### Fixed

- **2026-04-06** — Plan: `buffer_interval_ms = 0` corrected to `None` (Nautilus field is `PositiveInt | None`).
- **2026-04-06** — Plan: Redis stream topic names corrected to Nautilus's actual format (`events.order.{strategy_id}`, `events.position.{strategy_id}`, `events.account.{account_id}`).
- **2026-04-06** — Plan: stop sequence no longer calls `node.cancel_all_orders` / `node.close_all_positions` — those are `Strategy` methods, called from `Strategy.on_stop`.
- **2026-04-06** — Plan: order audit table gains `client_order_id` correlation key so a single audit row can be updated through the order lifecycle.
- **2026-04-06** — Plan: strategy code hash computed from file bytes at deploy time, not via `git rev-parse` (the container only mounts `src/` and `strategies/`).
- **2026-04-06** — Plan: explicit `GET /api/v1/live/status/{deployment_id}` route task added (was missing in v1).
- **2026-04-06** — Plan: Phase 1 task ordering corrected — 1.7/1.8/1.9/1.10/1.11 hot-edit the same files and must run sequentially, not in parallel as v1 claimed.

### Removed

(none yet — implementation has not started)

---

## Format

Each entry should include:

- Date (YYYY-MM-DD)
- Brief description
- Related issue/PR if applicable
