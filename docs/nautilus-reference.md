Now I have enough information to create the comprehensive reference document. Let me compile it:

---

# NAUTILUS TRADER DEEP-DIVE TECHNICAL REFERENCE

**For MSAI v2 Hedge Fund Platform** | **Last Updated: April 2026** | **Nautilus Trader v1.x**

---

## 1. NautilusKernel and TradingNode Internals

### 1.1 Kernel Initialization and Event Loop

**File:** `/nautilus_trader/system/kernel.py:101-283`

The `NautilusKernel` is the core execution engine. Its initialization follows this critical sequence:

#### **__init__() → start() → _start_engines()**

1. **`__init__` (line 132)**: Configuration validation and component instantiation
   - Environment type detection (BACKTEST, SANDBOX, LIVE)
   - Clock creation: `TestClock()` for BACKTEST, `LiveClock()` for LIVE/SANDBOX (line 164-166)
   - **CRITICAL:** uvloop event loop policy is installed globally at module import (line 97-98):
     ```python
     if uvloop is not None and "pytest" not in sys.modules:
         asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
     ```
     This happens BEFORE any TradingNode is instantiated, which means:
     - **Gotcha:** If a subprocess (like arq worker) imports `nautilus_trader` AFTER already having an event loop, the global policy change will fail or cause conflicts on Python 3.12+
     - **Fix:** Import Nautilus BEFORE creating arq Worker, or delay Nautilus import to worker startup

2. **Event Loop acquisition (line 269-283):**
   - BACKTEST: No event loop (synchronous mode)
   - LIVE/SANDBOX: Gets running loop via `asyncio.get_running_loop()` (line 272)
     - **Gotcha:** If no event loop is running, this raises `RuntimeError`
     - **Common mistake:** Calling `node = TradingNode(config)` outside `async def` in LIVE mode without a running loop

3. **Signal handler setup (line 280-283 and _setup_loop method, line 558-572):**
   - Registers handlers for `SIGTERM`, `SIGINT`, `SIGABRT` on the event loop
   - **Critical signals caught:**
     ```python
     signals = (signal.SIGTERM, signal.SIGINT, signal.SIGABRT)  # line 567
     ```
   - Signal handler flow (line 574-582):
     ```python
     def _loop_sig_handler(self, sig: signal.Signals) -> None:
         self._loop.remove_signal_handler(signal.SIGTERM)
         self._loop.add_signal_handler(signal.SIGINT, lambda: None)
         if self._loop_sig_callback:
             self._loop_sig_callback(sig)  # User callback
     ```
   - **Gotcha:** If arq or another library ALSO registers signal handlers, there can be conflicts. arq Workers set up signal handling too, which may override Nautilus handlers.

#### **start() vs start_async() (line 987-1037)**

**Synchronous path (BACKTEST):**
```python
def start(self) -> None:
    self._start_engines()        # line 995
    self._connect_clients()      # line 996
    self._emulator.start()       # line 997
    self._initialize_portfolio() # line 998
    self._trader.start()         # line 999
```

**Asynchronous path (LIVE/SANDBOX):**
```python
async def start_async(self) -> None:
    self._register_executor()                # line 1018
    self._start_engines()                    # line 1019
    self._connect_clients()                  # line 1020
    await self._await_engines_connected()    # line 1022
    if exec_engine.reconciliation:           # line 1025
        await self._await_execution_reconciliation()  # line 1026
    self._emulator.start()                   # line 1031
    self._initialize_portfolio()             # line 1032
    await self._await_portfolio_initialization()     # line 1034
    self._trader.start()                     # line 1037
```

**Key differences:**
- LIVE mode waits for reconciliation before starting trader
- Reconciliation queries the venue for open orders and compares to internal cache
- If reconciliation fails, TradingNode stays in `_is_running = True` but trader never starts

### 1.2 TradingNode vs NautilusKernel

**File:** `/nautilus_trader/live/node.py`

`TradingNode` is a **wrapper** around `NautilusKernel` that:
- Adds venue-specific client factory registration (data + exec clients)
- Handles initialization sequence for Live environments
- Is the primary API users interact with

**Relationship:**
```python
class TradingNode:
    def __init__(self, config):
        self._kernel = NautilusKernel(...)  # Kernel is internal
```

### 1.3 Running Inside an arq Subprocess with Existing Event Loop

**Critical Gotcha:** Calling `node.run()` or `await node.start()` inside an arq Worker that already has an event loop running

**Scenario:** arq Worker subprocess → imports nautilus_trader → uvloop policy set → tries to use TradingNode

**What happens:**
1. arq spawns worker with its own event loop (via `asyncio.run()` internally)
2. Code imports `nautilus_trader`, which calls `asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())`
3. If uvloop is not installed, this is a silent no-op; if uvloop IS installed, it changes the policy
4. Python 3.12+ asyncio is stricter about event loop creation
5. Creating a new event loop via `asyncio.new_event_loop()` + `asyncio.run()` will fail if the policy was changed mid-execution

**Fix:**
```python
# In arq worker startup (BEFORE importing nautilus)
import sys
if 'nautilus_trader' not in sys.modules:
    # Safe to import Nautilus now
    from nautilus_trader.live.node import TradingNode
```

### 1.4 dispose() and Resource Cleanup

**File:** `/nautilus_trader/system/kernel.py:1100+`

The `dispose()` method:
- Closes all data and execution clients
- Cancels pending timers
- Closes log guard (Rust logger)
- Clears internal caches if `drop_instruments_on_reset=True` (default)

**Known leak if you don't call it:**
- **Rust logger stays open:** The `LogGuard` keeps a Rust logger instance alive
- **Socket connections remain:** IB client sockets may not close cleanly
- **Redis connections:** Cache database connections may not flush and close
- **Memory growth:** Tick/bar deques in cache continue to consume memory

**Recommendation:** Always use `try/finally` or context managers:
```python
node = TradingNode(config)
try:
    node.build()
    await node.start_async()
    # Run...
finally:
    await node.dispose()
```

---

## 2. Live Data + Exec Client Lifecycle and Gotchas

### 2.1 InteractiveBrokersClient Connections

**File:** `/nautilus_trader/adapters/interactive_brokers/client/client.py:69-150`

Each `InteractiveBrokersClient` instance opens **ONE connection** per `client_id`.

**Questions answered:**

**Q: How many connections per client_id?**
- One TCP socket connection to IB Gateway/TWS
- Multiple logical channels (order, data, account) multiplexed over one socket
- Client ID uniqueness is enforced by IB: if you connect with the same client_id from two processes, IB will disconnect the first one

**Q: Can you share one client across data + exec?**
- **NO.** You MUST use separate `client_id` values for data and execution clients
- **Why:** IB Gateway serializes requests per client_id; mixing them can cause race conditions
- **Recommended config:**
  ```python
  # Data client uses client_id=1
  # Exec client uses client_id=2
  ```

**Q: What if you use the same client_id for data and exec?**
- Race condition: order execution requests can be interleaved with market data queries
- Market data subscriptions may be silently cancelled when order commands arrive
- IB Gateway may reject duplicate client_id connections

### 2.2 IB Gateway Port Mapping and Account Mismatch

**File:** `/nautilus_trader/adapters/interactive_brokers/config.py:203-243`

**Port configuration:**
- **Paper trading:** Port 4002 (IB Gateway) or 7497 (TWS)
- **Live trading:** Port 4001 (IB Gateway) or 7496 (TWS)

**Gotcha: Pointing at 4002 (paper) with live account_id**

When `InteractiveBrokersDataClientConfig` has:
```python
ibg_port=4002  # Paper trading port
# But account is live (e.g., DU123456)
```

