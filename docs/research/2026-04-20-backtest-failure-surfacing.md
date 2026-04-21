# Research: backtest-failure-surfacing

**Date:** 2026-04-20
**Feature:** Structured failure envelope (`{code, message, suggested_action, remediation}`) flowing worker → DB → API → CLI → UI for failed backtests.
**Researcher:** research-first agent
**Worktree:** `.worktrees/backtest-failure-surfacing`

---

## Libraries Touched

| Library                        | Our Version                                                           | Latest Stable                        | Breaking Changes                                                                                                                                                     | Source                                                                                                                                                                                                                            |
| ------------------------------ | --------------------------------------------------------------------- | ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| shadcn/ui (CLI)                | `shadcn@^3.8.5` (devDep), tooltip component already committed         | `shadcn@latest` (CLI moniker stable) | None relevant; `shadcn` (not `shadcn-ui`) is the current CLI name                                                                                                    | [shadcn/ui Tooltip docs](https://ui.shadcn.com/docs/components/tooltip) (2026-04-20)                                                                                                                                              |
| `radix-ui` (Tooltip primitive) | `radix-ui@^1.4.3` (mono-package)                                      | `radix-ui@^1.4.x`                    | None; our `tooltip.tsx` already uses `import { Tooltip as TooltipPrimitive } from "radix-ui"` (the new unified package, not per-component `@radix-ui/react-tooltip`) | [Radix Tooltip primitive](https://www.radix-ui.com/primitives/docs/components/tooltip) (2026-04-20)                                                                                                                               |
| Pydantic                       | `pydantic>=2.10.0`                                                    | `pydantic@2.x` latest                | None for the patterns we use; `Literal` discriminator + `model_dump(mode="json")` are the documented idioms                                                          | [Pydantic v2 discriminated unions discussion #3861](https://github.com/pydantic/pydantic/discussions/3861), [Different behaviour Literal vs Enum vs StrEnum #9791](https://github.com/pydantic/pydantic/issues/9791) (2026-04-20) |
| SQLAlchemy                     | `sqlalchemy[asyncio]>=2.0.36`                                         | `sqlalchemy@2.0.x` / `2.1` in alpha  | None; `mapped_column(JSONB, nullable=True)` with `dict` type is the established project pattern                                                                      | [SQLAlchemy Custom Types](https://docs.sqlalchemy.org/en/20/core/custom_types.html), [Mutation Tracking](https://docs.sqlalchemy.org/en/20/orm/extensions/mutable.html) (2026-04-20)                                              |
| Alembic                        | `alembic>=1.14.0`                                                     | `alembic@1.18.x`                     | None; `op.add_column(..., server_default=...)` with `nullable=False` is current recommended single-step for Postgres 11+                                             | [Alembic Ops docs](https://alembic.sqlalchemy.org/en/latest/ops.html), [Postgres 16 ALTER TABLE docs](https://www.postgresql.org/docs/16/sql-altertable.html) (2026-04-20)                                                        |
| Playwright                     | Scaffolded (no version pin in `package.json` yet — installed per-run) | `@playwright/test@1.49+`             | None relevant; `locator.hover()` + `getByRole('tooltip')` is the canonical pattern                                                                                   | [Playwright Locators](https://playwright.dev/docs/locators#locate-by-role), [Test Guild tooltips course](https://courses.testguild.com/course/ui-playwright-tooltips/) (2026-04-20)                                               |
| PostgreSQL                     | 16 (per `CLAUDE.md`)                                                  | 16.x                                 | None; Postgres 11+ fast-path non-rewriting `ADD COLUMN ... DEFAULT const NOT NULL` is exactly what we need                                                           | [Postgres 11 fast ADD COLUMN](https://dataegret.com/2018/03/waiting-for-postgresql-11-pain-free-add-column-with-non-null-defaults/) (2026-04-20)                                                                                  |

---

## Per-Library Analysis

### 1. shadcn/ui Tooltip

**Versions:** CLI `shadcn@^3.8.5` in devDeps; the Tooltip component is **already installed** in-repo at `frontend/src/components/ui/tooltip.tsx` (4 exports: `Tooltip`, `TooltipTrigger`, `TooltipContent`, `TooltipProvider`). We do NOT need to run the install command.

**Install command (if re-adding):** `pnpm dlx shadcn@latest add tooltip` (confirmed current, 2026-04-20).

**Breaking changes since training cutoff:** None. Our existing component is already on the current pattern: it imports from the unified `radix-ui` package (`import { Tooltip as TooltipPrimitive } from "radix-ui"`) rather than the legacy `@radix-ui/react-tooltip`.

**Deprecations:** None relevant.

**Recommended pattern for badge-hover tooltip:**

```tsx
<TooltipProvider>
  <Tooltip>
    <TooltipTrigger asChild>
      <Badge variant="destructive">failed</Badge>
    </TooltipTrigger>
    <TooltipContent side="top">
      {truncate(error_public_message, 150)}
    </TooltipContent>
  </Tooltip>
</TooltipProvider>
```

`TooltipProvider` should wrap the app root (or at least the backtests page) — shadcn docs are explicit about this. It's not in `app/layout.tsx` yet; plan phase needs to confirm where to mount it (single `TooltipProvider` at layout-root is idiomatic).

**Sources:**

1. [shadcn/ui Tooltip docs](https://ui.shadcn.com/docs/components/tooltip) — accessed 2026-04-20
2. Local repo: `frontend/src/components/ui/tooltip.tsx` — current implementation

**Design impact:** **Minor.** (a) No install step needed — Tooltip primitive already in tree. (b) Plan must add `TooltipProvider` mount point (likely `app/layout.tsx` or a scoped provider around `/backtests`). (c) `<TooltipTrigger asChild>` is required when wrapping a `<Badge>` so we don't double-wrap the DOM.

**Test implication:** Playwright spec for US-001 needs `locator.hover()` on the badge, then `expect(page.getByRole('tooltip')).toContainText(...)`. Tooltip appears in a Portal, so role-based lookup is mandatory (it's not a DOM descendant of the badge). See section 5 for the touch-device caveat.

---

### 2. Radix Tooltip — Touch-Device Behavior (critical finding)

**Versions:** `radix-ui@^1.4.3`.

**Breaking changes:** None.

**Deprecations:** None.

**Known behavior (confirmed via multiple GitHub issues, marked "Expected Behaviour" by maintainers):**

> Tooltip does NOT open on tap on mobile / touch devices.

Radix's stance is that `role="tooltip"` is a desktop hover/focus pattern per WAI-ARIA, and touch users should get a different primitive (Popover/HoverCard/toggletip). The issue has been open since 2022 and is not going to change.

**What this means for the PRD:**

The PRD section US-001 says the badge "exposes a short, human-readable reason **on hover**". On a phone, there's no hover. Today, a user tapping the badge on mobile gets **nothing** — no tooltip, no fallback. Since MSAI is a single-user dashboard used primarily on desktop, and the detail page (US-002) already shows the full envelope on click-through, this is **acceptable** but should be called out in the design.

**Sources:**

1. [Radix issue #2589 — Tooltip doesn't react on touch](https://github.com/radix-ui/primitives/issues/2589) — accessed 2026-04-20
2. [Radix issue #1573 — Tooltip does not open/close on mobile (iOS)](https://github.com/radix-ui/primitives/issues/1573) — accessed 2026-04-20
3. [shadcn-ui issue #2402 — Tooltip and HoverCard Mobile Support](https://github.com/shadcn-ui/ui/issues/2402) — accessed 2026-04-20

**Design impact:** **Yes — add one sentence to the plan.** US-001 should explicitly note that on touch devices the tooltip is a no-op and users access failure details by tapping through to `/backtests/[id]` (US-002). If any future requirement says "failure reason must be visible on mobile from the list view," the primitive choice must switch to `Popover` (click/tap-triggered). No code change needed in this PR — Pablo's workflow is desktop.

**Test implication:** Playwright mobile-viewport (`project: { use: { ...devices['iPhone 14'] } }`) specs for US-001 MUST be skipped or marked xfail — the tooltip won't appear. Desktop-viewport specs are the authoritative coverage.

---

### 3. Pydantic v2 — `Literal` vs `Enum` for `Remediation.kind`

**Versions:** `pydantic>=2.10.0`.

**Breaking changes since training cutoff:** None for our use case. Pydantic v2's discriminator semantics have been stable since 2.0; the community open-issues (`#10614`, `#3861`) are about _extending_ discriminator support to `Enum` as a convenience, not about changing `Literal` behavior.

**Deprecations:** None relevant.

**Recommended pattern for MVP + future extension:**

> **Use `Literal["ingest_data", "contact_support", "retry", "none"]` in the Pydantic `Remediation` model, exactly as the PRD specifies.** Do NOT switch to `StrEnum` for the discriminator field.

Reasons:

- **Discriminator compatibility** — Pydantic's `Field(discriminator=...)` requires a `Literal` on each union member. `Enum` is not yet supported as a discriminator despite open requests. If the auto-ingest follow-up PR grows `Remediation` into a discriminated union (e.g., `IngestDataRemediation | ContactSupportRemediation`), the `Literal` stays; swapping to `Enum` would force a refactor.
- **OpenAPI schema** — `Literal[str, ...]` emits JSON Schema `{"enum": [...], "type": "string"}` identically to an empty-method `StrEnum`. Frontend type-codegen (e.g., `openapi-typescript`) produces the same `type Kind = "ingest_data" | "contact_support" | ...` either way.
- **Serialization** — `model_dump(mode="json")` produces plain strings for both `Literal` and `StrEnum` values. No round-trip concern on JSONB.

**When we WOULD want `StrEnum` instead:** if the backend needs to attach methods to the kind (e.g., `kind.is_auto_available()`), or if the same type is a DB column value. `FailureCode` IS a DB column value in `backtests.error_code`, so `FailureCode` should be `StrEnum` (matching the precedent in `backend/src/msai/services/live/failure_kind.py`). `Remediation.kind` is purely in-JSONB and doesn't benefit.

**Sources:**

1. [Pydantic discussion #3861 — Support enums for discriminator union discriminator](https://github.com/pydantic/pydantic/discussions/3861) — accessed 2026-04-20
2. [Pydantic issue #10614 — Allow Union Discriminator to work with Enum](https://github.com/pydantic/pydantic/issues/10614) — accessed 2026-04-20
3. [Pydantic issue #9791 — Different behaviour Literal vs Enum vs StrEnum](https://github.com/pydantic/pydantic/issues/9791) — accessed 2026-04-20

**Design impact:** **Confirms PRD — no change.** `Remediation.kind: Literal[...]` is correct. Keep `FailureCode` as `StrEnum` (stored in the `String(32)` column). This split (Enum for persisted scalar, Literal for in-JSONB discriminator) matches existing project precedent (`live/failure_kind.py`'s `FailureKind(StrEnum)`).

**Test implication:** Unit test must cover round-trip `model_dump(mode="json") → JSONB → model_validate(...)` for each of the four `kind` values. OpenAPI contract test should assert the generated schema contains `"enum": ["ingest_data", "contact_support", "retry", "none"]` for the `kind` property.

---

### 4. Pydantic v2 + SQLAlchemy 2.0 JSONB serialization

**Versions:** `pydantic>=2.10.0`, `sqlalchemy[asyncio]>=2.0.36`.

**Breaking changes:** None.

**Deprecations:** None.

**Current best practice — two valid patterns:**

| Pattern                                                                                                      | Pros                                                                                                                                                                                         | Cons                                                                                                                                     |
| ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| **A: Plain `dict` column + manual `.model_dump(mode="json")` at write-time** (our current project precedent) | Zero magic; matches every existing JSONB column in the codebase (`backtests.config`, `research_jobs.config`, `instrument_cache.ib_contract_json`, etc.); explicit at call sites; simple mypy | Each writer must remember to call `.model_dump(mode="json")`; no automatic Pydantic re-hydration on read                                 |
| **B: `TypeDecorator` wrapping `JSONB` with `process_bind_param` / `process_result_value`**                   | Automatic Python-side typing (read returns `Remediation` not `dict`); encapsulated serialization                                                                                             | New pattern not used anywhere else in this repo; mutation-tracking gotcha (MutableDict doesn't track deep changes); adds mypy complexity |

**Recommendation for THIS PR: Pattern A (plain `dict` + manual `.model_dump()`)** to match the project's dominant pattern. A custom `TypeDecorator` would be the first in this codebase and would set a precedent requiring justification for why `error_remediation` deserves a different abstraction than `config`, `metrics`, `data_snapshot`, and 10+ other JSONB columns.

**Mutation-tracking gotcha (applies to BOTH patterns):** SQLAlchemy does NOT detect in-place mutation of a JSONB `dict` — you must reassign the column for the ORM to flush an UPDATE. Our worker path writes `error_remediation` once at failure-classification time (no in-place updates), so this doesn't affect US-005.

**Sources:**

1. [SQLAlchemy Custom Types — TypeDecorator](https://docs.sqlalchemy.org/en/20/core/custom_types.html) — accessed 2026-04-20
2. [SQLAlchemy Mutation Tracking / MutableDict](https://docs.sqlalchemy.org/en/20/orm/extensions/mutable.html) — accessed 2026-04-20
3. [Roman Imankulov — Pydantic in SQLAlchemy fields](https://roman.pt/posts/pydantic-in-sqlalchemy-fields/) — accessed 2026-04-20
4. Local precedent: `backend/src/msai/models/backtest.py:36,42,60` — all JSONB columns use plain `dict`/`list[dict]`

**Design impact:** **Yes — lock in Pattern A** in the plan. The model column is `error_remediation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)`. Writer code converts via `Remediation(...).model_dump(mode="json")`. API-layer re-validation uses `Remediation.model_validate(row.error_remediation)`.

**Test implication:** One integration test that writes a `Remediation` via the worker, reads the row from Postgres, re-validates with `Remediation.model_validate(...)`, and asserts field equivalence. Catches any silent type drift (e.g., `date` objects going in and coming back as ISO strings requiring `mode="json"` on the write).

---

### 5. Alembic — adding `NOT NULL DEFAULT 'unknown'` to non-empty `backtests` table

**Versions:** `alembic>=1.14.0`, PostgreSQL 16.

**Breaking changes:** None.

**Deprecations:** None.

**Current best practice for Postgres 11+ (includes 16):**

> **Single-step `op.add_column` with `server_default` + `nullable=False` is safe and fast** for the specific case of a constant, non-volatile default.

Mechanism: Postgres 11+ stores the default in `pg_attribute.attmissingval` and reads it on-the-fly for pre-existing rows, avoiding a full table rewrite. The `NOT NULL` constraint addition scans the table (to verify no nulls) but does not rewrite it. For `backtests` at MSAI's scale (single-user, thousands of rows), this is milliseconds.

**Recommended migration shape:**

```python
def upgrade() -> None:
    # Fast non-rewriting add on PG 11+
    op.add_column(
        "backtests",
        sa.Column(
            "error_code",
            sa.String(32),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column("backtests", sa.Column("error_public_message", sa.Text(), nullable=True))
    op.add_column("backtests", sa.Column("error_suggested_action", sa.Text(), nullable=True))
    op.add_column(
        "backtests",
        sa.Column("error_remediation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # Optional: backfill public_message from the old error_message for pre-existing failed rows.
    # Leave error_code at the 'unknown' default — that IS the backfill for those rows.
    op.execute(
        """
        UPDATE backtests
        SET error_public_message = error_message
        WHERE status = 'failed' AND error_message IS NOT NULL
        """
    )
```

**Three-step pattern (add-nullable → backfill → alter-not-null) is NOT required here** because:

- `'unknown'` is a constant non-volatile literal — Postgres fast-path applies.
- `error_code` has a meaningful default for every existing row (PRD US-006: "historical failed rows read as `error_code = 'unknown'`").
- Pre-existing `status != 'failed'` rows also get `'unknown'` — semantically harmless because the API only exposes `error_code` on `status === 'failed'`.

**When you WOULD need three-step:** if the default depended on another column (e.g., `error_code = CASE status WHEN 'failed' THEN ... ELSE NULL`). We don't; one-shot is fine.

**`server_default` vs `default` note:** Use `server_default="unknown"` (DDL-level, applies to existing + new rows at the DB). The SQLAlchemy Python-side `default=` does NOT backfill existing rows and does NOT emit a DDL default. PRD specifies the DDL-level behavior, so `server_default` is correct.

**Sources:**

1. [Postgres 16 ALTER TABLE docs](https://www.postgresql.org/docs/16/sql-altertable.html) — accessed 2026-04-20
2. [Data Egret — Postgres 11 fast ADD COLUMN with non-NULL defaults](https://dataegret.com/2018/03/waiting-for-postgresql-11-pain-free-add-column-with-non-null-defaults/) — accessed 2026-04-20
3. [Alembic Ops docs](https://alembic.sqlalchemy.org/en/latest/ops.html) — accessed 2026-04-20
4. [Ross Gray — Alembic migrations involving PostgreSQL column default values](https://www.rossgray.co.uk/posts/alembic-migrations-involving-postgresql-column-default-values/) — accessed 2026-04-20

**Design impact:** **Confirms PRD section 6 — no change**, with one caveat: after the `add_column(..., server_default='unknown', nullable=False)` lands, the design should NOT then call `op.alter_column('backtests', 'error_code', server_default=None)` to drop the default. Keeping the DB-level default means any future INSERT that forgets `error_code` lands as `'unknown'` rather than raising — which aligns with the PRD's "writer MUST enforce FailureCode membership" policy at the application layer without making the DB hostile.

**Test implication:** Alembic migration test (upgrade + downgrade round-trip) against a test DB seeded with at least one pre-existing `status='failed'` row with `error_message` populated but no `error_code` column. Assert post-upgrade: `error_code == 'unknown'`, `error_public_message == <original error_message>`. This is the proof for US-006.

---

### 6. Playwright — asserting Tooltip visibility deterministically

**Versions:** Playwright scaffold in `tests/e2e/`; `package.json` doesn't pin `@playwright/test` yet.

**Breaking changes:** None relevant.

**Deprecations:** None.

**Recommended pattern:**

```typescript
// Desktop spec for US-001
await page.goto("/backtests");
const failedRow = page.getByRole("row").filter({ hasText: "failed" }).first();
const badge = failedRow.getByTestId("status-badge"); // add data-testid in the UI
await badge.hover();
const tooltip = page.getByRole("tooltip");
await expect(tooltip).toBeVisible();
await expect(tooltip).toContainText(/No raw Parquet files/); // or whatever the test-data message is
```

**Why this works deterministically:**

- `locator.hover()` auto-waits for the badge to be actionable (visible + stable + receiving events).
- `getByRole('tooltip')` matches Radix's Portal-rendered `[role="tooltip"]` node — Radix sets this role on `TooltipContent` per ARIA spec.
- `expect(...).toBeVisible()` auto-retries until the tooltip's entry animation completes (our component uses `animate-in fade-in-0 zoom-in-95`, ~150ms).
- `delayDuration={0}` is set in our `TooltipProvider` wrapper — the tooltip opens immediately on hover with no delay, removing the main source of flakiness.

**Gotchas to avoid:**

- **Don't** use `.locator('.tooltip-class')` — tooltip renders in a Portal, so CSS-class selectors are fragile and break shadcn theming changes.
- **Don't** `await page.waitForTimeout(200)` — use the built-in `toBeVisible()` auto-retry instead.
- **Don't** assert from a **mobile viewport project** — see section 2; the tooltip won't render on touch. Gate mobile-viewport specs with `test.skip(isMobileViewport, 'Tooltip is desktop-only per Radix spec')`.

**Second flakiness source: `TooltipProvider` not mounted.** If the plan puts `TooltipProvider` scoped to one page and the spec navigates somewhere it isn't mounted, `role="tooltip"` will never appear. Guard with a component-level test that asserts the provider is in the tree.

**Sources:**

1. [Playwright Locators — locate by role](https://playwright.dev/docs/locators#locate-by-role) — accessed 2026-04-20
2. [Test Guild — testing tooltips in Playwright](https://courses.testguild.com/course/ui-playwright-tooltips/) — accessed 2026-04-20
3. [Playwright best practices — auto-waiting locators](https://playwright.dev/docs/best-practices) — accessed 2026-04-20

**Design impact:** **Minor.** (a) Add `data-testid="status-badge"` to the row badge so specs have a stable locator. (b) Keep `delayDuration={0}` on `TooltipProvider` (our tooltip.tsx already does this). (c) Gate any mobile-viewport E2E coverage with `skip` + explicit note.

**Test implication:** US-001's E2E spec uses `hover()` + `getByRole('tooltip')`. Add ONE component test that asserts `TooltipProvider` is mounted at the app root (catches the "provider missing" class of flake before it hits CI).

---

### 7. Python `StrEnum` in Pydantic v2 with SQLAlchemy `String` column

**Versions:** Python 3.12 (`StrEnum` in stdlib since 3.11), `pydantic>=2.10.0`, `sqlalchemy>=2.0.36`.

**Breaking changes:** None.

**Deprecations:** None.

**Ser/de round-trip (confirmed from docs + Pydantic issue #9791):**

| Direction                                                     | Pydantic v2 behavior                                                                    |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `StrEnum.MISSING_DATA` → `model_dump()`                       | Returns the `StrEnum` instance (Python mode preserves type)                             |
| `StrEnum.MISSING_DATA` → `model_dump(mode="json")`            | Returns the string value `"missing_data"`                                               |
| `"missing_data"` → `model_validate({"code": "missing_data"})` | Coerces to `FailureCode.MISSING_DATA` correctly                                         |
| SQLAlchemy `String(32)` column + `mapped_column(String(32))`  | Accepts both `str` and `StrEnum` (StrEnum IS a str subclass); reads back as plain `str` |

**OpenAPI schema difference:**

- `code: FailureCode` → OpenAPI emits a named schema component `FailureCode` with `enum: [...]` and `type: string`. Produces a clean TypeScript `type FailureCode = "missing_data" | ...` via `openapi-typescript`.
- `code: Literal["missing_data", ...]` → emits an inline `{"enum": [...], "type": "string"}` with no named component. Also produces a TS union type but not named.

For `FailureCode` (reusable across `ErrorEnvelope`, `BacktestListItem.error_code`, and future research/live envelopes per the PRD non-goals list), **a named `StrEnum` is preferable** — it gives the frontend a single importable type name rather than inlined union literals at every usage site.

**Round-trip gotcha:** When SQLAlchemy reads the `String(32)` column, the Python value is a plain `str`, not a `FailureCode`. If API code does `return ErrorEnvelope(code=row.error_code, ...)`, Pydantic coerces the plain string to `FailureCode` correctly (StrEnum values are hashable by string value). **But** if the DB has a historical value that's not in the enum, `Pydantic` raises `ValidationError` unless the API field type is `str` or the model uses `model_config = ConfigDict(use_enum_values=True)`. PRD US-006 mandates "unknown values map to `FailureCode.UNKNOWN`" — the project's existing `FailureKind.parse_or_unknown(value)` classmethod pattern (in `live/failure_kind.py`) is the tested precedent; **replicate it on `FailureCode`**.

**Sources:**

1. [Pydantic Standard Library Types](https://docs.pydantic.dev/latest/api/standard_library_types/) — accessed 2026-04-20
2. [Pydantic issue #9791 — Different behaviour Literal vs Enum vs StrEnum](https://github.com/pydantic/pydantic/issues/9791) — accessed 2026-04-20
3. Local precedent: `backend/src/msai/services/live/failure_kind.py` (`FailureKind(StrEnum)` + `parse_or_unknown` pattern)

**Design impact:** **Yes — two concrete decisions for the plan:**

1. Define `FailureCode(StrEnum)` in `backend/src/msai/schemas/backtest.py` (or a sibling module), with values exactly matching the PRD list. Add a `parse_or_unknown(value: str | None) -> FailureCode` classmethod copying the `FailureKind` pattern — required for US-006 null-safe historical read.
2. `ErrorEnvelope.code: FailureCode` (NOT `str`) so the OpenAPI schema emits a named component. Frontend consumes this as `type FailureCode = ...`.

**Test implication:** Unit test `FailureCode.parse_or_unknown` for (a) every valid value, (b) `None`, (c) a made-up string (must map to `UNKNOWN`, not raise). Matches the existing `test_failure_kind_parse_or_unknown` precedent.

---

## Not Researched (with justification)

- **Tailwind v4** — no new utility classes needed; the existing `tooltip.tsx` styles work.
- **Next.js 15** — no new routing, data-fetching, or RSC/Client-Component patterns involved. The failure card is a pure client-component addition to an existing `/backtests/[id]/page.tsx` route.
- **React 19** — no new hooks or concurrent features needed for this PR.
- **FastAPI** — no new router patterns; adding an optional field to an existing Pydantic response model is standard.
- **lightweight-charts, recharts, lucide-react, msal-\*, tw-animate-css** — not touched by this feature.
- **NautilusTrader / ib_async / arq / databento** — not touched; failure classification runs inside the existing `_mark_backtest_failed` call-site and does not change worker-level semantics.
- **Python `StrEnum` stdlib behavior** — stable since 3.11; nothing changed in 3.12.

---

## Open Risks

1. **`TooltipProvider` mount point is not yet decided.** If it's scoped too narrowly (single component), the US-001 tooltip won't render. If mounted at `app/layout.tsx`, one global provider covers every route and costs ~nothing. **Recommendation: mount at the root layout.** Plan-phase decision; low-risk either way but worth explicit.

2. **Touch-device UX gap on US-001.** The PRD says "hover" explicitly, which is fine — but a mobile user tapping a failed badge on the history list sees nothing and has to tap through. Not a bug per the PRD, but call it out in the plan so no reviewer files it as a defect later.

3. **OpenAPI schema emission for `FailureCode` depends on whether the frontend has a type-codegen step.** Audit whether `frontend/src/lib` has an auto-generated API-client types file. If not, `ErrorEnvelope.code` can still be typed as `FailureCode` on the backend, but the frontend will need a hand-maintained `type FailureCode = "..." | ...`. **Action: plan phase should `grep -r "openapi" frontend/` to confirm codegen presence.**

4. **Data-migration ordering under dev-container hot reload.** PRD says run backfill inside the Alembic upgrade. If the dev-stack is running and the migration is applied live, between `add_column` and the `UPDATE ... SET error_public_message = error_message` the backend API has a moment where it reads `error_public_message = NULL` for failed rows. This is harmless (API layer falls back to `error_code='unknown'` per US-006) but means the plan should run the migration during a quiet window OR sequence the UPDATE inside the same transaction (Alembic does this by default — verify `transaction_per_migration = True` in `alembic.ini`).

5. **`Remediation.auto_available` is always `false` in this PR.** Safe per PRD, but if the subsequent auto-ingest PR ships before MSAI adds a flag-rollout mechanism, there's no way to canary `true` for some users vs others. Single-user system, so this is theoretical — flag for the auto-ingest PR's research brief.

6. **No Playwright version pin in `frontend/package.json`.** The top-level scaffold uses `pnpm exec playwright test`, but without `@playwright/test` in dependencies the version is whatever the system installed. For the US-001/US-002 specs to be reproducible in CI, the plan should add `@playwright/test` as a devDep and pin to a specific version (≥ 1.49).

---

## Summary of Design-Changing Findings

| #   | Finding                                                                                                   | Plan Action                                                        |
| --- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| 1   | Tooltip primitive already installed — no CLI install step                                                 | Skip install; decide `TooltipProvider` mount point                 |
| 2   | Radix Tooltip has no touch-device support (by design)                                                     | Add explicit note to US-001; gate mobile specs with `test.skip`    |
| 3   | `Literal` is correct for `Remediation.kind`; `StrEnum` is correct for `FailureCode`                       | Confirm PRD; add `FailureCode.parse_or_unknown` classmethod        |
| 4   | Project's dominant pattern is plain `dict` + manual `.model_dump(mode="json")`, not `TypeDecorator`       | Use Pattern A; store `error_remediation` as `Mapped[dict \| None]` |
| 5   | Postgres 11+ fast-path makes single-step `add_column(..., server_default='unknown', nullable=False)` safe | Skip 3-step pattern; do it in one Alembic op                       |
| 6   | Playwright `hover()` + `getByRole('tooltip')` is the deterministic pattern                                | Add `data-testid="status-badge"`; keep `delayDuration={0}`         |
| 7   | Name `FailureCode` as an OpenAPI component via StrEnum typing                                             | Type `ErrorEnvelope.code: FailureCode` (not `str`)                 |

---

## Sources Index (de-duplicated)

- [shadcn/ui Tooltip docs](https://ui.shadcn.com/docs/components/tooltip) — 2026-04-20
- [Radix Primitives Tooltip](https://www.radix-ui.com/primitives/docs/components/tooltip) — 2026-04-20
- [Radix issue #2589 — Tooltip doesn't react on touch](https://github.com/radix-ui/primitives/issues/2589) — 2026-04-20
- [Radix issue #1573 — Tooltip does not open/close on mobile (iOS)](https://github.com/radix-ui/primitives/issues/1573) — 2026-04-20
- [shadcn-ui issue #2402 — Tooltip and HoverCard Mobile Support](https://github.com/shadcn-ui/ui/issues/2402) — 2026-04-20
- [Pydantic discussion #3861 — Enum discriminator support](https://github.com/pydantic/pydantic/discussions/3861) — 2026-04-20
- [Pydantic issue #10614 — Allow Enum discriminator](https://github.com/pydantic/pydantic/issues/10614) — 2026-04-20
- [Pydantic issue #9791 — Literal vs Enum vs StrEnum behavior](https://github.com/pydantic/pydantic/issues/9791) — 2026-04-20
- [Pydantic Standard Library Types](https://docs.pydantic.dev/latest/api/standard_library_types/) — 2026-04-20
- [SQLAlchemy Custom Types](https://docs.sqlalchemy.org/en/20/core/custom_types.html) — 2026-04-20
- [SQLAlchemy Mutation Tracking](https://docs.sqlalchemy.org/en/20/orm/extensions/mutable.html) — 2026-04-20
- [Roman Imankulov — Pydantic in SQLAlchemy fields](https://roman.pt/posts/pydantic-in-sqlalchemy-fields/) — 2026-04-20
- [Alembic Ops docs](https://alembic.sqlalchemy.org/en/latest/ops.html) — 2026-04-20
- [Data Egret — Postgres 11 fast ADD COLUMN](https://dataegret.com/2018/03/waiting-for-postgresql-11-pain-free-add-column-with-non-null-defaults/) — 2026-04-20
- [Postgres 16 ALTER TABLE docs](https://www.postgresql.org/docs/16/sql-altertable.html) — 2026-04-20
- [Ross Gray — Alembic PG default values](https://www.rossgray.co.uk/posts/alembic-migrations-involving-postgresql-column-default-values/) — 2026-04-20
- [Playwright Locators](https://playwright.dev/docs/locators#locate-by-role) — 2026-04-20
- [Playwright Best Practices](https://playwright.dev/docs/best-practices) — 2026-04-20
- [Test Guild — Playwright Tooltips](https://courses.testguild.com/course/ui-playwright-tooltips/) — 2026-04-20
