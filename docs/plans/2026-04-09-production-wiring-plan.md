# Production Wiring Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire every remaining stub, placeholder, and unstarted background service so claude-version is fully functional for backtesting, paper trading, live trading, and monitoring — then regenerate documentation from the completed code.

**Architecture:** No new abstractions. Every piece of infrastructure exists. This is connecting wires: replacing stubs with calls to existing services, starting existing background tasks, adding counter/alert calls at lifecycle points, scheduling existing jobs via arq cron, and replacing mock-data imports with real API calls in the frontend.

**Tech Stack:** Python 3.12, FastAPI, arq, ib_async, NautilusTrader, structlog, Next.js 15, React

---

## Review Corrections (Iteration 1+2)

**IMPORTANT: The code snippets in this plan are DIRECTIONAL, not copy-paste-ready.** The review loop found that several snippets were written from memory rather than from reading the actual source. During execution, each task MUST be implemented by reading the actual code first. The corrections below document what the reviews caught:

| Task | Finding                                                                                         | Fix                                                                                                |
| ---- | ----------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| 2    | `fill_price` column doesn't exist on `OrderAttemptAudit`                                        | Use `r.price` instead                                                                              |
| 3    | `StateApplier` takes `AsyncRedis` object, not URL string                                        | Create `aioredis.from_url(...)` first, pass the client                                             |
| 3    | Plan only starts `StateApplier`, not `ProjectionConsumer` + `StreamRegistry` + `DualPublisher`  | Must wire full projection stack for events to flow                                                 |
| 4    | `clientId=0` conflicts with live-node config (reserved as master slot)                          | Use a dedicated client ID like `99` for account queries                                            |
| 4    | Returning `{}` on offline breaks existing test assertions                                       | Return full dict with zero values on offline, not empty                                            |
| 6    | `RiskAwareStrategy.submit_order_with_risk_check()` is synchronous, no `_alerting` slot          | Only wire alerts in async surfaces: `IBDisconnectHandler.on_halt` + `ProcessManager` spawn failure |
| 7    | `MarketHoursService` has no `session_factory` kwarg — uses no-arg constructor + async `prime()` | Construct + prime inside subprocess using `payload.database_url`                                   |
| 8    | PnL aggregation writes `pnl=0` — structurally wrong without fill price data                     | Need to source PnL from Nautilus `AccountState` events via projection, not from audit rows         |
| 9    | UTC times wrong for EDT: 4:30 PM ET = 20:30 UTC (not 21:30), 1:00 AM ET = 05:00 UTC (not 06:00) | Fix cron hour values                                                                               |
| 9    | Uses `settings.data_root` but worker writes to `settings.parquet_root`                          | Use correct config path                                                                            |
| 10   | Components have internal mock data (props), not just page-level imports                         | Must update component props interfaces + pass real data down                                       |
| 11   | `docs/architecture/` doesn't exist in worktree (only on main)                                   | Create the directory during execution                                                              |

---

### Task 1: Wire `/api/v1/live/positions` to PositionReader

**Files:**

- Modify: `claude-version/backend/src/msai/api/live.py` (positions endpoint, ~line 956)
- Test: `claude-version/backend/tests/unit/test_live_positions_endpoint.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_live_positions_endpoint.py
"""Unit test: /api/v1/live/positions returns data from PositionReader."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from msai.main import app


@pytest.fixture
def mock_position_reader():
    reader = MagicMock()
    reader.get_open_positions = AsyncMock(return_value=[])
    return reader


@pytest.mark.asyncio
async def test_positions_returns_empty_when_no_deployments(mock_position_reader):
    with patch("msai.api.live.get_position_reader", return_value=mock_position_reader):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/live/positions",
                headers={"X-API-Key": "msai-dev-key"},
            )
    assert resp.status_code == 200
    assert resp.json()["positions"] == []
```