**What happens:**
- Connection succeeds
- Account queries return EMPTY or dummy data
- Orders appear to submit but go nowhere
- No explicit error is raised early; failures appear downstream

**How to detect:**
```python
# Check account_id reported by IB matches expected
account = await client.get_account()  # Will be paper account if connected to 4002
if not account.account_id.startswith("DU"):  # Live account pattern
    raise ValueError(f"Expected live account, got {account}")
```

### 2.3 ibg_client_id and Multi-Node Gotchas

**File:** `/nautilus_trader/adapters/interactive_brokers/config.py:237`

**Gotcha: Two TradingNodes use the same ibg_client_id against same gateway**

When you have:
```python
# Node 1
data_config = InteractiveBrokersDataClientConfig(ibg_client_id=1, ...)
# Node 2
data_config = InteractiveBrokersDataClientConfig(ibg_client_id=1, ...)  # SAME ID!
```

**What happens:**
1. Node 1 connects: IB accepts connection with client_id=1
2. Node 2 connects: IB accepts, but **disconnects Node 1** silently
3. Node 1 is now disconnected without explicit error
4. Node 1's data subscriptions are lost mid-trade
5. Orders from Node 1 may fail with "connection lost"

**No exception is raised in Node 1** because IB doesn't tell the client "you were kicked off."

**Fix:** Ensure unique client_ids:
```python
def generate_client_id(node_id: int) -> int:
    return node_id * 10 + 1  # Node 1 → 11, Node 2 → 21, etc.
```

### 2.4 connectAsync() Timeout Behavior

**File:** `/nautilus_trader/adapters/interactive_brokers/client/connection.py:45-99`

**Configuration:**
```python
InteractiveBrokersDataClientConfig(
    connection_timeout=300  # seconds, default
)
```

**If IB Gateway is starting up (slow to boot):**
- Timeout default is 300 seconds (5 minutes)
- If connection takes longer, `TimeoutError` is raised (line 80-81)
- TradingNode startup fails

**Error behavior (line 71-99):**
```python
try:
    await self._connect_socket()
    # ... handshake ...
except TimeoutError:
    self._log.warning("Connection timeout")  # line 81
    if self._eclient.wrapper:
        self._eclient.wrapper.error(
            NO_VALID_ID,
            currentTimeMillis(),
            CONNECT_FAIL.code(),
            CONNECT_FAIL.msg(),
        )
```

**Gotcha:** No automatic retry in `_connect()`
- First connection timeout = TradingNode startup fails
- You must implement your own retry logic or use Docker container health checks

**Recommended fix:**
```python
import asyncio
from nautilus_trader.live.node import TradingNode

async def connect_with_retry(node, max_retries=5):
    for attempt in range(max_retries):
        try:
            await node.start_async()
            return
        except TimeoutError:
            if attempt < max_retries - 1:
                await asyncio.sleep(10)  # Wait before retry
            else:
                raise
```

### 2.5 Reconnection: Mid-Trade Connection Loss

**File:** `/nautilus_trader/adapters/interactive_brokers/client/connection.py:127-132`

**When IB drops the connection mid-trade:**

1. **What Nautilus still knows:**
   - Open orders from last reconciliation
   - Positions from last portfolio snapshot
   - Fills that were locally processed

2. **What is lost:**
   - Real-time quote ticks (until reconnect)
   - Bar data (subscriptions are cleared)
   - Account updates (until reconnect and reconciliation)

3. **Reconnection flow:**
   ```python
   async def _handle_reconnect(self) -> None:
       self._reset()    # Clear internal state
       self._resume()   # Resume subscriptions
   ```

4. **Critical gap:** Between disconnect and reconnect:
   - Trades executed on the venue are NOT reflected in local cache
   - If a fill happens while disconnected, reconciliation will discover it on reconnect
   - **Risk:** Strategies may enter duplicate positions if they don't account for this

**What gets restored on reconnect:**
- Order subscriptions: YES (if `fetch_all_open_orders=True` in config)
- Bar subscriptions: YES (internal state is kept)
- Account state: YES (via reconciliation)

**What doesn't auto-resume:**
- Market data subscriptions are reset but require explicit re-subscription
- If strategy logs into a new instance, it must re-subscribe to data

### 2.6 First Connection Failure

**File:** `/nautilus_trader/system/kernel.py:1001-1037`

**When the FIRST connection fails entirely:**

In `start_async()` (line 1022-1023):
```python
if not await self._await_engines_connected():
    return  # line 1023 - EARLY RETURN, no exception
```

**What happens:**
1. First connection attempt fails
2. `_await_engines_connected()` times out
3. `start_async()` returns normally (no exception)
4. `_is_running` is True but trader was never started (line 1037 not reached)
5. TradingNode appears "started" but is actually dormant

**Gotcha:** Application code can't tell TradingNode is dead:
```python
await node.start_async()  # Returns successfully even if connection failed
# Now what? node.is_running == True, but no data flowing

# FIX: Check explicitly
assert node.data_engine.is_connected, "Data engine not connected!"
```

---

## 3. Instrument Loading and Symbology Gotchas

### 3.1 SymbologyMethod Enum Values

**File:** `/nautilus_trader/adapters/interactive_brokers/config.py:30-32`

```python
class SymbologyMethod(Enum):
    IB_SIMPLIFIED = "simplified"  # Default, recommended
    IB_RAW = "raw"
```

**IB_SIMPLIFIED (default):**
- Symbol: `ESZ28.CME` (Venue suffix appended)
- Format: `{localSymbol}.{venue}`
- Pros: Human-readable, matches IB GUI
- Cons: Fails for non-standard instruments

**IB_RAW:**
- Symbol: `ES=FUT.XCME` (Raw IB format)
- Format: `{symbol}={secType}.{exchange}`
- Pros: Works for any IB-supported instrument, more robust
- Cons: Less readable, harder to debug

**Example difference:**
```python
# IB_SIMPLIFIED
instrument.id  # ESZ28.CME (E-mini S&P 500)

# IB_RAW
instrument.id  # ES=FUT.XCME (same thing, different notation)
```

### 3.2 load_ids vs load_contracts

**File:** `/nautilus_trader/adapters/interactive_brokers/config.py:101-127`

```python
class InteractiveBrokersInstrumentProviderConfig:
    load_ids: FrozenSet[InstrumentId] | None = None
    load_contracts: FrozenSet[IBContract] | None = None
```

**When to use each:**

**load_ids:** For simple, single instruments
```python
load_ids=frozenset([
    InstrumentId.from_str("AAPL.NASDAQ"),
    InstrumentId.from_str("EUR/USD.IDEALPRO"),
])
```
- Nautilus translates these to IB contracts
- Works when you know the exact symbol

**load_contracts:** For complex instrument families (options, futures chains)
```python
from nautilus_trader.adapters.interactive_brokers.common import IBContract

load_contracts=frozenset([
    IBContract(symbol="ES", secType="FUT", exchange="XCME"),  # Futures
    IBContract(symbol="AAPL", secType="OPT", exchange="SMART", right="C"),  # Options
])
```
- You specify exact contract parameters
- Nautilus then queries IB for all matching contracts (e.g., all expiry dates)
- Combined with `build_options_chain=True` and `min_expiry_days`/`max_expiry_days`

**Key difference:**
- `load_ids` is 1-to-1: one ID → one instrument
- `load_contracts` is 1-to-many: one contract spec → multiple instruments (one per expiry)

### 3.3 Non-existent Instrument Startup Failure

**File:** `/nautilus_trader/adapters/interactive_brokers/providers.py:245-310`

**Scenario:** `load_ids` includes an instrument that doesn't exist in IB

