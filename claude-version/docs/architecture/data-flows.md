# Data Flows

The five primary data flows through the system, with ASCII diagrams
showing the exact services, Redis keys, and DB tables involved.

## 1. Market Data Ingestion

Historical OHLCV data enters the system via Polygon.io (stocks) or
Databento (futures) and is stored as Parquet files.

```
                     CLI: msai ingest                API: POST /api/v1/market-data/ingest
                         |                                       |
                         v                                       v
                  DataIngestionService                    arq enqueue (run_ingest)
                         |                                       |
              +----------+----------+                            v
              |                     |                   backtest-worker container
              v                     v                            |
        PolygonClient         DatabentoClient                    v
        (stocks/options)      (futures)                  DataIngestionService
              |                     |                            |
              +----------+----------+                   (same flow as CLI)
                         |
                         v
                    ParquetStore
                    atomic writes
                         |
                         v
         {DATA_ROOT}/parquet/{asset_class}/{symbol}/{YYYY}/{MM}.parquet
```

Path structure example:

```
data/parquet/stocks/AAPL/2025/01.parquet
data/parquet/stocks/AAPL/2025/02.parquet
data/parquet/futures/ES/2025/03.parquet
```

Nightly cron job (`run_nightly_ingest` at 05:00 UTC) runs an
incremental update for all existing symbols.

## 2. Backtesting

A backtest request flows through the arq job queue to a worker that
runs NautilusTrader's BacktestNode.

```
Frontend                    Backend (FastAPI)              backtest-worker
   |                              |                              |
   |  POST /api/v1/backtests/run  |                              |
   |----------------------------->|                              |
   |                              |                              |
   |                   1. Verify strategy exists (strategies table)
   |                   2. Compute strategy_code_hash (SHA256)
   |                   3. Normalize instruments to canonical form
   |                   4. INSERT backtests row (status=pending)
   |                   5. enqueue_backtest via arq Redis pool
   |                              |                              |
   |  201 {id, status=pending}    |                              |
   |<-----------------------------|                              |
   |                              |         arq dequeues job     |
   |                              |         run_backtest_job     |
   |                              |                              |
   |                              |    6. Read backtest row from DB
   |                              |    7. Build NautilusTrader catalog
   |                              |       (BarDataWrangler on Parquet)
   |                              |    8. BacktestRunner.run()
   |                              |       - BacktestNode(config)
   |                              |       - node.run()
   |                              |    9. Extract metrics + trades
   |                              |   10. Generate QuantStats HTML
   |                              |   11. UPDATE backtests row
   |                              |       (status=completed, metrics,
   |                              |        report_path)
   |                              |   12. INSERT trades rows
   |                              |                              |
   |  GET /{job_id}/status (poll) |                              |
   |----------------------------->|                              |
   |  {status=completed}          |                              |
   |<-----------------------------|                              |
   |                              |                              |
   |  GET /{job_id}/results       |                              |
   |----------------------------->|                              |
   |  {metrics, trades}           |                              |
   |<-----------------------------|                              |
   |                              |                              |
   |  GET /{job_id}/report        |                              |
   |----------------------------->|                              |
   |  QuantStats HTML file        |                              |
   |<-----------------------------|                              |
```

Report files are stored at `{DATA_ROOT}/reports/` and served via
`FileResponse` with path traversal protection.

## 3. Live Trading Startup

A deployment request flows through three idempotency layers, then
to the live supervisor via Redis Streams.

```
Frontend / CLI                    Backend (FastAPI)
      |                                  |
      |  POST /api/v1/live/start         |
      |  + Idempotency-Key header        |
      |--------------------------------->|
      |                                  |
      |            Layer 1: HTTP Idempotency-Key
      |            - SETNX in Redis (user-scoped)
      |            - InFlight (425) if key exists + pending
      |            - CachedOutcome if key exists + done
      |            - BodyMismatchReservation if body differs
      |                                  |
      |            Layer 2: Halt flag check
      |            - EXISTS msai:risk:halt in Redis
      |            - 503 if set
      |                                  |
      |            Layer 3: Identity-based upsert
      |            - Compute identity_signature from
      |              (user_id, strategy_id, strategy_code_hash,
      |               config_hash, account_id, paper_trading,
      |               instruments)
      |            - ON CONFLICT DO UPDATE on identity_signature
      |            - If active process exists -> already_active (200)
      |                                  |
      |            Publish START command  |
      |            to Redis stream       |
      |            msai:live:commands     |
      |                                  |
      |                                  |     live-supervisor container
      |                                  |            |
      |                                  |   LiveCommandBus.consume()
      |                                  |   (XREADGROUP + XAUTOCLAIM)
      |                                  |            |
      |                                  |   handle_command(START)
      |                                  |            |
      |                                  |   ProcessManager.spawn()
      |                                  |     Phase A: Reserve slot
      |                                  |       INSERT live_node_processes
      |                                  |       (status=starting)
      |                                  |     Phase B: Halt re-check +
      |                                  |       payload factory +
      |                                  |       halt re-check (2nd) +
      |                                  |       mp.Process.start()
      |                                  |     Phase C: Record pid
      |                                  |            |
      |                                  |     bus.ack(entry_id)
      |                                  |            |
      |            Poll loop             |            |
      |            (60s timeout,         |            |
      |             250ms interval)      |     Trading subprocess
      |            SELECT latest         |     (fresh Python interpreter)
      |            live_node_processes   |            |
      |            row for deployment    |     1. Self-write pid +
      |                                  |        status=building
      |                                  |     2. Start heartbeat thread
      |                                  |     3. node = TradingNode(config)
      |                                  |     4. node.build()
      |                                  |        (IB contract loading)
      |                                  |     5. node.run_async() as task
      |                                  |     6. wait_until_ready()
      |                                  |        (poll trader.is_running)
      |                                  |     7. status=ready, status=running
      |                                  |     8. Start disconnect handler
      |                                  |     9. Block on node.run_async()
      |                                  |
      |  200 {id, status, slug, ...}     |
      |<---------------------------------|
```

