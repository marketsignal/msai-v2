# CLAUDE.md - MSAI v2 (MarketSignal AI)

## Project Overview

### What Is This?

MSAI v2 is a personal hedge fund platform for automated trading via Interactive Brokers. It enables defining trading strategies as Python files, backtesting them against historical minute-level data, deploying them to live/paper trading, and monitoring portfolio performance through a web dashboard.
MSAI v2 is an API-first, CLI-second, UI-third product.

### History: the two-version experiment

This project was originally built in parallel by Claude Opus 4.6 (`claude-version/`) and OpenAI Codex GPT-5.3 (`codex-version/`) from the same PRD, and compared side-by-side through 2026-02 to 2026-04. The comparison concluded 2026-04-19 with council verdict **keep claude-version, kill codex-version** (see [`docs/decisions/which-version-to-keep.md`](docs/decisions/which-version-to-keep.md)). `codex-version/` was archived at tag `codex-final` and removed from the tree. A reattempt at porting the codex Playwright specs was abandoned when plan review found the UI drift too large to port faithfully (see the postscript in the decision doc). **Only `claude-version/` ships.**

### Stack

- **Backend:** Python 3.12 + FastAPI + NautilusTrader + arq (Redis job queue)
- **Frontend:** Next.js 15 + React + shadcn/ui + Tailwind CSS + TradingView Charts
- **Database:** PostgreSQL 16 + Parquet files + DuckDB + Redis 7
- **Auth:** Azure Entra ID (MSAL frontend, PyJWT backend)
- **Deploy:** Docker Compose on Azure VM

### Ports (dev)

| Service            | Host port | Container port |
| ------------------ | --------- | -------------- |
| Frontend (Next.js) | `3300`    | `3000`         |
| Backend (FastAPI)  | `8800`    | `8000`         |
| PostgreSQL         | `5433`    | `5432`         |
| Redis              | `6380`    | `6379`         |

### Running the stack

```bash
cd claude-version && docker compose -f docker-compose.dev.yml up -d

# Health checks
curl http://localhost:8800/health
open http://localhost:3300

# Stop
cd claude-version && docker compose -f docker-compose.dev.yml down
```

### File Structure

```
msai-v2/
├── claude-version/              # The shipping implementation
│   ├── backend/                 # FastAPI + Python (see claude-version/CLAUDE.md for counts)
│   ├── frontend/                # Next.js 15 + shadcn/ui
│   ├── docker-compose.dev.yml   # Ports: 3300, 8800, 5433, 6380
│   ├── docker-compose.prod.yml
│   └── CLAUDE.md                # Version-specific instructions
├── docs/
│   ├── decisions/
│   │   └── which-version-to-keep.md    # Council verdict + option-C postscript
│   ├── plans/
│   │   ├── 2026-02-25-msai-v2-design.md           # Architecture (the PRD)
│   │   └── 2026-02-25-msai-v2-implementation.md   # 50-task plan
│   └── CHANGELOG.md
├── research/
│   └── trading-research-links.md   # 52 curated research links
├── tests/e2e/                   # Shared Playwright scaffold (no specs shipped yet)
│   ├── fixtures/
│   ├── specs/
│   └── use-cases/
├── .claude/                     # Claude Code configuration
│   ├── commands/                # Workflow commands
│   └── rules/                   # Coding standards (auto-loaded)
├── playwright.config.ts         # Default baseURL http://localhost:3300
├── CLAUDE.md                    # This file (parent)
└── CONTINUITY.md                # Session state
```

### Key Commands

```bash
# Backend
cd claude-version/backend && uv run pytest tests/ -v
cd claude-version/backend && uv run ruff check src/
cd claude-version/backend && uv run mypy src/ --strict

# Frontend
cd claude-version/frontend && pnpm build
cd claude-version/frontend && pnpm lint

# Docker dev
cd claude-version && docker compose -f docker-compose.dev.yml up -d
cd claude-version && docker compose -f docker-compose.dev.yml logs -f
cd claude-version && docker compose -f docker-compose.dev.yml down

# Worker stale-import refresh (after merges to src/msai/services|workers|live_supervisor)
cd claude-version && ./scripts/restart-workers.sh
```

### Revival of `codex-version` (if ever needed)

```bash
git checkout codex-final -- codex-version/
```

Everything in `codex-version/` at deletion time is preserved at tag `codex-final`. No active work relies on it.

---

### E2E Configuration

**interface_type:** `fullstack` — MSAI v2 exposes an HTTP API (primary) and a Next.js UI (secondary). Per the project ordering rule ("API-first, CLI-second, UI-third"), the `verify-e2e` agent MUST test the API surface first, then the UI. An API failure means the contract/state is broken — stop immediately and diagnose; do not proceed to UI checks.

**Server URLs:**