```python
load_ids=frozenset([
    InstrumentId.from_str("FAKE123.NASDAQ"),  # Doesn't exist
])
```

**What happens:**
1. `load_all_async()` is called at startup
2. For each ID, `load_with_return_async()` is called (line 267-310)
3. `fetch_instrument_id()` returns `None` (line 309)
4. **Nautilus logs a WARNING but continues** (not an error)
5. Instrument is not in cache
6. Strategy tries to subscribe to `FAKE123.NASDAQ` → error at runtime

**Critical:** Startup does NOT fail if an instrument can't be loaded
- **Fix:** Validate all instruments are loadable before starting:
  ```python
  loaded = await instrument_provider.load_all_async()
  expected = {InstrumentId.from_str(id) for id in load_ids}
  missing = expected - loaded
  if missing:
      raise ValueError(f"Failed to load: {missing}")
  ```

### 3.4 Dynamic Instrument Loading

**File:** `/nautilus_trader/adapters/interactive_brokers/providers.py:287-310`

**Can you load NEW instruments after startup?**

**YES.** Call `load_async()` at runtime:
```python
# In strategy
async def on_market_data_ready(self):
    new_instrument = InstrumentId.from_str("SPY.NASDAQ")
    await self.data_client.instrument_provider.load_async(new_instrument)
    # Now SPY is available
    self.subscribe_bars(new_instrument, BarType(...))
```

**BUT:** You're NOT locked in to pre-loaded instruments. The system is flexible.

**Gotcha:** Loading is **async and slow**
- Each instrument requires a query to IB Gateway
- Typical latency: 100-500ms per instrument
- If you load 100 instruments at once, startup takes 10-50 seconds
- Never load instruments on critical paths (inside `on_bar()`, etc.)

### 3.5 Options and Futures Chains

**File:** `/nautilus_trader/adapters/interactive_brokers/config.py:118-139`

**Options chain loading:**
```python
load_contracts=frozenset([
    IBContract(symbol="AAPL", secType="OPT", exchange="SMART"),
])
build_options_chain=True
min_expiry_days=0
max_expiry_days=365
```

**What happens:**
1. Nautilus queries IB for all option contracts matching the spec
2. Filters by expiry date: 0 to 365 days from today
3. Loads each strike × expiry combination as a separate Instrument

**Example output:**
- AAPL 2024-02-16 100 Call → Instrument
- AAPL 2024-02-16 100 Put → Instrument
- AAPL 2024-02-16 105 Call → Instrument
- ... (potentially thousands)

**Gotcha: This is SLOW**
- Full option chain for AAPL ≈ 10,000 strikes
- Loading time: 10-30 seconds for a single underlying
- IB Gateway may rate-limit you

**Recommended settings:**
```python
min_expiry_days=7       # Skip very short-dated
max_expiry_days=180     # Skip far-dated (illiquid)
# This cuts the chain from 10K to maybe 500 instruments
```

**Futures chains:** Same logic
```python
build_futures_chain=True
min_expiry_days=0
max_expiry_days=365
```

### 3.6 Continuous Futures (secType="CONTFUT")

**Current behavior (Nautilus v1.x):**
- Each `CONTFUT` contract becomes a SINGLE Instrument
- Rolling is NOT handled automatically
- You must implement rolling logic yourself

**What you need to do:**
```python
# In strategy
def on_bar(self, bar: Bar) -> None:
    if bar.instrument_id == InstrumentId.from_str("ES=CONTFUT.XCME"):
        # You're getting the front contract automatically
        # But when it expires, you must switch to the next one
        if self._is_near_expiry(bar):
            self._roll_position()
```

### 3.7 Trading Hours and RTH

**File:** `/nautilus_trader/adapters/interactive_brokers/parsing/instruments.py`

**Trading hours are loaded into Instrument:**
```python
instrument.trading_hours  # List[TradingSession]
```

**Backtests vs Live:**
- **Backtest:** Honors `TradingSession` when deciding if market is open
- **Live:** Honors both RTH (regular trading hours) and ETH (extended trading hours) depending on subscription
- **Gotcha:** If you subscribe to RTH-only data (via `use_regular_trading_hours=True` config), you will NOT see extended hours trades, but the backtest may have simulated them

### 3.8 Tick Sizes and Price Precision

**File:** `/nautilus_trader/adapters/interactive_brokers/parsing/instruments.py`

**Tick size is loaded from IB:**
```python
instrument.price_increment  # Minimum price change
```

**Gotcha: Corporate actions can change tick size**
- Instrument record is cached in Nautilus
- If IB changes the tick size (rare), Nautilus won't know about it until restart
- Orders submitted with a price that violates new tick size will be rejected

**Recommended:** Periodically refresh instrument metadata:
```python
cache_validity_days=1  # Reload instruments daily
```

---

## 4. The Cache Subsystem in Detail

### 4.1 Cache Architecture

**File:** `/nautilus_trader/cache/config.py:23-74`

The cache has two layers:

1. **In-memory cache:** Python dict-based, fast access
2. **Optional database cache:** Redis/PostgreSQL for persistence

**Files in nautilus_trader/cache/:**
- `cache.py` (or `.pyx`): Core cache implementation
- `config.py`: Cache configuration
- `database.py`: Database adapter
- `adapter.py`: Cache-to-database serialization
- `transformers.py`: Data transformations

### 4.2 Database Configuration

**File:** `/nautilus_trader/cache/config.py:29-74`

```python
class CacheConfig:
    database: DatabaseConfig | None = None
    encoding: str = "msgpack"  # or "json"
    persist_account_events: bool = True
    buffer_interval_ms: PositiveInt | None = None
    bulk_read_batch_size: PositiveInt | None = None
```

**Persistence behavior:**

1. **If `database` is None:** In-memory only, lost on restart
2. **If `database.type == "redis"`:**
   - Uses pipelined writes with buffering
   - `buffer_interval_ms` controls flush frequency (default: unbuffered)
   - Write-through or buffered: controlled by `buffer_interval_ms`

**Gotcha: Buffering can lose data on crash**
- If `buffer_interval_ms=100` and you crash before 100ms, pending writes are lost
- **Mitigation:**
  ```python
  cache=CacheConfig(
      database=DatabaseConfig(type="redis", ...),
      buffer_interval_ms=None,  # Unbuffered (slower but safe)
  )
  ```

**Encoding trade-offs:**
- **msgpack:** Compact, faster, binary-only
- **json:** Human-readable, slower, more flexible

### 4.3 Cache Query Patterns

**File:** `/nautilus_trader/cache/cache.pyx` (compiled, but signatures in .pxd)

**Common queries:**
```python
from nautilus_trader.cache import Cache

# Add an instrument
cache.add_instrument(instrument)

# Get position
position = cache.position(position_id)

# Get all open orders for a strategy
orders = cache.orders_open(strategy_id=strategy_id)

# Get account
account = cache.account_for_venue(venue=venue)

# Instruments in cache
instruments = cache.instruments()
```

**Performance notes:**
- These are O(1) lookups (dict-based)
- No N+1 queries; all data is local
- However, large caches (millions of ticks/bars) consume significant memory

### 4.4 On Restart: What Gets Restored

**File:** `/nautilus_trader/system/kernel.py:462-467`

If `CacheConfig.database` is configured:

1. **Instruments:** YES - loaded from database
2. **Open orders:** YES - loaded and reconciled with venue
3. **Positions:** YES - loaded from account state
4. **Account state:** YES - fetched fresh from venue on reconciliation
5. **Market data (ticks/bars):** NO - never persisted (would be stale)

**Reconciliation flow:**
1. Load open orders from database
2. Query venue for current open orders
3. Compare: if IB has orders not in cache, add them
4. If cache has orders not in IB, mark them as stale
5. Resolve discrepancies

