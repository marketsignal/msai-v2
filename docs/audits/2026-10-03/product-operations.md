# Product, access control, and operations assessment

Date: 2026-10-03. Source revision: `7f9eb2b5ad252bc3ba730f53a159ca04046278eb`.

Assessment only. No application changes, deployments, broker sessions, migrations, credential reads, or dependency installs were performed. Source references below are relative to the repository root; line numbers refer to this revision. Tests establish only the behavior they exercised. Azure configuration, branch protection, production workload, live Entra login, and browser journeys were not verified.

## Overall assessment

The product already has substantial API, CLI, and dashboard functionality. It fits a technically operated personal research platform better than its old “first real backtest” description suggests. Retain the API-first architecture, Python strategy files, PostgreSQL/Parquet separation, and existing workflow surfaces. The main gaps for a few partners are access policy, consistent user identity across REST and WebSockets, trustworthy P&L semantics, and reproducible release validation. These need explicit completion before treating it as a shared investment operations product.

The following are source-proven behavior or configuration gaps, not proof of a production incident. Priority is relative to the intended partner platform, not an assertion that an unauthorized person currently has access.

## Functionality map

| Capability | Implemented source surface | Assessment |
| --- | --- | --- |
| Identity | Entra RS256 issuer/audience/expiry/scope/version validation; API-key alternative; MSAL browser redirect and silent access-token acquisition (`backend/src/msai/core/auth.py:41-89,106-164`; `frontend/src/lib/auth.ts:61-80`; `frontend/src/lib/msal-config.ts:42-48`) | Real authentication; authorization remains substantially single-operator. |
| Strategy registry | API list/detail/edit/validate/delete (`backend/src/msai/api/strategies.py:41-43,77-80,137-141,210-213,249-252`); CLI list/show/validate/edit/delete (`backend/src/msai/cli.py:416-442,2655-2693`); strategies pages linked in sidebar (`frontend/src/components/layout/sidebar.tsx:34-51`) | Consistent with git-managed Python source. No need to introduce a visual strategy language. |
| Backtests | Submit, history, status, results, trades, signed report flow (`backend/src/msai/api/backtests.py:254-267,437-480,697-705,730-733,801-806,877-883,927-931`); CLI run/history/show/report/trades (`backend/src/msai/cli.py:451-507,2834-2863`) | Substantial API/CLI surface exists; execution correctness belongs to the research/backtest assessment. |
| Research | Sweeps, walk-forward, job list/detail/cancel/promote (`backend/src/msai/api/research.py:52-55,94-101,144-148,172-175,206-209,246-253`); browser launch form actually submits both research modes (`frontend/src/components/research/launch-form.tsx:98-144`); matching CLI sweep/walk-forward/promote (`backend/src/msai/cli.py:2775-2810`) | Real user flow, with JSON configuration still appropriate for technical users. |
| Graduation / portfolio | Candidate stages/transitions (`backend/src/msai/api/graduation.py:45-46,72-77,132-136,184-188`); portfolio creation, runs, cancellation, promotion to live (`backend/src/msai/api/portfolio.py:72-77,113-136,335-340,482-487,588-593`) | Useful workflow coverage. A full browser journey was not run. |
| Live operations | API portfolio start, stop, halt/resume, positions/trades; CLI maps most operations through HTTP (`backend/src/msai/cli.py:654-672,764-771,1048-1094`); UI kill-all invokes API and renders failures (`frontend/src/components/live/kill-switch.tsx:56-68`) | Shared operating controls exist; permission boundaries need work. |
| Dashboard | Independent queries for strategy count, gateway account summary, active deployments, alerts, recent trades (`frontend/src/app/dashboard/page.tsx:35-68,148-168`) | Good explicit error states; P&L label is wrong; a historical live equity series is not wired here. |
| Broker accounts | API create/list/detail/edit/credential rotation/archive (`backend/src/msai/api/broker_accounts.py:88-109,141-161,183-229,254`); CLI add/list/show/rotate/archive (`backend/src/msai/cli.py:1301-1432`); UI wizard test verifies persistence and secret masking (`frontend/tests/e2e/specs/broker-accounts.spec.ts:28-79`) | Practical operator surface; all authenticated principals presently share privileges. |
| Health | `/health` liveness, `/ready` database connectivity; protected `/system/health` aggregates API/DB/Redis/IB/workers/Parquet (`backend/src/msai/main.py:546-572`; `backend/src/msai/api/system.py:324-353`) | Distinguish process health from trading readiness. |