| Surface    | URL                     |
| ---------- | ----------------------- |
| API base   | `http://localhost:8800` |
| UI base    | `http://localhost:3300` |
| PostgreSQL | `localhost:5433`        |
| Redis      | `localhost:6380`        |

All API routes are versioned under `/api/v1/` (see `.claude/rules/api-design.md`). Health: `GET /health`.

**Pre-flight (before any E2E run):**

1. `curl -sf http://localhost:8800/health` — if it fails, start the stack: `cd claude-version && docker compose -f docker-compose.dev.yml up -d`.
2. Confirm the UI responds at `http://localhost:3300` (only if UI use cases are in scope).
3. For live-trading use cases: confirm IB Gateway is reachable (paper account `DU...` on port 4002, live account on 4001) — see `.claude/rules/nautilus.md` gotcha #6.

**Auth.** The app uses Azure Entra ID (MSAL on the frontend, PyJWT on the backend). E2E runs should authenticate via the documented login flow OR use a dev-mode bypass token if one is configured — never by forging JWTs or reading secrets from disk.

**ARRANGE (test setup) is allowed via any user-accessible interface:**

- Public API: `POST /api/v1/strategies`, `POST /api/v1/backtests`, `POST /api/v1/live/start`, etc.
- CLI scripts exposed in `claude-version/backend/` (treat as documented commands only).
- The dev seed/bootstrap scripts if present.

**ARRANGE is NOT allowed via:**

- Direct Postgres queries against `localhost:5433`
- Writing Parquet files into `claude-version/data/` by hand
- Pushing into Redis queues directly
- Reading environment secrets to mint tokens

**VERIFY (assertions) MUST go through the same interface the use case targets.** API use cases check response bodies and subsequent GETs; UI use cases check what Playwright sees on screen (`data-testid`, role selectors) and reload to confirm persistence. Never peek at Postgres, DuckDB, or Parquet to "confirm" — if it isn't visible through the API or UI, it doesn't count as verified.

**Live-trading safety rails.** Default every E2E use case that touches order submission to a paper IB account (see `reference_ib_accounts.md`). Live-account use cases must be opt-in, explicit in the use-case file, and never triggered from the standard regression suite. Stop-the-world when any API use case returns 5xx during a live/paper flow — do not continue UI verification against a node in unknown state (gotcha #13: stopping Nautilus does not close positions).

**Core use-case categories** (for inventory in `tests/e2e/use-cases/`):

- `strategies/` — create, edit, list, hash versioning
- `backtests/` — submit, poll status, fetch report, download artifacts
- `live/` — portfolio create, deploy, start/stop, positions, order events
- `data/` — instrument lookup, catalog browse, bar chart rendering
- `auth/` — login, token refresh, logout, RBAC

See `.claude/rules/testing.md` for the full use-case lifecycle (draft → execute → graduate) and failure classification (PASS / FAIL_BUG / FAIL_STALE / FAIL_INFRA).

### Playwright Framework

Scaffolded at the repo root:

- `playwright.config.ts` — `baseURL` defaults to `http://localhost:3300` (claude UI). Override per run with `PLAYWRIGHT_BASE_URL=<url>`.
- `tests/e2e/specs/` — graduated spec files (currently empty; future feature work should author claude-native specs here using `getByTestId`).
- `tests/e2e/use-cases/` — markdown use cases (draft before graduation).
- `tests/e2e/fixtures/` — auth fixture + helpers.
- `tests/e2e/reports/` — HTML + JSON output.

Run specs locally:

```bash
pnpm exec playwright test
```

API-only use cases don't need Playwright — the `verify-e2e` agent hits the REST endpoints directly with curl/httpx.

---

### Visual Design Preferences

- Never generate plain static rectangles for hero sections, landing pages, or key visual moments
- Always include at least one dynamic/animated element: SVG waves, Lottie, shader gradients, or canvas particles
- Prefer organic shapes (blobs, curves, clip-paths) over straight edges and 90-degree corners
- Animations must respect `prefers-reduced-motion` — provide static fallbacks

## Detailed Rules

All coding standards, workflow rules, and policies are in `.claude/rules/`.
These files are auto-loaded by Claude Code with the same priority as this file.

**What's in `.claude/rules/`:**

- `principles.md` — Top-level principles and design philosophy
- `workflow.md` — Decision matrix for choosing the right command
- `worktree-policy.md` — Git worktree isolation rules
- `critical-rules.md` — Non-negotiable rules (branch safety, TDD, etc.)
- `memory.md` — How to use persistent memory and save learnings
- `security.md`, `testing.md`, `api-design.md` — Coding standards
- `nautilus.md` — **NautilusTrader top-20 gotchas** (read before any Nautilus code work). Full reference: `docs/nautilus-reference.md`
- Language-specific: `python-style.md`, `typescript-style.md`, `database.md`, `frontend-design.md`
