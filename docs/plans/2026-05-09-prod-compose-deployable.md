# Prod Compose Deployable — Precursor PR Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `docker-compose.prod.yml` actually deployable so a future deploy-pipeline branch can wire CI/CD on top — fixes the structural defects the engineering council surfaced (`docs/decisions/deployment-pipeline-architecture.md`, Blocking Objections items 1-6 + missing-evidence #4 env-var coverage). After this PR, a CI pipeline can do `docker compose -f docker-compose.prod.yml pull && up -d --wait` against pre-built images and migrations run race-free in-container.

**Architecture:** Three-file refactor. (1) `backend/Dockerfile` adds `COPY alembic/` + `COPY alembic.ini` so migrations can run inside the container instead of the host workaround at `scripts/verify-paper-soak.sh:211`. (2) `docker-compose.prod.yml` switches `build:` blocks to `image:` references with `:?` guards on every CI-required variable, adds a one-shot `migrate` service that all app services `depends_on: condition: service_completed_successfully`, adds the missing `ingest-worker` service that consumes the `msai:ingest` queue routed at `backend/src/msai/core/queue.py:150`, and pipes through every required runtime env var (`REPORT_SIGNING_SECRET` per `backend/src/msai/core/config.py:295` production validation, Entra/JWT settings, `CORS_ORIGINS`). (3) `frontend/Dockerfile` accepts `NEXT_PUBLIC_AZURE_CLIENT_ID` and `NEXT_PUBLIC_AZURE_TENANT_ID` as build args so MSAL config is baked into the JS bundle (Next.js bakes `NEXT_PUBLIC_*` at build time, not runtime). Plus runbook fixes: `/api/v1/health` → `/health` in `vm-setup.md` + `disaster-recovery.md` (the app exposes `/health`, `/ready`, `/metrics` — never had `/api/v1/health`).

**Tech Stack:** Docker Compose v2 schema · Dockerfile multi-stage builds (Python 3.12-slim, Node 22-slim) · Alembic migrations (already on disk at `backend/alembic/` + `backend/alembic.ini`) · Pydantic v2 settings validation. No new external libraries.

---

## Approach Comparison

> **Persisted from Phase 3.1b → 3.1c. Source of truth:** the engineering council verdict at `docs/decisions/deployment-pipeline-architecture.md` (ratified 2026-05-09). The verdict's "Next Step" section IS this plan's deliverable list; the verdict's "Blocking Objections (must resolve before first push-to-deploy)" items 1-6 ARE the failure modes we're closing here. Per memory feedback `feedback_skip_phase3_brainstorm_when_council_predone.md`, when a ratified council chairman verdict already exists, Phases 3.1/3.1b/3.1c are PRE-DONE and re-running them produces stale ceremony.

### Chosen Default

**Land a precursor PR `feat/prod-compose-deployable` that fixes the existing prod compose structural defects BEFORE any CI/CD pipeline branch opens.** Adds `alembic/` + `alembic.ini` to the backend image; adds `migrate` one-shot service + `ingest-worker` service to prod compose; switches `build:` to `image:` with `:?` guards; pipes through the missing env vars (`REPORT_SIGNING_SECRET`, Entra/JWT, CORS, frontend MSAL); fixes runbook `/api/v1/health` → `/health`. **No CI workflow, no Bicep, no ACR/KV provisioning** — those land on the deployment-pipeline branch after this merges.

### Best Credible Alternative

**Bundle the prod-compose fixes INTO the deployment-pipeline branch** so the first deploy-pipeline PR contains everything from "fix the compose" to "wire push-to-main → ACR → SSH". Single PR, single review, ships in one stroke.

### Scoring (5 axes from `rules/workflow.md` Approach-Comparison Protocol)