Static inventory found 24 frontend page files and seven Playwright spec files. This is breadth, not journey coverage or proof those pages function against today's backend.

## Findings and smallest useful changes

### PO-01 — High: roles are displayed but do not constrain operations

`User` describes `admin`, `trader`, and read-only `viewer` (`backend/src/msai/models/user.py:16-19,28`). First `/auth/me` creates a viewer (`backend/src/msai/api/auth.py:48-58`); the general identity resolver instead creates an `operator` (`backend/src/msai/core/auth.py:218-229`). The role shown in Settings therefore depends on which flow provisioned the user (`frontend/src/app/settings/page.tsx:123-133`).

Authentication returns validated JWT claims without resolving or enforcing the persisted role (`backend/src/msai/core/auth.py:125-164`). Portfolio deployment and broker-account creation require only that dependency (`backend/src/msai/api/live.py:1150-1158`; `backend/src/msai/api/broker_accounts.py:88-109`). The API-key route uses one shared synthetic identity for every holder (`backend/src/msai/core/auth.py:27-31,133-135`), and the CLI authenticates only with that shared key (`backend/src/msai/cli.py:146-151`). Searches across API/core found no role enforcement helper.

**Implication:** a valid tenant user with the delegated API scope has operational capabilities even if the UI labels them “viewer.” Entra issuer and audience restrictions are real; whether app assignment currently limits who can obtain that scope was not inspected. This is not anonymous access, but it is not read-only partner access either. A shared CLI key also loses individual attribution/revocation.

**Recommendation:** define the small actual policy needed: owner/operator can mutate trading and broker configuration; partner reader can inspect. Enforce it in API dependencies, make user provisioning use one role vocabulary, and choose named CLI credentials before giving CLI access to partners. If all partners deliberately share full control, document that fact and remove misleading role claims instead of implementing speculative tenant infrastructure.

**Test evidence:** auth tests cover valid/invalid token contracts, not trading permissions (`backend/tests/integration/auth_gate/test_auth_contract.py:64-120,125-185`); the common test fixture overrides `get_current_user` (`backend/tests/conftest.py:49-60`). The dedicated auth gate correctly removes that override (`backend/tests/integration/auth_gate/conftest.py:103-107`). The focused auth gate passed, which does not prove RBAC.

### PO-02 — Medium: REST and WebSocket collaboration policies disagree

REST positions intentionally return all operators' active deployments (`backend/src/msai/api/live.py:3906-3916`), while WebSockets admit only the initiating JWT user, or anyone using the shared API key (`backend/src/msai/api/websocket.py:279-310,335-349`). Thus a partner who can see and operate a deployment over REST may be denied its live stream. The browser reconnects on every close, including authorization failure, without distinguishing terminal auth errors (`frontend/src/lib/use-live-stream.ts:251-258,278-295`).

**Recommendation:** apply the same partner visibility policy to REST and WebSockets; surface terminal authorization failures rather than indefinitely reconnecting. Test two named users through both transports. Existing WebSocket unit coverage targets authentication, snapshot formatting, and forwarding (`backend/tests/unit/test_websocket_live_stream.py:4-16,65-95`); that is not proof of multi-user parity.

### PO-03 — Medium: “Daily P&L” is unrealized P&L; live performance history is absent from the dashboard

The card assigns `dailyPnl = accountData?.unrealized_pnl` then renders it as “Daily P&L” (`frontend/src/components/dashboard/portfolio-summary.tsx:127-128,154-168`). The backend field is mapped directly from IB `UnrealizedPnL` (`backend/src/msai/services/ib_account_snapshot.py:102-118`). No daily-period calculation occurs along this path. Unrealized P&L and daily gain/loss answer different questions.

The dashboard renders account totals, strategy state, alerts, and recent trades; its earlier empty equity chart has been removed (`frontend/src/app/dashboard/page.tsx:148-168`). `EquityChart` still exists as a generic component, but this dashboard does not fetch a live historical equity series. Account balance cards remain tied to the connected gateway account rather than the global account selector; the code correctly labels that account explicitly (`frontend/src/app/dashboard/page.tsx:70-86,155`). This is honest current-account monitoring, not a multi-account performance ledger.

**Recommendation:** immediately use the correct “Unrealized P&L” label, then define the desired daily and total return series, account scope, cash-flow adjustment, fees, and timestamps before implementing historical performance. Do not infer investment performance from positions alone.

