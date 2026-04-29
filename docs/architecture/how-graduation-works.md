<!-- forge:doc how-graduation-works -->

# How Graduation Works

Graduation is the **gate** between "this looked good in research" and "this is allowed to allocate real capital." It is not a compute step — there is no worker, no arq job, no NautilusTrader subprocess. It is a state machine plus an immutable audit log: a `GraduationCandidate` row moves through stages (`discovery → validation → … → live_running → archived`), and every move stamps an immutable `GraduationStageTransition` row. When humans (or policy) decide a candidate is approved, the system freezes its metadata and exposes it as eligible for portfolio inclusion.

---

## Component Diagram

```
                                ┌─ FROM RESEARCH ─────────────────────┐
                                │  POST /api/v1/research/promotions   │
                                │  (research_job_id, trial_index)     │
                                │  → GraduationService.create_candidate│
                                └──────────────┬──────────────────────┘
                                               │
                                               ▼
                          ┌─ DISCOVERY (entry stage) ──────────────────┐
                          │  GraduationCandidate row                   │
                          │  stage = "discovery"                       │
                          │  config (JSONB), metrics (JSONB)           │
                          │  + initial GraduationStageTransition       │
                          │    (from_stage="", to_stage="discovery")   │
                          └──────────────┬─────────────────────────────┘
                                         │
                                         ▼
            ┌─ 9-STAGE STATE MACHINE (services/graduation.py:37–47) ───────┐
            │                                                              │
            │   discovery ──► validation ──► paper_candidate               │
            │       │             │                │                       │
            │       │             ▼                ▼                       │
            │       │         archived         paper_running               │
            │       │                              │                       │
            │       │                              ▼                       │
            │       └◄── paper_review ◄────────────┘                       │
            │              │                                               │
            │              ▼                                               │
            │         live_candidate ──► live_running ◄──► paused          │
            │              │                  │              │             │
            │              ▼                  ▼              ▼             │
            │           archived           archived       archived         │
            │                                                              │
            │   archived  = TERMINAL (no outgoing transitions)             │
            │                                                              │
            │   Every move writes 1 immutable GraduationStageTransition    │
            │   row keyed (candidate_id, created_at). Append-only.         │
            │                                                              │
            └────────────────┬─────────────────────────────────────────────┘
                             │
                             ▼
                ┌─ APPROVED / live_candidate STAGE ────────┐
                │  Eligible for portfolio inclusion        │
                │  (config + metrics frozen at this point) │
                └──────────────┬───────────────────────────┘
                               │
                               ▼
              ┌─ TO BACKTEST PORTFOLIO ─────────────────────┐
              │  POST /api/v1/portfolios                    │
              │  PortfolioAllocation.candidate_id FK        │
              │  → references any candidate row by FK       │
              │    (stage is NOT enforced at allocation)    │
              └─────────────────────────────────────────────┘
```

The seam **in** is `POST /api/v1/research/promotions` — research promotes a winning trial into discovery. The seam **out** is `PortfolioAllocation.candidate_id` — the FK references `graduation_candidates.id`. There is **no DB-level constraint and no application-side check** that allocations may only reference approved/live stages — any `GraduationCandidate` row can be allocated regardless of `stage`. Treat "only graduated candidates get allocated" as an operator convention, not a system invariant.

---

## TL;DR

Graduation is a **gate**, not a compute step. It exists because backtest performance plus walk-forward OOS plots are not, on their own, enough authority to allocate capital. A human (or, eventually, a policy automation) has to look at a candidate and say "yes, this gets a slot." The gate is implemented as a 9-stage state machine over `GraduationCandidate` rows. The **only** rejection class on `POST .../stage` is illegal-transition (HTTP 422 with the allowed next stages in the body) — there is no risk-overlay, no metrics-shape check, and no per-stage RBAC at the API layer in current code. Every move writes one immutable `GraduationStageTransition` row (append-only). The three surfaces are **API** (`/api/v1/graduation/candidates`), **CLI** (`msai graduation list` / `msai graduation show`), and **UI** (`/graduation` Kanban board). Approve/reject lives only on the UI in Phase 1 — the CLI is read-only.

---

## Table of Contents