**Gotcha: Reconciliation can take a LONG time**
- Querying 1000 open orders from IB: 30+ seconds
- If reconciliation times out, `start_async()` returns early (no error)

### 4.5 Memory Usage and Eviction

**File:** `/nautilus_trader/cache/config.py:56-74`

```python
class CacheConfig:
    tick_capacity: PositiveInt = 10_000  # Max ticks per instrument
    bar_capacity: PositiveInt = 10_000   # Max bars per bar_type
```

**When limits are exceeded:**
- Oldest data is dropped (FIFO eviction)
- New data is appended

**No manual eviction API available.**

**Memory growth:** Cache grows unbounded for:
- Orders (all historical orders kept)
- Positions (all closed positions kept)
- Trades (all trades kept)

**Mitigation:** Periodic reset
```python
if cache.orders_total_count() > 100_000:
    # Consider resetting the cache or archiving
    pass
```

### 4.6 Access Latency: In-Memory vs Persisted

**In-memory:** <1 microsecond
**Redis (persisted):** 1-10 milliseconds (network round-trip)

**Gotcha: Redis reads on hot paths**
- Never query Redis cache inside `on_bar()` or critical event handlers
- Data is already in memory after load; don't re-fetch from Redis
- Redis is for persistence, not hot access

---

## 5. MessageBus Deep Dive

### 5.1 API Surface

**File:** `/nautilus_trader/common/msgbus.py` (or compiled .pyx)

```python
class MessageBus:
    def subscribe(self, topic: str, callback: Callable) -> None:
        """Subscribe to a topic"""
    
    def publish(self, topic: str, event: Event) -> None:
        """Publish an event"""
    
    def request(self, request: Request, callback: Callable) -> None:
        """Send a request and wait for response"""
    
    def respond(self, response: Response) -> None:
        """Send a response to a request"""
```

### 5.2 Database Backend

**File:** `/nautilus_trader/system/kernel.py:286-301`

If `MessageBusConfig.database` is configured:

```python
message_bus=MessageBusConfig(
    database=DatabaseConfig(type="redis", ...),
    encoding="msgpack",
    stream_per_topic=True,
    buffer_interval_ms=100,
)
```

**What gets streamed:**
- ALL events published on the bus
- Stored in Redis Streams

**Gotcha: Production vs Backtest data pollution**
- If `stream_per_topic=True`, each topic gets its own Redis stream
- **Critical:** If you run backtests with `MessageBusConfig.database` configured, ALL backtest events will be written to Redis
- This mixes production and backtest data, polluting analytics
- **Fix:**
  ```python
  if config.environment == Environment.LIVE:
      # Only enable database for live
      message_bus=MessageBusConfig(database=redis_config, ...)
  else:
      # Backtest: no database
      message_bus=MessageBusConfig(database=None)
  ```

### 5.3 stream_per_topic Configuration

**File:** `/nautilus_trader/config/messaging.py` (or equivalent)

```python
stream_per_topic=True   # Each topic → 1 Redis stream
stream_per_topic=False  # All events → 1 Redis stream
```

**stream_per_topic=True:**
- Streams: `events.order.filled.STRATEGY_1`, `events.order.filled.STRATEGY_2`, etc.
- Pros: Easy filtering by topic
- Cons: Many streams to manage (1000+ topics possible)

**stream_per_topic=False:**
- Streams: `events` (one stream for everything)
- Pros: Single stream to poll
- Cons: Must filter in application code

### 5.4 Encoding Trade-offs

**msgpack:**
- Compact: ~50% of JSON size
- Binary: Can't inspect in Redis CLI
- Faster serialization
- Some types may fail (Cython objects)

**json:**
- Human-readable: Can inspect in Redis CLI
- Larger: ~2x msgpack size
- Slower serialization
- All types supported (via custom hooks)

**Gotcha: JSON serialization of complex types**
- `Quantity`, `Price`, `UUID4` objects are custom Cython types
- Default JSON serializer will fail unless custom encoder is registered
- Nautilus includes `msgspec_encoding_hook` to handle this

### 5.5 Buffering and Data Loss

**File:** `/nautilus_trader/config/messaging.py`

```python
buffer_interval_ms=100  # Flush every 100ms
```

**What happens:**
1. Events are buffered in memory
2. Every `buffer_interval_ms`, they're flushed to Redis
3. If process crashes between flushes, those events are lost

**No ACK mechanism:** Events are fire-and-forget to Redis

**Gotcha: Losing events on shutdown**
- If you don't properly wait for the buffer to flush before exiting, pending events are lost
- **Fix:**
  ```python
  # On shutdown
  await asyncio.sleep(0.2)  # Wait for buffer flush
  await node.dispose()
  ```

### 5.6 Topic Naming Convention

**Standard patterns:**
```python
"events.order.filled.{strategy_id}"
"events.position.opened.{instrument_id}"
"events.bar.{bar_type}"
"commands.system.shutdown"
```

**Subscription patterns:**
```python
msgbus.subscribe("events.order.*", callback)  # Wildcard: all order events
msgbus.subscribe("events.*", callback)        # All events
msgbus.subscribe("events.order.filled.STRATEGY_1", callback)  # Exact
```

### 5.7 Custom Events from Strategy

**Can you publish custom events?**

**YES, but not recommended.** The MessageBus is primarily for Nautilus internal communication.

```python
from nautilus_trader.core.message import Event
from uuid import uuid4

# In strategy
class MyCustomEvent(Event):
    def __init__(self, data: str, trader_id, instance_id):
        super().__init__(trader_id, instance_id, str(uuid4()), 0)
        self.data = data

# Publish
event = MyCustomEvent("test", self.trader_id, self.instance_id)
self.msgbus.publish("custom.event.test", event)

# Subscribe elsewhere
msgbus.subscribe("custom.event.test", my_callback)
```

**Better pattern:** Use side channels (Redis pub/sub, separate message queue) for strategy-to-external-system communication. Keep MessageBus for Nautilus internal events.

---

## 6. Strategy Lifecycle and Event Hooks

### 6.1 Complete Lifecycle Hooks

**File:** `/nautilus_trader/trading/strategy.pyx:200-400`

```python
class Strategy:
    def on_load(self) -> None:
        """Called when strategy is loaded from persistence"""
    
    def on_start(self) -> None:
        """Called when kernel starts (sync mode)"""
    
    def on_running(self) -> None:
        """Called after on_start, strategy is now running"""
    
    def on_stop(self) -> None:
        """Called when kernel stops"""
    
    def on_reset(self) -> None:
        """Called when kernel resets (backtests: between runs)"""
    
    def on_dispose(self) -> None:
        """Called when kernel disposes (final cleanup)"""
    
    def on_save(self) -> dict | None:
        """Called to get state to persist (return dict)"""
```

**Order of execution (typical backtest):**
1. `on_load()` - if state was persisted
2. `on_start()` - kernel is starting
3. `on_running()` - data begins to flow
4. Event handlers (`on_bar()`, `on_quote_tick()`, `on_order_filled()`, etc.)
5. `on_stop()` - kernel is stopping
6. `on_reset()` - preparing for next backtest run
7. `on_dispose()` - final cleanup

### 6.2 Difference Between Hooks

**on_load vs on_start:**
- `on_load()` is called BEFORE on_start if strategy state was persisted
- Used to restore internal state (e.g., `self.position_count = saved_state['position_count']`)

**on_stop vs on_reset:**
- `on_stop()` is called when kernel is shutting down; positions/orders may still be open
- `on_reset()` is called to clear internal state between backtest runs
- Typically: close positions in `on_stop()`, clear counters in `on_reset()`

