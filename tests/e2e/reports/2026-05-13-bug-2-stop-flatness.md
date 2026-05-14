# verify-e2e report — Bug #2 (stop-flatness verification + TIF=DAY)

**Date:** 2026-05-13 21:02 EDT
**Branch:** `fix/stop-flatness-verification`
**Account:** `DUP733213` (fund-master-paper sub, $1M)
**Operator:** Pablo (authorized real-money via option C; switched to paper
because US equities market closed at 21:00 EDT and the smoke strategy
needs a live bar feed — FX 24/5 used as proxy under Pablo's "go with
that" / β-path authorization).

## Setup

- Stopped the long-running `fresh-vm-data-path-closure` Docker stack
  (held the existing `msai-claude-*` container names).
- `.env` flipped from live `U4705114`/`4003` to paper
  `marin1016test`/`DUP733213`/`4004` (backup at
  `/tmp/dotenv-live-backup.env`; reverted post-drill).
- `COMPOSE_PROFILES=broker docker compose -f docker-compose.dev.yml up -d`
  from the worktree built and started the full stack including
  `msai-claude-ib-gateway` (Pablo's paper login auto-resolved).
- `/api/v1/account/health` reported `gateway_connected: true,
consecutive_failures: 0` within ~10 s of startup.

## Use case executed

### UC2 — Deployment with open position → `/stop` returns broker-flat

Goal: prove the new flatness wire end-to-end. Before this PR the
`/stop` response was `{id, status, process_status}`. After this PR
the response must additionally carry `stop_nonce`, `broker_flat`, and
`remaining_positions`, AND `broker_flat=true` must be backed by an
actual Nautilus `market_exit()` having closed the position.

#### Arrange

| Step | Action                                                                                                     | Result                                                 |
| ---- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| 1    | `POST /api/v1/graduation/candidates` (smoke_market_order, `EUR/USD.IDEALPRO`)                              | `id=d96fd20e-...`, stage=discovery                     |
| 2    | Stage transitions discovery → validation → paper_candidate → paper_running → paper_review → live_candidate | All 5 transitions ok                                   |
| 3    | `POST /api/v1/live-portfolios/{pid}/strategies` (member with EUR/USD config + instruments)                 | `40df3705-...` member added, weight 1.0                |
| 4    | `POST /api/v1/live-portfolios/{pid}/snapshot`                                                              | Revision `42e0eab4-...`, frozen, hash=071baea5...      |
| 5    | `POST /api/v1/live/start-portfolio` with `paper_trading=true`, `ib_login_key=marin1016test`                | `c5c46000-...` `status=running`, slug=0f9926e90abd0713 |

#### Act — wait for fill, capture position

```
== Live supervisor log ==
SmokeMarketOrderStrategy: OrderFilled(
    instrument_id=EUR/USD.IDEALPRO,
    venue_order_id=101,
    account_id=INTERACTIVE_BROKERS-DUP733213,
    order_side=BUY, order_type=MARKET,
    last_qty=1.00, last_px=1.17165 USD,
    commission=2.00 USD)

SmokeMarketOrderStrategy: PositionOpened(
    instrument_id=EUR/USD.IDEALPRO,
    side=LONG, quantity=1.00, avg_px_open=1.17165)
```

```
== GET /api/v1/live/positions (pre-stop) ==
{"positions":[{"deployment_id":"c5c46000-aa27-4dd0-b167-e3a4bcc1a87a",
  "instrument_id":"EUR/USD.IDEALPRO","qty":"1.00",
  "avg_price":"1.17165","unrealized_pnl":"0",
  "realized_pnl":"-2","ts":"2026-05-14T01:02:39Z"}]}
```

#### Act — `/stop`

```
$ curl -sL -X POST -H "X-API-Key: msai-dev-key" \
    -H "Content-Type: application/json" \
    http://localhost:8800/api/v1/live/stop \
    -d '{"deployment_id":"c5c46000-aa27-4dd0-b167-e3a4bcc1a87a"}'

{"id":"c5c46000-aa27-4dd0-b167-e3a4bcc1a87a",
 "status":"stopped",
 "process_status":"stopped",
 "stop_nonce":"9d6fe6d8ed9799864be3413f10366617",   ← NEW (Bug #2)
 "broker_flat":true,                                ← NEW (Bug #2)
 "remaining_positions":[]}                          ← NEW (Bug #2)

elapsed = 11 s (well under the 30 s API deadline)
```

#### Verify — post-stop state

```
== GET /api/v1/live/positions (post-stop) ==
{"positions":[]}
```

Position closed at the broker. Nautilus's `market_exit()` (triggered
by `manage_stop=True`) successfully closed the long, child subprocess
wrote `stop_report:9d6fe6d8ed9799864be3413f10366617` with
`broker_flat=true` + empty `remaining_positions`, API GET-polled the
key within 11 s, parsed JSON, returned to caller.

## Earlier datapoint — empty-position stop on `847439a3-...` (AAPL, market closed)

The very first deployment in this drill was AAPL.NASDAQ during US
equity-market-closed hours (no bars → strategy idle, no order, no
position). `/stop` on that deployment returned:

```
{"id":"847439a3-532e-4e09-a37b-9f69e828e673",
 "status":"stopped","process_status":"stopped",
 "stop_nonce":"d607a3fa72e02b94f8e230f6ccb1c50b",
 "broker_flat":true,
 "remaining_positions":[]}
```

That confirms the new fields are surfaced even on the empty-position
path — the wire fires regardless of whether there's anything to
flatten.

## Classification

| Use case                                     | Classification |
| -------------------------------------------- | -------------- |
| Wire plumbing (new fields surfaced at /stop) | **PASS**       |
| Position-open → /stop → broker_flat:true     | **PASS**       |
| Post-stop position cleared at broker         | **PASS**       |
| 30 s API deadline respected (actual 11 s)    | **PASS**       |

**Overall: PASS — Bug #2 ready for PR.**

## What the drill did NOT cover

- **Real-money path (`U4705114` on port 4003).** Pablo authorized
  option C (real money 1-share AAPL); switched to paper-FX (β) when
  equities market was closed at 21:00 EDT. The wire is identical
  paper vs. live — same `kernel.cache.positions_open()`, same
  `STOP_AND_REPORT_FLATNESS` Redis flow, same child shutdown drain.
  Real-money exposure of this wire would have been a 1-share AAPL buy
  on `mslvp000` test-lvp during market hours.
- **Force-rejected `market_exit` → `broker_flat: false` branch.**
  The happy path was exercised; the failure branch (Nautilus exhausts
  `max_attempts` while positions remain) is covered by the unit-test
  `test_flatness_drain.py::test_drain_reports_non_flat_when_my_positions_remain`
  but not against IB Gateway. Hard to force on a paper account that
  always fills cleanly.
- **SET-NX coalescing of two concurrent /stop callers.** Covered by
  `test_flatness_service.py::test_second_caller_coalesces_onto_existing_nonce`
  but not exercised against the real Redis container during this drill.
- **TIF=DAY override for US equities.** Equities market closed; not
  exercised on a live order. Covered by unit tests in
  `test_live_node_config_tif.py` and `test_portfolio_node_config_tif.py`.

## Followups for the Bug #2 PR

None blocking. The two empirical datapoints (empty-position + open-position)
plus the 115 unit tests + ruff + mypy --strict + ADR + 5 operational
metrics exhaust the pre-merge gate.

## Teardown

- Stopped EUR/USD portfolio deployment via the same `/stop` wire under test.
- Brought down worktree stack: `docker compose -f docker-compose.dev.yml down`.
- Reverted `.env` from `/tmp/dotenv-live-backup.env` to live config
  (`U4705114`/`4003`/`marin1016`/`mslvp000`).
- Restarted the pre-existing `fresh-vm-data-path-closure` stack if Pablo
  wants it back.

Drill artifacts: this report. Full container logs available in
`docker logs msai-claude-live-supervisor` while the stack was running.