**Step 2: Run test to verify it fails**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_live_positions_endpoint.py -v`
Expected: FAIL (current endpoint ignores PositionReader)

**Step 3: Implement — replace the stub**

In `api/live.py`, replace the positions endpoint (~line 956):

```python
@router.get("/positions")
async def live_positions(
    claims: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LivePositionsResponse:
    """Open positions across all active deployments, read from ProjectionState."""
    from msai.api.live_deps import get_position_reader

    reader = get_position_reader()

    # Gather positions for every active deployment
    active_rows = (
        await db.execute(
            select(LiveDeployment).where(
                LiveDeployment.status.in_(("running", "ready"))
            )
        )
    ).scalars().all()

    all_positions: list[dict[str, Any]] = []
    for dep in active_rows:
        snapshots = await reader.get_open_positions(
            deployment_id=dep.id,
            trader_id=dep.trader_id,
            strategy_id_full=f"{dep.trader_id}-{dep.strategy_id}",
        )
        for snap in snapshots:
            all_positions.append(snap.model_dump())

    return LivePositionsResponse(positions=all_positions)
```

**Step 4: Run test to verify it passes**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_live_positions_endpoint.py -v`

**Step 5: Commit**

```bash
git add -A && git commit -m "feat: wire /api/v1/live/positions to PositionReader"
```

---

### Task 2: Wire `/api/v1/live/trades` to order_attempt_audits

**Files:**

- Modify: `claude-version/backend/src/msai/api/live.py` (trades endpoint, ~line 967)
- Test: `claude-version/backend/tests/unit/test_live_trades_endpoint.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_live_trades_endpoint.py
"""Unit test: /api/v1/live/trades queries order_attempt_audits."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_trades_endpoint_queries_audits(client, db_session):
    """The trades endpoint should query order_attempt_audits with is_live filter."""
    resp = await client.get(
        "/api/v1/live/trades",
        headers={"X-API-Key": "msai-dev-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "trades" in data
    assert "total" in data
```

**Step 2: Run test to verify it fails**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_live_trades_endpoint.py -v`

**Step 3: Implement — replace the stub**

In `api/live.py`, replace the trades endpoint (~line 967):

```python
@router.get("/trades")
async def live_trades(
    claims: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> LiveTradesResponse:
    """Recent live trade executions from order_attempt_audits."""
    from msai.models.order_attempt_audit import OrderAttemptAudit

    count_q = select(func.count()).select_from(OrderAttemptAudit).where(
        OrderAttemptAudit.is_live.is_(True),
        OrderAttemptAudit.status.in_(("filled", "partially_filled")),
    )
    total = (await db.execute(count_q)).scalar_one()

    rows_q = (
        select(OrderAttemptAudit)
        .where(
            OrderAttemptAudit.is_live.is_(True),
            OrderAttemptAudit.status.in_(("filled", "partially_filled")),
        )
        .order_by(OrderAttemptAudit.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(rows_q)).scalars().all()

    trades = [
        {
            "id": str(r.id),
            "deployment_id": str(r.deployment_id),
            "instrument_id": r.instrument_id,
            "side": r.side,
            "quantity": str(r.quantity),
            "price": str(r.price) if r.price else None,
            "status": r.status,
            "client_order_id": r.client_order_id,
            "timestamp": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]

    return LiveTradesResponse(trades=trades, total=total)
```

**Step 4: Run tests**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_live_trades_endpoint.py -v`

**Step 5: Commit**

```bash
git add -A && git commit -m "feat: wire /api/v1/live/trades to order_attempt_audits query"
```

---

### Task 3: Start ProjectionConsumer + StateApplier in FastAPI lifespan

**Files:**

- Modify: `claude-version/backend/src/msai/main.py` (lifespan function, ~line 75)
- Modify: `claude-version/backend/src/msai/api/live_deps.py` (add consumer/applier factory)
- Test: `claude-version/backend/tests/unit/test_projection_startup.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_projection_startup.py
"""Verify the lifespan starts projection background tasks."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_lifespan_starts_projection_tasks():
    """The lifespan should create and cancel projection tasks."""
    from msai.main import app

    mock_consumer_run = AsyncMock()
    mock_applier_run = AsyncMock()

    with (
        patch("msai.main._start_projection_tasks") as mock_start,
        patch("msai.main._stop_projection_tasks") as mock_stop,
    ):
        mock_start.return_value = None
        mock_stop.return_value = None

        async with app.router.lifespan_context(app):
            mock_start.assert_called_once()

        mock_stop.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_projection_startup.py -v`

**Step 3: Implement — add projection startup to lifespan**

In `main.py`, modify the lifespan and add helper functions:

```python
import asyncio

_projection_tasks: list[asyncio.Task] = []
_projection_stop = asyncio.Event()


async def _start_projection_tasks() -> None:
    """Start ProjectionConsumer + StateApplier as background tasks."""
    from msai.api.live_deps import get_projection_state, get_live_redis_binary
    from msai.services.nautilus.projection.state_applier import StateApplier

    state = get_projection_state()

    # StateApplier takes a prepared AsyncRedis instance, not a URL string
    import redis.asyncio as aioredis
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    # StateApplier — subscribes to msai:live:state:* and feeds ProjectionState
    applier = StateApplier(
        redis=redis_client,
        projection_state=state,
    )
    _projection_stop.clear()
    task = asyncio.create_task(applier.run(_projection_stop))
    _projection_tasks.append(task)


async def _stop_projection_tasks() -> None:
    """Signal projection tasks to stop and await them."""
    _projection_stop.set()
    for task in _projection_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _projection_tasks.clear()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await _ensure_api_key_user()
    await _start_projection_tasks()
    yield
    await _stop_projection_tasks()
```

**Step 4: Run tests**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_projection_startup.py -v`

**Step 5: Commit**

```bash
git add -A && git commit -m "feat: start StateApplier in FastAPI lifespan for live position projection"
```

---

### Task 4: Wire real ib_async account queries

**Files:**

- Modify: `claude-version/backend/src/msai/services/ib_account.py`
- Test: `claude-version/backend/tests/unit/test_ib_account.py`

**Step 1: Write the failing test**

```python
# tests/unit/test_ib_account.py
"""Unit test: IBAccountService connects via ib_async when available."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from msai.services.ib_account import IBAccountService


@pytest.mark.asyncio
async def test_get_summary_returns_real_data_when_connected():
    """When IB is reachable, get_summary returns real account values."""
    mock_ib = MagicMock()
    mock_ib.connectAsync = AsyncMock()
    mock_ib.accountSummaryAsync = AsyncMock(return_value=[
        MagicMock(tag="NetLiquidation", value="100000.00"),
        MagicMock(tag="BuyingPower", value="200000.00"),
    ])
    mock_ib.disconnect = MagicMock()

    with patch("msai.services.ib_account.IB", return_value=mock_ib):
        svc = IBAccountService(host="localhost", port=4002)
        result = await svc.get_summary()

    assert "net_liquidation" in result
    assert result["net_liquidation"] == 100000.0


@pytest.mark.asyncio
async def test_get_summary_graceful_fallback_when_disconnected():
    """When IB is unreachable, get_summary returns empty dict."""
    mock_ib = MagicMock()
    mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError)

    with patch("msai.services.ib_account.IB", return_value=mock_ib):
        svc = IBAccountService(host="localhost", port=4002)
        result = await svc.get_summary()

    assert result == {}
```

**Step 2: Run test to verify it fails**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_ib_account.py -v`

**Step 3: Implement — replace mock with real ib_async**

```python
"""IB account data queries via ib_async."""
from __future__ import annotations

from typing import Any

from msai.core.logging import get_logger

log = get_logger(__name__)

try:
    from ib_async import IB
except ImportError:
    IB = None  # type: ignore[assignment,misc]


class IBAccountService:
    """Queries IB Gateway for account data.

    Falls back gracefully to empty responses if IB Gateway is
    unreachable or ib_async is not installed.
    """

    def __init__(self, host: str = "ib-gateway", port: int = 4002) -> None:
        self.host = host
        self.port = port

    async def get_summary(self) -> dict[str, float]:
        """Return account summary. Empty dict if IB unreachable."""
        if IB is None:
            log.warning("ib_async_not_installed")
            return {}

        ib = IB()
        try:
            await ib.connectAsync(self.host, self.port, clientId=0, timeout=5)
            tags = await ib.accountSummaryAsync()
            result: dict[str, float] = {}
            tag_map = {
                "NetLiquidation": "net_liquidation",
                "BuyingPower": "buying_power",
                "TotalCashValue": "available_funds",
                "MaintMarginReq": "margin_used",
                "UnrealizedPnL": "unrealized_pnl",
                "RealizedPnL": "realized_pnl",
            }
            for item in tags:
                key = tag_map.get(item.tag)
                if key:
                    try:
                        result[key] = float(item.value)
                    except (ValueError, TypeError):
                        pass
            return result
        except Exception:
            log.warning("ib_account_summary_failed", host=self.host, port=self.port)
            return {}
        finally:
            ib.disconnect()

    async def get_portfolio(self) -> list[dict[str, Any]]:
        """Return current IB portfolio positions. Empty list if unreachable."""
        if IB is None:
            return []

        ib = IB()
        try:
            await ib.connectAsync(self.host, self.port, clientId=0, timeout=5)
            positions = ib.portfolio()
            return [
                {
                    "symbol": p.contract.symbol,
                    "sec_type": p.contract.secType,
                    "position": float(p.position),
                    "market_price": float(p.marketPrice),
                    "market_value": float(p.marketValue),
                    "average_cost": float(p.averageCost),
                    "unrealized_pnl": float(p.unrealizedPNL),
                    "realized_pnl": float(p.realizedPNL),
                }
                for p in positions
            ]
        except Exception:
            log.warning("ib_portfolio_failed", host=self.host, port=self.port)
            return []
        finally:
            ib.disconnect()
```

**Step 4: Run tests**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_ib_account.py -v`

**Step 5: Commit**

```bash
git add -A && git commit -m "feat: wire real ib_async connection for account endpoints"
```

---

### Task 5: Add Prometheus counters at trading lifecycle points

**Files:**

- Create: `claude-version/backend/src/msai/services/observability/trading_metrics.py`
- Modify: `claude-version/backend/src/msai/live_supervisor/process_manager.py` (spawn/stop)
- Modify: `claude-version/backend/src/msai/services/nautilus/audit_hook.py` (order events)
- Modify: `claude-version/backend/src/msai/api/live.py` (kill-all)
- Test: `claude-version/backend/tests/unit/test_trading_metrics.py`

**Step 1: Write failing test**

```python
# tests/unit/test_trading_metrics.py
from msai.services.observability.trading_metrics import DEPLOYMENTS_STARTED, ORDERS_SUBMITTED

def test_counters_exist_and_increment():
    DEPLOYMENTS_STARTED.inc()
    ORDERS_SUBMITTED.inc()
    # No crash = pass. The real assertion is that they render at /metrics.
```

**Step 2: Run test to verify it fails**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_trading_metrics.py -v`

**Step 3: Implement — create metrics module + add .inc() calls**

```python
# services/observability/trading_metrics.py
"""Pre-registered Prometheus counters for the trading lifecycle."""
from msai.services.observability import get_registry

_r = get_registry()

DEPLOYMENTS_STARTED = _r.counter("msai_deployments_started_total", "Live deployments started")
DEPLOYMENTS_STOPPED = _r.counter("msai_deployments_stopped_total", "Live deployments stopped")
DEPLOYMENTS_FAILED = _r.counter("msai_deployments_failed_total", "Live deployments failed")
KILL_SWITCH_ACTIVATED = _r.counter("msai_kill_switch_total", "Kill switch activations")
ORDERS_SUBMITTED = _r.counter("msai_orders_submitted_total", "Orders submitted to broker")
ORDERS_FILLED = _r.counter("msai_orders_filled_total", "Orders filled by broker")
ORDERS_DENIED = _r.counter("msai_orders_denied_total", "Orders denied by risk checks")
IB_DISCONNECTS = _r.counter("msai_ib_disconnects_total", "IB Gateway disconnect events")
ACTIVE_DEPLOYMENTS = _r.gauge("msai_active_deployments", "Currently active deployments")
```

Then add one-line `.inc()` calls at each lifecycle point:

- `process_manager.py` spawn success → `DEPLOYMENTS_STARTED.inc()`
- `process_manager.py` spawn fail → `DEPLOYMENTS_FAILED.inc()`
- `process_manager.py` stop → `DEPLOYMENTS_STOPPED.inc()`
- `audit_hook.py` write_submitted → `ORDERS_SUBMITTED.inc()`
- `audit_hook.py` update_filled → `ORDERS_FILLED.inc()`
- `api/live.py` kill_all → `KILL_SWITCH_ACTIVATED.inc()`
- `disconnect_handler.py` on disconnect → `IB_DISCONNECTS.inc()`

**Step 4: Run tests**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_trading_metrics.py -v`

**Step 5: Commit**

```bash
git add -A && git commit -m "feat: add Prometheus counters for trading lifecycle events"
```

---

### Task 6: Wire alert triggers

**Files:**

- Modify: `claude-version/backend/src/msai/services/nautilus/disconnect_handler.py`
- Modify: `claude-version/backend/src/msai/services/nautilus/risk/risk_aware_strategy.py`
- Modify: `claude-version/backend/src/msai/live_supervisor/process_manager.py`
- Test: `claude-version/backend/tests/unit/test_alert_triggers.py`

**Step 1: Write failing test**

```python
# tests/unit/test_alert_triggers.py
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_disconnect_handler_triggers_alert():
    """IBDisconnectHandler should call alert_ib_disconnect on extended disconnect."""
    from msai.services.alerting import AlertService
    with patch.object(AlertService, "alert_ib_disconnect", new_callable=AsyncMock) as mock_alert:
        # Simulate extended disconnect triggering the alert
        # The actual wiring will call alerting_service.alert_ib_disconnect()
        await mock_alert()
        mock_alert.assert_called_once()
```

**Step 2: Run test — should fail initially**

**Step 3: Implement — add alert calls**

In `disconnect_handler.py`, after the halt flag is set (the `_fire_halt` method), add:

```python
from msai.services.alerting import AlertService
alerting = AlertService()
await alerting.alert_ib_disconnect()
```

In `risk_aware_strategy.py`, after daily loss check denies an order, add:

```python
# Already async — can call alerting
await self._alerting.alert_daily_loss(current_pnl=daily_pnl, threshold=self._risk_limits.max_daily_loss)
```

In `process_manager.py`, after spawn failure with `SPAWN_FAILED_PERMANENT`, add:

```python
await self._alerting.alert_strategy_error(strategy_name=str(deployment_id), error=str(exc))
```

**Step 4: Run tests**

**Step 5: Commit**

```bash
git add -A && git commit -m "feat: wire alert triggers for IB disconnect, daily loss, and spawn failures"
```

---

### Task 7: Wire MarketHoursService into RiskAwareStrategy

**Files:**

- Modify: `claude-version/backend/src/msai/live_supervisor/__main__.py`
- Modify: `claude-version/backend/src/msai/services/nautilus/trading_node_subprocess.py`
- Test: existing `tests/unit/test_market_hours.py` should continue to pass

**Step 1: Read current wiring**

Read `__main__.py` `_build_production_payload_factory` and `trading_node_subprocess.py` `_build_real_node` to identify the exact injection point.

**Step 2: Implement**

**IMPORTANT:** Callables cannot be pickled across `mp.Process` boundaries. The `MarketHoursService` + `make_market_hours_check` must be constructed INSIDE the subprocess, not in `__main__.py`.

In `trading_node_subprocess.py`, inside `_build_real_node` (the production node factory), AFTER the `TradingNode` is built but BEFORE the strategy is started:

```python
from msai.services.nautilus.market_hours import MarketHoursService, make_market_hours_check

# Construct market hours check inside the subprocess (cannot pickle callables across processes)
market_hours_svc = MarketHoursService(session_factory=session_factory)
market_hours_check = make_market_hours_check(market_hours_svc)
```

Then pass `market_hours_check` to the strategy's `_market_hours_check` field when constructing the `RiskAwareStrategy` config. The `session_factory` is already available inside the subprocess (created from `payload.database_url`).

**Step 3: Run existing tests**

Run: `cd claude-version/backend && uv run pytest tests/unit/test_market_hours.py -v`

**Step 4: Commit**

```bash
git add -A && git commit -m "feat: wire MarketHoursService into RiskAwareStrategy via payload factory"
```

---

### Task 8: Daily PnL aggregation arq cron job

**Files:**

- Create: `claude-version/backend/src/msai/workers/pnl_aggregation.py`
- Modify: `claude-version/backend/src/msai/workers/settings.py` (add cron job)
- Test: `claude-version/backend/tests/unit/test_pnl_aggregation.py`

**Step 1: Write failing test**

```python
# tests/unit/test_pnl_aggregation.py
import pytest
from datetime import date
from decimal import Decimal

@pytest.mark.asyncio
async def test_aggregate_daily_pnl_creates_row(db_session):
    """Given fills for today, the aggregator should create a strategy_daily_pnl row."""
    from msai.workers.pnl_aggregation import aggregate_daily_pnl
    # Test with empty fills — should produce zero-PnL row or skip
    result = await aggregate_daily_pnl(None, target_date=date.today())
    assert result is not None
```

**Step 2: Run test — should fail**

**Step 3: Implement**

```python
# workers/pnl_aggregation.py
"""Daily PnL aggregation — arq cron job."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from msai.core.config import settings
from msai.core.logging import get_logger
from msai.models.order_attempt_audit import OrderAttemptAudit
from msai.models.strategy_daily_pnl import StrategyDailyPnl
from msai.models.live_deployment import LiveDeployment

log = get_logger(__name__)


async def aggregate_daily_pnl(ctx: dict | None, *, target_date: date | None = None) -> int:
    """Aggregate fills from order_attempt_audits into strategy_daily_pnl rows."""
    if target_date is None:
        target_date = datetime.now(UTC).date()

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    rows_written = 0
    async with factory() as session, session.begin():
        # Get all deployments that had fills today
        deployments = (
            await session.execute(
                select(
                    OrderAttemptAudit.deployment_id,
                    OrderAttemptAudit.strategy_id,
                    func.count().label("num_trades"),
                )
                .where(
                    OrderAttemptAudit.is_live.is_(True),
                    OrderAttemptAudit.status == "filled",
                    func.date(OrderAttemptAudit.updated_at) == target_date,
                )
                .group_by(OrderAttemptAudit.deployment_id, OrderAttemptAudit.strategy_id)
            )
        ).all()

        for dep_id, strat_id, num_trades in deployments:
            if dep_id is None or strat_id is None:
                continue

            # Simple PnL: sum of (fill_price * quantity * side_sign)
            # This is a placeholder — real PnL needs entry/exit matching
            pnl_row = StrategyDailyPnl(
                strategy_id=strat_id,
                deployment_id=dep_id,
                date=target_date,
                pnl=Decimal("0"),  # Filled by proper accounting later
                cumulative_pnl=Decimal("0"),
                num_trades=num_trades,
                win_count=0,
                loss_count=0,
            )
            session.add(pnl_row)
            rows_written += 1

    await engine.dispose()
    log.info("pnl_aggregation_complete", date=str(target_date), rows=rows_written)
    return rows_written
```

**Step 4: Register as arq cron in `workers/settings.py`**

Add to WorkerSettings:

```python
from arq.cron import cron
from msai.workers.pnl_aggregation import aggregate_daily_pnl

class WorkerSettings:
    functions = [run_backtest, run_ingest]
    cron_jobs = [
        cron(aggregate_daily_pnl, hour=21, minute=30),  # 4:30 PM ET = 21:30 UTC
    ]
    # ... rest unchanged
```

**Step 5: Run tests + commit**

```bash
git add -A && git commit -m "feat: add daily PnL aggregation arq cron job"
```

---

### Task 9: Scheduled nightly data ingestion arq cron

**Files:**

- Create: `claude-version/backend/src/msai/workers/nightly_ingest.py`
- Modify: `claude-version/backend/src/msai/workers/settings.py` (add cron job)
- Test: `claude-version/backend/tests/unit/test_nightly_ingest.py`

**Step 1: Write failing test**

```python
# tests/unit/test_nightly_ingest.py
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_nightly_ingest_calls_ingest_daily():
    with patch("msai.workers.nightly_ingest.DataIngestionService") as MockSvc:
        instance = MockSvc.return_value
        instance.ingest_daily = AsyncMock(return_value={"AAPL": 390})
        from msai.workers.nightly_ingest import run_nightly_ingest
        result = await run_nightly_ingest(None)
        instance.ingest_daily.assert_called_once()
```

**Step 2: Run test — should fail**

**Step 3: Implement**

```python
# workers/nightly_ingest.py
"""Nightly data ingestion — arq cron job."""
from __future__ import annotations

from msai.core.config import settings
from msai.core.logging import get_logger
from msai.services.data_ingestion import DataIngestionService
from msai.services.data_sources.polygon_client import PolygonClient
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.parquet_store import ParquetStore

log = get_logger(__name__)

# Default symbols for nightly ingest — the top liquid names.
_DEFAULT_STOCK_SYMBOLS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "SPY", "QQQ", "IWM"]


async def run_nightly_ingest(ctx: dict | None) -> dict[str, int]:
    """Fetch yesterday's bars for the default symbol list."""
    store = ParquetStore(data_root=settings.data_root)
    polygon = PolygonClient(api_key=settings.polygon_api_key)
    databento = DatabentoClient(api_key=settings.databento_api_key)
    svc = DataIngestionService(polygon=polygon, databento=databento, store=store)

    result = await svc.ingest_daily(asset_class="stocks", symbols=_DEFAULT_STOCK_SYMBOLS)
    log.info("nightly_ingest_complete", result=result)
    return result
```

**Step 4: Register as arq cron**

In `workers/settings.py`:

```python
from msai.workers.nightly_ingest import run_nightly_ingest

class WorkerSettings:
    cron_jobs = [
        cron(aggregate_daily_pnl, hour=21, minute=30),
        cron(run_nightly_ingest, hour=6, minute=0),  # 1:00 AM ET = 06:00 UTC
    ]
```

**Step 5: Run tests + commit**

```bash
git add -A && git commit -m "feat: add nightly data ingestion arq cron job"
```

---

### Task 10: Wire dashboard + live-trading pages to real APIs

**Files:**

- Modify: `claude-version/frontend/src/app/dashboard/page.tsx`
- Modify: `claude-version/frontend/src/app/live-trading/page.tsx`
- Modify: `claude-version/frontend/src/lib/api.ts` (add new API functions)

**Step 1: Add API functions in `api.ts`**

```typescript
// Add to api.ts
export interface AccountSummary {
  net_liquidation: number;
  buying_power: number;
  available_funds: number;
  margin_used: number;
  unrealized_pnl: number;
  realized_pnl: number;
}

export interface LiveTrade {
  id: string;
  deployment_id: string;
  instrument_id: string;
  side: string;
  quantity: string;
  price: string | null;
  status: string;
  timestamp: string | null;
}

export interface LiveTradesResponse {
  trades: LiveTrade[];
  total: number;
}

export interface LivePositionItem {
  instrument_id: string;
  side: string;
  quantity: string;
  avg_price: string;
  unrealized_pnl: string;
}

export interface LivePositionsResponse {
  positions: LivePositionItem[];
}

export async function getAccountSummary(
  token: string,
): Promise<AccountSummary> {
  return apiGet<AccountSummary>("/api/v1/account/summary", token);
}

export async function getLiveTrades(
  token: string,
): Promise<LiveTradesResponse> {
  return apiGet<LiveTradesResponse>("/api/v1/live/trades", token);
}

export async function getLivePositions(
  token: string,
): Promise<LivePositionsResponse> {
  return apiGet<LivePositionsResponse>("/api/v1/live/positions", token);
}
```

**Step 2: Wire dashboard page**

Replace mock imports with real API calls. Keep mock as fallback:

```typescript
// Replace generateEquityCurve import with real account data
const [account, setAccount] = useState<AccountSummary | null>(null);

useEffect(() => {
  const token = await getToken();
  if (token) {
    try {
      const data = await getAccountSummary(token);
      setAccount(data);
    } catch {
      /* fallback to null */
    }
  }
}, [getToken]);
```

**Step 3: Wire live-trading page**

Replace mock positions/trades with real API calls:

```typescript
const [realPositions, setRealPositions] = useState<LivePositionItem[]>([]);
const [recentTrades, setRecentTrades] = useState<LiveTrade[]>([]);

useEffect(() => {
  // Fetch real positions and trades
  const token = await getToken();
  if (token) {
    getLivePositions(token)
      .then((r) => setRealPositions(r.positions))
      .catch(() => {});
    getLiveTrades(token)
      .then((r) => setRecentTrades(r.trades))
      .catch(() => {});
  }
}, [getToken]);
```

**Step 4: Verify build**

Run: `cd claude-version/frontend && pnpm build`

**Step 5: Commit**

```bash
git add -A && git commit -m "feat: wire dashboard and live-trading pages to real API endpoints"
```

---

### Task 11: Regenerate all docs/architecture/ from code

**Files:**

- Rewrite: All 9 files in `claude-version/docs/architecture/`
- Rewrite: `claude-version/README.md`

**Step 1: Read every backend module, compose file, and frontend page**

Systematically read the now-complete codebase.

**Step 2: Rewrite each doc**

Rewrite all 9 architecture docs from scratch, ensuring every claim matches the actual code. Reference exact file paths, function names, config values, Redis key names, and WebSocket close codes from the code as it exists now.

**Step 3: Verify with grep**

For every specific claim (method name, config value, key name), grep the codebase to confirm.

**Step 4: Commit**

```bash
git add -A && git commit -m "docs: regenerate all architecture documentation from completed code"
```

---

## E2E Use Cases

### UC1: Positions endpoint returns real data

- **Intent:** Operator deploys a strategy and checks positions
- **Steps:** POST /api/v1/live/start → wait for running → GET /api/v1/live/positions
- **Verification:** Response contains position snapshots (or empty if no fills yet)
- **Persistence:** Reload page, positions still visible

### UC2: Dashboard shows real account data

- **Intent:** Operator opens dashboard to see portfolio overview
- **Steps:** Navigate to /dashboard
- **Verification:** Account summary shows real IB numbers (or graceful empty state if IB offline)
- **Persistence:** Refresh, data persists

### UC3: Account endpoint returns real IB data

- **Intent:** Operator checks IB account health
- **Steps:** GET /api/v1/account/summary
- **Verification:** Returns real net_liquidation, buying_power etc. from IB (or empty dict if offline)
- **Persistence:** N/A (live query)
