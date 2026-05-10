# Pydantic config production validators fire at import time, not just at runtime

**Date:** 2026-05-09
**Branch where surfaced:** `feat/prod-compose-deployable` (council Blocking Objection #4 + plan-review iter 1 P0)
**Related files:** `backend/src/msai/core/config.py:285-307`, `backend/alembic/env.py:24`, `docker-compose.prod.yml`

## Problem

A Pydantic `Settings` class with a `@model_validator(mode="after")` that raises `ValueError` in production mode is part of the **module-import path** for every consumer that does `from msai.core.config import settings`. That includes:

- `backend/alembic/env.py:24` — `from msai.core.config import settings`
- Every arq worker (`backtest`, `research`, `portfolio`, `ingest`)
- `live_supervisor`
- The FastAPI backend
- The CLI

If the validator raises, `import` raises. The container **crashloops at startup** before any application code runs. Logs show `ValidationError`, not a meaningful application failure.

## Specific gotcha

```python
@model_validator(mode="after")
def _validate_production_secrets(self):
    if self.environment == "production":
        if self.report_signing_secret == "dev-report-signing-secret-change-in-prod":
            raise ValueError("REPORT_SIGNING_SECRET must be set …")
        if len(self.report_signing_secret) < 32:
            raise ValueError("REPORT_SIGNING_SECRET must be at least 32 chars …")
    return self
```

This was originally written assuming "production validation = HTTP backend startup." It is not. **Every container that imports `settings` runs this validator.**

## Concrete failure path that bit us

The first plan draft for `feat/prod-compose-deployable` (T6.3 in the original plan) said:

> "REPORT_SIGNING_SECRET is required only by the backend (the report signing is in API code paths). Workers do not import it. This avoids a needless secret-spread surface."

This was wrong. It was caught by Claude self-review in plan-review iteration 1 (before any code shipped). Without the catch, the deploy workflow would have:

1. Pulled images, run `migrate` one-shot.
2. `migrate` runs `alembic upgrade head`.
3. `alembic` imports `msai.core.config.settings`.
4. Settings validator fires; secret not set; `ValidationError`; alembic exits 1.
5. Deploy workflow sees `migrate` failed → rollback. **Database never migrated. Operator confusion.**

## Solution

**Every service in `docker-compose.prod.yml` that runs Python code from `msai/` must receive `REPORT_SIGNING_SECRET`** in its `environment:` block — not just the backend. The cheapest pattern: the backend service `:?`-guards the var (so compose fails fast at config-resolve time), and downstream services interpolate the now-resolved value:

```yaml
backend:
  environment:
    REPORT_SIGNING_SECRET: ${REPORT_SIGNING_SECRET:?Set REPORT_SIGNING_SECRET in .env}

migrate:
  environment:
    REPORT_SIGNING_SECRET: ${REPORT_SIGNING_SECRET} # Interpolation only

backtest-worker:
  environment:
    REPORT_SIGNING_SECRET: ${REPORT_SIGNING_SECRET}
# ... and so on for every worker + supervisor
```

## Prevention

When auditing a service's required env vars:

1. **Don't ask "what does this service do?"** — ask "what does its entrypoint module import transitively?" Production validators in shared config modules will fire at any import in any service.
2. **Test config import in isolation** to verify the production validator surface: `docker run --rm -e ENVIRONMENT=production --entrypoint python <image> -c "from msai.core.config import settings"` should succeed in production with all required env vars set.
3. **Plan-review must include the import-graph audit** as a step when modifying environment-variable contracts. The "this var is only for service X" claim is a red flag — verify by grep-ing for `import` chains, not by reading service docs.

---

## Bonus finding from the same PR — latent `COPY strategies/` defect

The original `backend/Dockerfile` had:

```dockerfile
COPY src/ ./src/
COPY strategies/ ./strategies/    # ← path relative to build context
```

With `docker-compose.prod.yml` doing `build: { context: ./backend }`, this `COPY` could not work — `strategies/` lives at the repo root, not inside `backend/`. The bug never previously surfaced because:

1. Dev compose uses `Dockerfile.dev` (no `COPY strategies/` — strategies mount as a volume).
2. Nobody had ever run `docker compose -f docker-compose.prod.yml build` against the current shape.

It surfaced when this PR's smoke test (`T8.1`) actually ran the prod backend build for the first time. Fix: switch the build context to repo root (`docker build -f backend/Dockerfile .`) and prefix the COPY paths with `backend/`. Plus: add a repo-root `.dockerignore` (caught by code-review iter 2 P1) to keep `data/`, `.git/`, `.env` etc. out of the build context.

**Lesson:** "the existing Dockerfile compiles in dev" is not evidence that it compiles in prod. The prod and dev Dockerfiles diverge for legitimate reasons; verify the prod build actually builds before relying on it.