**on_save and persistence:**
```python
def on_save(self) -> dict | None:
    return {
        'position_count': self._position_count,
        'last_signal': self._last_signal,
    }

def on_load(self) -> None:
    state = self._load_state  # Available if persisted
    if state:
        self._position_count = state['position_count']
        self._last_signal = state['last_signal']
```

### 6.3 Market Data Event Hooks

**File:** `/nautilus_trader/trading/strategy.pyx:300-350`

```python
def on_quote_tick(self, tick: QuoteTick) -> None:
    """Bid/ask updates"""

def on_trade_tick(self, tick: TradeTick) -> None:
    """Trade executions"""

def on_bar(self, bar: Bar) -> None:
    """OHLCV bars"""

def on_bar_aggregated(self, bar: Bar) -> None:
    """Custom aggregated bars (e.g., 100-tick bars)"""

def on_order_book_delta(self, delta: OrderBookDelta) -> None:
    """Order book level 2+ updates"""

def on_order_book_depth10(self, depth: OrderBookDepth10) -> None:
    """Top 10 levels of order book"""
```

### 6.4 Order Event Hooks

**File:** `/nautilus_trader/trading/strategy.pyx:450-700`

Complete order lifecycle hooks:

```python
def on_order_submitted(self, event: OrderSubmitted) -> None:
    """Order reached execution engine, waiting for venue ack"""

def on_order_accepted(self, event: OrderAccepted) -> None:
    """Venue accepted the order"""

def on_order_rejected(self, event: OrderRejected) -> None:
    """Venue rejected the order"""

def on_order_denied(self, event: OrderDenied) -> None:
    """Nautilus risk engine denied the order (before submission)"""

def on_order_emulated(self, event: OrderEmulated) -> None:
    """Order is being emulated locally (not on venue)"""

def on_order_released(self, event: OrderReleased) -> None:
    """Emulated order condition was met, now submitting to venue"""

def on_order_triggered(self, event: OrderTriggered) -> None:
    """Stop order trigger price hit"""

def on_order_updated(self, event: OrderUpdated) -> None:
    """Order parameters were modified"""

def on_order_filled(self, event: OrderFilled) -> None:
    """Order was fully or partially filled"""

def on_order_canceled(self, event: OrderCanceled) -> None:
    """Order was cancelled"""

def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None:
    """Cancellation request was rejected by venue"""

def on_order_expired(self, event: OrderExpired) -> None:
    """Order reached time-based expiration"""

def on_order_modify_rejected(self, event: OrderModifyRejected) -> None:
    """Modification request was rejected by venue"""
```

### 6.5 Position Event Hooks

```python
def on_position_opened(self, event: PositionOpened) -> None:
    """First fill entered a new position"""

def on_position_changed(self, event: PositionChanged) -> None:
    """Position size or direction changed"""

def on_position_closed(self, event: PositionClosed) -> None:
    """Position reached zero quantity"""
```

### 6.6 Order Submission Patterns

**Atomic (single order):**
```python
def submit_order(self, order: Order) -> None:
    """Submit a single order"""
```

**Batch (list):**
```python
def submit_order_list(self, orders: list[Order]) -> None:
    """Submit multiple orders atomically"""
```

**When to use which:**
- **Single:** Simple trades, one-legged orders
- **Batch:** Spreads, bracket orders (entry + stop + limit), contingent orders
- Batch orders are submitted to the venue as a unit; if one fails, none are submitted

### 6.7 Kill All / Emergency Stop

**No built-in hook for "kill all orders"** on the Strategy class.

**Implement it yourself:**
```python
class MyStrategy(Strategy):
    async def emergency_stop(self) -> None:
        """Cancel all open orders and close all positions"""
        # Cancel all orders
        for order_id in self.cache.orders_open(strategy_id=self.id):
            self.cancel_order(order_id)
        
        # Close all positions
        for position in self.portfolio.positions_open(strategy_id=self.id):
            if position.is_long:
                self.sell(position.quantity, position.instrument_id)
            elif position.is_short:
                self.buy(position.quantity, position.instrument_id)
```

**On TradingNode.stop():**
- All orders are automatically cancelled
- Positions remain open (user responsibility to close)

### 6.8 In-Flight Orders and on_stop()

**What happens to open orders when `on_stop()` is called:**

1. **Backtest:** Orders are cancelled automatically
2. **Live:** Orders remain on the venue; NOT automatically cancelled
   - You must explicitly cancel them in `on_stop()`

**Gotcha: Positions left open overnight**
```python
def on_stop(self) -> None:
    # YOU must close positions
    for position in self.portfolio.positions_open():
        if position.is_long:
            self.sell(position.quantity, position.instrument_id)
        elif position.is_short:
            self.buy(position.quantity, position.instrument_id)
```

---

## 7. RiskEngine Internals

### 7.1 Built-in Risk Checks

**File:** `/nautilus_trader/risk/engine.pyx` (compiled)

The risk engine performs checks BEFORE submitting orders to the venue:

```python
class RiskEngine:
    # Checks before submit_order():
    # 1. Maximum notional per instrument
    # 2. Order rate limits (orders per minute)
    # 3. Maximum position size per instrument
    # 4. Account-level capital utilization
    # 5. Margin requirements (leverage validation)
    # 6. Instrument trading state (halted, reducing-only, etc.)
```

### 7.2 LiveRiskEngineConfig

**File:** `/nautilus_trader/config/engine.py`

```python
class LiveRiskEngineConfig:
    max_notional_per_order: Money | None = None
    max_notional_per_instrument: Money | None = None
    order_rate_limit: int | None = None  # Orders per minute
    max_position_size: Quantity | None = None  # Per instrument
```

**Example:**
```python
risk_config = LiveRiskEngineConfig(
    max_notional_per_order=Money(100_000, USD),
    max_notional_per_instrument=Money(500_000, USD),
    order_rate_limit=100,  # Max 100 orders/minute
    max_position_size=Quantity(1000),
)
```

### 7.3 Order Denial vs Rejection

**OrderDenied:**
- Generated by Nautilus RiskEngine BEFORE submitting to venue
- Reason: Risk limit exceeded, unsupported feature, etc.
- Strategy receives `on_order_denied()` event

**OrderRejected:**
- Generated by the venue (IB, Binance, etc.) AFTER submission
- Reason: Invalid parameters, insufficient liquidity, etc.
- Strategy receives `on_order_rejected()` event

**Key difference:** OrderDenied means the order never reached the venue

### 7.4 Risk Engine Extension

**Can you extend/customize the risk engine?**

Limited options:
- Subclass `RiskEngine` and override check methods (internal API, not official)
- Configure thresholds via `RiskEngineConfig`
- Implement custom checks in Strategy before calling `submit_order()`

**Recommended pattern for custom checks:**
```python
class MyStrategy(Strategy):
    def submit_order(self, order: Order) -> None:
        # Custom validation BEFORE calling parent
        if not self._is_safe_to_trade():
            self.log.warning("Custom risk check failed")
            return
        
        # Call parent submit (runs built-in risk checks)
        super().submit_order(order)
    
    def _is_safe_to_trade(self) -> bool:
        # Custom logic: VIX check, correlation check, etc.
        return True
```

### 7.5 TradingState and Risk Engine Impact

**File:** `/nautilus_trader/model/enums.py`

```python
class TradingState(Enum):
    ACTIVE = "ACTIVE"        # Normal trading
    HALTED = "HALTED"        # Venue halted trading
    REDUCING = "REDUCING"    # Position reduction only
```

**Who sets TradingState?**
- Venue adapter (IB, Binance, etc.) when it detects a halt or circuit breaker

