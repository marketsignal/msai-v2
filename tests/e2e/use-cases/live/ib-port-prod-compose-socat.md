# IB Gateway broker-port resolution: clients reach gnzsnz socat, not loopback bind

Graduated 2026-05-12 from `docs/plans/2026-05-12-ib-port-prod-compose-4004.md`.
Source PR: `fix/ib-port-prod-compose-4004` (PR #\_\_).
Regression for: silent broker-connect timeout when clients targeted the IB Gateway's loopback-only bind port (4002 paper) instead of the gnzsnz socat proxy port (4004 paper).

---

## UC1 — broker resolution smoke from backend to IB Gateway

**Intent.** Confirm that, with the broker profile up on the prod stack, a sibling container (backend) can complete a full IB API handshake to `ib-gateway` and read managed-account metadata. This is the regression guard for the 2026-05-12 prod paper-drill discovery (clients pointed at port 4002 silently TCP-connect but timeout on the API handshake; only port 4004 — the socat proxy — actually works).

**Interface.** API / operational (`ib_async.IB.connectAsync` from inside `msai-backend-1`).

**Setup (sanctioned ARRANGE).** Operator brings up the broker profile against the latest deployed prod compose:

```bash
COMPOSE_PROFILES=broker docker compose \
  -f /opt/msai/docker-compose.prod.yml \
  --env-file /run/msai.env \
  --env-file /run/msai-images.env \
  up -d ib-gateway
```

Wait for `msai-ib-gateway-1` health to flip to `healthy` (IBC log line `Login has completed` typically arrives within 15-30 s; healthcheck's `start_period` is 180 s).

**Steps.**

1. From inside `msai-backend-1`, run an `ib_async` connect probe targeting the compose-default port — i.e. the same port `Settings.ib_port` resolves to, which after this fix is 4004 for paper.

   ```bash
   docker exec msai-backend-1 python -c "
   import asyncio
   from ib_async import IB
   from msai.core.config import settings
   async def go():
       ib = IB()
       await ib.connectAsync(settings.ib_host, settings.ib_port, clientId=996, timeout=30)
       print('sv=', ib.client.serverVersion())
       print('accounts=', ib.managedAccounts())
       ib.disconnect()
   asyncio.run(go())
   "
   ```

2. (Negative-case sanity, optional) Confirm the OLD pre-fix port still times out — proves the diagnosis is right, not just lucky:

   ```bash
   docker exec msai-backend-1 python -c "
   import asyncio
   from ib_async import IB
   async def go():
       ib = IB()
       try:
           await asyncio.wait_for(ib.connectAsync('ib-gateway', 4002, clientId=995, timeout=20), timeout=25)
           print('UNEXPECTED: 4002 worked')
       except (asyncio.TimeoutError, Exception) as e:
           print('expected timeout/refusal:', type(e).__name__)
   asyncio.run(go())
   "
   ```

**Verification.**

- Step 1 prints `sv= <int>` (typically 178+) and `accounts= [...]` with at least one paper sub-account (`DUP*` / `DFP*` / `DU*` prefix). Exit code 0.
- Step 2 (if run) prints `expected timeout/refusal: TimeoutError`. Exit code 0.

**Persistence.** N/A — broker session is stateless from the API surface.

**Failure modes.**

- **`TimeoutError` on step 1 against port 4004** → either (a) IB Gateway never logged in (check `docker logs msai-ib-gateway-1` for `Login has completed`); (b) the gnzsnz image version changed its socat layout (re-probe ports 4003-4005 with `docker top msai-ib-gateway-1` looking for the `socat TCP-LISTEN:NNNN` line); (c) prod is on a SHA pre-merge of this fix (`docker inspect msai-backend-1 --format '{{.Image}}'` should be the post-merge tag).
- **`Step 1 succeeds but step 2 also succeeds on 4002`** → gnzsnz image version changed and IB Gateway is now binding to `0.0.0.0:4002` instead of `127.0.0.1:4002`. Re-validate whether socat is still in the picture; the validator's `IB_PAPER_PORTS = (4002, 4004)` already accepts both, so step-1's compose-default check is what determines correctness.

---

## UC2 — paper live drill resumes after the fix (operational checklist)

**Intent.** Continue the paper live drill that the original IB_PORT bug halted. UC2 is the operational milestone that this PR unblocks, not a fix-validation gate per se.

**Interface.** API / operational.

**Setup.** This fix is deployed to prod (verify via `docker inspect msai-backend-1 --format '{{.Config.Env}}' | tr ' ' '\n' | grep -E 'IB_PORT|IB_API_PORT'` — should show `IB_PORT=4004`, `IB_API_PORT=4002`). Broker profile up. Paper account `marin1016test` / sub-account `DUP733213` (memory `reference_ib_accounts.md` — clean $1M sub).

**Steps (high-level — full procedure mirrors the original 8-step council plan).**

1. **AAPL/SPY data entitlement probe.** Probe inside `msai-backend-1` against `ib-gateway:${IB_PORT}`. Expect `SUBSCRIBED` for both (per `reference_ib_market_data_model`: marin1016test has the free IBKR-PRO bundle shared from the live account).
2. **Stack health.** `curl https://platform.marketsignal.ai/health` → 200.
3. **Supervisor up.** `docker ps` shows `msai-live-supervisor-1` healthy.
4. **Command bus start/stop.** `POST /api/v1/live/start-portfolio` with EMA-cross + 1-share trade size + AAPL or SPY; then `POST /api/v1/live/stop`.
5. **Kill-all.** `POST /api/v1/live/kill-all` returns 200; zero open orders + zero open positions in both `GET /api/v1/live/status` and `GET /api/v1/account/portfolio`.

**Verification.**

- Step 1 returns `SUBSCRIBED` with non-zero bid/ask/last for AAPL and SPY.
- Steps 2-3 all healthy.
- Step 4 receives at least one `on_bar` event (Nautilus log line) before stop. Missing bars = infrastructure failure (NOT strategy failure) per Contrarian's blocking objection.
- Step 5 shows flat state through BOTH the API surface and IB's account view (`GET /api/v1/account/portfolio` and the IB portal as cross-check).

**Persistence.** Trade rows persist in `backtests` / `live_orders` / `live_trades` tables (smoke-flag separation per PR #61); `kill-all` row in `live_deployment_events`.

**Failure modes.** Treat every 5xx during a live/paper flow as stop-the-world per `CLAUDE.md` Live-trading safety rails. Memory `feedback_e2e_before_pr_for_live_fixes` applies — bugs found here become the next fix-bug branch.
