# E2E Use Cases — UI Completeness Trader Journeys

**Feature:** ui-completeness
**Branch:** feat/ui-completeness
**Graduated:** 2026-05-17 (after iter-3 PASS — see `tests/e2e/reports/2026-05-17-ui-completeness-iter3-convergence.md`)

The 11 trader-journey UCs originally drafted in `docs/plans/2026-05-16-ui-completeness.md` §R23 (lines 1161+). They cover every UI surface added or restructured by the ui-completeness PR. The original plan section is the source of truth for Intent/Steps/Verification/Persistence; this file is the regression checklist.

## Pre-flight

- `curl -sf http://localhost:8800/health` returns 200
- UI at `http://localhost:3300` responds with 200
- Auth: `X-API-Key: msai-dev-key` (dev API-key user → `role: admin`)
- For TJ-4 (paper deploy) + TJ-6 (real kill-all): IB Gateway must be up via `COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml --env-file .env up -d ib-gateway`; verified via `/api/v1/account/health → gateway_connected: true`

## Use cases

| TJ    | Goal                                                    | Spec file                                            | Smoke? | Touches                                                                          |
| ----- | ------------------------------------------------------- | ---------------------------------------------------- | ------ | -------------------------------------------------------------------------------- |
| TJ-1  | Morning checklist                                       | `frontend/tests/e2e/specs/morning-checklist.spec.ts` | yes    | `/dashboard`, alerts feed, alert detail sheet, `/live-trading`, audit log drawer |
| TJ-2  | Design a strategy                                       | `frontend/tests/e2e/specs/strategy-design.spec.ts`   | yes    | `/strategies`, `/strategies/[id]`                                                |
| TJ-3  | Backtest + review                                       | `frontend/tests/e2e/specs/backtest-flow.spec.ts`     | no     | `/strategies/[id]`, `/backtests`, `/backtests/[id]`                              |
| TJ-4  | Deploy a paper portfolio                                | `frontend/tests/e2e/specs/paper-deploy.spec.ts`      | yes    | `/live-trading/portfolio` compose + snapshot + start dialog → `/live-trading`    |
| TJ-5  | Respond to an alert                                     | `frontend/tests/e2e/specs/alert-response.spec.ts`    | yes    | `/alerts`, detail sheet, header bell                                             |
| TJ-6  | Emergency kill-all                                      | `frontend/tests/e2e/specs/kill-all.spec.ts`          | yes    | `/live-trading` STOP ALL + flatness display                                      |
| TJ-7  | Archive an obsolete strategy                            | `frontend/tests/e2e/specs/strategy-archive.spec.ts`  | no     | `/strategies/[id]` delete dialog with type-name-to-confirm                       |
| TJ-8  | Operator daily check                                    | `frontend/tests/e2e/specs/operator-checkin.spec.ts`  | no     | `/system` page subsystem grid + version + uptime; `/account` 3 tabs              |
| TJ-9  | Honest settings page (role-agnostic per iter-5 Issue B) | `frontend/tests/e2e/specs/settings-honest.spec.ts`   | no     | `/settings` — no fakes, role badge matches `auth/me.role`                        |
| TJ-10 | Cancel a stuck research sweep                           | `frontend/tests/e2e/specs/research-cancel.spec.ts`   | no     | `/research/[id]` cancel CTA + AlertDialog confirm                                |
| TJ-11 | Error recovery (404 + render-throw)                     | `frontend/tests/e2e/specs/error-pages.spec.ts`       | no     | `/this-page-does-not-exist`, `/__e2e_throw`                                      |

## Observed selectors (recorded during iter-3)

See `tests/e2e/reports/2026-05-17-ui-completeness-iter3-convergence.md` "Selectors Observed" section + iter-2 report for the full inventory. Phase 6.2c spec authoring uses these for stable replay.

## Known environmental gates

These UCs cannot run in regression mode against a dev environment without external dependencies:

- TJ-3 polling, TJ-4 deploy, TJ-5 Stop, TJ-10 Cancel — require IB Gateway up + AAPL parquet data
- TJ-11 render-throw — requires container rebuild with `NEXT_PUBLIC_E2E_AUTH_BYPASS=1`

These are FAIL_INFRA carve-outs, not FAIL_BUG, per `.claude/rules/testing.md` failure classification matrix.

## Iter-history

- **iter-1 (2026-05-17 pre-cycle):** FAIL_INFRA cluster — Docker stack was mounted from main-branch path, worktree code wasn't served. Resolved by `docker compose down + up -d --build` from worktree path + `alembic upgrade head`. Also surfaced 6 API contract bugs (5 fixed in-branch + 1 false positive).
- **iter-2 (2026-05-17 post-cycle):** PARTIAL — all 6 iter-1 bugs RESOLVED, 0 FAIL_BUG, 3 P2 product defects discovered: TJ-4 `?onboard=` deep-link, audit drawer Side raw integer, Recent Trades Side raw integer. Fixed in-branch.
- **iter-3 (2026-05-17 convergence):** PASS → SHIP. All 3 iter-2 P2 fixes verified RESOLVED. Zero new silent failures. Branch ready for PR creation.