| Axis                      | Precursor PR (chosen)                                                                                                                                                                  | Bundled (alternative)                                                                                                                                                  |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Complexity**            | LOW — 3 files modified, ~50 lines net diff, no new infra                                                                                                                               | HIGH — 3 files + Bicep + GH Actions workflow + KV bootstrap script + ACR provisioning + secrets render shell + runbook rewrite, ~500-800 lines net diff                |
| **Blast Radius**          | LOW — purely improves the existing (broken) prod compose. Nothing live currently runs against prod compose; first deploy is ahead of us. Safe to land + sit                            | HIGH — touches prod-compose AND remote-deploy automation simultaneously. A defect anywhere in the bundle blocks the whole branch from shipping                         |
| **Reversibility**         | EASY — a 3-file revert. Decision-doc commit can stay; the compose file just rolls back to its current (broken) state                                                                   | MEDIUM — pipeline includes Bicep state, ACR registry, KV secret writes; rollback is multi-step                                                                         |
| **Time to Validate**      | FAST — `docker compose -f docker-compose.prod.yml config` validates YAML; `docker compose up -d --wait migrate backend` exercises the migration path locally. Full smoke ~10 min       | SLOW — needs Azure provisioning to truly validate. ACR push, OIDC federation, SSH-to-VM, KV-rendered env, container actually boots in cloud. Full loop ~2 days         |
| **User/Correctness Risk** | LOW — strict superset of council recommendations; The Contrarian's OBJECT is the literal motivation for this PR. **5/5 council consensus** that current prod compose is non-deployable | MEDIUM — every defect surfaces simultaneously in cloud; "succeed-then-crash" pattern the Contrarian flagged. Hard to bisect when 6 objections + new infra all interact |

### Cheapest Falsifying Test

> "If the existing `docker-compose.prod.yml` is already deployable as-is, the precursor PR is over-engineering and the bundled approach can ship faster."

Confirmed FALSE during decision authoring. Empirical evidence (already cited in `docs/decisions/deployment-pipeline-architecture.md`):

- `backend/Dockerfile:6` does NOT copy `alembic/` or `alembic.ini`. `scripts/verify-paper-soak.sh:211` admits migrations run from the host because the container lacks Alembic files.
- `docker-compose.prod.yml` uses `build:` (not `image:`) on backend + 4 workers + frontend — a CI image-pull deploy CANNOT use this file as-is.
- `docker-compose.prod.yml` has NO `ingest-worker` service. `IngestWorkerSettings` exists at `backend/src/msai/workers/ingest_settings.py:35` and `backend/src/msai/core/queue.py:150` routes ingest jobs to a queue with no consumer.
- `docker-compose.prod.yml:44` does not pass `REPORT_SIGNING_SECRET`. `backend/src/msai/core/config.py:295-303` HARD-RAISES at production startup if the secret is the dev default — silent backend crash on deploy.
- Frontend MSAL config at `frontend/src/lib/msal-config.ts:5-6` reads `process.env.NEXT_PUBLIC_AZURE_*` which Next.js bakes at build time. `frontend/Dockerfile` has zero `ARG`s — the prod bundle has no client ID baked in. Auth silently broken.

The falsifying test FAILED on every check. The precursor PR is mandatory. Cost to validate the falsifying test was < 30 min (already done during the council session). ✅

## Contrarian Verdict

**Gate result: PRE-DONE per council** (`docs/decisions/deployment-pipeline-architecture.md`).

The full 5-advisor council (Simplifier, Scalability Hawk, Pragmatist, Contrarian, Maintainer) ran 2026-05-09 on the broader deployment-pipeline architecture decision. **The Contrarian (Codex) returned OBJECT,** citing the exact set of structural defects this PR closes (REPORT_SIGNING_SECRET injection missing, Alembic not in image, no `ingest-worker`, broker coupling undefined, rollback semantics undefined). The chairman synthesis preserved the OBJECT as a precursor-PR requirement: this PR is the literal resolution. Re-trigger condition (verbatim from the verdict): "If the implementer attempts to wire the GitHub Actions workflow before fixing items 1-6 above, the Contrarian's OBJECT stands — the deploy will succeed-then-crash on first run because backend startup will fail on `REPORT_SIGNING_SECRET` validation." That's the contract this branch fulfills.

The Hawk + Maintainer's CONDITIONAL verdicts on managed Postgres / observability are out of scope here; those land on the deployment-pipeline branch.

---

## File Structure

### New files

