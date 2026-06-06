# PRD Discussion: PR 1b — Data-Stale Safety

**Status:** Completed — shipped as PR #89 (`a271917`, merged 2026-06-04); doc salvaged from the worktree post-merge
**Started:** 2026-06-03
**Participants:** Pablo, Claude

## Original User Stories

Extracted from the council-ratified sources (not free-form user input):

- `docs/decisions/multi-account-broker-fleet.md` — "Data-stale auto-halt — per-node detection, fleet-wide action" + blocking objection #9 + the PR 1 / PR 1b split (Pablo, 2026-05-28, accepting Codex P2 #6).
- `docs/prds/multi-account-broker-fleet.md` — PR 1b boundary ("data-stale safety, gates LVP/HVP graduation").

Distilled stories:

- As the **fleet operator**, I want every live TradingNode's market-data freshness to be **observable per account/node/dataset/symbol**, so I can tell which feed is healthy and which is not — a single fleet "Databento healthy" light is insufficient.
- As the **fleet operator**, I want the fleet to **auto-halt** (same emergency-halt latch) when any required live feed goes stale, carrying `reason=data_stale` + source account/node/dataset/symbol + `detected_at` + `last_event_ts`, so no account flies blind and I can tell a data-stale halt apart from my own `/kill-all`.
- As the **fleet operator**, I want **resume to be explicit and gated** — only after data-warm verification AND reconciliation completion — so a flapping feed can never silently resume trading.
- As the **release gatekeeper**, I want the halt proven under **five failure modes** (Databento disconnect; stale-timestamp feed — connection up, bars frozen; partial-dataset stall; single-symbol stall; reconnect storm), because objection #9 binds at the LVP/HVP real-money graduation gate.

Already SETTLED by the council (not re-opened here):

- Per-node/per-dataset/per-symbol detection; **fleet-wide** halt action.
- Session-aware freshness: event timestamps + expected interval + grace (default ≈30s **after the next expected data point**), with asset-class/session overrides — never a raw "last bar older than 30s".
- Reuses the fleet emergency-halt latch code path (objection #4 deliverable), distinct cause attribution.
- No auto-resume, ever.
- Equities-first; OPRA real-time explicitly out of scope (separate capacity spike).
- PR 1b blocks LVP/HVP graduation, not PR 1 merge.

## Discussion Log

(The Q&A was never recorded here — design outcomes were captured directly in
`docs/decisions/multi-account-broker-fleet.md` "Addendum 2026-06-03 — PR 1b (data-stale auto-halt) implemented"
and the PR #89 plan/implementation. This file is preserved as the user-story record.)
