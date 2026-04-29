# Research Brief — Developer-Journey How-Tos

**Date:** 2026-04-28
**Feature:** how-tos-developer-journey
**Status:** N/A (no external libraries researched — feature is self-documentation)

## Why N/A

This feature documents the existing MSAI v2 codebase. It does not introduce new libraries, APIs, or external dependencies. Every claim in the resulting docs comes from reading the local source tree (`backend/src/msai/`, `frontend/src/`, `alembic/versions/`, etc.) — not from external documentation.

External references that _will_ appear in the docs (NautilusTrader, FastAPI, arq, IB Gateway protocol, Polygon, Databento) are already covered by:

- `docs/nautilus-reference.md` (60KB, full Nautilus reference)
- `docs/architecture/nautilus-integration.md`
- Existing CLAUDE.md sections + `.claude/rules/nautilus.md` gotchas

Where third-party behavior is described in the new how-tos, citations point to the existing in-repo references — not to scraped external docs (which would rot independently).

## Verification done

- Read `mcpgateway/docs/architecture/how-tools-work.md` and `mcpgateway/docs/architecture/how-auth-works.md` for style + structure baseline (mcpgateway sibling repo at `/Users/pablomarin/Code/mcpgateway/`).
- Verified codebase surfaces (20 API routers, 10 CLI sub-apps, 13 UI pages) — see PRD for inventory.
- Codex consult 2026-04-28 (general mode, gpt-5.4 xhigh) on doc structure + ordering + parity-table convention; verdict folded into the plan.

## Open risks

- File:line citations rot. The plan includes a discipline note: lead with stable identifiers (function/class/route names), cite line numbers as supporting evidence.

---

**Gate criteria:** met — feature has no external libraries to research; this brief documents the N/A justification with verification steps.