| Path                                                 | Responsibility                                                                                                                                 |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `docs/decisions/deployment-pipeline-architecture.md` | Council verdict ratifying this PR's scope. Already drafted on `main` (uncommitted); committed via T0 below as the first commit on this branch. |
| `docs/plans/2026-05-09-prod-compose-deployable.md`   | This plan file.                                                                                                                                |

### Modified files

| Path                                 | Change                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `backend/Dockerfile`                 | Add `COPY alembic/ ./alembic/` and `COPY alembic.ini ./` in the builder stage. Carry both into the runtime stage. Migrations now run inside the container.                                                                                                                                                                                             |
| `frontend/Dockerfile`                | Add `ARG NEXT_PUBLIC_AZURE_CLIENT_ID` and `ARG NEXT_PUBLIC_AZURE_TENANT_ID`, set as `ENV` before `pnpm build` so MSAL config is baked into the JS bundle.                                                                                                                                                                                              |
| `docker-compose.prod.yml`            | Six changes — see Tasks below. Net effect: switch from `build:` to `image:` with `:?` guards; add `migrate` one-shot + `ingest-worker` services; route every app service through `depends_on: migrate: condition: service_completed_successfully`; pipe through `REPORT_SIGNING_SECRET`, Entra/JWT settings, `CORS_ORIGINS`, frontend MSAL build-args. |
| `docs/runbooks/vm-setup.md`          | Replace `/api/v1/health` with `/health` (lines 80, 119). Update D4s_v5 → D4s_v6 if referenced (council verdict §Verification — quota correction).                                                                                                                                                                                                      |
| `docs/runbooks/disaster-recovery.md` | Replace `/api/v1/health` with `/health` (lines 96, 119).                                                                                                                                                                                                                                                                                               |

### Out of scope (deferred to deployment-pipeline branch)

- Bicep/Terraform IaC.
- GitHub Actions workflow + Azure OIDC federation.
- ACR provisioning + image push automation.
- Key Vault provisioning + managed identity + boot-time env render.
- Nightly `pg_dump` / DR automation.
- Azure Log Analytics agent.
- VM provisioning (`scripts/deploy-azure.sh` runs separately).
- Managed Postgres / Redis migration (Phase 2, real money trigger).

---

## Tasks (TDD — Red, Green, Refactor)

### T0. Commit the council verdict

- [ ] **T0.1** Stage and commit `docs/decisions/deployment-pipeline-architecture.md` on this branch as the first commit (`docs(decisions): ratify deployment-pipeline architecture verdict`). The verdict was authored on `main` uncommitted; it travels into this branch via the worktree copy-in. This precursor PR ships the verdict alongside the fixes the verdict requires.

### T1. Backend Dockerfile — Alembic in image

- [ ] **T1.1 RED:** Write a smoke test that proves the current `backend/Dockerfile` cannot run `alembic upgrade head`. Approach: `docker build -t msai-backend:test ./backend && docker run --rm --entrypoint alembic msai-backend:test --help` — currently fails because `alembic.ini` is not in the image. Capture the failure mode in a comment in the plan file (this is a one-shot proof, not a persistent test).
- [ ] **T1.2 GREEN:** Edit `backend/Dockerfile` — add `COPY alembic/ ./alembic/` and `COPY alembic.ini ./` to the builder stage (after `COPY src/`); add corresponding `COPY --from=builder /app/alembic /app/alembic` and `COPY --from=builder /app/alembic.ini /app/alembic.ini` in the runtime stage. Re-run T1.1 — should now succeed.
- [ ] **T1.3 REFACTOR:** Verify image size delta is acceptable (alembic dir is small — ~10 KB). Verify `chown msai:msai /app` line still covers the new files (it should — it operates on `/app` recursively).

### T2. Prod compose — switch `build:` to `image:` with `:?` guards

