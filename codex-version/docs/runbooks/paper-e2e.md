# Paper E2E

This runbook describes the broker-connected paper-trading smoke lane for the
Codex version. It keeps Interactive Brokers credentials in ignored local env
files and uses the existing Nautilus + IB path rather than a mocked adapter.

## Local env

Create an ignored file such as `codex-version/.env.paper-e2e.local` with:

```bash
TWS_USERID=your-paper-username
TWS_PASSWORD=your-paper-password
IB_ACCOUNT_ID=your-paper-account-id
TRADING_MODE=paper
READ_ONLY_API=no
IB_ALLOW_MOCK_FALLBACK=false
MSAI_API_KEY=msai-dev-key
IB_AUTO_RESTART_TIME=11:45 PM
IB_RELOGIN_AFTER_TWOFA_TIMEOUT=yes
IB_TWOFA_TIMEOUT_ACTION=restart
IB_EXISTING_SESSION_ACTION=primary
IB_TWS_ACCEPT_INCOMING=accept
IB_BYPASS_WARNING=yes
IB_TIME_ZONE=America/Chicago
IB_JAVA_HEAP_SIZE=1024
```

`READ_ONLY_API=no` is required for order-path and liquidation tests.
`IB_ALLOW_MOCK_FALLBACK=false` ensures the backend fails honestly if IB is not
reachable.
The Compose stack reserves distinct IB API client-id ranges on purpose:

- `backend` probe/reconciliation: `10`
- `backend` instrument lookup: `20`
- `live-runtime` kill-all/reconciliation: `30`
- managed Nautilus live deployments: `101+`

IBKR allows only one active connection per `clientId`, so these ranges should
not be collapsed back together.
`IB_AUTO_RESTART_TIME` supports the required daily restart, but IBKR still
requires periodic manual reauthentication.

## Start the stack

```bash
docker compose \
  --profile broker \
  --env-file .env.paper-e2e.local \
  -f docker-compose.dev.yml \
  up -d postgres redis backend ib-gateway live-runtime
```

Wait for the gateway to settle, then verify:

```bash
docker compose --env-file .env.paper-e2e.local -f docker-compose.dev.yml ps
curl -H 'X-API-Key: msai-dev-key' http://127.0.0.1:8400/ready
```

`ib-gateway` now fails fast if `TWS_USERID` or `TWS_PASSWORD` are missing or
still set to placeholder values. This prevents accidental login attempts with
dummy credentials.

The dev and prod stacks also use the forwarded gateway ports `4004` (paper) and
`4003` (live) for inter-container traffic. Those are the reachable ports from
`backend` and `live-runtime` when using the `gnzsnz/ib-gateway` image.

## Backend paper smoke

Runs against the real backend on `localhost:8400`:

```bash
RUN_PAPER_E2E=1 \
PAPER_E2E_API_KEY=msai-dev-key \
uv --directory backend run pytest tests/e2e/test_ib_paper_smoke.py -q
```

This test:

- checks `/ready`
- syncs strategies
- starts a paper live deployment through `/api/v1/live/start`
- polls `/api/v1/live/status`
- verifies risk and positions endpoints
- stops the deployment cleanly
- resets any halt state during teardown

## Frontend paper smoke

Runs the Next.js frontend in API-key test mode against the live backend:

```bash
PW_REAL_BACKEND=1 \
NEXT_PUBLIC_E2E_API_KEY=msai-dev-key \
NEXT_PUBLIC_API_URL=http://127.0.0.1:8400 \
pnpm --dir frontend exec playwright test e2e/live-paper-real.spec.ts
```

## Notes

- Use only paper credentials here, never live credentials.
- IB market data permissions still control whether quotes are real-time or delayed.
- If the gateway cannot authenticate or initialize market data, these tests should fail rather than falling back to mocks.
