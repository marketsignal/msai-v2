# Mypy --strict cleanup plan (2026-04-23)

## Context

PR #42 unblocked CI by adding library-stub overrides and marking the `mypy --strict` step `continue-on-error: true`. 128 errors remain across 42 files. Nature: accumulated drift while CI was broken. No single feature caused it; every unmodified file that happened to drift into `--strict` compliance just stays clean, while touched files gradually diverged.

**Goal:** `uv run mypy src/ --strict` exits 0. Then remove `continue-on-error: true` from `.github/workflows/ci.yml`. Also add `actionlint` as a pre-commit hook so the next `ci.yml` parse bug is caught locally.

## Non-goals

- No runtime behavior change
- No new tests (behavior is unchanged; existing tests protect against regression)
- No additional library-stub overrides unless new imports surface

## Error breakdown (128 total, 42 files)

| Count | Code               | Nature                                        | Fix shape                                                                    |
| ----: | ------------------ | --------------------------------------------- | ---------------------------------------------------------------------------- |
|    31 | `type-arg`         | `Mapped[dict]` / bare `dict`/`list` in annot. | Add concrete params — usually `dict[str, Any]`                               |
|    26 | `name-defined`     | SQLAlchemy FK refs unquoted by PR #42 UP037   | Add TYPE_CHECKING imports of referenced model classes                        |
|    18 | `unused-ignore`    | Stale `# type: ignore` post-override          | Remove the noqa                                                              |
|    11 | `attr-defined`     | Cython/library attrs mypy can't verify        | Per-site judgment: real bug → fix; stub gap → `# type: ignore[attr-defined]` |
|    10 | `misc`             | Various                                       | Per-site                                                                     |
|     8 | `valid-type`       | Function being used as annotation             | Rewrite annotation                                                           |
|     8 | `arg-type`         | Real call-site type mismatch                  | Fix                                                                          |
|     6 | `no-any-return`    | Typed fn returning Any                        | Add `cast()` or narrow the return path                                       |
|     3 | `no-untyped-call`  | Calling untyped fn                            | Annotate the callee if ours, else `# type: ignore[no-untyped-call]`          |
|     3 | `import-not-found` | Optional azure stack                          | Extend library overrides                                                     |
|     2 | `no-untyped-def`   | Missing annotation                            | Annotate                                                                     |
|     2 | `assignment`       | Real type mismatch                            | Fix                                                                          |

## Approach

One PR, category-by-category commits so review stays readable. No architectural changes; each fix is local to its call site.

### Tasks (execution order — easiest first, hardest last)

1. **M1 — name-defined fixes in `models/*`** (26 errors, ~9 files). Add a `if TYPE_CHECKING:` block at the top of each model file importing the related-class names that SQLAlchemy relationships reference. SQLAlchemy still resolves via its class registry at runtime; mypy now has names to resolve. No behavior change.
2. **M2 — type-arg fixes in `models/*`** (most of the 31). Replace bare `dict` / `list` in `Mapped[...]` with concrete params. For model JSONB fields, `dict[str, Any]` is canonical (matches existing PR-touched fields).
3. **M3 — type-arg fixes outside `models/`**. Apply the same treatment to `services/`, `cli.py`, etc. Some sites may need `dict[str, str]` or `list[str]` instead of `dict[str, Any]` — per-site choice.
4. **M4 — unused-ignore cleanup**. 18 errors. Mechanical — `# type: ignore[...]` deletions. Safe because the ignore was making mypy swallow nothing.
5. **M5 — import-not-found**. 3 × azure library imports. Extend `[[tool.mypy.overrides]]` in pyproject.toml with `azure.core.*`, `azure.identity.*`, `azure.keyvault.*`. This also auto-resolves several `attr-defined` on the `object` fallback pattern.
6. **M6 — valid-type fixes**. 8 errors, likely in `services/*` where method references leaked into annotations. Per-site rewrite.
7. **M7 — attr-defined fixes (non-library)**. 11 errors. Split into: (a) real bugs → fix; (b) Cython attrs of Nautilus/Redis → narrow `# type: ignore[attr-defined]` with a one-line comment.
8. **M8 — arg-type + assignment fixes**. 10 errors total. Per-site — usually requires either a `cast()` or a local variable type declaration.
9. **M9 — no-any-return + no-untyped-call + no-untyped-def**. ~11 errors. Add return-type annotations or cast at the return boundary.
10. **M10 — misc**. 10 errors. Final sweep.
11. **M11 — CI hardening**.
    - Remove `continue-on-error: true` from the mypy step in `.github/workflows/ci.yml`.
    - Add `.pre-commit-config.yaml` with `actionlint` (YAML workflow linter) so future `ci.yml` parse bugs are caught locally.
    - Verify CI goes red if I re-introduce a deliberate mypy error; then fix.

### Success criteria

- `uv run mypy src/ --strict` exits 0
- `uv run ruff check src/` still exits 0 (no regression from the noqa churn)
- `uv run pytest tests/` exits 0 (no regression from the `Any`-return cast work)
- CI run on the branch shows backend job green without `continue-on-error`

## E2E use cases

**N/A** — this is internal type-annotation cleanup with zero user-facing behavior change. No runtime code paths are modified. The pytest suite + CI gate provide sufficient regression coverage.

## Risks

1. **A `# type: ignore[...]` removal uncovers a real bug.** Mitigation: when mypy reports a new error after removal, fix the real issue instead of re-adding the ignore.
2. **A `cast()` masks a real type mismatch.** Mitigation: prefer narrowing at the source (e.g., proper return type on the helper) over `cast()`. Only use `cast()` at library boundaries.
3. **TYPE_CHECKING imports introduce circular import structure.** Mitigation: imports inside `if TYPE_CHECKING:` don't execute at runtime, so they can't cause import cycles. But if a referenced class lives in a file that itself imports this model, we have a real dependency loop that would surface in the next `ruff`/test run — fix by restructuring.
4. **Nautilus attr-defined `# type: ignore`s hide real API drift post-upgrade.** Mitigation: add a `# type: ignore[attr-defined] — Nautilus 1.223 API` comment so the next upgrader knows to re-verify.