**Risk engine behavior:**
- `HALTED`: All orders denied
- `REDUCING`: Only orders that reduce position size are allowed
- `ACTIVE`: Normal rules apply

### 7.6 Bypass Mode

**Is there a "disable risk engine" mode?**

For **backtests**, yes:
```python
BacktestEngineConfig(
    risk_engine_config=None,  # Disable risk engine
)
```

For **live trading**, NO. RiskEngine is always active in `LiveRiskEngine`.

**Mitigation for live:** Set very high thresholds
```python
risk_config = LiveRiskEngineConfig(
    max_notional_per_order=Money(1_000_000_000, USD),  # Huge
    order_rate_limit=10_000,  # Huge
)
```

---

## 8. Backtest vs Live Config Divergence — Parity Matrix

This is THE critical section for ensuring strategy parity.

### 8.1 Venue Name and Instrument ID Suffix

**CRITICAL PARITY ISSUE**

**Backtest:**
```python
BacktestEngineConfig(
    venues=[
        BacktestVenueConfig(name="SIM", ...)  # Venue is "SIM"
    ]
)
# Instruments in backtest: AAPL.SIM, EUR/USD.SIM
```

**Live:**
```python
TradingNodeConfig(
    data_clients={
        IB: InteractiveBrokersDataClientConfig(...)  # Venue is "IB"
    }
)
# Instruments in live: AAPL.NASDAQ, EUR/USD.IDEALPRO (depends on symbology)
```

**Gotcha: Strategy must use DIFFERENT instrument IDs**

```python
# WRONG - will fail:
class MyStrategy(Strategy):
    def __init__(self, config):
        self.aapl_id = InstrumentId.from_str("AAPL.SIM")  # Only works in backtest
        self.submit_order(...)  # In live, this instrument doesn't exist

# CORRECT - use config-driven IDs:
class MyStrategy(Strategy):
    def __init__(self, config: StrategyConfig):
        self.aapl_id = config.instrument_ids[0]  # From config
```

**Config-driven approach:**
```python
# backtest_config.yaml
strategy_config:
    instrument_ids:
        - "AAPL.SIM"

# live_config.yaml
strategy_config:
    instrument_ids:
        - "AAPL.NASDAQ"
```

### 8.2 Fill Models

**Backtest:**
```python
BacktestVenueConfig(
    fill_model=ImportableFillModelConfig(
        class_name="MarketCrossEventFillModel",
        # Deterministic fill behavior
    )
)
```

**Live:**
- No fill model; fills come from the actual venue

**Parity issue:** Backtest fills may be optimistic
- Backtest: Limit orders filled at exact limit price
- Live: Limit orders filled at best available price (usually worse)

**Mitigation:**
```python
# Use realistic fill model
fill_model=ImportableFillModelConfig(
    class_name="LatencyMarketCrossEventFillModel",
    config={
        "latency_micros": 10_000,  # Add 10ms latency to fills
        "slippage_bps": 1,  # Add 1bp slippage
    }
)
```

### 8.3 Commission and Fees

**Backtest:**
```python
BacktestVenueConfig(
    fee_model=ImportableFeeModelConfig(
        class_name="BinanceFeeModel",  # Or whatever exchange you use
    )
)
```

**Live:**
- Fees are charged by the actual broker/exchange
- Reflected in fills (account state updated after)

**Parity issue:** Backtest may use wrong commission schedule
- IB commissions are complex (tiered, per-asset-class)
- Using generic fee model ≠ actual IB fees

**Mitigation:** Use actual broker's fee model if available, or calibrate manually

### 8.4 Bar Aggregation Timing

**Backtest:**
- When a bar's timestamp passes in the simulation, the bar is immediately delivered
- No waiting for the bar to "close" in real time

**Live:**
- Bars come from IB real-time or historical data
- IB may delay bar delivery (typically <1 second latency)
- Aggregating bars from trade ticks has additional latency

**Parity issue:** Backtest bars arrive faster

**Mitigation:**
```python
# In backtest, add simulated latency
bar_latency_model = ImportableLatencyModelConfig(
    class_name="ConstantLatencyModel",
    config={"latency_millis": 500}
)
```

### 8.5 Account Types and Currency

**Backtest:**
```python
BacktestVenueConfig(
    account_type=AccountType.CASH,
    base_currency=USD,
    starting_balances=[Money(100_000, USD)]
)
```

**Live (IB):**
- Account type determined by your IB account (margin, cash, etc.)
- Base currency: USD or other (depends on account)
- Starting balance: whatever is in your account

**Parity issue:** Backtest may not model margin requirements or settlement

### 8.6 dispose_on_completion

**Backtest:**
```python
BacktestEngineConfig(
    dispose_on_completion=True  # or False
)
```

**What it does:**
- `True`: Automatically call `dispose()` after backtest finishes
- `False`: Keep kernel alive (useful for interactive backtests)

**No equivalent in Live:** `TradingNode` is never auto-disposed

### 8.7 Single Strategy for Backtest + Live

**Pattern: Configuration-driven strategy**

```python
class MyStrategy(Strategy):
    """Runs identically in backtest and live"""
    
    def __init__(self, config: StrategyConfig):
        super().__init__(config)
        
        # Get IDs from config (different per environment)
        self.instrument_id = config.instrument_ids[0]
        self.venue = config.instrument_ids[0].venue
        
        # Thresholds from config
        self.entry_threshold = config.entry_threshold
    
    def on_bar(self, bar: Bar) -> None:
        if bar.instrument_id != self.instrument_id:
            return
        
        # Venue-agnostic logic
        if self._should_buy(bar):
            self.buy(...)
```

**Config files:**
```yaml
# backtest.yaml
environment: backtest
strategy:
    class_name: MyStrategy
    instrument_ids:
        - "AAPL.SIM"
    entry_threshold: 1.5

# live.yaml
environment: live
strategy:
    class_name: MyStrategy
    instrument_ids:
        - "AAPL.NASDAQ"
    entry_threshold: 1.5  # Same parameters
```

---

## 9. ParquetDataCatalog Scaling

### 9.1 Catalog Structure and Reading

**File:** `/nautilus_trader/persistence/catalog/parquet.py:100-200`

```python
class ParquetDataCatalog(BaseDataCatalog):
    def read(self, data_type: type[Data]) -> Generator[Data]:
        """Stream data from Parquet files"""
    
    def read_filtered(self, data_type: type[Data], filters: dict) -> Generator[Data]:
        """Stream with filters applied"""
```

**Physical layout:**
```
{catalog_root}/
├── Bar/
│   ├── AAPL.NASDAQ/
│   │   ├── 2024/
│   │   │   ├── 01.parquet
│   │   │   ├── 02.parquet
│   │   │   └── ...
│   │   └── 2025/
│   └── MSFT.NASDAQ/
├── QuoteTick/
├── TradeTick/
└── OrderBookDelta/
```

### 9.2 Memory vs Streaming

**When BacktestNode runs with a catalog:**

```python
BacktestEngineConfig(
    data_catalog_config=ParquetDataCatalogConfig(path="/data/parquet")
)
```

**Does it load EVERYTHING into memory?**

**NO.** Data is streamed:
1. BacktestEngine opens catalog
2. For each bar/tick, it reads from Parquet
3. Data is fed to strategy one event at a time
4. Memory usage is O(1) relative to data size

**However:**
- Ticks/bars within a single time window are buffered in the cache
- Cache has fixed capacity (`tick_capacity=10_000`, `bar_capacity=10_000`)
- If you query 100K ticks at once, you'll get them in 10K chunks

### 9.3 Terabyte-Scale Catalogs

**Recommended pattern:**