**Test evidence:** the morning checklist asserts only that “Daily P&L” is visible (`frontend/tests/e2e/specs/morning-checklist.spec.ts:18-28`), so it would preserve the wrong label without validating its meaning. Alerts and audit-drawer assertions can skip when no rows are seeded (`:39-59`).

### PO-04 — High: image deployment is not explicitly gated by CI/auth success for the same revision

CI runs independently on push and pull request (`.github/workflows/ci.yml:1-5`) and owns Ruff, mypy, pytest, frontend lint/build (`:36-43,57-59`). Image building also triggers directly on main push (`.github/workflows/build-and-push.yml:8-11`). Deployment listens only for successful `Build and Push Images` completion (`.github/workflows/deploy.yml:26-30`) and depends on its optional preflight plus that image workflow result (`:276-290`). No CI or auth-gate result lookup is present in this chain.

**Implication:** main image builds/deployment can race or outlive failed CI for the same push. Protected merge policy could reduce exposure, but it was not verified and is not an explicit deploy invariant. The deploy flow does correctly pin checkout/image resolution to the upstream build revision (`.github/workflows/deploy.yml:300-360`).

**Recommendation:** require successful owning CI/auth results for the exact candidate revision before promoting images. Keep the existing deploy serialization and revision pinning; do not add a new orchestration platform.

### PO-05 — Medium: browser regression coverage is small, bypass-based, and not run by normal CI

Seven specs exist, but the frontend CI job performs lint/build only (`.github/workflows/ci.yml:45-59`). Playwright uses `TEST_API_KEY` plus UI auth bypass (`frontend/playwright.config.ts:35-44,103-110`), so those tests cannot establish actual MSAL login/consent/refresh behavior. Its server command is still `pnpm dev -- --port 3300` (`:104`); the broker-account spec records the resulting Next argument-parsing startup failure (`frontend/tests/e2e/specs/broker-accounts.spec.ts:18-23`). That reported command defect was not rerun because frontend dependencies are absent.

**Recommendation:** fix the runner command, run a bounded deterministic API→CLI→browser journey in CI, and retain a distinct scheduled/operator Entra login check. Prioritize backtest submit/results/report, partner permissions, and live state display; do not start broker trading in unattended UI CI. Code-side identity drift tests already exist and passed, but they do not inspect portal configuration.

### PO-06 — High at incompatible migration boundaries: rollback restores images, not schema/data

Production migrates before backend/workers start (`docker-compose.prod.yml:94-119,208-214`). Automatic rollback restores prior image environment and reruns Compose (`scripts/deploy-on-vm.sh:322-326`), with no schema reversal or restore. Historical migrations include table deletion (`backend/alembic/versions/e2f3g4h5i6j7_drop_instrument_cache.py:299`) and deletion of five deployment columns (`backend/alembic/versions/s7n8o9p0q1r2_drop_legacy_deployment_columns.py:51-56`). The cache migration explicitly says downgrade recreates an empty table and does not restore data (`:303-308`).

**Implication:** arbitrary old-SHA rollback is not necessarily compatible with the current DB. This does not mean every rollback fails. It is a limitation at destructive/data-contract boundaries, despite the additive-migrations policy described in deployment documentation.

**Recommendation:** classify migration boundaries and certify the previous-image compatibility window; prefer expand/contract releases, and require an actual restore plan for the incompatible boundaries. The AST graph is coherent (48 revisions, one head, no missing parents), but no database migration or restoration was executed in this audit. Existing tests use isolated containers and execute real migrations (`backend/tests/integration/test_alembic_migrations.py:46-76`); those tests were deliberately not run here.

### PO-07 — Medium: Redis state durability and telemetry aggregation are incomplete at deployment level

The production Redis service has no declared persistent volume or AOF setting (`docker-compose.prod.yml:80-92`). Container recreation can discard queue/stream/cache state; the live-safety assessment must determine precisely which trading authority depends on it. PostgreSQL persistence does not automatically make Redis stream state durable.

The custom metrics registry is explicitly a per-process singleton (`backend/src/msai/services/observability/metrics.py:321-334`) while production Uvicorn starts two processes (`backend/Dockerfile:51`) and background/supervisor workloads run separately. Broker-account counters use that local registry (`backend/src/msai/services/observability/broker_account_metrics.py:23-30`). A scrape of one API process cannot aggregate those independent counters. The Redis-hydrated feed-health gauges in `/metrics` are a useful exception (`backend/src/msai/main.py:513-534`).

