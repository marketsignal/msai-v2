# Drill: live-path registry wiring

**Purpose:** Validate that the registry-backed live-start path produces real fills on a live IB account before merging.

**Council verdict constraint #5 (2026-04-19):** Real-money drill on `U4705114` equivalent to the 2026-04-16 AAPL BUY/SELL drill, exercising the new registry-backed path (not `canonical_instrument_id`). MANDATORY before merge.

**Worktree:** `.worktrees/live-path-wiring-registry` on branch `feat/live-path-wiring-registry`.

---

## Pre-flight

### A. Registry seed

```bash
cd backend
uv run msai instruments refresh --symbols QQQ --provider interactive_brokers
```

Expected: row inserted in `instrument_definitions` + `instrument_aliases`. Verify:

```bash
psql $DATABASE_URL -c "SELECT raw_symbol, listing_venue, routing_venue, asset_class FROM instrument_definitions WHERE raw_symbol = 'QQQ'"
```

Expect exactly one row with `asset_class=equity`, `listing_venue=NASDAQ` (or `ARCA` depending on IB qualification), `routing_venue=SMART`.

### B. Databento alias co-existence sanity check

```bash
psql $DATABASE_URL -c "SELECT DISTINCT provider, COUNT(*) FROM instrument_aliases GROUP BY provider"
```