## 4. Projection Pipeline (Nautilus Events to Dashboard)

The projection pipeline translates Nautilus message bus events from
Redis streams into dashboard-friendly JSON on dual pub/sub channels.

```
Trading Subprocess                    Redis                     Backend (FastAPI)
       |                                |                              |
  TradingNode writes to                 |                              |
  MessageBus Redis stream               |                              |
  (msgpack encoding,                    |                              |
   stream_per_topic=False,              |                              |
   use_trader_prefix=True,              |                              |
   use_trader_id=True)                  |                              |
       |                                |                              |
       v                                |                              |
  stream/MSAI-{slug}                    |                              |
  (single stream per trader)            |                              |
       |                                |                              |
       +-----> Redis Stream             |                              |
                    |                   |                              |
                    |          ProjectionConsumer                      |
                    |          (XREADGROUP, consumer group)            |
                    |                   |                              |
                    |          Translator                              |
                    |          (msgpack -> InternalEvent JSON)         |
                    |                   |                              |
                    |          DualPublisher                           |
                    |                   |                              |
                    +----> PUBLISH msai:live:state:{dep_id}            |
                    |      (consumed by StateApplier)                  |
                    |                   |                              |
                    +----> PUBLISH msai:live:events:{dep_id}           |
                           (consumed by WebSocket handler)             |
                                        |                              |
                               StateApplier                            |
                               subscribes to                           |
                               msai:live:state:*                       |
                                        |                              |
                                        v                              |
                               ProjectionState                         |
                               (in-memory per-worker)                  |
                                        |                              |
                                        v                              |
                               PositionReader                          |
                               (fast path: ProjectionState,            |
                                cold path: Redis cache)                |
                                        |                              |
                            Used by GET /positions,                     |
                            GET /status/{id},                          |
                            WS snapshot                                |
```

### Redis Key/Channel Reference

| Key/Channel                        | Type    | Producer                       | Consumer                                    |
| ---------------------------------- | ------- | ------------------------------ | ------------------------------------------- |
| `stream/MSAI-{slug}`               | Stream  | TradingNode MessageBus         | ProjectionConsumer                          |
| `msai:live:state:{deployment_id}`  | Pub/Sub | DualPublisher                  | StateApplier                                |
| `msai:live:events:{deployment_id}` | Pub/Sub | DualPublisher                  | WebSocket handler                           |
| `msai:live:commands`               | Stream  | LiveCommandBus (API)           | LiveCommandBus (supervisor)                 |
| `msai:live:commands:dlq`           | Stream  | LiveCommandBus                 | Operator (manual)                           |
| `msai:risk:halt`                   | String  | /kill-all, IBDisconnectHandler | /start, supervisor spawn, RiskAwareStrategy |
| `msai:risk:halt:set_by`            | String  | /kill-all                      | Operator (diagnostic)                       |
| `msai:risk:halt:set_at`            | String  | /kill-all                      | Operator (diagnostic)                       |
| `msai:risk:halt:reason`            | String  | IBDisconnectHandler            | Operator (diagnostic)                       |
| `msai:risk:halt:source`            | String  | IBDisconnectHandler            | Operator (diagnostic)                       |

All halt keys have a 24-hour TTL (86400 seconds).

## 5. WebSocket Live Streaming

Per-deployment real-time event streaming from Redis pub/sub to the
browser.

```
Browser                           Backend (FastAPI)                Redis
   |                                     |                           |
   | WS /api/v1/live/stream/{dep_id}     |                           |
   |------------------------------------>|                           |
   |                                     |                           |
   |                          1. Accept connection                   |
   |                          2. Wait for auth message               |
   |                             (JWT or API key,                    |
   |                              5s timeout -> close 4001)          |
   |                          3. Validate token                      |
   |                          4. Load LiveDeployment row             |
   |                          5. Authorization check                 |
   |                             (API-key: all deployments,          |
   |                              JWT: own deployments only)         |
   |                          6. Send initial snapshot               |
   |                             (positions + account from           |
   |                              PositionReader)                    |
   |                                     |                           |
   |  {"type":"snapshot",                |                           |
   |   "positions":[...],               |                           |
   |   "account":{...}}                  |                           |
   |<------------------------------------|                           |
   |                                     |                           |
   |                          7. Subscribe to pub/sub                |
   |                             msai:live:events:{dep_id}           |
   |                                     |<------------------------->|
   |                                     |                           |
   |                          8. Forward loop:                       |
   |                             get_message -> validate JSON        |
   |                             -> send_text to client              |
   |                                     |                           |
   |  {"type":"position_changed",...}    |                           |
   |<------------------------------------|                           |
   |                                     |                           |
   |                          9. Heartbeat every 30s if idle:        |
   |  {"type":"heartbeat","ts":"..."}    |                           |
   |<------------------------------------|                           |
   |                                     |                           |
   |  (client disconnect)               |                           |
   |                          10. Unsubscribe + cleanup              |
```

WebSocket close codes:

- `4001` -- Authentication timeout or invalid token
- `4403` -- Forbidden (JWT user doesn't own the deployment)
- `4404` -- Deployment not found
- `1011` -- Snapshot failed (internal error)
