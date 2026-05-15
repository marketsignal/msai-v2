# Live Deployment Workflow — UI + CLI Catch-up

**Goal:** Bring the UI and CLI up to parity with the safety-trio-hardened live-deployment API (PRs #64/#65/#66). Today operators must use curl to deploy real money safely.

**Architecture:** Wire existing internal APIs (`/api/v1/live-portfolios/*`, `/api/v1/live/start-portfolio`, `/api/v1/live/resume`, stop/kill-all flatness response shapes) to (a) new UI screens with binding-fingerprint confirm + paper-trading toggle + 422 BINDING_MISMATCH surfacing, (b) new CLI subcommands mirroring the same contracts.

**Two backend fixes** required to make this feature work correctly (Codex plan-review iter 1):

1. `GET /api/v1/live/status` currently returns `_risk_engine.is_halted` (in-memory) instead of the persistent Redis halt flag set by `/kill-all`. The Resume button would disappear on page reload otherwise. Fix: read Redis halt flag in `/status` (1-line change in `api/live.py:1931`).
2. Add `GET /api/v1/live-portfolio-revisions/{revision_id}/members` — currently the only members endpoint reads the DRAFT (`/live-portfolios/{id}/members`); after snapshot the draft is empty and the UI/CLI have no read path for frozen revision contents.

**Tech Stack:** Next.js 15 + shadcn/ui (frontend), Typer (CLI), TypeScript strict + Python 3.12 type hints, no new external dependencies.

## Approach Comparison — N/A

Streamlined per Pablo "(2)": the design is concrete catch-up work hitting already-hardened APIs. No architectural fork to compare. The Codex audit ranked these gaps and served as the contrarian validation.

## Contrarian Verdict

VALIDATE — Codex audit (this session) explicitly recommended scoping as "Live Deployment Workflow Catch-up" over generic "Portfolios UI". Codex caught two gaps I missed (the actively-broken 410 button + the `ib_login_key` CLI omission). Treating Codex audit as in-place contrarian gate, no separate council needed.

## Files

### NEW

- `frontend/src/app/live-trading/portfolio/page.tsx` — Portfolio compose + snapshot + start screen
- `frontend/src/components/live/portfolio-compose.tsx` — Add/remove member rows; weight + instruments + config JSON editor; snapshot button
- `frontend/src/components/live/portfolio-start-dialog.tsx` — Binding-fingerprint preview, paper_trading toggle with real-money warning, ib_login_key required field, BINDING_MISMATCH 422 diff display
- `frontend/src/components/live/flatness-display.tsx` — `broker_flat` badge + `remaining_positions` table; used by both stop result and kill-all detail
- `frontend/src/components/live/resume-button.tsx` — Post-kill-all recovery action; only visible when `risk_halted: true`
- `frontend/src/lib/api/live-portfolios.ts` — typed client for `/api/v1/live-portfolios/*` CRUD + `/snapshot`
- `tests/e2e/use-cases/live/portfolio-compose-deploy.md` — UC1..UC6 graduated post-PASS

### MODIFIED

- `frontend/src/components/live/strategy-status.tsx` — **DELETE the start-strategy handler entirely** (lines 47–66). Codex plan-review P2: ALSO update the Stop button's enabled condition from `running` only → `starting | building | ready | running` to match backend active filtering (`api/live.py:1903`).
- `frontend/src/components/live/kill-switch.tsx` — **Stop discarding the `/kill-all` JSON response** (line 30). Pass response to `<FlatnessDisplay>` so operator sees `any_non_flat` + per-deployment `flatness_reports`.
- `frontend/src/app/live-trading/page.tsx` — Add link to the new `/live-trading/portfolio` route; surface `risk_halted` flag + Resume button at top.
- `frontend/src/lib/api.ts` — Add types for `LivePortfolio`, `LivePortfolioRevision`, `LivePortfolioMemberFrozen` (from new revision-members endpoint), `LivePortfolioStrategy`, `PortfolioStartResponse`, `LiveStopResponse` (broker_flat + remaining_positions), `LiveKillAllResponse` (top-level: `stopped`, `failed_publish`, `risk_halted`, `any_non_flat`, `flatness_reports[].stop_nonce`), `LiveResumeResponse`. Add fetchers for `/live/resume`, `/live/positions`, `/live/trades`, `/live-portfolios/*`, `/live-portfolio-revisions/{id}/members`.
- `backend/src/msai/api/live.py` — Backend fix #1: change `/live/status` to read Redis halt flag instead of `_risk_engine.is_halted` (line 1931).
- `backend/src/msai/api/portfolios.py` — Backend fix #2: add `GET /api/v1/live-portfolio-revisions/{revision_id}/members` returning frozen revision strategy members. Codex iter-2 P2: `portfolios.py`'s router has `prefix="/api/v1/live-portfolios"`; the new endpoint uses a different prefix, so add it via a **second `APIRouter`** in the same file (`revisions_router = APIRouter(prefix="/api/v1/live-portfolio-revisions", tags=["live-portfolio-revisions"])`) and register both routers in `main.py`. Alternative: rewrite the existing router to drop the prefix and put paths inline — more invasive; use the second-router approach.
- `backend/src/msai/cli.py` — Add `live` sub-app commands. Use `--strategy-id` (UUID), not `--strategy <name>` (avoids name-resolution round-trip + 422 risk). The existing `live start` Typer command already hits `/start-portfolio`; add a **required** `--ib-login-key` option and confirm prompt on `--no-paper`. NO `--deployment` flag on `positions` (the API doesn't support it; `/live/trades` does).

## Tasks

| ID  | Depends on | Writes (concrete file paths)                                                                                                                                        |
| --- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T0a | —          | `backend/src/msai/api/live.py` (fix `/live/status` to read Redis halt flag — backend fix #1)                                                                        |
| T0b | —          | `backend/src/msai/api/portfolios.py` + `backend/src/msai/schemas/live_portfolio.py` + `backend/src/msai/main.py` (register new `revisions_router`) — backend fix #2 |
| T0c | T0a, T0b   | `backend/tests/unit/test_live_api_status.py`, `backend/tests/unit/test_live_portfolios_api.py` (regression tests for both backend fixes)                            |
| T1  | T0a, T0b   | `frontend/src/lib/api.ts` (types + fetchers — additive; includes new revision-members type)                                                                         |
| T2  | T1         | `frontend/src/lib/api/live-portfolios.ts`                                                                                                                           |
| T3  | T1         | `frontend/src/components/live/flatness-display.tsx`                                                                                                                 |
| T4  | T1         | `frontend/src/components/live/resume-button.tsx`                                                                                                                    |
| T5  | T2, T3, T4 | `frontend/src/components/live/portfolio-compose.tsx`                                                                                                                |
| T6  | T2, T3, T4 | `frontend/src/components/live/portfolio-start-dialog.tsx`                                                                                                           |
| T7  | T5, T6     | `frontend/src/app/live-trading/portfolio/page.tsx`                                                                                                                  |
| T8  | T7         | `frontend/src/app/live-trading/page.tsx` + `frontend/src/components/live/kill-switch.tsx` (wire flatness response)                                                  |
| T9  | T8         | `frontend/src/components/live/strategy-status.tsx` (delete handleStartStrategy + button + widen Stop condition)                                                     |
| T10 | T0a, T0b   | `backend/src/msai/cli.py` (add 9 new commands; `--strategy-id` UUID; `--ib-login-key` required; confirm on `--no-paper`; NO `--deployment` on `positions`)          |
| T11 | T10        | `backend/tests/unit/test_cli_live_portfolio.py` (Typer CliRunner tests; mock httpx)                                                                                 |
| T12 | T7, T10    | `tests/e2e/use-cases/live/portfolio-compose-deploy.md` (UC1..UC6 — drafted in plan, graduated post-PASS)                                                            |

**Concurrency cap:** 3. Sequential mode for T1-T4 because they all edit `frontend/src/lib/api.ts` indirectly via the same type-export surface. T5 + T6 can run in parallel after T2-T4 land. T10 + T11 can run in parallel with the frontend track.

## Implementation Notes

### T1: api.ts type additions

Add types **before** any imports change. Match the backend Pydantic schemas exactly (verified against `backend/src/msai/schemas/live.py` + `schemas/live_portfolio.py` during Codex plan-review):

- `PortfolioStartResponse`: `{id: UUID, deployment_slug: str, status: str, paper_trading: bool, warm_restart: bool}` (the `binding_fingerprint` lives only inside idempotency body_hash; no body field).
- `LiveStopResponse`: `{id: UUID, status: str, process_status?: string, stop_nonce?: string, broker_flat?: bool | null, remaining_positions?: list}` — Codex iter-3 P2: `process_status`, `stop_nonce`, `broker_flat`, `remaining_positions` are **optional**. The idempotent already-stopped path returns only `{id, status}`; only the cold-stop path with a real stop_report populates the flatness fields. `FlatnessDisplay` must handle undefined for all four optionals (render "No stop report available" when all are absent).
- `LiveKillAllResponse`: `{stopped: int, failed_publish: int, risk_halted: bool, any_non_flat: bool, flatness_reports: list[{deployment_id, broker_flat: bool | null, remaining_positions, stop_nonce}]}` — note `failed_publish`, NOT `kill_nonce` at top level; per-deployment nonces are inside `flatness_reports[]`. **`broker_flat` is `bool | null`** — null when the flatness poll timed out / report never arrived; UI must render that as "Unknown" with a distinct color (orange/yellow), not coerce to false.
- `LiveResumeResponse`: `{resumed: bool}`.
- `LivePortfolioMemberFrozen`: response shape of the new `/live-portfolio-revisions/{id}/members` endpoint (T0b) — mirror `schemas/live_portfolio.LivePortfolioMemberResponse`.

### T6: start-dialog UX

Stages:

1. **Form** — `ib_login_key` (required, free-text, default empty), `paper_trading` toggle (default ON, OFF shows red "REAL MONEY" callout with account_id preview from form), `account_id` (free-text, validate U-prefix vs DU-prefix on submit if `paper_trading=false`).
2. **Preview** — GET the NEW `/api/v1/live-portfolio-revisions/{revision_id}/members` (T0b) to show the snapshot's frozen members + each member's `instruments` + config preview. (`/live-portfolios/{id}/members` reads the DRAFT, which is empty post-snapshot — do NOT use it.) No `binding_fingerprint` shown (it's idempotency-internal); instead show "Binding contract: matching config + instruments will be verified server-side."
3. **Confirm** — explicit "Deploy" button only enabled after operator types account_id in a confirmation input (real-money path only). Paper path: single click.
4. **Result** — On **200 or 201**: navigate to deployment list (200 returns when warm-restart / cached-outcome path serves the request; treat both as success). On 422 BINDING_MISMATCH: render the `details.mismatches` list with `member_value` vs `candidate_value` columns. On 422 LIVE_DEPLOY_CONFLICT: render existing deployment info + "Stop existing" CTA.

### T9: delete the 410 button + widen Stop condition

1. Delete `handleStartStrategy` at `frontend/src/components/live/strategy-status.tsx:47-66` + the "Start" Button in the table row. Verify with `grep -n "handleStartStrategy\|Start Strategy" frontend/src/components/live/strategy-status.tsx` — should return nothing after edit.
2. **Widen the Stop button's enabled condition** (Codex P2): currently shows only on `status == "running"` (line 145). The backend's active-deployment filter at `api/live.py:1903` is `("starting", "building", "ready", "running")` — operators may need to stop a deployment in any of those states (e.g., to cancel a `starting` deployment that hasn't finished spawning). Update to match the backend filter.

The component's job is to LIST existing deployments + offer Stop, not start new ones; portfolio start lives on its own page.

### T10: CLI command shapes

```
msai live portfolio-create --name "drill-abc" --description "..."
msai live portfolio-add-strategy <portfolio-id> --strategy-id <uuid> --config @file.json --instruments AAPL.NASDAQ --weight 1.0
msai live portfolio-snapshot <portfolio-id>
msai live portfolio-members <revision-id>          # hits NEW /live-portfolio-revisions/{id}/members from T0b
msai live start-portfolio --revision <id> --account <id> --ib-login-key <key> [--no-paper]
msai live resume
msai live positions                                # NO --deployment flag; the API doesn't filter
msai live trades [--deployment <id>] [--limit N]   # only /trades supports --deployment
msai live audits <deployment-id>
```

`--strategy-id` is a UUID (not a name) — the backend's `LivePortfolioAddStrategyRequest.strategy_id: UUID` requires it. Avoiding name-resolution keeps the CLI a thin shim and eliminates one 422 failure mode.

On `--no-paper`:

```python
typer.confirm(
    f"This will start REAL-MONEY trading on {account_id}. Continue?",
    abort=True,
)
```

`--ib-login-key` is `typer.Option(..., help="...")` — no default → Typer treats as required.

**CLI base URL** for local dev: the existing `_api_base()` in `cli.py` defaults to `http://localhost:8000`, but dev compose exposes the backend at host port `8800`. E2E and operator runs MUST export `MSAI_API_URL=http://localhost:8800` + `MSAI_API_KEY=msai-dev-key` (or run the CLI inside the backend container via `docker compose exec backend …`). Document this in the new commands' `--help` text where reasonable.

## E2E Use Cases (drafted; graduated post-PASS)

**Shared ARRANGE for UC1/UC2/UC5** (run before each UC; idempotent — checks existing state before mutating):

1. **Resolve the strategy UUID** (not hardcoded — UUIDs are `uuid4` per-environment):
   ```
   curl -sf -H "X-API-Key: msai-dev-key" 'http://localhost:8800/api/v1/strategies/' \
     | jq -r '.items[] | select(.name=="example.smoke_market_order") | .id'
   ```
   Export as `STRATEGY_UUID`.
2. **Find an unlinked `live_candidate`:**
   ```
   curl -sf -H "X-API-Key: msai-dev-key" 'http://localhost:8800/api/v1/graduation/candidates?stage=live_candidate' \
     | jq '.items[] | select(.strategy_id==env.STRATEGY_UUID and .deployment_id==null) | .id'
   ```
3. **If none exists:** create + walk a fresh candidate through the pipeline:
   - `POST /api/v1/graduation/candidates` with body `{strategy_id: $STRATEGY_UUID, config: {bar_type, instruments: ["AAPL.NASDAQ"], instrument_id: "AAPL.NASDAQ"}, metrics: {}}` — the create payload requires `strategy_id`, `config`, `metrics`. Stage defaults to `discovery` server-side; do NOT pass `stage` in the create body.
   - `POST /api/v1/graduation/candidates/{id}/stage` **five times** to walk: discovery → validation → paper_candidate → paper_running → paper_review → live_candidate (5 transitions, not 6 — the create gives you `discovery` for free).
4. **If duplicate `live_candidate` rows exist:** archive all but one via `POST /api/v1/graduation/candidates/{id}/stage` with target `archived`.
5. Between UCs: stop any deployment created by the previous UC (`POST /api/v1/live/stop`) AND archive its now-linked candidate (`POST /api/v1/graduation/candidates/{id}/stage` → `archived`) so the next UC sees a fresh unlinked `live_candidate`.

The verify-e2e agent runs these as ARRANGE; they all use sanctioned public-API methods (no DB writes, no internal endpoints).

### UC1 — UI: Portfolio compose happy path (paper)

**Intent:** Operator creates a portfolio + adds one strategy + snapshots + starts on paper.

**Interface:** UI + API.

**Setup:** Shared ARRANGE above. Note the unlinked candidate's `id` + `config`.

**Steps:**

1. Navigate to `/live-trading/portfolio`
2. Click "New Portfolio", fill name + description, submit
3. Click "Add Strategy", paste the UUID from setup into the strategy-id field, paste a config **matching the candidate's config exactly** (bar_type, instruments, instrument_id), set `weight=1.0`, submit
4. Click "Snapshot" → revision is frozen, "Deploy" button activates
5. Click "Deploy" → fill `ib_login_key=marin1016test`, `account_id=DUP733213`, leave `paper_trading=true`, submit confirmation
6. Response 201; UI navigates back to `/live-trading` (no detail route exists yet — staying on the list is the documented behavior)

**Verify:** `GET /api/v1/live/status?active_only=true` shows the deployment with `status=running` + `paper_trading=true`.

**Persist:** Reload `/live-trading` — deployment visible in the active list.

### UC2 — UI: 422 BINDING_MISMATCH rendered

**Intent:** Member with diverging config (extra `fast_ema_period: 99` key); deploy returns 422 with field-level diff.

**Setup:** Same shared ARRANGE as UC1 — **must be a fresh unlinked candidate**, not the one UC1 just linked.

**Steps:** UC1 steps 1–4, but in step 3 add `fast_ema_period: 99` to the config (which is not in the candidate's config).

**Verify:** Step 5 returns 422; UI shows a table with `field`, `member_value`, `candidate_value` columns. No deployment created.

### UC3 — UI: paper_trading=false real-money confirmation gate

**Intent:** Real-money deploy requires typing `account_id` to confirm.

**Steps:** UC1 steps 1–4, then toggle `paper_trading` OFF, fill `ib_login_key`. The "Deploy" button is disabled until a confirmation input matching `account_id` is filled.

**Verify:** Button disabled until match; once enabled, clicking submits with `paper_trading: false`.

**Persist:** N/A (do not actually fire — this UC tests the gate, not the deploy. UC4 covers the real-money drill).

### UC4 — Operator: real-money UC5 carve-out via UI

**Intent:** Operator uses the UI to deploy `paper_trading=false` on real account; verifies fill happens AND stop produces `broker_flat: true`.

**Operator-only**, paper_trading=false. Requires `.env` flipped to live (mslvp000 / U4705114 / 4003) and IB Gateway up.

**Verify:** Status `running`; positions table shows 1 share AAPL; stop modal shows `broker_flat: true, remaining_positions: []`.

### UC5 — CLI: portfolio-create → snapshot → start-portfolio

**Intent:** Replicate UC1 via CLI.

**Setup:** Shared ARRANGE above. Export `MSAI_API_URL=http://localhost:8800` + `MSAI_API_KEY=msai-dev-key`. Save the candidate's `id` to `STRATEGY_UUID` env var.

**Steps:**

```
msai live portfolio-create --name e2e-cli-uc5 --description ""
msai live portfolio-add-strategy <portfolio-id> --strategy-id $STRATEGY_UUID --config @cfg.json --instruments AAPL.NASDAQ --weight 1.0
msai live portfolio-snapshot <portfolio-id>
msai live start-portfolio --revision <revision-id> --account DUP733213 --ib-login-key marin1016test
```

**Verify:** Each command prints JSON. Final command prints `{"id": "...", "status": "starting" | "building" | "ready" | "running"}` — accept any active status. The 200 already-active shortcut can return any of these, and the 201 cold path returns whatever state was reached by the time the response was assembled. UC verification then polls `GET /api/v1/live/status?active_only=true` to confirm transition to `running` within 30s.

### UC6 — CLI: start-portfolio --no-paper requires confirmation

**Intent:** `--no-paper` triggers a typer confirm prompt; aborting cleanly exits.

**Setup:** A fresh frozen revision (run portfolio-create + add-strategy + snapshot first via UC5 steps 1–3 OR use any prior frozen revision).

**Steps:**

```
echo n | msai live start-portfolio --revision <id> --account U4705114 --ib-login-key mslvp000 --no-paper
```

**Verify:** Non-zero exit code (Typer's `abort=True` exits with code 1); output contains the "REAL-MONEY" string from the confirm prompt. `GET /api/v1/live/status` shows no new deployment. (Note: Typer's `confirm` writes the prompt to stdout, not stderr — the assertion is "contains 'REAL-MONEY'" without specifying which stream.)

## Out of Scope

Defer to follow-up PRs:

- Multi-deployment WebSocket UI (currently first-running only — separate work)
- Account health / broker portfolio UI
- Alerts list UI/CLI
- Research/graduation CLI gaps
- Strategy edit/delete UI
- Settings page cleanup (`/auth/me`, hardcoded Admin, fake notification save, nonexistent `/api/v1/admin/clear-data`)