`/metrics` is intentionally unauthenticated at the API, but production binds backend to loopback and Caddy does not forward `/metrics` to it (`docker-compose.prod.yml:123-128`; `Caddyfile:25-43`). Do not report this as a proven public metrics leak. No application metrics scraper is declared in the inspected production Compose/IaC. Azure's availability test targets `/health`, not DB/worker/broker readiness (`infra/alerts.bicep:98-118`), although the protected system-health API covers more subsystems.

**Recommendation:** persist any Redis state required for recovery, then explicitly test restart recovery. Export/scrape each relevant process or use a small central aggregation scheme. Alert on trading-critical data freshness, worker progress, and broker state in addition to liveness. Existing metrics tests verify one in-process registry (`backend/tests/unit/test_metrics.py:17-34,47-66`), not multi-process collection.

### PO-08 — High: deploy refusal misses deployments that are still stopping

The deployment gate calls `/api/v1/live/status?active_only=true` (`.github/workflows/deploy.yml:444`). That endpoint filters to `starting`, `building`, `ready`, and `running`, excluding `stopping` (`backend/src/msai/api/live.py:3718-3720`). The workflow independently repeats the same omission in its `jq` predicate (`.github/workflows/deploy.yml:464-473`). There is no separate SQL query or broader stopping-state check in this gate.

The project's authoritative broker-account active set includes `stopping` specifically because teardown can still hold IB positions (`backend/src/msai/services/live/broker_account_service.py:68-79`). Consequently, a fleet containing only stopping deployments passes the deployment refusal gate while teardown may still be in progress. Routine deployment leaves broker-profile containers running, but changes their shared API, database, Redis, and worker environment (`scripts/deploy-on-vm.sh:50-54`).

**Recommendation:** reuse one authoritative active-lifecycle contract across status, deployment refusal, archival, and startup checks; add a regression with only a stopping deployment. The offline source-derived check parsed the actual API filter and ran the exact workflow `jq` expression against a synthetic stopping row: the API filter excludes it and `active_ids` is empty. No real deployment or trading state was touched.

## Operational strengths and limits

- Backend runs as a non-root user and prepares writable data directories (`backend/Dockerfile:30-49`). Frontend runs from a standalone Next artifact but has no explicit non-root `USER` instruction (`frontend/Dockerfile:46-53`); small hardening opportunity, not evidence of an exploit.
- Production pins required frontend identity/API build arguments and fails empty configuration (`frontend/Dockerfile:23-44`). The browser's generic API helper will use a `NEXT_PUBLIC_MSAI_API_KEY` if one is supplied (`frontend/src/lib/api.ts:12-13,64-68`), whereas production image builds pass only Entra/API URLs (`.github/workflows/build-and-push.yml:129-132`). No production API-key exposure was established. A build-time prohibition would prevent future accidental misuse of that public variable.
- Production broker credentials select Key Vault through managed identity; startup refuses missing/unreachable secret storage when active deployments require it (`backend/src/msai/main.py:279-303`). Local file-backed credentials are an intentional development mode, not equivalent to production storage.
- Deployments are serialized (`.github/workflows/deploy.yml:86-90`), attempt to refuse persisted active deployment state (`:399-444`, with the stopping omission in PO-08), keep broker-profile services outside routine updates (`scripts/deploy-on-vm.sh:50-54`), and run a post-deploy data-path smoke with explicit upstream-warning/skipped outcomes (`:486-529`). These are meaningful safeguards; a skipped smoke is not candidate execution proof.
- PostgreSQL and Parquet backups are implemented (`scripts/backup-to-blob.sh:73-90,113-134`), scheduled nightly (`scripts/backup-to-blob.timer:4-15`), and retained through a Blob lifecycle policy (`infra/main.bicep:265-286`). Current backup freshness and restorability were not checked. The script only checks a minimum blob size, not restored application consistency (`scripts/backup-to-blob.sh:92-109`). Its PostgreSQL container selector is name-based and takes the first match (`:75`), a configuration risk if the VM ever hosts another PostgreSQL container. Keep the single-purpose-VM assumption explicit or use Compose labels.

## Maintainability and efficiency

