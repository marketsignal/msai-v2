# CLI completeness — 100% API parity

**Goal:** Bring `msai` CLI to **complete REST parity** with every public `/api/v1/*` HTTP endpoint, so any operator can manage MSAI 100% from a terminal. After this PR there should be zero ops tasks that require `curl` or the UI. WebSocket streaming (`/api/v1/live/stream/{deployment_id}`) is intentionally **N/A** — the CLI is a one-shot/RPC surface; long-lived event streaming is a different ergonomics class. Users who need it can subscribe via the UI's `useLiveStream` hook or build their own client.

**Architecture:** Thin shims over existing API endpoints. Same pattern as PR #67's CLI track. No backend changes. No novel architecture.

**Tech Stack:** Typer + httpx (in use). Tests via `CliRunner` + `unittest.mock.patch` on `msai.cli.httpx.request`.

## Approach — N/A

Streamlined: scope is concrete CLI catch-up over already-shipped APIs. No architectural choice.

## Comprehensive gap matrix

API endpoint inventory cross-referenced against `msai` CLI as of `d20fc26` (post-PR #67). ✅ = covered today; ❌ = gap this PR addresses; — = N/A (Prometheus metrics, raw infra probes).

| Endpoint                                                  | Methods                                                                                                                                       | Current CLI                                                                          | Plan                                                                                    |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `/strategies/`                                            | GET                                                                                                                                           | `strategy list` ✅                                                                   | —                                                                                       |
| `/strategies/{id}`                                        | GET                                                                                                                                           | `strategy show` ✅                                                                   | —                                                                                       |
| `/strategies/{id}`                                        | PATCH                                                                                                                                         | ❌                                                                                   | `strategy edit`                                                                         |
| `/strategies/{id}`                                        | DELETE                                                                                                                                        | ❌                                                                                   | `strategy delete`                                                                       |
| `/strategies/{id}/validate`                               | POST                                                                                                                                          | `strategy validate` ✅                                                               | —                                                                                       |
| `/strategy-templates/`                                    | GET                                                                                                                                           | ❌                                                                                   | NEW sub-app `template list`                                                             |
| `/strategy-templates/scaffold`                            | POST                                                                                                                                          | ❌                                                                                   | NEW sub-app `template scaffold`                                                         |
| `/backtests/run`                                          | POST                                                                                                                                          | `backtest run` ✅                                                                    | —                                                                                       |
| `/backtests/history`                                      | GET                                                                                                                                           | `backtest history` ✅                                                                | —                                                                                       |
| `/backtests/{id}/status`                                  | GET                                                                                                                                           | partial via `show` ✅                                                                | —                                                                                       |
| `/backtests/{id}/results`                                 | GET                                                                                                                                           | partial via `show` ✅                                                                | —                                                                                       |
| `/backtests/{id}/report-token` + `/backtests/{id}/report` | POST→GET                                                                                                                                      | ❌                                                                                   | `backtest report <id> --out`                                                            |
| `/backtests/{id}/trades`                                  | GET (paginated, max 500/page)                                                                                                                 | ❌                                                                                   | `backtest trades <id> [--page N --page-size M --out FILE]`                              |
| `/research/jobs`                                          | GET                                                                                                                                           | `research list` ✅                                                                   | —                                                                                       |
| `/research/jobs/{id}`                                     | GET                                                                                                                                           | `research show` ✅                                                                   | —                                                                                       |
| `/research/jobs/{id}/cancel`                              | POST                                                                                                                                          | `research cancel` ✅                                                                 | —                                                                                       |
| `/research/sweeps`                                        | POST                                                                                                                                          | ❌                                                                                   | `research sweep`                                                                        |
| `/research/walk-forward`                                  | POST                                                                                                                                          | ❌                                                                                   | `research walk-forward`                                                                 |
| `/research/promotions`                                    | POST                                                                                                                                          | ❌                                                                                   | `research promote`                                                                      |
| `/graduation/candidates`                                  | GET                                                                                                                                           | `graduation list` ✅                                                                 | —                                                                                       |
| `/graduation/candidates`                                  | POST                                                                                                                                          | ❌                                                                                   | `graduation create`                                                                     |
| `/graduation/candidates/{id}`                             | GET                                                                                                                                           | `graduation show` ✅ (combines `transitions`)                                        | —                                                                                       |
| `/graduation/candidates/{id}/stage`                       | POST                                                                                                                                          | ❌                                                                                   | `graduation stage`                                                                      |
| `/graduation/candidates/{id}/transitions`                 | GET                                                                                                                                           | bundled in `show` ✅                                                                 | —                                                                                       |
| `/live/start`                                             | POST                                                                                                                                          | `live start` ✅                                                                      | —                                                                                       |
| `/live/start-portfolio`                                   | POST                                                                                                                                          | `live start-portfolio` ✅                                                            | —                                                                                       |
| `/live/stop`                                              | POST                                                                                                                                          | `live stop` ✅                                                                       | —                                                                                       |
| `/live/kill-all`                                          | POST                                                                                                                                          | `live kill-all` ✅                                                                   | —                                                                                       |
| `/live/resume`                                            | POST                                                                                                                                          | `live resume` ✅                                                                     | —                                                                                       |
| `/live/status`                                            | GET                                                                                                                                           | `live status` ✅                                                                     | —                                                                                       |
| `/live/status/{deployment_id}`                            | GET                                                                                                                                           | ❌                                                                                   | `live status-show <id>`                                                                 |
| `/live/positions`                                         | GET                                                                                                                                           | `live positions` ✅                                                                  | —                                                                                       |
| `/live/trades`                                            | GET                                                                                                                                           | `live trades` ✅                                                                     | —                                                                                       |
| `/live/audits/{id}`                                       | GET                                                                                                                                           | `live audits` ✅                                                                     | —                                                                                       |
| `/live-portfolios`                                        | GET / POST                                                                                                                                    | `portfolio-create` ✅ (POST); list ❌                                                | `live portfolio-list`                                                                   |
| `/live-portfolios/{id}`                                   | GET                                                                                                                                           | ❌                                                                                   | `live portfolio-show`                                                                   |
| `/live-portfolios/{id}/members` (draft)                   | GET                                                                                                                                           | ❌                                                                                   | `live portfolio-draft-members`                                                          |
| `/live-portfolios/{id}/strategies`                        | POST                                                                                                                                          | `portfolio-add-strategy` ✅                                                          | —                                                                                       |
| `/live-portfolios/{id}/snapshot`                          | POST                                                                                                                                          | `portfolio-snapshot` ✅                                                              | —                                                                                       |
| `/live-portfolio-revisions/{id}/members` (frozen)         | GET                                                                                                                                           | `portfolio-members` ✅                                                               | —                                                                                       |
| `/portfolios` (research-backtest)                         | GET / POST                                                                                                                                    | `portfolio list` ✅; create ❌                                                       | `portfolio create`                                                                      |
| `/portfolios/{id}`                                        | GET                                                                                                                                           | `portfolio show` ✅                                                                  | —                                                                                       |
| `/portfolios/runs`                                        | GET                                                                                                                                           | `portfolio runs` ✅                                                                  | —                                                                                       |
| `/portfolios/runs/{id}`                                   | GET                                                                                                                                           | ❌                                                                                   | `portfolio run-show`                                                                    |
| `/portfolios/runs/{id}/report`                            | GET (HTML)                                                                                                                                    | ❌                                                                                   | `portfolio run-report --out`                                                            |
| `/portfolios/{id}/runs`                                   | POST                                                                                                                                          | `portfolio run` ✅                                                                   | —                                                                                       |
| `/market-data/bars/{symbol}`                              | GET                                                                                                                                           | ❌                                                                                   | NEW sub-app `market-data bars`                                                          |
| `/market-data/ingest`                                     | POST                                                                                                                                          | top-level `ingest` calls LOCAL `DataIngestionService`, NOT the API — Codex iter-2 P1 | NEW `market-data ingest` (API-backed); keep top-level `ingest` as offline shortcut      |
| `/market-data/status`                                     | GET                                                                                                                                           | top-level `data-status` reads LOCAL filesystem stats, NOT the API — Codex iter-2 P1  | NEW `market-data status` (API-backed); keep top-level `data-status` as offline shortcut |
| `/market-data/symbols`                                    | GET                                                                                                                                           | ❌                                                                                   | NEW sub-app `market-data symbols`                                                       |
| `/live/stream/{deployment_id}`                            | WS                                                                                                                                            | N/A — WebSocket, intentionally out of scope                                          | —                                                                                       |
| `/account/summary`                                        | GET                                                                                                                                           | `account summary` ✅                                                                 | —                                                                                       |
| `/account/portfolio`                                      | GET                                                                                                                                           | `account positions` ✅                                                               | —                                                                                       |
| `/account/health`                                         | GET                                                                                                                                           | `account health` ✅                                                                  | —                                                                                       |
| `/alerts/`                                                | GET (envelope `{alerts: [...]}`, query `limit` clamped to `[1,200]`)                                                                          | ❌                                                                                   | NEW sub-app `alerts list [--limit N]`                                                   |
| `/symbols/onboard`                                        | POST                                                                                                                                          | `symbols onboard` ✅                                                                 | —                                                                                       |
| `/symbols/onboard/dry-run`                                | POST                                                                                                                                          | `symbols onboard --dry-run` ✅ (already present at d20fc26 per Codex iter-2 P3)      | —                                                                                       |
| `/symbols/onboard/{run_id}/repair`                        | POST                                                                                                                                          | `symbols repair` ✅                                                                  | —                                                                                       |
| `/symbols/onboard/{run_id}/status`                        | GET                                                                                                                                           | `symbols status` ✅                                                                  | —                                                                                       |
| `/symbols/inventory`                                      | GET (query: `start`, `end`, `asset_class`)                                                                                                    | ❌                                                                                   | `symbols inventory`                                                                     |
| `/symbols/readiness`                                      | GET (query: `symbol` REQ, `asset_class` REQ, optional `start`/`end` window — without them `backtest_data_available=null` per Codex iter-2 P1) | ❌                                                                                   | `symbols readiness --symbol --asset-class [--start --end]`                              |
| `/symbols/{symbol}`                                       | DELETE (query: `asset_class` REQ)                                                                                                             | ❌                                                                                   | `symbols delete`                                                                        |
| `/instruments/bootstrap`                                  | POST                                                                                                                                          | `instruments bootstrap` ✅                                                           | —                                                                                       |
| `/auth/me`                                                | GET                                                                                                                                           | ❌                                                                                   | NEW sub-app `auth me` (alias `whoami`)                                                  |
| `/auth/logout`                                            | POST                                                                                                                                          | ❌                                                                                   | NEW sub-app `auth logout`                                                               |

**Net (recounted per Codex iter-3 nit — modified sub-apps = 7, total sub-apps touched = 11):**

| Bucket                     | Sub-apps                                                                             | Commands                                                                                                                                                                                                                                                                                                |
| -------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| New sub-apps               | `alerts`, `auth`, `market-data`, `template` (4)                                      | `alerts list`, `auth me`, `auth logout`, `market-data bars`, `market-data ingest`, `market-data status`, `market-data symbols`, `template list`, `template scaffold` (9)                                                                                                                                |
| Modified existing sub-apps | `strategy`, `backtest`, `research`, `graduation`, `live`, `portfolio`, `symbols` (7) | strategy edit/delete (2) + backtest report/trades (2) + research sweep/walk-forward/promote (3) + graduation create/stage (2) + live status-show/portfolio-list/portfolio-show/portfolio-draft-members (4) + portfolio create/run-show/run-report (3) + symbols inventory/readiness/delete (3) = **19** |
| **Total**                  | **11 sub-apps touched (4 new + 7 modified)**                                         | **28 new commands**                                                                                                                                                                                                                                                                                     |

After this PR, the CLI mirrors every `/api/v1/*` REST endpoint. WebSocket `/live/stream/{deployment_id}` is intentionally N/A.

## Files

### MODIFIED

- `backend/src/msai/cli.py` — add commands under existing sub-apps + register new sub-apps; add 4 new sub-app variables.
- `backend/src/msai/cli_symbols.py` — add the 3 new symbols commands inside the existing file (Codex iter-1 P2 fix — symbols sub-app lives here, not in cli.py).

### NEW

- `backend/tests/unit/test_cli_completeness.py` — `CliRunner` tests per command (mock httpx.request).

## Endpoint contracts (verified against backend schemas + Codex iter-1)

These are the **exact** request/response shapes; the original plan was wrong on many of these per Codex iter-1 review.

### Strategy edit (PATCH)

- Body: `{description?, default_config?}` — those are the **only** PATCH fields (`schemas/strategy.py`). No `code_hash_override`.
- Response: 200 `StrategyResponse`.

### Strategy delete

- DELETE `/strategies/{id}` → **200** `MessageResponse {message: "..."}` (NOT 204).

### Strategy templates

- `GET /api/v1/strategy-templates/` → list of `{id, label, description, default_config}` per Codex iter-2 P1 (NOT `{id, name, description, source}` — there is no `schemas/strategy_template.py`).
- `POST /api/v1/strategy-templates/scaffold` body: `{template_id, module_name, description?}` per Codex iter-2 P1 (NO `target_filename`).
- CLI: `template list`, `template scaffold --template-id <id> --module-name <name> [--description "..."]`.

### Backtest report (two-step)

1. `POST /api/v1/backtests/{id}/report-token` → response `{signed_url, expires_at}` (NOT `{token}`).
2. `GET <signed_url>` (the response contains the full path; CLI just hits the path as-given) → HTML body.

- Write HTML to `--out FILE` or stdout.

### Backtest trades (paginated)

- `GET /api/v1/backtests/{id}/trades?page=1&page_size=100` (server clamps page_size to **max 500**).
- CLI flags: `--page N --page-size M --out FILE`. Default page 1, page_size 100. If `--all` is passed, loop through pages — Codex iter-6 P2: **use the response's actual `page_size` field for the loop condition**, not the user-passed value (server may have clamped down to 500 silently); terminate when returned rows < server's `page_size`.

### Research sweep

- `POST /research/sweeps` body shape (read `schemas/research.py` at impl time): `{strategy_id, instruments: [...], start_date, end_date, parameter_grid: {...}, ...}` — flat body, **NOT** `{strategy_id, config}`.
- CLI: `research sweep --config @sweep.json` where the JSON file IS the full body.

### Research walk-forward

- `POST /research/walk-forward` body: like sweep + `train_days, test_days` required.
- CLI: `research walk-forward --config @wf.json`.

### Research promote

- `POST /research/promotions` body: `{research_job_id, trial_index?, notes?}` (NOT `{job_id}`).
- CLI: `research promote --job-id <uuid> [--trial-index N] [--notes "..."]` — flag name is `--job-id` for ergonomics but maps to `research_job_id` in the body.

### Graduation create

- `POST /api/v1/graduation/candidates` body `GraduationCandidateCreate{strategy_id, config, metrics, research_job_id?, notes?}` — `metrics` is required (defaults to `{}` in CLI), `stage` is server-set to `discovery`. `research_job_id` and `notes` are optional but exposing them keeps the CLI at full create-body parity (Codex iter-6 P2).
- CLI: `graduation create --strategy-id <uuid> --config @cfg.json [--metrics @metrics.json] [--research-job-id <uuid>] [--notes "..."]`.

### Graduation stage

- `POST /graduation/candidates/{id}/stage` body: `{stage, reason?}`. `stage` is the target stage; `reason` optional.

### Live status-show

- `GET /live/status/{deployment_id}` → single-deployment status. Distinct from `live status` which lists.

### Live portfolio list/show/draft-members

- `GET /live-portfolios` → array of `LivePortfolioResponse`.
- `GET /live-portfolios/{id}` → one portfolio.
- `GET /live-portfolios/{id}/members` → DRAFT members. (Frozen-revision members go via `portfolio-members` which already exists, hitting `/live-portfolio-revisions/{id}/members`.)

### Portfolio (research-backtest) create + run-show + run-report

- `POST /portfolios` body: read `schemas/portfolio.py`.
- `GET /portfolios/runs/{run_id}` → single run detail.
- `GET /portfolios/runs/{run_id}/report` → HTML report; write to `--out`.

### Market-data bars / symbols / status / ingest

- `GET /api/v1/market-data/bars/{symbol}?start=YYYY-MM-DD&end=YYYY-MM-DD&interval=1m` → `BarsResponse` (rows of OHLCV).
  - CLI: `market-data bars <symbol> --start --end [--interval 1m]`.
- `GET /api/v1/market-data/symbols` → `SymbolsResponse` (symbols grouped by asset class).
  - CLI: `market-data symbols`.
- `GET /api/v1/market-data/status` → `StatusResponse{status, storage: {asset_classes, total_files, total_bytes}}`.
  - CLI: `market-data status`. Distinct from top-level `data-status` (LOCAL filesystem stats, offline).
- `POST /api/v1/market-data/ingest` body `IngestRequest{asset_class, symbols, start, end, provider="auto", dataset?, data_schema?}` → `202 Accepted` with `IngestResponse{message, asset_class, symbols, start, end}` (Codex iter-4 P2 — the route discards the enqueue handle, so the response echoes the request fields plus a status message, NOT a job_id).
  - `asset_class` enum: `stocks | equities | indexes | futures | options | crypto`.
  - CLI: `market-data ingest --asset-class <class> --symbols A,B,C --start YYYY-MM-DD --end YYYY-MM-DD [--provider auto|databento|polygon] [--dataset <name>] [--data-schema <schema>]`. Distinct from top-level `ingest` (calls LOCAL `DataIngestionService` directly, bypasses arq queue).

### Alerts list

- `GET /api/v1/alerts/?limit=N` (limit clamped to `[1, 200]` server-side per Codex iter-2 P2) → response envelope `{alerts: [...]}`.
- CLI: `alerts list [--limit N]` (default 50; surfaced limit value gets passed verbatim and server clamps).

### Symbols inventory

- `GET /symbols/inventory` query params: `start`, `end`, `asset_class` (NOT `limit`).
- CLI: `--start`, `--end`, `--asset-class` (equity|futures|fx|option).

### Symbols readiness

- `GET /api/v1/symbols/readiness` query params: **both** `symbol` and `asset_class` REQUIRED + optional `start`/`end` window (without them `backtest_data_available` is `null` per Codex iter-2 P1).
- CLI: `readiness --symbol AAPL.NASDAQ --asset-class equity [--start 2024-01-01 --end 2025-01-01]`.

### Symbols delete

- `DELETE /api/v1/symbols/{symbol}?asset_class=...` — `asset_class` REQUIRED. The route uses `/{symbol}` (single path segment, not `{symbol:path}`), so symbols containing `/` (e.g. `EUR/USD.IDEALPRO`) cannot be deleted via this endpoint regardless of URL-encoding — even `%2F` returns 404. **Slash-bearing symbols are out of scope for delete in this PR; document the limitation in `--help`** (Codex iter-6 P2). If support is needed later, the backend route would need to change to `/{symbol:path}` — track as a follow-up.
- Returns **HTTP 204 with empty body** (Codex iter-5 P2). CLI MUST NOT call `response.json()` on success — print a synthesized success message ("Deleted {symbol}") and return; only parse JSON on non-2xx error bodies.
- CLI: `symbols delete <symbol> --asset-class <class> [--yes]`. Confirm prompt unless `--yes`.

### Auth me / logout

- `GET /api/v1/auth/me` → current user.
- `POST /api/v1/auth/logout` → 200 `MessageResponse` (NOT 204 per Codex iter-2 P2).

## Tasks (serialized — all touch cli.py / cli_symbols.py)

| ID  | Scope                                                              | New commands                                                                                                                                                                    |
| --- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T1  | Alerts (new sub-app `alerts`)                                      | `alerts list`                                                                                                                                                                   |
| T2  | Strategy edit/delete + Strategy templates (new sub-app `template`) | `strategy edit`, `strategy delete`, `template list`, `template scaffold`                                                                                                        |
| T3  | Graduation lifecycle                                               | `graduation create`, `graduation stage`                                                                                                                                         |
| T4  | Research launchers + promotions                                    | `research sweep`, `research walk-forward`, `research promote`                                                                                                                   |
| T5  | Backtest export                                                    | `backtest report`, `backtest trades`                                                                                                                                            |
| T6  | Live status-show + live portfolio list/show/draft-members          | `live status-show`, `live portfolio-list`, `live portfolio-show`, `live portfolio-draft-members`                                                                                |
| T7  | Portfolio (research-backtest) create + run-show + run-report       | `portfolio create`, `portfolio run-show`, `portfolio run-report`                                                                                                                |
| T8  | Market-data (new sub-app `market-data`)                            | `market-data bars`, `market-data ingest`, `market-data status`, `market-data symbols` (4 — top-level `ingest`/`data-status` aliases NOT changed; they remain offline shortcuts) |
| T9  | Symbols inventory/readiness/delete (in `cli_symbols.py`)           | `symbols inventory`, `symbols readiness`, `symbols delete`                                                                                                                      |
| T10 | Auth (new sub-app `auth`)                                          | `auth me` (alias `whoami`), `auth logout`                                                                                                                                       |
| T11 | Tests for T1-T10                                                   | 1 TestClass per family, target ~60 tests (28 commands × ~2 cases each)                                                                                                          |

All tasks edit `cli.py` and/or `cli_symbols.py` and `test_cli_completeness.py`; serialize to avoid file conflicts.

## E2E use cases

Project type fullstack but this PR is CLI-only. Interface: CLI per `rules/testing.md` matrix.

Drive end-to-end against the dev stack (no IB Gateway, no real money):

- **UC1 alerts** — `msai alerts list` returns envelope.
- **UC2 strategy edit + delete** — edit description → `show` confirms; delete returns 200 message; `show` returns 404.
- **UC3 template list + scaffold** — `template list` returns at least one; `template scaffold --template-id <id> --module-name e2e_scaffold_test` returns the scaffolded module path/config. Verify the new strategy module is registered (visible in `strategy list`) per the backend's actual contract (Codex iter-3 P2 — `--module-name`, NOT `--target /tmp/x.py`).
- **UC4 graduation create + stage** — create returns candidate UUID; stage transition updates `show`.
- **UC5 research sweep launch** — `sweep --config @sweep.json` returns job UUID; `research show` shows queued.
- **UC6 research promote** — needs a completed research_job — partial coverage; verify request shape via mock + one real attempt against a completed historical job if present.
- **UC7 backtest export** — `report --out /tmp/r.html` writes non-empty HTML; `trades --page 1` returns JSON.
- **UC8 live status-show + portfolio-list/show/draft-members** — verify shape against a known portfolio (create via existing `portfolio-create`).
- **UC9 portfolio create + run-show + run-report** — create, trigger a run via `portfolio run`, fetch report.
- **UC10 market-data bars + symbols + status + ingest** — fetch bars for a symbol with data; list available symbols; `market-data status` returns `{status, storage: {...}}`; `market-data ingest` returns 202 with `{message, asset_class, symbols, start, end}` (queue path, distinct from local-service top-level `ingest`).
- **UC11 symbols inventory + readiness + delete** — inventory list, readiness for a known symbol, delete a test row.
- **UC12 auth me** — `auth me` returns current user (works under `X-API-Key` or JWT).

No operator drill required — zero live-trading touchpoints; commands hit endpoints that predate the safety trio.

## Out of scope

- UI counterparts for any of these.
- Auth `logout` server-side state — endpoint is a placeholder per CLAUDE.md.
- Pagination beyond `--page`/`--page-size` flags (no cursors).
- Output formatting beyond JSON (no `--format=table`).
