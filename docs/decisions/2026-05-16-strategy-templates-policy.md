# Strategy Templates Backend Feature — CUT (Phase 1 git-only policy upheld)

**Date:** 2026-05-16
**Branch:** `feat/ui-completeness`
**Status:** Ratified — backend feature removed in T4 of the ui-completeness PR.
**Supersedes:** No prior decision document. The original templates
implementation was added without an architectural decision doc, which
silently contradicted CLAUDE.md "Key Design Decisions" (no UI uploads in
Phase 1 — git-only).

## Verdict

**CUT** the strategy-templates backend feature in its entirety.

Files removed:

- `backend/src/msai/api/strategy_templates.py`
- `backend/src/msai/services/strategy_templates.py`
- `backend/tests/unit/test_strategy_templates.py`
- `backend/src/msai/cli.py` — `template_app` sub-app + `template_list` /
  `template_scaffold` commands
- `backend/tests/unit/test_cli_completeness.py` — `TestTemplateList` /
  `TestTemplateScaffold` classes + `"template"` entries in the
  command-tree registration tests

Files awaiting T4b serial integration (see Revision R5 of
`docs/plans/2026-05-16-ui-completeness.md`):

- `backend/src/msai/main.py` — `strategy_templates_router` import +
  `app.include_router(strategy_templates_router)`

Schema file `backend/src/msai/schemas/strategy_template.py` was checked
for and confirmed not to exist (templates only used inline Pydantic
models from `api/strategy_templates.py`).

## Rationale

### 1. The feature contradicts the ratified Phase 1 architecture

CLAUDE.md "Key Design Decisions" states explicitly:

> Strategies are Python files in `strategies/` dir (no UI uploads in
> Phase 1 — git-only)

The `StrategyTemplateService.scaffold` method writes a new strategy
file into `STRATEGIES_ROOT` from an HTTP request — i.e. a UI upload by
another name. The service was added without a decision doc and no UI
consumer was ever built (see `docs/decisions/2026-05-16-ui-completeness-scope.md`
council finding: "the strategy templates scaffolder is **not simple UI
completeness**. It reverses (or weakens) the ratified Phase 1 git-only
strategy-authoring decision.").

### 2. The council scope decision flagged this gap as blocked, not deferred

`docs/decisions/2026-05-16-ui-completeness-scope.md` §"Stage 2
prerequisites" (Gap 3) reads:

> Decision doc amending Phase 1 "git-only strategies" policy. The
> current scaffolder service (`services/strategy_templates.py`) silently
> contradicts CLAUDE.md "no UI uploads in Phase 1 — git-only". UI
> surface either honors the policy (cut feature) or the policy is
> explicitly amended in a ratified council/decision doc. Do not let UI
> implicitly reverse architecture.

The two paths offered were:

1. Ship a decision doc amending the Phase 1 policy and continue building
   the UI scaffolder, OR
2. Cut the backend feature so policy and code agree.

**Path 2 chosen** per the in-PR scope discussion ratified by Pablo. This
keeps the Phase 1 "git-only strategies" decision intact and removes the
silent contradiction. Authoring a new strategy remains:

```bash
cp strategies/examples/<x>.py strategies/<new>.py
$EDITOR strategies/<new>.py
git add strategies/<new>.py
```

### 3. Cost-benefit: dead code is a maintenance liability

- No UI consumer existed at deletion time.
- The CLI commands `msai template list` / `msai template scaffold` were
  thin wrappers around the same service and added zero value over
  the `cp` + `$EDITOR` flow above.
- The service carried real complexity (path-traversal validation, file
  collision checks, module-name validation, atomic write semantics) that
  required ongoing maintenance for zero user-facing benefit.
- mypy/ruff/test surface for a feature with no consumer is pure
  carrying cost.

Per CLAUDE.md "No Bugs Left Behind Policy" + `.claude/rules/principles.md`
(Brutal simplicity over clever solutions): when the architecture says
no, the code must say no. Patching the contradiction is cheap _now_;
patching it later — after a UI grows on top of it — is expensive.

## When to re-evaluate

The decision-doc gate for re-introducing strategy-authoring through any
non-git surface (UI scaffolder, API upload, CLI authoring) is:

- **≥ 20 strategies** registered in the strategy registry — at which
  point `cp` + `$EDITOR` friction across many files justifies tooling.
- **≥ 2 strategy authors on the team** — solo work doesn't benefit from
  in-app authoring; collaboration does.
- **A ratified decision document** explicitly amending the Phase 1
  git-only policy, with council review of:
  - How code provenance is tracked when the file doesn't come from git
    HEAD (the `strategy_code_hash` chain depends on this).
  - How rollback semantics work when a UI-authored strategy fails in
    backtest / live (does the UI become the editor too?).
  - How RBAC governs who can author strategies via the UI surface.

Until those three conditions are met, the answer is "use `cp` + git".

## What survives the cut

Nothing in the strategy _runtime_ path changes:

- The strategy registry (`backend/src/msai/services/strategy_registry.py`)
  still scans `STRATEGIES_ROOT` and registers every `.py` file under it.
- Backtests + live deployments still load strategies by import path
  (`strategies.examples.ema_cross.EmaCross` style).
- `strategy_code_hash` (SHA256 over the strategy file's content) is
  still recorded on every Backtest + LiveDeployment row for
  reproducibility.

Only the _authoring_ surface that originated outside `strategies/`-via-git
is removed.

## Cross-references

- `CLAUDE.md` — Key Design Decisions, Phase 1 architecture
- `docs/decisions/2026-05-16-ui-completeness-scope.md` — Council scope verdict (parent decision)
- `docs/plans/2026-05-16-ui-completeness.md` — T4 task definition + R4
  revision (test deletions) + R5 revision (main.py wiring deferred to T4b)
