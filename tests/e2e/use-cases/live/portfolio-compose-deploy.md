# Live Portfolio Compose + Deploy — UC1..UC6

Graduated from `docs/plans/2026-05-15-live-deployment-workflow-ui-cli.md` after PASS in `tests/e2e/reports/live-deployment-workflow-ui-cli-20260515T115100.md` (2026-05-15).

## Shared ARRANGE (run before each UC; idempotent)

1. Resolve `STRATEGY_UUID`:
   ```bash
   curl -sf -H "X-API-Key: msai-dev-key" 'http://localhost:8800/api/v1/strategies/' \
     | jq -r '.items[] | select(.name=="example.smoke_market_order") | .id'
   ```
2. Find or create an unlinked `live_candidate` for that strategy. If none exists, create via `POST /api/v1/graduation/candidates` with `{strategy_id, config: {bar_type, instruments, instrument_id}, metrics: {}}` then walk 5 stage transitions: `validation → paper_candidate → paper_running → paper_review → live_candidate`.
3. Between UCs: stop any test deployment + archive its linked candidate so the next UC sees a fresh unlinked `live_candidate`.

## UC1 — UI: Portfolio compose → snapshot → deploy (paper)

**Intent:** Operator creates a portfolio + adds one strategy + snapshots + deploys on paper.

**Interface:** UI + API.

**Steps:**

1. Navigate to `/live-trading/portfolio`
2. Click `live-portfolio-page-create-new` → fill name + description → "Create portfolio"
3. Click "Add member" disclosure → select strategy → paste matching config → instruments + weight=1 → "Add Member"
4. Click `portfolio-compose-snapshot` → confirm → revision freezes
5. Deploy dialog auto-opens — fill `account_id=DUP733213`, `ib_login_key=marin1016test`, paper_trading=true → "Preview"
6. Verify Stage 2 preview table shows frozen members from `GET /live-portfolio-revisions/{id}/members`
7. Click `portfolio-start-deploy-button`

**Verify:** HTTP 200/201 from `/live/start-portfolio`; `GET /api/v1/live/status?active_only=true` shows the deployment running.

**Persist:** Reload `/live-trading` — deployment visible in active list.

## UC2 — UI: 422 BINDING_MISMATCH rendered

**Intent:** Member with diverging config returns 422 and UI renders the field/member_value/candidate_value diff.

**Setup:** Shared ARRANGE — must be a FRESH unlinked candidate not consumed by UC1.

**Steps:** UC1 steps but in step 3 add a field the candidate doesn't have (e.g. `fast_ema_period: 99`).

**Verify:** Step 7 returns 422; `portfolio-start-mismatches-table` rendered with `Field | Member value (frozen) | Candidate value (current)` columns showing the divergent config.

**Persist:** No new deployment row in `/live/status`.

## UC3 — UI: real-money confirmation gate

**Intent:** Real-money deploy requires re-typing `account_id` to enable the Deploy button.

**Operator-only carve-out.** Tests the confirm input disabled-state without firing a real-money order.

## UC4 — Operator UI: real-money deploy

**Intent:** Operator drives the full UI flow with `paper_trading=false` on a U-prefix account; confirms 201 + binding-linked + supervisor spawn.

**Operator-only**, requires `.env` flipped to live + `broker` profile up + IB Gateway live.

Reference drill: PR #66 paper-money drill 2026-05-14, `tests/e2e/reports/snapshot-binding-operator-drill-20260514T134610.md`.

## UC5 — CLI: portfolio-create → add-strategy → snapshot → members

**Intent:** Replicate UC1 via CLI.

**Setup:** Shared ARRANGE; `MSAI_API_URL=http://localhost:8800` + `MSAI_API_KEY=msai-dev-key` (or run via `docker compose exec backend`).

**Steps:**

```bash
P=$(uv run python -m msai.cli live portfolio-create --name "e2e-uc5-$(date +%s)" --description "" | jq -r .id)
uv run python -m msai.cli live portfolio-add-strategy "$P" --strategy-id $STRATEGY_UUID --config @cfg.json --instruments AAPL.NASDAQ --weight 1.0
REV=$(uv run python -m msai.cli live portfolio-snapshot "$P" | jq -r .id)
uv run python -m msai.cli live portfolio-members "$REV"
```

**Verify:** Each command prints well-formed JSON; the members command returns the frozen revision's member list with `weight: "1.000000"` and `instruments: ["AAPL.NASDAQ"]`.

## UC6 — CLI: safety gates

**(a) `--no-paper` confirmation aborts on 'n':**

```bash
echo n | uv run python -m msai.cli live start-portfolio --revision <id> --account U... --ib-login-key ... --no-paper
```

Output contains "REAL-MONEY"; exit code != 0; no HTTP request fired.

**(b) `DU*` + `--no-paper` blocked by prefix guard:**

Output: `--account 'DU...' is not a live-prefix account (expected U*, NOT DU/DF). Remove --no-paper for paper accounts.` Exit code != 0.

**(c) `U*` + `--paper` (default) blocked by prefix guard:**

Output: `--account 'U...' is not a paper-prefix account (expected DU* or DF*). Pass --no-paper for real-money accounts.` Exit code != 0.

**(d) Missing `--ib-login-key`:**

Typer renders `Missing option '--ib-login-key'.` and exits with code 2.

All four gates fire BEFORE any HTTP request reaches `/start-portfolio` — no stuck deployment rows.