- [ ] **T2.1 RED:** Run `docker compose -f docker-compose.prod.yml config` from the worktree. It currently passes (using `build:`). Document that this means CI cannot deploy this file with image-pull semantics — the compose file always tries to build locally, which is wrong for the deployment shape the council ratified.
- [ ] **T2.2 GREEN:** Replace `build:` blocks with `image:` references on backend, backtest-worker, research-worker, portfolio-worker, live-supervisor, and frontend services:
  - Backend services: `image: ${MSAI_REGISTRY:?Set MSAI_REGISTRY}/${MSAI_BACKEND_IMAGE:?Set MSAI_BACKEND_IMAGE}:${MSAI_GIT_SHA:?Set MSAI_GIT_SHA}`
  - Frontend service: `image: ${MSAI_REGISTRY:?Set MSAI_REGISTRY}/${MSAI_FRONTEND_IMAGE:?Set MSAI_FRONTEND_IMAGE}:${MSAI_GIT_SHA:?Set MSAI_GIT_SHA}`
  - Note: `postgres`, `redis`, `ib-gateway` already use upstream images — no change.
- [ ] **T2.3 VERIFY:** `MSAI_REGISTRY=local MSAI_BACKEND_IMAGE=msai-backend MSAI_FRONTEND_IMAGE=msai-frontend MSAI_GIT_SHA=test docker compose -f docker-compose.prod.yml config` succeeds. Running without those vars fails with each missing var named in the error.

### T3. Prod compose — `migrate` one-shot service