Expected: `interactive_brokers` rows present; `databento` rows either absent or only on futures-root symbols NOT touched by this drill. If Databento rows exist on drill symbols, log the finding in the drill report (not a blocker — resolver's `provider="interactive_brokers"` filter prevents cross-contamination).

### C. Stack up + IB Gateway live mode

```bash
# Confirm stack running
curl -sf http://localhost:8800/health

# Switch IB Gateway to LIVE port/account
export IB_GATEWAY_PORT=4001
export IB_ACCOUNT_ID=U4705114
```

Re-start supervisor + workers if needed so they pick up the env change:

```bash
./scripts/restart-workers.sh --with-broker
```

---

## Drill procedure

### Step 1: Create portfolio

```bash
export BEARER_TOKEN=<paste-fresh-token>

curl -X POST http://localhost:8800/api/v1/live-portfolios/ \
  -H "Authorization: Bearer $BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "drill-qqq-registry", "description": "Registry-backed live-start drill — UC-L-REG-001"}'
```

Capture the returned `id`.

### Step 2: Add strategy member

Pick a simple buy-hold strategy (e.g., `strategies/example/buy_hold.py`):

```bash
curl -X POST http://localhost:8800/api/v1/live-portfolios/<PORTFOLIO_ID>/strategies \
  -H "Authorization: Bearer $BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "file_path": "strategies/example/buy_hold.py",
    "strategy_class": "BuyHoldStrategy",
    "weight": "1.0",
    "order_index": 0,
    "instruments": ["QQQ"],
    "config": {}
  }'
```

### Step 3: Snapshot + freeze revision

```bash
curl -X POST http://localhost:8800/api/v1/live-portfolios/<PORTFOLIO_ID>/revisions \
  -H "Authorization: Bearer $BEARER_TOKEN"
```

Capture the returned revision `id`.

### Step 4: Start live deployment (REAL MONEY on U4705114)

```bash
curl -X POST http://localhost:8800/api/v1/live/start-portfolio \
  -H "Authorization: Bearer $BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "portfolio_revision_id": "<REVISION_ID>",
    "account_id": "U4705114",
    "paper_trading": false
  }'
```

Expected: HTTP 200 with `status=running` (or `ready`), `paper_trading=false`, `deployment_slug` set.

### Step 5: Confirm registry resolution in supervisor log

```bash
docker compose -f docker-compose.dev.yml logs live-supervisor 2>&1 | grep live_instrument_resolved | tail -5
```

Expected line:

```
event=live_instrument_resolved source=registry symbol=QQQ canonical_id=QQQ.NASDAQ asset_class=equity as_of_date=<ISO>
```

**If `source=registry_miss` or `source=registry_incomplete` appears → ABORT drill, fix registry seed, re-start from Step 1.**

### Step 6: Wait for first bar (max 60s)

```bash
curl -sf http://localhost:8800/api/v1/live/status \
  -H "Authorization: Bearer $BEARER_TOKEN" | jq '.deployments[] | select(.id == "<DEPLOYMENT_ID>")'
```

`status=running` within 60s indicates successful IB subscription + first bar.

### Step 7: Capture initial fill evidence

`BuyHoldStrategy` should submit a 1-share BUY on the first bar. Verify fill:

```bash
curl -sf "http://localhost:8800/api/v1/live/trades?deployment_id=<DEPLOYMENT_ID>" \
  -H "Authorization: Bearer $BEARER_TOKEN" | jq .
```

Expected: one trade with `side=BUY`, `quantity=1`, `is_live=true`, `instrument_id=QQQ.NASDAQ`, non-null `broker_trade_id`.

### Step 8: /kill-all to flatten

```bash
curl -X POST http://localhost:8800/api/v1/live/kill-all \
  -H "Authorization: Bearer $BEARER_TOKEN"
```

Verify:

```bash
# Positions flat
curl -sf http://localhost:8800/api/v1/live/positions \
  -H "Authorization: Bearer $BEARER_TOKEN" | jq .
```

Expected: no open positions.

```bash
# Trades show the offsetting SELL
curl -sf "http://localhost:8800/api/v1/live/trades?deployment_id=<DEPLOYMENT_ID>" \
  -H "Authorization: Bearer $BEARER_TOKEN" | jq 'map(.side) | unique'
```

Expected: `["BUY", "SELL"]`.

### Step 9: Capture drill report

Create `docs/runbooks/drill-reports/YYYY-MM-DD-live-path-registry-drill.md` with:

1. Pre-flight outputs (registry seed confirmation, Databento co-existence check)
2. HTTP response from `/start-portfolio` (status + timing)
3. First `live_instrument_resolved` log line (grepped)
4. First-bar timestamp
5. BUY trade row from `/trades`
6. `/kill-all` timing (goal: < 500ms kill-to-flat)
7. SELL trade row from `/trades`
8. Final `/positions` (should be empty)
9. Net drill cost (slippage + commissions)

Target: drill cost < $5, all 8 items captured. Attach report to the PR.

---

## Failure modes

| Failure                                          | Likely cause                                                                                | Fix                                                                               |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| HTTP 422 `REGISTRY_MISS`                         | Registry not seeded or spawn_today outside alias window                                     | Re-run pre-flight A; if seeded, check `instrument_aliases.effective_from/to`      |
| HTTP 422 `REGISTRY_INCOMPLETE`                   | Operator-edited alias row with NULL fields, or `raw_symbol` not in `BASE/QUOTE` form for FX | Check alerts at `/api/v1/alerts/` + re-run `msai instruments refresh`             |
| HTTP 422 `UNSUPPORTED_ASSET_CLASS`               | Registry row is `asset_class=option` or `crypto`; this PR doesn't support those paths       | Pick an equity/ETF/FX/futures symbol instead                                      |
| HTTP 422 `AMBIGUOUS_REGISTRY` (cross-asset)      | Bare ticker matches multiple rows (e.g. SPY exists as equity AND option)                    | Deploy with dotted form: `"QQQ.NASDAQ"` instead of `"QQQ"`                        |
| HTTP 422 `AMBIGUOUS_REGISTRY` (same-day overlap) | Two aliases have same `effective_from`                                                      | Set `effective_to` on the stale row via SQL, or re-run `msai instruments refresh` |
| `source=registry` but no bar events              | IB entitlements / subscriptions issue (not a code bug — see `reference_ib_entitlements.md`) | Check IB account market-data entitlements at broker.ibkr.com                      |

---

## Council constraints satisfied by this drill

- Constraint #1 (three-surface wiring): supervisor + IB preload builder + live_node_config all participate in the deploy
- Constraint #3 (Chicago-local `spawn_today`): verified by the resolver log line's `as_of_date` field
- Constraint #4 (no silent fallback): a registry miss produces HTTP 422 + structured log + no subprocess spawn — does NOT fall back to `canonical_instrument_id`
- Constraint #5 (real-money drill): this procedure satisfies the constraint
- Constraint #6 (structured telemetry): `live_instrument_resolved` log + `msai_live_instrument_resolved_total` counter are both exercised
