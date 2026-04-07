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