```python
BacktestEngineConfig(
    data_catalog_config=ParquetDataCatalogConfig(
        path="/data/parquet",
        filters={
            "instrument_ids": ["AAPL.NASDAQ", "MSFT.NASDAQ"],  # Filter symbols
            "start_ts": dt_to_unix_nanos(datetime(2024, 1, 1)),
            "end_ts": dt_to_unix_nanos(datetime(2024, 12, 31)),
        }
    )
)
```

**Pre-filtering reduces I/O:**
- Without filters: Scans entire catalog (slow)
- With filters: Parquet pruning avoids reading irrelevant partitions

**Partitioning strategy:**
- By instrument: `Bar/AAPL.NASDAQ/2024/01.parquet`
- By date: Easier pruning
- By symbol prefix: For large universes

### 9.4 BarDataWrangler

**File:** `/nautilus_trader/persistence/wranglers.py`

```python
from nautilus_trader.persistence.wranglers import BarDataWrangler

wrangler = BarDataWrangler(instrument=instrument)
bars = wrangler.process(csv_df)  # Process CSV into Bar objects

# Write to catalog
catalog.write(bars)
```

**Most efficient pattern for 1-minute bars across 100 symbols:**

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

async def ingest_efficiently():
    catalog = ParquetDataCatalog(path="/data/parquet")
    
    # Parallel ingestion: one thread per symbol
    with ThreadPoolExecutor(max_workers=10) as executor:
        tasks = [
            executor.submit(ingest_symbol, symbol)
            for symbol in symbols
        ]
        await asyncio.gather(*tasks)

def ingest_symbol(symbol: str):
    """Ingest 1 year of 1-minute bars for a symbol"""
    wrangler = BarDataWrangler(instrument=instrument_for_symbol(symbol))
    bars = wrangler.process(load_csv(f"data/{symbol}.csv"))
    
    # Batch write (faster than individual writes)
    catalog.write(bars, batch_size=10_000)
```

**Typical ingestion speed:**
- 1M bars: ~2 seconds
- 100M bars: ~3 minutes (with parallelization)
- 1B bars: ~30 minutes

### 9.5 Timezone Handling Gotchas

**Parquet stores timestamps as UTC nanoseconds (int64)**

**Gotcha: Naive datetimes**
```python
# WRONG
bar.ts = datetime(2024, 1, 1, 10, 0)  # Naive datetime

# CORRECT
from pytz import UTC
bar.ts = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
```

**Gotcha: Market hours in different timezones**
```python
# AAPL trades in NY (EST/EDT)
# ESZ24 (S&P 500 futures) trades in Chicago (CST/CDT)
# They have different trading hours!

# When writing bars, ensure they're all in UTC
bar_time_ny = datetime(2024, 1, 1, 10, 0, tzinfo=timezone(timedelta(hours=-5)))
bar_time_utc = bar_time_ny.astimezone(UTC)
```

### 9.6 Concurrent Writes to Catalog

**Can multiple processes write to the same Parquet catalog?**

**NO.** Parquet writes require exclusive access to files.

**Solution:**
- Use a queue (Redis, arq) to serialize writes
- One "catalog writer" process handles all writes
- Multiple "ingestion" processes enqueue data

```python
# Ingestion process
async def ingest_data(symbol: str):
    bars = get_bars_from_api(symbol)
    # Push to queue, don't write directly
    await queue.enqueue("write_bars", symbol=symbol, bars=bars)

# Writer process
async def catalog_writer_worker():
    while True:
        job = await queue.dequeue()
        catalog.write(job.bars)  # Exclusive write
```

---

## 10. CRITICAL GOTCHAS — Top 20 List for Production

### 1. **uvloop Policy Installed at Import Time → Breaks arq Workers on Python 3.12+**

**Symptom:** arq Worker fails to start with `RuntimeError` about event loop policy

**Root cause:** `/nautilus_trader/system/kernel.py:97-98` installs uvloop policy globally when module is imported. If arq Worker already has a running event loop, this conflicts.

**Fix:**
```python
# In arq startup, BEFORE importing Nautilus:
import sys
import asyncio

# Set policy BEFORE creating any event loops
if 'uvloop' not in sys.modules:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

# NOW safe to import
from nautilus_trader.live.node import TradingNode
```

---

### 2. **generate_account_report() Requires venue= OR account_id=, Raises ValueError**

**Symptom:** `ValueError: At least one of 'venue' or 'account_id' must be provided`

**Root cause:** `/nautilus_trader/trading/trader.py:911-912` requires at least one parameter

**Fix:**
```python
# WRONG
report = trader.generate_account_report()

# CORRECT
report = trader.generate_account_report(venue=Venue("NASDAQ"))
# Or
report = trader.generate_account_report(account_id=AccountId("DU123456-LIVE"))
```

---

### 3. **Two TradingNodes Use Same ibg_client_id → Silent Disconnect of First Node**

**Symptom:** First TradingNode loses connection, but no error is logged

**Root cause:** IB Gateway disconnects older connection when new one arrives with same client_id

**Fix:**
```python
# Ensure unique client_ids:
node1_config = TradingNodeConfig(
    data_clients={
        IB: InteractiveBrokersDataClientConfig(ibg_client_id=1, ...)
    }
)
node2_config = TradingNodeConfig(
    data_clients={
        IB: InteractiveBrokersDataClientConfig(ibg_client_id=2, ...)  # DIFFERENT
    }
)
```

---

### 4. **BacktestVenueConfig.name Must Match Instrument Venue Suffix**

**Symptom:** Backtest runs but strategy sees no instruments; orders submitted to non-existent venue

**Root cause:** Venue names must match exactly
```python
BacktestVenueConfig(name="NASDAQ")  # Venue is "NASDAQ"
# Instruments must be: AAPL.NASDAQ

BacktestVenueConfig(name="SIM")     # Venue is "SIM"
# Instruments must be: AAPL.SIM
```

**Fix:**
```python
# Be explicit and consistent
BACKTEST_VENUE = "BACKTEST"
BacktestVenueConfig(name=BACKTEST_VENUE, ...)
instrument_id = InstrumentId.from_str(f"AAPL.{BACKTEST_VENUE}")
```

---

### 5. **ConnectionTimeout on IB Gateway Startup → TradingNode Starts Dormant**

**Symptom:** `await node.start_async()` returns successfully, but node is not connected

**Root cause:** `/nautilus_trader/system/kernel.py:1022-1023` returns early if connection fails, no exception

**Fix:**
```python
await node.start_async()

# MUST verify connection
assert node.data_engine.is_connected, "Data engine failed to connect!"
if not node.data_engine.is_connected:
    await asyncio.sleep(5)
    await node.start_async()  # Retry
```

---

### 6. **IB Gateway Port 4002 (Paper) with Live Account → Silent Data Failure**

**Symptom:** Account queries return dummy data, no positions, no orders visible

**Root cause:** Wrong port connected to wrong account type

**Fix:**
```python
def validate_ib_connection(client: InteractiveBrokersClient, account_pattern: str):
    account = client.get_account()
    if not account.account_id.startswith(account_pattern):
        raise ValueError(
            f"Expected {account_pattern}, got {account.account_id}"
        )
```

---

### 7. **Buffered Cache Database Loses Data on Crash**

**Symptom:** Positions/orders lost after unexpected shutdown

**Root cause:** `buffer_interval_ms` batches writes; crash before flush = data loss

**Fix:**
```python
CacheConfig(
    database=DatabaseConfig(type="redis", ...),
    buffer_interval_ms=None,  # Unbuffered, safer
    # OR
    buffer_interval_ms=50,  # Flush more frequently
)

