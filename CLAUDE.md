# CLAUDE.md - MSAI v2 (MarketSignal AI)

## Project Overview

### What Is This?

MSAI v2 is a personal hedge fund platform for automated trading via Interactive Brokers. It enables defining trading strategies as Python files, backtesting them against historical minute-level data, deploying them to live/paper trading, and monitoring portfolio performance through a web dashboard.

### Two Competing Implementations

This project has **two versions** built from the same design and implementation plan, now being compared side-by-side:

|                 | Claude Version                                    | Codex Version           |
| --------------- | ------------------------------------------------- | ----------------------- |
| **Built by**    | Claude Opus 4.6                                   | OpenAI Codex (GPT-5.3)  |
| **Location**    | `claude-version/`                                 | `codex-version/`        |
| **Frontend**    | `http://localhost:3300`                           | `http://localhost:3400` |
| **Backend API** | `http://localhost:8800`                           | `http://localhost:8400` |
| **PostgreSQL**  | `localhost:5433`                                  | `localhost:5434`        |
| **Redis**       | `localhost:6380`                                  | `localhost:6381`        |
| **Design doc**  | `docs/plans/2026-02-25-msai-v2-design.md`         | Same                    |
| **Impl plan**   | `docs/plans/2026-02-25-msai-v2-implementation.md` | Same                    |

Both versions implement the same 50-task plan across 8 milestones. The goal is to run both simultaneously and compare code quality, UI design, test coverage, and production readiness.

### Running Both Versions Side-by-Side

```bash
# Start Claude version (ports 3300, 8800, 5433, 6380)
cd claude-version && docker compose -f docker-compose.dev.yml up -d

# Start Codex version (ports 3400, 8400, 5434, 6381)
cd codex-version && docker compose -f docker-compose.dev.yml up -d

# Open both in browser
open http://localhost:3300   # Claude frontend
open http://localhost:3400   # Codex frontend

# API health checks
curl http://localhost:8800/health   # Claude backend
curl http://localhost:8400/health   # Codex backend

# Stop both
cd claude-version && docker compose -f docker-compose.dev.yml down
cd codex-version && docker compose -f docker-compose.dev.yml down
```

### Tech Stack (both versions)

- **Backend:** Python 3.12 + FastAPI + NautilusTrader + arq (Redis job queue)
- **Frontend:** Next.js 15 + React + shadcn/ui + Tailwind CSS + TradingView Charts
- **Database:** PostgreSQL 16 + Parquet files + DuckDB + Redis 7
- **Auth:** Azure Entra ID (MSAL frontend, PyJWT backend)
- **Deploy:** Docker Compose on Azure VM

### File Structure

```
msai-v2/
├── claude-version/              # Built by Claude Opus 4.6
│   ├── backend/                 # FastAPI + Python (153 tests)
│   ├── frontend/                # Next.js 15 + shadcn/ui
│   ├── docker-compose.dev.yml   # Ports: 3300, 8800, 5433, 6380
│   ├── docker-compose.prod.yml
│   └── CLAUDE.md                # Version-specific instructions
├── codex-version/               # Built by OpenAI Codex (GPT-5.3)
│   ├── backend/                 # FastAPI + Python
│   ├── frontend/                # Next.js 15 + shadcn/ui
│   ├── docker-compose.dev.yml   # Ports: 3400, 8400, 5434, 6381
│   ├── docker-compose.prod.yml
│   └── CLAUDE.md                # Version-specific instructions
├── docs/
│   ├── plans/
│   │   ├── 2026-02-25-msai-v2-design.md           # Architecture (shared)
│   │   └── 2026-02-25-msai-v2-implementation.md    # 50-task plan (shared)
│   └── CHANGELOG.md
├── research/
│   └── trading-research-links.md   # 52 curated research links
├── .claude/                     # Claude Code configuration
│   ├── commands/                # Workflow commands
│   └── rules/                   # Coding standards (auto-loaded)
├── CLAUDE.md                    # This file (parent)
└── CONTINUITY.md                # Session state
```

### Key Commands

```bash
# Claude version
cd claude-version/backend && uv run pytest tests/ -v       # 153 tests
cd claude-version/frontend && pnpm build                    # Build check
cd claude-version && docker compose -f docker-compose.dev.yml up -d

# Codex version
cd codex-version/backend && uv run pytest tests/ -v         # Tests
cd codex-version/frontend && pnpm build                     # Build check
cd codex-version && docker compose -f docker-compose.dev.yml up -d

# Run both simultaneously
cd claude-version && docker compose -f docker-compose.dev.yml up -d
cd codex-version && docker compose -f docker-compose.dev.yml up -d
```

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
- Language-specific: `python-style.md`, `typescript-style.md`, `database.md`, `frontend-design.md`
