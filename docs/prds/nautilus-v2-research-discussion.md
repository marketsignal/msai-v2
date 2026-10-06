# PRD Discussion: Nautilus V2 research integration

**Status:** Complete; PRD v1.0 approved on 2026-10-06
**Started:** 2026-10-06
**Participants:** User, Codex

## Original user direction

The user corrected the status: the bounded V2 compatibility trial already happened and produced partial passes and explicit failures. The next step is narrow V2 research integration, proving a real strategy journey through API, CLI and browser. V2 has not been installed. Milestone 1 and the wider correctness/readiness gaps remain open. Reconcile stale release paragraphs first, then continue with the next steps.

## Discussion log

- October 6: reconciled MASTER_PLAN.md, MASTER_MAP.md and docs/agent-context.md in local commit 9d51a63, based on accepted Azure/main fb757953. Focused documentation checks passed.
- October 6: user instructed: “ok continue with the next steps then”. This authorizes preparation of the already described narrow integration. No new deployment, broker order, publication or native Goal authorization is inferred.
- October 6: user explicitly replied “approved” to the PRD v1.0 approval request. Proceed with bounded research, plan review and the authorized feature workflow; consequential publication/deployment actions retain separate gates.
- Existing master-plan requirements already settle persona, interface coverage, narrow scope, saved-result preservation, failure/correction, native reuse and live-readiness exclusions. Repeating those questions would add no useful requirements evidence.

## Refined understanding

### Persona

- Existing MSAI research operator: discover and configure a versioned Python strategy, run an experiment, inspect its evidence, and revisit results through API, CLI and browser.

### User stories

- Discover and validate a representative existing strategy on V2, with understandable configuration errors.
- Execute and reconcile a bounded real backtest, with explicit financial assumptions and inclusive dates.
- Follow progress and inspect consistent persisted results through all three interfaces.
- Preserve training-only selection and exploratory Discovery boundaries in affected research paths.
- Preserve existing release/data/results and retain coherent application startup and live-interface behavior during isolated development.

### Non-goals

Full live-engine migration, production installation, broker orders, complete multi-account portfolio acceptance, realistic-cost certification, data-completeness certification, immutable experiment certification, final-validation/graduation repair and performance benchmarking are separate work.

### Key decisions already recorded

- Direct V2 development; no default 1.231 bridge and no permanent two-engine framework.
- Preserve the 1.223 reference and the completed trial's negative results.
- API first, CLI second, real browser third; require persistence and a meaningful failure/correction path.
- Formal PRD approval and clean plan evidence precede implementation under the new-feature workflow.

### Remaining questions

No new product-scope question is needed to draft this bounded slice. Exact candidate selection and implementation details belong to research/design after PRD approval.