1. [Concepts and data model](#1-concepts-and-data-model)
2. [The three surfaces](#2-the-three-surfaces)
3. [Internal sequence diagram](#3-internal-sequence-diagram)
4. [See, verify, troubleshoot](#4-see-verify-troubleshoot)
5. [Common failures](#5-common-failures)
6. [Idempotency and retry behavior](#6-idempotency-and-retry-behavior)
7. [Rollback and repair](#7-rollback-and-repair)
8. [Key files](#8-key-files)

---

## 1. Concepts and data model

### 1.1 The `GraduationCandidate` row

A candidate is one (strategy, configuration, metrics) tuple that came out of research and is being shepherded toward capital allocation. It is **not** a backtest, **not** a deployment — it is a label on a strategy + config combo that says "this is a thing we are deciding about."

`backend/src/msai/models/graduation_candidate.py:22–55` — `GraduationCandidate`:

| Column            | Type                              | Purpose                                                                                                                                                                                                                                                                                                                                         |
| ----------------- | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`              | UUID, PK                          | Stable identifier. Persists across all stage moves.                                                                                                                                                                                                                                                                                             |
| `strategy_id`     | FK → `strategies`                 | Which strategy this is a candidate for.                                                                                                                                                                                                                                                                                                         |
| `research_job_id` | FK → `research_jobs`, optional    | Which research job promoted it (nullable to allow direct creation outside the research path).                                                                                                                                                                                                                                                   |
| `deployment_id`   | FK → `live_deployments`, optional | Set when the candidate has been wired into a live deployment.                                                                                                                                                                                                                                                                                   |
| `promoted_by`     | FK → `users`, optional            | Set **once at creation** to the calling user (`services/graduation.py:83`). Never updated by `update_stage()` — this is the **creator**, not the approver. To find who approved a stage move, query `graduation_stage_transitions.transitioned_by`.                                                                                             |
| `stage`           | String, default `"discovery"`     | Current state in the 9-stage machine.                                                                                                                                                                                                                                                                                                           |
| `config`          | JSONB                             | Frozen strategy configuration. The thing capital would run with.                                                                                                                                                                                                                                                                                |
| `metrics`         | JSONB                             | Snapshot of research metrics at promotion time (Sharpe, Sortino, OOS coverage, etc.).                                                                                                                                                                                                                                                           |
| `notes`           | Text, optional                    | Operator commentary.                                                                                                                                                                                                                                                                                                                            |
| `promoted_at`     | Timestamp, optional               | Set **once at creation** to `datetime.now(UTC)` (`services/graduation.py:84`). Despite the name, it does **not** track when the candidate crossed into an approved stage — it tracks when the candidate row was created. For "when did this candidate enter stage X?", scan `graduation_stage_transitions.created_at` filtered by `to_stage=X`. |
| `created_at`      | Timestamp                         | Row birth.                                                                                                                                                                                                                                                                                                                                      |
| `updated_at`      | Timestamp                         | `TimestampMixin` — bumps on every `stage` write.                                                                                                                                                                                                                                                                                                |

The row is **mutable in exactly one field**: `stage`. Everything else is set at creation (or, in the case of `deployment_id`, set once when the deployment binds and then frozen). The `config` and `metrics` JSONB blobs are intentionally not edited after promotion — that is what gives "graduated candidate" a stable meaning over time.

### 1.2 The 9 stages

`backend/src/msai/services/graduation.py:37–47` defines the canonical stage list and the valid-transitions matrix. The nine stages and their meanings:

| Stage             | Meaning                                                                                                           |
| ----------------- | ----------------------------------------------------------------------------------------------------------------- |
| `discovery`       | Just promoted from research. Has metrics but has not been independently validated.                                |
| `validation`      | Operator has reviewed the metrics and re-run any sanity checks (OOS coverage, parameter sensitivity).             |
| `paper_candidate` | Approved to deploy on **paper** trading. Not yet running.                                                         |
| `paper_running`   | Currently running on a paper IB account (`DU…`). Live order events flowing.                                       |
| `paper_review`    | Paper run completed (or is being evaluated mid-flight). Operator decides: live, back to discovery, or archive.    |
| `live_candidate`  | Approved to deploy on **live** trading. Not yet running.                                                          |
| `live_running`    | Currently running on a live IB account (`U…`). Real money.                                                        |
| `paused`          | Was running live, has been paused. Position state preserved; can resume to `live_running`.                        |
| `archived`        | **Terminal.** No outgoing transitions. Used to take a candidate out of consideration (revoked approval, retired). |

The `archived` stage is terminal by design — `services/graduation.py:46` defines its valid-next-stages as the empty set `{}`. Once you archive, you cannot un-archive. To bring a strategy back, you create a **new** `GraduationCandidate` row (typically by re-promoting from research).

### 1.3 The valid-transitions matrix

`backend/src/msai/services/graduation.py:37–47` — `VALID_TRANSITIONS`:

```python
discovery        → {validation, archived}
validation       → {paper_candidate, archived}
paper_candidate  → {paper_running, archived}
paper_running    → {paper_review, archived}
paper_review     → {live_candidate, discovery, archived}
live_candidate   → {live_running, archived}
live_running     → {paused, archived}
paused           → {live_running, archived}
archived         → {}                        (terminal)
```

Two non-obvious paths:

- **`paper_review → discovery`** — explicit "send back to research" path. If paper performance disagrees with backtest performance, the candidate is not necessarily dead — you can move it back and run more research before re-promoting.
- **Every non-terminal stage can go to `archived`** — there is always an exit. You never get a candidate stuck in a stage you can't get out of.

Any transition not in this dict raises `GraduationStageError` (`services/graduation.py:30–31`), which the API layer at `api/graduation.py:132–176` catches and turns into HTTP **422** with the list of allowed next stages in the body. The frontend mirrors this matrix at `frontend/src/app/graduation/page.tsx:45–65` so the buttons it renders only show legal moves — but the server is the authority.

### 1.4 The `GraduationStageTransition` row (immutable audit)

`backend/src/msai/models/graduation_stage_transition.py:19–46` — `GraduationStageTransition`:

| Column            | Type                                  | Purpose                                                                                                                                                                                                                          |
| ----------------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`              | BigInteger, PK, autoincrement         | Append-only — `BigInteger` because the table never gets pruned.                                                                                                                                                                  |
| `candidate_id`    | FK → `graduation_candidates`, CASCADE | Which candidate this transition belongs to.                                                                                                                                                                                      |
| `from_stage`      | String                                | Stage we left. Empty string `""` for the initial `→ discovery`.                                                                                                                                                                  |
| `to_stage`        | String                                | Stage we entered.                                                                                                                                                                                                                |
| `reason`          | Text, optional                        | Operator-supplied explanation on stage advances. The initial `→ discovery` row is written by `create_candidate()` with the literal string `"Candidate created"` (`services/graduation.py:94`). Highly recommended on rejections. |
| `transitioned_by` | FK → `users`, optional                | Who moved it.                                                                                                                                                                                                                    |
| `created_at`      | Timestamp                             | When the move happened.                                                                                                                                                                                                          |

Three properties make this audit log trustworthy:

1. **No `updated_at` column.** The model has no mechanism to mutate a transition row.
2. **Cascade-delete from candidate, not the other way around.** If you delete a candidate row, its transitions go too — but you cannot delete a transition without deleting the candidate.
3. **Append-only at the application layer.** The service writes one row per `update_stage()` call. There is no UPDATE path.

Together these mean: **"what was this candidate's stage on date X?"** is always answerable from history, even if the current stage has moved on.

### 1.5 What freezes at creation (there is no separate "approve" freeze step)

There is **no approve-time event** that freezes anything. The candidate's `config`, `metrics`, `strategy_id`, `research_job_id`, `promoted_by`, and `promoted_at` are all written **once at creation** in `services/graduation.py:76–85` and never touched again by any service method — `update_stage()` only writes `stage` and inserts a transition row. There is no `update_config()`, no `update_metrics()`, and no separate freeze that fires when you advance into `paper_candidate` / `live_candidate`.

What gets stamped at creation:

- **`config`** — the strategy parameter dict. Frozen from creation.
- **`metrics`** — the snapshot from the originating research trial (Sharpe, Sortino, OOS Sharpe, fold count, walk-forward fingerprint, etc., as supplied by the caller). Frozen from creation.
- **`research_job_id`** — provenance (nullable; set when the candidate came via the research-promotion path, NULL on manual create). Frozen from creation.
- **`strategy_id`** — which strategy file. Frozen from creation. The strategy's own `code_hash` lives on the `Strategy` row and on Backtest rows, not on the candidate.
- **`promoted_by`** — set to the calling user. Frozen from creation.
- **`promoted_at`** — set to `datetime.now(UTC)` at creation. Frozen from creation.

What can change after creation:

- **`stage`** — the only field `update_stage()` writes.
- **`deployment_id`** — set when the candidate is bound to a `LiveDeployment` (set by other code, never by `update_stage()`).
- **`updated_at`** — `TimestampMixin` bumps this on any UPDATE on the row.
- **`notes`** — schema is `Text, nullable`; nothing in the current service mutates it after creation, but there is no DB-level constraint preventing future code from doing so.

The naming is misleading: `promoted_at` sounds like an approval timestamp, but it is really a "row created at" timestamp. If you need "when did this enter live?", scan `graduation_stage_transitions` filtered by `to_stage="live_running"`.

### 1.6 Why a state machine and not a boolean `approved` flag

Two reasons. The **first** is that the journey from "research winner" to "running with real money" is not one decision; it is a chain of decisions, and each link benefits from being individually auditable. "When did we move this onto paper?" "Who approved the live transition?" "Why was it paused?" — the audit log is the answer, and you only get a readable audit log if the steps are explicit.

The **second** is that operational reality has more than two states. A candidate that is currently `paused` after running live is a different object than one that is `paper_review` waiting for evaluation. They cannot share a single `approved=true|false` boolean without losing information.

---

## 2. The three surfaces

Per the project ordering rule (API-first, CLI-second, UI-third), every operation has a canonical API; the CLI is a thin wrapper for read-class operations; the UI carries the operator-facing approve/reject controls.

| Intent                         | API                                                                                                                                          | CLI                                                                | UI                                                                  | Observe / Verify                                                      |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------- | --------------------------------------------------------------------- |
| List candidates (filterable)   | `GET /api/v1/graduation/candidates?stage={stage}&limit={N}` — `api/graduation.py:45–64`                                                      | `msai graduation list` — `cli.py:502–512`                          | `/graduation` Kanban board — `frontend/src/app/graduation/page.tsx` | Card appears in the column for its current stage.                     |
| Show one (detail + history)    | `GET /api/v1/graduation/candidates/{id}` + `GET /api/v1/graduation/candidates/{id}/transitions` — `api/graduation.py:110–124` and `:184–206` | `msai graduation show {id}` (merges both calls) — `cli.py:515–540` | Click a card on `/graduation` → inline `DetailPanel`                | Detail panel shows config, metrics, full transition list.             |
| Create candidate (manual path) | `POST /api/v1/graduation/candidates` — `api/graduation.py:72–102` (422 on missing strategy / research_job)                                   | n/a (CLI is read-only)                                             | n/a (created via promote in `/research`)                            | New candidate appears in `discovery` with one transition row.         |
| Advance stage (approve/reject) | `POST /api/v1/graduation/candidates/{id}/stage` body `{"stage": "...", "reason": "..."}` — `api/graduation.py:132–176`                       | n/a (CLI is read-only)                                             | Stage-advance form inside the inline `DetailPanel` on `/graduation` | Candidate stage updated; new `GraduationStageTransition` row written. |
| Audit trail                    | `GET /api/v1/graduation/candidates/{id}/transitions` — `api/graduation.py:184–206`                                                           | (rolled into `msai graduation show`)                               | History list inside the `DetailPanel`                               | Sorted-by-`created_at` list with from/to/reason/actor.                |

Two things to call out explicitly:

- **Approve and reject are UI-only in Phase 1.** The CLI does not expose `POST .../stage`. This is deliberate — the project ordering rule still applies (the API is canonical), but the CLI surface for graduation is intentionally read-only, because graduation is an operator decision, not a scriptable cron action. If you want to script it, hit the API directly with `curl` or `httpx`.
- **There is no separate "approve" or "reject" endpoint.** Stage advancement is a single `POST .../stage` route. "Approve" is whichever stage move advances the candidate (e.g., `validation → paper_candidate`); "reject" is `→ archived` with a `reason`. The UI buttons render those two as distinct actions but they post to the same route.

---

## 3. Internal sequence diagram

What happens after `POST /api/v1/graduation/candidates/{id}/stage` enters the API:

```
Caller (UI button | curl | httpx)
   │
   │  POST /api/v1/graduation/candidates/{id}/stage
   │  body: {"stage": "paper_candidate", "reason": "OOS coverage OK"}
   ▼
api/graduation.py:132–176  (update_candidate_stage)
   │
   ├─► auth: PyJWT validates Bearer or X-API-Key
   │
   ▼
GraduationService.update_stage(session, candidate_id, new_stage, reason, user_id)
   │   services/graduation.py:107–149
   │
   ├─► load candidate by id
   │     │
   │     └─ not found  ─► raise ValueError ─► API returns 404
   │
   ├─► validate (current_stage, new_stage) ∈ VALID_TRANSITIONS
   │     │
   │     └─ illegal move
   │           ├─ raise GraduationStageError("Cannot transition from '<cur>' to '<new>'. ...")
   │           └─ API catches at api/graduation.py:160–172
   │                returns 422 with body {
   │                  "detail": {
   │                    "message": "Cannot transition from 'validation' to 'live_running'. ...",
   │                    "current_stage": "validation",
   │                    "allowed_transitions": ["archived", "paper_candidate"]
   │                  }
   │                }
   │
   │   No further validation — there is no risk-overlay, no metrics-shape check,
   │   no per-stage RBAC. Illegal-transition is the only rejection class on this path.
   │
   ├─► UPDATE graduation_candidates SET stage = new_stage, updated_at = now()
   │
   ├─► INSERT graduation_stage_transitions
   │       (candidate_id, from_stage, to_stage, reason, transitioned_by=user_id)
   │
   ├─► flush() (no commit — caller controls tx boundary)
   │
   ▼
return GraduationCandidate (refreshed)
   │
   ▼
Pydantic GraduationCandidateResponse (schemas/graduation.py:29–45)
   │
   ▼
HTTP 200 with refreshed candidate body
```

A few notes on the reject paths from the stage-advance endpoint:

- **404 (candidate not found)** — `update_stage()` raises `ValueError` (`services/graduation.py:117–119`); the API translates.
- **422 (illegal transition)** — the only rejection class on `POST .../stage`. Comes from the `VALID_TRANSITIONS` lookup. The `detail.allowed_transitions` list is computed by `_service.get_allowed_transitions(current_stage)` (`api/graduation.py:164`) so the caller (especially the UI) can render a useful error.

The sibling `POST /api/v1/graduation/candidates` route (manual create) has its own 422 path: if `strategy_id` or `research_job_id` does not exist, `create_candidate()` raises `ValueError` and the API returns 422 with `detail` set to the exception message (`api/graduation.py:94–98`, `services/graduation.py:64–74`). Same status code, different shape — `detail` is a plain string here, not the structured object the stage-advance endpoint returns.

The transition row is written **inside the same transaction** as the candidate stage update. Either both happen or neither does. There is no window where the candidate's `stage` has advanced but the audit log is silent.

The initial creation transition (`from_stage="" → to_stage="discovery"`) is written by `create_candidate()` (`services/graduation.py:51–105`) using the same pattern — one INSERT for the candidate, one INSERT for the transition, in the same transaction.

---

## 4. See, verify, troubleshoot

### 4.1 The `/graduation` queue

`frontend/src/app/graduation/page.tsx` is the operator's home screen for the gate. It is a single-route page — no `[id]` segment, no separate detail route — laid out as:

- A **KPI strip** of four cards at the top (`page.tsx:507–528`): Total Candidates, Paper Flow (sum of `paper_candidate + paper_running + paper_review`), Live Flow (sum of `live_candidate + live_running`), and Paused / Archived. These are stage groupings, not per-stage counts.
- A horizontally-scrolling **Kanban board** (`page.tsx:530–584`) with **one column per stage** — all nine stages in source order. Each column shows its label, a count badge, and the candidate cards currently in that stage.
- Candidate cards (`page.tsx:110–142`) render the strategy name and three metric values pulled by key from the frozen `metrics` JSONB: `sharpe_ratio`, `total_return`, `win_rate` (formatted as `S: …`, `R: …`, `W: …`). Click a card to select it.

The page calls `apiGet("/api/v1/graduation/candidates?limit=100")` once on mount (`page.tsx:382–384`). There is **no `?stage=` URL filtering** — the filter argument exists on the API but the page does not wire it. Filtering is visual: each stage is its own column.

### 4.2 The inline detail panel and transition history

There is **no separate detail route**. Selecting a card on the Kanban board renders an inline `DetailPanel` component (`page.tsx:158–350`, mounted at `:586–597`) below the board, scoped to the selected candidate. When a card is selected, `selectCandidate()` (`page.tsx:412–432`) issues `GET /api/v1/graduation/candidates/{id}/transitions` to populate the history; the candidate detail itself is already in memory from the list call.

The panel renders:

- **Header** — strategy name (or truncated UUID fallback), a stage badge, and a `Created <timestamp>` line. There is no "promoted by" display in the current panel.
- **Metrics grid** — every key/value pair from the candidate's frozen `metrics` JSONB, rendered as small tiles. Numbers are formatted to four decimals.
- **Config block** — the frozen `config` JSONB pretty-printed in a `<pre>` block.
- **Notes** — rendered if non-empty.
- **Research-job link** — if `research_job_id` is set, a button linking to `/research/{research_job_id}`.
- **Stage advance form** — a `Select` populated from the frontend's mirror of `VALID_TRANSITIONS` (`page.tsx:57–67`) for the candidate's current stage, plus an optional reason textarea, plus an "Advance Stage" button. Submission posts to `POST /api/v1/graduation/candidates/{id}/stage` (`page.tsx:434–458`); on success the page re-loads the list and clears the selection. If the current stage is `archived` (no legal next stage), the form is hidden.
- **Transition history** — a chronological list of `GraduationStageTransition` rows (oldest-first, matching the API's sort) showing `from_stage → to_stage`, the timestamp, and the optional reason.

The history is read-only by design — there is no edit/delete action on a transition row anywhere in the UI.

### 4.3 The CLI as a quick read-out

```bash
# All candidates currently in paper_review:
uv run msai graduation list --stage paper_review

# Full detail + transition history for one candidate:
uv run msai graduation show <candidate-id>
```

`msai graduation show` (`backend/src/msai/cli.py:515–540`) merges the GET-detail and GET-transitions responses into a single JSON object with a `transitions` array. Useful for quick `jq` filtering or dumping into a runbook log.

### 4.4 Audit trail as the source of truth

If anyone ever asks "who approved candidate X for live, and when?" — the answer is in `graduation_stage_transitions`. The query you want is:

```sql
SELECT created_at, from_stage, to_stage, transitioned_by, reason
FROM graduation_stage_transitions
WHERE candidate_id = '...'
ORDER BY created_at;
```

You should not need to run that SQL by hand — the API exposes it as `GET /api/v1/graduation/candidates/{id}/transitions`. But the table is your forensic record and is intentionally append-only.

---

## 5. Common failures

### 5.1 Illegal transition (422)

You tried to move a candidate from a stage that has no path to your target stage. Examples:

- `archived → live_running` — archived is terminal.
- `discovery → paper_running` — must go through `validation` first.
- `live_candidate → paper_running` — the live and paper paths don't cross at this point.

The 422 body lists the allowed next stages. Either pick a legal next stage, or unwind your assumption about the candidate's state — usually the latter (the UI buttons only show legal moves, so a 422 typically means the UI was working off a stale state).

### 5.2 Manual-create rejection (422)

`POST /api/v1/graduation/candidates` returns 422 when `strategy_id` does not exist (`services/graduation.py:64–66`) or when a non-null `research_job_id` does not exist (`:69–74`). The body is `{"detail": "Strategy <uuid> not found"}` or `{"detail": "Research job <uuid> not found"}` — `detail` is a plain string here, distinct from the structured `detail` object the stage-advance endpoint returns. The fix is the obvious one: pass real IDs.

### 5.3 Candidate not found (404)

The `id` in the URL doesn't exist (or was cascade-deleted along with its parent strategy). 404 comes from `update_stage()` / `get_candidate()` raising `ValueError`, caught by the API layer. Check the candidate ID against the list endpoint.

### 5.4 No per-stage RBAC at the API layer (today)

There is no role-based gating on stage transitions in current code. The graduation endpoints depend on `get_current_user` for authentication only — any authenticated caller can advance any candidate to any legal next stage. **403 is not a path you will see from these endpoints today.** If per-stage RBAC is added later, it will need to be wired into `update_candidate_stage` (`api/graduation.py:136–176`); none of that code exists yet.

---

## 6. Idempotency and retry behavior

### 6.1 Re-approving an already-approved candidate

The behavior is **HTTP 422** with `detail.message` describing the rejection, `detail.current_stage` set to the candidate's actual stage, and `detail.allowed_transitions` listing the legal next stages. The reason: `paper_candidate → paper_candidate` is not in `VALID_TRANSITIONS` — there is no "self-loop" anywhere in the matrix.

This is the right behavior for an audit log: a no-op transition would either silently succeed (corrupting the "what is the meaningful sequence?" question) or write a duplicate `from=X to=X` row (corrupting the audit). Returning 422 forces the caller to recognize the candidate is already where they want it.

If your script wants idempotent "ensure stage is X", read the candidate's current stage first and skip the POST if it's already there.

### 6.2 Each transition row is independent

There is no unique constraint on `(candidate_id, from_stage, to_stage)` — a candidate can legitimately revisit a stage (e.g., `paper_review → discovery → validation → paper_candidate → paper_running → paper_review` is a perfectly normal cycle). Each visit gets its own transition row, distinguished by `created_at`.

### 6.3 No retries needed at the worker layer

There is no worker. There is no queue. Graduation is a synchronous request/response state machine, so "retry behavior" is just standard HTTP retry semantics — if you get a 5xx, retry once; if you get a 4xx, fix the request and retry. No arq dead-letter, no compute slot bookkeeping, no heartbeats. This is by design — graduation should be cheap, transactional, and never have to be reconciled.

### 6.4 Promotion idempotency (the seam in)

`POST /api/v1/research/promotions` (the research-side seam, documented in `how-research-and-selection-works.md`) creates a new `GraduationCandidate` row each time it is called. There is no de-duplication. If you promote the same `(research_job_id, trial_index)` twice, you get two candidates — both at `discovery`, both with the same config and metrics. Archive one. (The research-side caller is expected to be idempotent itself, and the UI Promote button is debounced.)

---

## 7. Rollback and repair

### 7.1 You cannot delete a transition

`GraduationStageTransition` rows are append-only by design. The model exposes no UPDATE or DELETE endpoint, the service has no method to mutate a transition, and the only way one can disappear is via cascade-delete when its parent candidate is removed.

This is the right design. The audit log is the audit log. If you could erase rows, the question "who approved X on Y date?" would no longer have a reliable answer.

### 7.2 To "revoke" approval, transition forward — never backward

If you approved a candidate and then realized that was wrong, the repair path is **not** to delete the approval. The repair path is to add a new transition that reflects the new decision:

- `live_candidate → archived` (with `reason="approval revoked: [why]"`) — kills it.
- `live_running → paused → live_candidate → archived` — if it is currently running, walk it down through legal stages (you cannot skip directly from `live_running` to `archived` — well, actually you can per the matrix, but the conventional path is `live_running → paused → archived` so the operator is forced to confirm the position state).
- `paper_review → discovery` — explicit "send back to research" path. Useful when paper performance disagreed with backtest performance and you want to re-evaluate.

In every case, the audit log preserves the full history. Looking back, you can see "approved on T1, revoked on T2" — which is exactly the information you want when reviewing what happened.

### 7.3 You cannot delete a candidate that has a deployment

`GraduationCandidate.deployment_id` is set when the candidate is bound to a `LiveDeployment`. Foreign-key constraints from `live_deployments` and `portfolio_allocations` will block a `DELETE` of a candidate that any live or portfolio object references. This is intentional — if a deployment is running with a candidate, that candidate's row is part of the live audit trail and cannot be made to disappear.

The operational answer is `archive`, not `delete`. Move the candidate to `archived` and leave the row in place. Archival is cheap, reversible-by-creating-a-new-candidate, and preserves the history.

### 7.4 To "fix" a candidate's config

You can't. The `config` JSONB is frozen at creation. If the config is wrong, archive the candidate and create a new one with the correct config (typically by re-promoting from research with the correct trial selected).

The same applies to `metrics`. If the metrics blob is incomplete (missing the OOS coverage field, for instance), you cannot patch it — archive and re-promote.

This is a deliberate constraint. A candidate's config and metrics are what the gate is deciding **about**. If you let those mutate, every prior decision in the audit log becomes meaningless ("approved on T1 with metrics M1, but M1 has been edited since, so the approval no longer corresponds to anything").

---

## 8. Key files

| Path                                                           | What lives here                                                                            |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `backend/src/msai/api/graduation.py:45–64`                     | `GET /api/v1/graduation/candidates` — list with stage filter.                              |
| `backend/src/msai/api/graduation.py:72–102`                    | `POST /api/v1/graduation/candidates` — manual create (rare; usually via research promote). |
| `backend/src/msai/api/graduation.py:110–124`                   | `GET /api/v1/graduation/candidates/{id}` — detail.                                         |
| `backend/src/msai/api/graduation.py:132–176`                   | `POST /api/v1/graduation/candidates/{id}/stage` — advance stage (the gate action).         |
| `backend/src/msai/api/graduation.py:184–206`                   | `GET /api/v1/graduation/candidates/{id}/transitions` — audit-trail read.                   |
| `backend/src/msai/services/graduation.py:30–31`                | `GraduationStageError` — raised on illegal transitions.                                    |
| `backend/src/msai/services/graduation.py:34–188`               | `GraduationService` — state-machine logic.                                                 |
| `backend/src/msai/services/graduation.py:37–47`                | `VALID_TRANSITIONS` dict — the canonical matrix.                                           |
| `backend/src/msai/services/graduation.py:51–105`               | `create_candidate()` — write candidate + initial transition in one tx.                     |
| `backend/src/msai/services/graduation.py:107–149`              | `update_stage()` — validate, write candidate update + transition in one tx.                |
| `backend/src/msai/services/graduation.py:151–165`              | `list_candidates()` — read with optional stage filter.                                     |
| `backend/src/msai/services/graduation.py:174–184`              | `get_transitions()` — read audit trail for one candidate.                                  |
| `backend/src/msai/services/graduation.py:186–188`              | `get_allowed_transitions()` — what next stages are legal? (used in 422 body)               |
| `backend/src/msai/models/graduation_candidate.py:22–55`        | `GraduationCandidate` ORM model.                                                           |
| `backend/src/msai/models/graduation_stage_transition.py:19–46` | `GraduationStageTransition` ORM model — append-only audit row.                             |
| `backend/src/msai/schemas/graduation.py:12–19`                 | `GraduationCandidateCreate` request schema.                                                |
| `backend/src/msai/schemas/graduation.py:22–26`                 | `GraduationStageUpdate` request schema (the body of POST .../stage).                       |
| `backend/src/msai/schemas/graduation.py:29–45`                 | `GraduationCandidateResponse` schema.                                                      |
| `backend/src/msai/schemas/graduation.py:55–66`                 | `GraduationTransitionResponse` schema.                                                     |
| `backend/src/msai/cli.py:502–512`                              | `msai graduation list` CLI command.                                                        |
| `backend/src/msai/cli.py:515–540`                              | `msai graduation show` CLI command (merges detail + transitions).                          |
| `frontend/src/app/graduation/page.tsx`                         | `/graduation` Kanban board + KPI strip + inline `DetailPanel` (single route, no `[id]`).   |
| `frontend/src/app/graduation/page.tsx:45–65`                   | Frontend mirror of `VALID_TRANSITIONS` (server is the authority).                          |
| `backend/src/msai/api/research.py:249–322`                     | `POST /api/v1/research/promotions` — the seam IN (research → discovery).                   |
| `backend/src/msai/models/portfolio_allocation.py:19–48`        | `PortfolioAllocation.candidate_id` — the seam OUT (graduation → backtest portfolio).       |

---

**Date verified against codebase:** 2026-04-28

← Previous: [How Research and Selection Work](how-research-and-selection-works.md) · Next: [How Backtest Portfolios Work](how-backtest-portfolios-work.md) → · Up: [Developer Journey](00-developer-journey.md)