API-first is substantially implemented: the CLI's shared HTTP helper handles authentication, request failures, and non-2xx responses (`backend/src/msai/cli.py:253-296`). It is not universal: `ingest` and `ingest-daily` instantiate the ingestion/data-store services locally (`:342-359,374-395`), and instrument/watchdog operations also have local service paths. This is workable for operator tools, but a partner cannot assume all commands target the remote API or share its authorization/audit policy. Distinguish operator-local commands and make ordinary workflow commands consistently API-backed. The CLI default URL is port 8000 (`:138-143`), whereas Docker host use needs its configured 8800 endpoint.

The largest operational maintainability hotspot is `api/live.py`: 4,087 lines, including `live_start_portfolio` spanning lines 1151–2387. `cli.py` is 3,529 lines and mixes HTTP commands with gateway/watchdog/instrument logic. These counts are static measurements, not complexity scores. Extract stable orchestration/service boundaries opportunistically when correcting concrete defects; a wholesale rewrite would put already-encoded trading safeguards at risk.

For a few users, Compose on one Azure VM, managed identity, PostgreSQL, Parquet/DuckDB, and a few queues are reasonable. The existing custom metrics implementation, duplicated REST/WS identity policies, manual frontend response-type mirrors (`frontend/src/lib/api.ts:121-124`), and very large handlers create more maintenance burden than the number of users requires. Prefer established serialization/metrics tools and one policy boundary where that removes demonstrated defects. Do not introduce Kubernetes, multi-tenant provisioning, or a strategy-builder UI without a supported requirement. Shared-host research and live trading still need measured resource budgets and recovery isolation; service limits are not proof of capacity.

## Verification performed

Commands used only existing local tools. The focused pytest run used a clean environment and `/private/tmp` working directory so application settings did not read the project's `.env`; the signing secret below is a synthetic test value.

| Command / check | Result |
| --- | --- |
| `cd backend && ./.venv/bin/ruff check src/` | Exit 0; `All checks passed!` |
| `cd backend && ./.venv/bin/mypy src/ --strict` | Exit 1; four errors in two files, 224 source files checked. Missing pandas/pandas.api.types stubs at `services/analytics_math.py:7-8`; unresolved Nautilus model imports at `services/nautilus/schema_hooks.py:70-71`. These are local typing/dependency failures, not reproduced application exceptions. |
| Focused pytest command below | Exit 0; **45 passed in 10.37s**. Covers auth validation, optional auth, health, HTTP JWT contract, identity-file drift. |
| `cd frontend && pnpm lint` | Exit 1; `eslint: command not found`. |
| `cd frontend && pnpm build` | Exit 1; `next: command not found`. |
| Static dependency-directory inspection | `frontend/node_modules` contains zero entries. No installs attempted. Frontend build/typing/browser execution remains unverified. |
| Python AST read of every migration's `revision` / `down_revision` | 48 revisions; single head `f6a7b8c9d0e1`; no missing parent revisions. No SQL generated or run. |
| Source-derived stopping-state regression: AST-read `live.py:3719` status list; run the workflow's exact `jq` expression on `{"deployments":[{"id":"synthetic-stopping","status":"stopping"}]}` | `stopping` absent from API active set; workflow `active_ids` is empty. Both layers omit the row. |
| Static Next surface search | App Router present; `next.config.ts:3-5` contains only `output: "standalone"`; no middleware, `route.ts`, `next/image` use, AVIF configuration, remote image patterns, or rewrites found in frontend source/config. This narrows reachable version-advisory surfaces; dependency vulnerability assessment is separate. |

```sh
cd /private/tmp
env -i PATH=/usr/bin:/bin ENVIRONMENT=test REPORT_SIGNING_SECRET=audit-test-only \
  PYTHONPATH=/Users/pablomarin/Code/msai-v2/backend/src \
  /Users/pablomarin/Code/msai-v2/backend/.venv/bin/python -m pytest \
  /Users/pablomarin/Code/msai-v2/backend/tests/unit/test_auth.py \
  /Users/pablomarin/Code/msai-v2/backend/tests/unit/test_auth_optional.py \
  /Users/pablomarin/Code/msai-v2/backend/tests/unit/test_health.py \
  /Users/pablomarin/Code/msai-v2/backend/tests/integration/auth_gate/test_auth_contract.py \
  /Users/pablomarin/Code/msai-v2/backend/tests/integration/auth_gate/test_identity_contract.py \
  -q -p no:cacheprovider
```

Not run: full backend regression, container-backed migration tests, frontend lint/build beyond the dependency failure, Playwright, Azure deployment/backup restore, Entra portal inspection or delegated browser login, and broker/live-order paths. No production-readiness conclusion follows from this audit.