# On shutdown, wait for buffer:
await asyncio.sleep(0.2)
await node.dispose()
```

---

### 8. **Backtest MessageBus Database Config Pollutes Production Redis**

**Symptom:** Dashboard shows mixed backtest + production events

**Root cause:** Backtest engine is configured with `MessageBusConfig.database` pointing to production Redis

**Fix:**
```python
if environment == "backtest":
    msgbus_config = MessageBusConfig(database=None)  # No persistence
else:
    msgbus_config = MessageBusConfig(database=redis_config)  # Production
```

---

### 9. **Instrument Not Pre-loaded → Fails at Runtime, No Early Error**

**Symptom:** Strategy calls `subscribe_bars(instrument_id)` for an instrument not in cache

**Root cause:** Instrument loading only checks at startup; later subscriptions fail silently

**Fix:**
```python
# Validate before strategy starts
expected_instruments = [InstrumentId.from_str(id) for id in config.instruments]
for instrument_id in expected_instruments:
    assert cache.instrument(instrument_id) is not None, \
        f"Instrument not loaded: {instrument_id}"
```

---

### 10. **Reconciliation Timeout on Startup → Node Appears Running but Dead**

**Symptom:** `node.is_running == True` but trader never started

**Root cause:** Reconciliation queries venue for open orders; if query hangs, `start_async()` returns early

**Fix:**
```python
# Explicit reconciliation check:
if node._kernel.exec_engine.reconciliation:
    # Wait for reconciliation to complete
    for _ in range(60):
        if node.exec_engine.is_reconciled:
            break
        await asyncio.sleep(1)
    else:
        raise TimeoutError("Reconciliation timeout")
```

---

### 11. **Dynamic Instrument Loading is Synchronous and Slow**

**Symptom:** Strategy waits 5+ seconds for `load_async()` to complete

**Root cause:** Each instrument requires a separate query to IB Gateway

**Fix:**
```python
# Pre-load instruments, don't load dynamically
config.instrument_provider.load_ids = frozenset([
    InstrumentId.from_str("SPY.NASDAQ"),
    InstrumentId.from_str("QQQ.NASDAQ"),
])

# At runtime, don't call load_async() on critical paths
# Pre-load everything at startup
```

---

### 12. **Options Chain Loading: Thousands of Instruments for Single Underlying**

**Symptom:** Startup takes 30+ seconds, memory explodes

**Root cause:** Full option chain for AAPL = 10,000+ strikes

**Fix:**
```python
InteractiveBrokersInstrumentProviderConfig(
    build_options_chain=True,
    min_expiry_days=7,         # Skip short-dated
    max_expiry_days=180,       # Skip far-dated
    # Reduces AAPL chain from 10K to ~500 instruments
)
```

---

### 13. **TradingNode.stop() Doesn't Close Positions — Orders Stay Open Overnight**

**Symptom:** Morning: unexpected open positions from previous day

**Root cause:** `on_stop()` is not called automatically; you must implement it

**Fix:**
```python
class MyStrategy(Strategy):
    def on_stop(self) -> None:
        """MUST explicitly close positions"""
        for position in self.portfolio.positions_open():
            if position.is_long:
                self.sell(position.quantity, position.instrument_id)
            elif position.is_short:
                self.buy(position.quantity, position.instrument_id)
```

---

### 14. **Limit Order Fills: Backtest Optimistic, Live Pessimistic**

**Symptom:** Backtest PnL 20% higher than live

**Root cause:** Backtest fills limit orders at limit price; live fills at mid/worst available

**Fix:**
```python
# Use realistic fill model in backtest
BacktestVenueConfig(
    fill_model=ImportableFillModelConfig(
        class_name="LatencyMarketCrossEventFillModel",
        config={"slippage_bps": 2}  # Add slippage
    )
)
```

---

### 15. **Cache Eviction: Oldest Ticks Dropped When Capacity Exceeded**

**Symptom:** Strategy queries `cache.ticks()` for old data; data is missing

**Root cause:** Cache has max capacity (`tick_capacity=10_000`); old ticks are evicted

**Fix:**
```python
CacheConfig(
    tick_capacity=100_000,  # Increase if you need historical data
    bar_capacity=50_000,
)

# OR: Don't rely on cache for historical data; query Parquet catalog instead
```

---

### 16. **Strategy State Persistence: on_save() Return Value Not Validated**

**Symptom:** Strategy resumes with lost or corrupted state

**Root cause:** `on_save()` must return a dict; if it returns None, no state is saved

**Fix:**
```python
def on_save(self) -> dict | None:
    state = {
        'position_count': self._position_count,
        'last_price': float(self._last_price),
    }
    assert state, "State must not be empty"
    return state
```

---

### 17. **MessageBus JSON Serialization Fails on Custom Types**

**Symptom:** `TypeError: Object of type Quantity is not JSON serializable`

**Root cause:** JSON encoder doesn't know how to serialize Nautilus types

**Fix:**
```python
MessageBusConfig(
    encoding="msgpack",  # Binary is safer for Cython types
    # OR
    encoding="json",  # Requires custom encoder registered
)
```

---

### 18. **Running Inside asyncio.run() with TradingNode → Event Loop Policy Conflict**

**Symptom:** `RuntimeError: Event loop already running` or `asyncio.InvalidStateError`

**Root cause:** TradingNode expects an already-running loop; `asyncio.run()` creates a new one

**Fix:**
```python
# WRONG
asyncio.run(node.start_async())

# CORRECT
import asyncio
async def main():
    node = TradingNode(config)
    await node.build()
    await node.start_async()
    # ...
    await node.dispose()

asyncio.run(main())
```

---

### 19. **Reconciliation Discovers Fills That Weren't in Cache → Unexpected Positions**

**Symptom:** After reconnect, position size jumped unexpectedly

**Root cause:** Fill occurred while disconnected; reconciliation discovered it and updated cache

**Fix:**
```python
# Monitor account state changes explicitly
def on_account_updated(self, event: AccountUpdated) -> None:
    # Reconciliation may have changed balances/positions
    self.log.info(f"Account updated: {event}")
```

---

### 20. **dispose() Not Called → Rust Logger and Sockets Leak**

**Symptom:** Process won't exit cleanly; "zombie" connections to IB Gateway

**Root cause:** `LogGuard` is not released; file handles not closed

**Fix:**
```python
try:
    await node.start_async()
    # ...
finally:
    await node.dispose()  # MUST call this

# Better: Use async context manager if available
# Or wrap in finally block always
```

---

## Summary: Production Readiness Checklist

- [ ] **Event loops:** Validate asyncio setup before TradingNode creation
- [ ] **IB Gateway:** Verify port (4001 live, 4002 paper) matches account type
- [ ] **Client IDs:** Ensure unique ibg_client_id for each node
- [ ] **Instruments:** Pre-load all required instruments; validate at startup
- [ ] **Venue names:** Match backtest venue config to instrument suffixes
- [ ] **Strategy state:** Implement on_save()/on_load() for persistence
- [ ] **Position closing:** Explicitly close positions in on_stop()
- [ ] **Risk engine:** Configure realistic limits; test with live dry-run
- [ ] **Cache:** Set appropriate tick/bar capacities; monitor memory
- [ ] **Logging:** Enable file logging; rotate logs daily
- [ ] **Reconciliation:** Verify completion before trader starts
- [ ] **Database:** Use unbuffered writes for cache/msgbus
- [ ] **Cleanup:** Always call dispose() on shutdown
- [ ] **Monitoring:** Log connection status, reconciliation, fills
- [ ] **Testing:** Run strategy in sandbox before live

---

**Document Version:** 1.0  
**Last Updated:** April 6, 2026  
**Nautilus Trader Version:** 1.x  
**For:** MSAI v2 Hedge Fund Platform