- [ ] **T3.1 GREEN:** Add a `migrate` service to `docker-compose.prod.yml`:
  - `image: ${MSAI_REGISTRY}/${MSAI_BACKEND_IMAGE}:${MSAI_GIT_SHA}` (same image as backend; alembic now in image per T1).
  - `command: ["alembic", "upgrade", "head"]`
  - `restart: "no"` — must NOT restart on success or failure.
  - `depends_on: postgres: condition: service_healthy` — only postgres, not redis (migrations don't need redis).
  - Same `DATABASE_URL` env as backend.
- [ ] **T3.2 VERIFY:** `docker compose -f docker-compose.prod.yml run --rm migrate` runs `alembic upgrade head` against a healthy postgres and exits 0. (Requires actual postgres up — covered by T8 smoke test.)

### T4. Prod compose — add `ingest-worker` service

- [ ] **T4.1 GREEN:** Add `ingest-worker` service to `docker-compose.prod.yml`, modeled on `backtest-worker`:
  - Same `image:` reference as other backend services.
  - `command: ["python", "-m", "arq", "msai.workers.ingest_settings.IngestWorkerSettings"]` (matches dev compose; uses `python -m arq` — not `uv run arq` which is dev-only).
  - Same volumes (`app_data:/app/data`).
  - Same env (`DATABASE_URL`, `REDIS_URL`, `DATA_ROOT`, `ENVIRONMENT`, `POLYGON_API_KEY`, `DATABENTO_API_KEY` per the data-source pattern).
  - `depends_on: migrate: service_completed_successfully` + `postgres: service_healthy` + `redis: service_healthy`.
  - Same resource limits as other workers.
- [ ] **T4.2 NOTE:** Skip `job-watchdog` from dev compose for now. It runs `WorkerSettings` (same as `backtest-worker`) and its production necessity is unclear from the codebase. The Maintainer council advisor flagged it as conditional — "include if part of the operating model." Defer to the deployment-pipeline branch where operating model is being formalized.

### T5. Prod compose — wire `depends_on: migrate: service_completed_successfully` everywhere

- [ ] **T5.1 GREEN:** Add `migrate: condition: service_completed_successfully` to the `depends_on` block of: backend, backtest-worker, research-worker, portfolio-worker, ingest-worker, live-supervisor. (Frontend doesn't need migrate — it has no DB dependency at startup.)
- [ ] **T5.2 VERIFY:** Compose graph is acyclic. `docker compose -f docker-compose.prod.yml config | grep -A 3 depends_on` shows the wiring.

### T6. Prod compose — pipe through required env vars

- [ ] **T6.1 GREEN:** Add to backend service `environment:`:
  - `REPORT_SIGNING_SECRET: ${REPORT_SIGNING_SECRET:?Set REPORT_SIGNING_SECRET in .env (openssl rand -base64 48)}`
  - `AZURE_TENANT_ID: ${AZURE_TENANT_ID:?Set AZURE_TENANT_ID in .env}`
  - `AZURE_CLIENT_ID: ${AZURE_CLIENT_ID:?Set AZURE_CLIENT_ID in .env}`
  - `JWT_TENANT_ID: ${JWT_TENANT_ID:-${AZURE_TENANT_ID}}` (per CLAUDE.md, JWT*\* defaults to AZURE*\*; explicit override allowed)
  - `JWT_CLIENT_ID: ${JWT_CLIENT_ID:-${AZURE_CLIENT_ID}}`
  - `CORS_ORIGINS: ${CORS_ORIGINS:?Set CORS_ORIGINS in .env (JSON list of allowed origins)}`
  - `MSAI_API_KEY: ${MSAI_API_KEY:-}` (optional — empty default disables X-API-Key auth)
- [ ] **T6.2 GREEN — every backend service that imports `msai.core.config.settings` ALSO needs `REPORT_SIGNING_SECRET`** because the production-mode validator at `backend/src/msai/core/config.py:295-303` raises at any settings import in production. This applies to: `migrate`, `backtest-worker`, `research-worker`, `portfolio-worker`, `ingest-worker`, `live-supervisor`. Each of those services gets:
  - `REPORT_SIGNING_SECRET: ${REPORT_SIGNING_SECRET}` (no `:?` repeat — backend's `:?` already gates the compose file; downstream services interpolate the now-validated value)
  - Existing `DATABASE_URL`, `REDIS_URL`, `DATA_ROOT`, `ENVIRONMENT` — already present, leave alone.
  - `POLYGON_API_KEY` + `DATABENTO_API_KEY` for `ingest-worker` (per the data-source pattern; backend already passes these).
  - Workers do NOT need `AZURE_TENANT_ID` / `JWT_*` / `CORS_ORIGINS` / `MSAI_API_KEY` — they have permissive defaults, no production-only validation, and the workers do not run HTTP listeners or auth.
- [ ] **T6.3 GREEN — `migrate` service env (REVISION):** `migrate` runs `alembic upgrade head`, which imports `msai.core.config.settings` via `backend/alembic/env.py:24`. Therefore `migrate` MUST receive `DATABASE_URL`, `ENVIRONMENT=production`, AND `REPORT_SIGNING_SECRET`. Without `REPORT_SIGNING_SECRET`, alembic fails at import time before the migration runs — silent crashloop on deploy. **This is a P0 finding from plan-review iteration 1: the original plan's "Workers do not import it" claim was wrong; alembic + every worker imports `msai.core.config` and therefore triggers the prod validator.**
- [ ] **T6.4 VERIFY:** Run `docker compose -f docker-compose.prod.yml config` with all required vars set — passes. Run with `REPORT_SIGNING_SECRET` unset — fails with the named var. Run with `AZURE_TENANT_ID` unset — fails with the named var.

### T7. Frontend Dockerfile — MSAL build-time args

- [ ] **T7.1 RED:** Build the current frontend image (`docker build -t msai-frontend:test ./frontend`); inspect the bundle (`docker run --rm msai-frontend:test cat /app/.next/server/pages-manifest.json` or similar) — confirm `NEXT_PUBLIC_AZURE_CLIENT_ID` is empty / unset in the built artifact.
- [ ] **T7.2 GREEN:** Edit `frontend/Dockerfile`:
  - In the builder stage, add `ARG NEXT_PUBLIC_AZURE_CLIENT_ID` and `ARG NEXT_PUBLIC_AZURE_TENANT_ID` BEFORE `RUN pnpm build`.
  - Set as `ENV` so Next.js sees them: `ENV NEXT_PUBLIC_AZURE_CLIENT_ID=$NEXT_PUBLIC_AZURE_CLIENT_ID` and `ENV NEXT_PUBLIC_AZURE_TENANT_ID=$NEXT_PUBLIC_AZURE_TENANT_ID`.
  - Also `ARG NEXT_PUBLIC_API_URL` + `ENV` for the API base URL. **CRITICAL:** Next.js bakes ALL `NEXT_PUBLIC_*` vars at `pnpm build` time, not runtime. The current prod compose passes `NEXT_PUBLIC_API_URL` at runtime via the frontend service `environment:` block — that's silently a no-op for the bundled JS. So: the build args bake the values into the bundle, AND the runtime env can override only on server-rendered pages (Next.js standalone server). For client-side bundles (the MSAL-using auth code), build-args are the only path.
- [ ] **T7.3 NOTE:** The frontend service in `docker-compose.prod.yml` switches from `build:` to `image:` (T2). CI must pass `--build-arg NEXT_PUBLIC_AZURE_CLIENT_ID=...`, `--build-arg NEXT_PUBLIC_AZURE_TENANT_ID=...`, and `--build-arg NEXT_PUBLIC_API_URL=...` when building the frontend image. The runtime `NEXT_PUBLIC_API_URL` env in the compose service block is still set (for Next.js server-side fallback), but is NOT the binding source of truth for bundled JS. Document this in a code comment near the frontend service block AND in the deployment-pipeline branch's CI workflow (out of scope here).

### T8. Smoke test — image build + compose config validation

> **Scope-narrowed during plan-review iteration 1.** Original T8 attempted a full `compose up` smoke test, but the prod compose pins `name: msai_postgres_data` (same volume as dev compose). A smoke `down -v` would wipe Pablo's local dev DB — `-p smoke` does not isolate `name:`-pinned volumes. Council verdict's verification step is only `docker compose config` validation — that's our scope here. Actual bring-up is deferred to the deployment-pipeline branch where it runs against an isolated cloud env.

- [ ] **T8.1 VERIFY — backend image builds:** `docker build -t local/msai-backend:test ./backend` succeeds. Then `docker run --rm --entrypoint alembic local/msai-backend:test --help` succeeds — proves T1 placed `alembic/` and `alembic.ini` correctly inside the image.
- [ ] **T8.2 VERIFY — frontend image builds with build args:** `docker build -t local/msai-frontend:test --build-arg NEXT_PUBLIC_AZURE_CLIENT_ID=test-client-id --build-arg NEXT_PUBLIC_AZURE_TENANT_ID=test-tenant-id --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 ./frontend` succeeds.
- [ ] **T8.3 VERIFY — compose config with all vars set:** Set every `:?` var to a test value (`MSAI_REGISTRY=local`, `MSAI_BACKEND_IMAGE=msai-backend`, `MSAI_FRONTEND_IMAGE=msai-frontend`, `MSAI_GIT_SHA=test`, `POSTGRES_PASSWORD=smoke`, `REPORT_SIGNING_SECRET=$(openssl rand -base64 48)`, `AZURE_TENANT_ID=test`, `AZURE_CLIENT_ID=test`, `CORS_ORIGINS='["http://localhost"]'`, `IB_ACCOUNT_ID=DUtest`, `TWS_USERID=test`, `TWS_PASSWORD=test`). Run `docker compose -f docker-compose.prod.yml config > /tmp/compose-resolved.yml`. Exit 0; produced YAML contains the migrate service, ingest-worker service, and image references for backend + frontend.
- [ ] **T8.4 VERIFY — compose config fails cleanly when each `:?` var is unset:** Unset `REPORT_SIGNING_SECRET` → `docker compose -f docker-compose.prod.yml config` exits non-zero with the named missing var. Repeat for `AZURE_TENANT_ID`, `MSAI_REGISTRY`, `MSAI_BACKEND_IMAGE`, `CORS_ORIGINS` — each unset run produces the named-var error. This is the contract that lets CI detect missing secrets at the gate.
- [ ] **T8.5 NO CLEANUP NEEDED:** No volumes created (only `config` ran, never `up`). No `down -v` risk to dev DB.

### T11. Move `ib-gateway` + `live-supervisor` behind `broker` profile in prod compose (council Blocking Objection #7)

> **Caught during plan-review iteration 1 re-read.** Council Blocking Objection #7 mandates that auto-deploy explicitly EXCLUDE the broker profile during active live sessions (NautilusTrader gotcha #3 — duplicate `client_id` silently disconnects the live trading client when `ib-gateway` is recreated). The deployment-pipeline workflow needs `profiles:` on the broker services to enforce this via `COMPOSE_PROFILES`. Currently `docker-compose.prod.yml` has neither service profiled — they always start. The dev compose pattern (`profiles: ["broker"]` on `live-supervisor`, ib-gateway is also broker-only via `COMPOSE_PROFILES=broker docker compose ... up -d`) is the model.

- [ ] **T11.1 GREEN:** Add `profiles: ["broker"]` to the `live-supervisor` service in `docker-compose.prod.yml`.
- [ ] **T11.2 GREEN:** Add `profiles: ["broker"]` to the `ib-gateway` service in `docker-compose.prod.yml`. (live-supervisor depends on ib-gateway being healthy; both go behind the same profile to keep the dependency satisfiable when broker IS started.)
- [ ] **T11.3 VERIFY:** `docker compose -f docker-compose.prod.yml config` (no profile) → output does NOT include `ib-gateway` or `live-supervisor`. `COMPOSE_PROFILES=broker docker compose -f docker-compose.prod.yml config` → output INCLUDES both. The deployment-pipeline workflow on the next branch will run without the broker profile by default and add `--profile broker` only on operator-confirmed live-deploy events.
- [ ] **T11.4 NOTE:** This adds a runtime asymmetry: `docker compose up -d` from a workstation now does NOT start the broker. To run with broker (e.g., during paper-soak verification), pass `COMPOSE_PROFILES=broker` exactly like dev compose. Add a one-line operator note to `docs/runbooks/vm-setup.md` documenting this, alongside the `/api/v1/health` → `/health` fix in T9.

### T9. Runbook fixes

- [ ] **T9.1 GREEN — `docs/runbooks/vm-setup.md`:**
  - Line 22: `Standard_D4s_v5` → `Standard_D4s_v6` (council Verification — DSv5 quota was 0/0 on MarketSignal2; Ddsv6 has 0/10 default).
  - Line 80: `curl http://localhost:8000/api/v1/health` → `curl http://localhost:8000/health`.
  - Line 119: `Backend health endpoint responds (`/api/v1/health`)` → `Backend health endpoint responds (`/health`)`.
  - Add a one-paragraph note (placement: near the "Running the stack" or "Operator commands" section) documenting the broker-profile change from T11: `docker compose -f docker-compose.prod.yml up -d` no longer starts `ib-gateway` + `live-supervisor`; pass `COMPOSE_PROFILES=broker` to start the trading services explicitly. Mirrors the dev-compose pattern already in CLAUDE.md.
- [ ] **T9.2 GREEN — `docs/runbooks/disaster-recovery.md`:**
  - Line 96: `curl http://localhost:8000/api/v1/health` → `curl http://localhost:8000/health`.
  - Line 119: `Backend health check passes: `curl http://localhost:8000/api/v1/health``→ use`/health`.
- [ ] **T9.3 VERIFY:** `grep -rln "/api/v1/health" docs/ scripts/ backend/ frontend/` returns no matches (except the decision doc which records the historical defect — acceptable). `grep -rln "D4s_v5" docs/` returns no matches.

### T10. Update state files + decision-doc references

- [ ] **T10.1** Update `.claude/local/state.md` Done section after PR merges (post-flight, not pre-flight).
- [ ] **T10.2** Verify `docs/decisions/deployment-pipeline-architecture.md` text references match what landed (e.g., the file path of this plan, the actual VM size committed).

---

## E2E Use Cases

**E2E: N/A — infrastructure-only refactor; no API/UI/CLI behavior change.**

Justification: This PR modifies build-time + deploy-time configuration (Dockerfile, compose YAML, runbook text). Zero changes to:

- HTTP API routes, request/response shapes, or auth flows
- UI components, pages, or routes
- CLI commands or output
- Database schema or migration ordering (the migrations themselves are unchanged; we only change WHERE they run from)
- Business logic, services, workers, or strategies

The verification path IS the E2E for this scope: `docker compose -f docker-compose.prod.yml config` validates YAML, and the T8 smoke test runs `migrate` + `backend` end-to-end against a healthy local postgres. If both pass, the PR's user-facing impact (zero) is verified.

The deployment-pipeline branch will need full E2E (deploy from CI → curl /health on the public IP → verify live-supervisor consuming Redis stream). Out of scope here.

---

## Implementation Notes

### Why ship the council verdict in this PR?

The verdict was authored on `main` uncommitted (no time to commit it independently before starting the precursor work). Two choices:

1. Commit it standalone on `main` first, then create the worktree (requires explicit user approval to commit; adds a back-and-forth turn).
2. Carry it into this branch and commit alongside the fixes the verdict requires.

Option 2 is cleaner: the verdict + the precursor fixes are conceptually one unit (verdict says "fix these things"; this PR fixes them). PR description references the verdict, code review sees the rationale.

### Why NOT include the deployment-pipeline work here?

The council's Next Step is explicit: "Open a precursor branch named `feat/prod-compose-deployable` (NOT a deployment-pipeline branch yet)." The bundled approach was scored higher on every risk axis except shipping speed, and shipping speed isn't the binding constraint — correctness is. The Contrarian's OBJECT was the council's strongest signal; bundling the OBJECT-resolution work with the cause-OBJECT-might-have-fired-anyway work is the wrong order.

### Why D4s_v6 not D4s_v5?

Verified during decision authoring: MarketSignal2 subscription has `0/0` quota for `Standard DSv5 Family vCPUs` in every region checked. `Standard Ddsv6 Family vCPUs` has `0/10` in eastus2 (and elsewhere). D4s_v6 is one generation newer than D4s_v5 with same 4 vCPU / 16 GB / premium SSD, and ships without a quota request blocker. Decision-doc §Verification has the empirical capture.

### Decision-doc references to keep accurate after this PR

- D4s_v5 → D4s_v6 (council verdict §Verification + this PR's runbook fixes if applicable).
- `msai-rg` → `msaiv2_rg` (existing empty RG in eastus2 on MarketSignal2; council verdict §Verification).
- KSGAI subscription mention in CLAUDE.md ("Azure has only `pablovm/PABLOVM_RG`") is technically a per-tenant statement; defer the broader CLAUDE.md update to a follow-up doc commit.

---

## Dispatch Plan

This is a small plan (11 tasks, mostly serial). **Sequential mode** — each task either modifies the same file as a prior task (`docker-compose.prod.yml` is touched by T2-T6 and T11) or depends on a prior task's verification (T8 depends on T1-T7+T11 done). Single-subagent execution; no parallelism.

| Task ID | Depends on  | Writes (concrete file paths)                                                                                                |
| ------- | ----------- | --------------------------------------------------------------------------------------------------------------------------- |
| T0      | —           | `docs/decisions/deployment-pipeline-architecture.md` (commit only — already on disk)                                        |
| T1      | T0          | `backend/Dockerfile`                                                                                                        |
| T2      | T1          | `docker-compose.prod.yml`                                                                                                   |
| T3      | T2          | `docker-compose.prod.yml` (same file — sequential)                                                                          |
| T4      | T3          | `docker-compose.prod.yml` (same file — sequential)                                                                          |
| T5      | T4          | `docker-compose.prod.yml` (same file — sequential)                                                                          |
| T6      | T5          | `docker-compose.prod.yml` (same file — sequential)                                                                          |
| T11     | T6          | `docker-compose.prod.yml` (same file — sequential; broker profile guards)                                                   |
| T7      | —           | `frontend/Dockerfile` (independent of compose; could parallelize with T2-T6+T11 in theory, but executor is single subagent) |
| T8      | T1-T7, T11  | (smoke test — `docker compose config` validation only, no file writes, no `up`, no volume risk)                             |
| T9      | T11         | `docs/runbooks/vm-setup.md`, `docs/runbooks/disaster-recovery.md` (also documents the broker-profile change from T11)       |
| T10     | T9, post-PR | `.claude/local/state.md`                                                                                                    |

Execute T0 → T1 → T2 → T3 → T4 → T5 → T6 → T11 → T7 → T8 → T9 → T10. Roughly 60-90 min of focused work + plan-review iterations.
