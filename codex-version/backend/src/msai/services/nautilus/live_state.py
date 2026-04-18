from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Coroutine
from concurrent.futures import CancelledError as ConcurrentCancelledError
from concurrent.futures import Future as ConcurrentFuture
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models import LiveOrderEvent, Trade
from msai.services.live_updates import (
    publish_live_snapshot,
    publish_live_snapshot_sync,
    publish_live_update,
    publish_live_update_sync,
)
from msai.services.risk_engine import RiskEngine, RiskMetrics

try:
    from nautilus_trader.core.datetime import unix_nanos_to_dt
    from nautilus_trader.live.config import ControllerConfig
    from nautilus_trader.model.enums import PriceType
    from nautilus_trader.model.events.order import OrderEvent, OrderFilled
    from nautilus_trader.model.identifiers import AccountId, StrategyId
    from nautilus_trader.trading.controller import Controller

    _NAUTILUS_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - import depends on environment
    _NAUTILUS_IMPORT_ERROR = exc

logger = get_logger("nautilus.live_state")


class LiveStateControllerConfig(ControllerConfig, kw_only=True, frozen=True):
    deployment_id: str
    strategy_db_id: str
    strategy_name: str
    strategy_code_hash: str
    strategy_id_full: str | None = None
    portfolio_revision_id: str | None = None
    strategy_members: list[dict[str, Any]] | None = None
    instrument_ids: tuple[str, ...]
    startup_instrument_id: str
    startup_quantity: float = 1.0
    account_id: str | None = None
    paper_trading: bool = True
    snapshot_interval_secs: float = 5.0
    liquidation_topic: str | None = None


class LiveStateController(Controller):
    def __init__(self, trader, config: LiveStateControllerConfig | None = None) -> None:
        if _NAUTILUS_IMPORT_ERROR is not None:  # pragma: no cover - environment dependent
            raise RuntimeError(f"Nautilus live-state imports unavailable: {_NAUTILUS_IMPORT_ERROR}")
        if config is None:
            config = LiveStateControllerConfig(
                deployment_id="unknown",
                strategy_db_id="unknown",
                strategy_name="unknown",
                strategy_code_hash="unknown",
                strategy_id_full=None,
                portfolio_revision_id=None,
                strategy_members=[],
                instrument_ids=(),
                startup_instrument_id="unknown",
            )
        super().__init__(trader=trader, config=config)
        self._snapshot_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._threadsafe_futures: set[ConcurrentFuture[None]] = set()
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._timer_name = f"live-state:{config.deployment_id}"
        self._risk_engine = RiskEngine()
        self._liquidation_topic = str(config.liquidation_topic or "").strip()
        self._liquidation_requested_at: datetime | None = None
        self._liquidation_reason: str | None = None
        self._shutdown_after_liquidation = False
        self._account_refresh_grace_until: datetime | None = None
        self._shutdown_requested = False
        self._nautilus_strategy_id: StrategyId | None = None
        self._order_event_topics: set[str] = set()

    def on_start(self) -> None:
        if self._liquidation_topic:
            self.msgbus.subscribe(
                topic=self._liquidation_topic,
                handler=self._handle_liquidation_message,
            )
        self._nautilus_strategy_id = self._resolve_live_strategy_id()
        self._ensure_order_event_subscription()

        startup_snapshot = self._build_runtime_snapshot(
            reason="startup_validated",
            status="running",
        )
        decision = self._risk_engine.validate_start_sync(
            strategy=self.config.strategy_name,
            instrument=self.config.startup_instrument_id,
            quantity=self.config.startup_quantity,
            metrics=RiskMetrics(
                current_pnl=float(startup_snapshot["risk"]["current_pnl"]),
                portfolio_value=float(startup_snapshot["risk"]["portfolio_value"]),
                notional_exposure=float(startup_snapshot["risk"]["notional_exposure"]),
                margin_used=float(startup_snapshot["risk"]["margin_used"]),
            ),
            paper_trading=self.config.paper_trading,
        )
        if not decision.allowed:
            blocked_snapshot = self._build_runtime_snapshot(
                reason=decision.reason,
                status="blocked",
            )
            self._publish_runtime_snapshot_sync(blocked_snapshot)
            raise RuntimeError(f"Live startup blocked by risk policy: {decision.reason}")

        self._publish_runtime_snapshot_sync(startup_snapshot)
        self.clock.set_timer(
            name=self._timer_name,
            interval=timedelta(seconds=max(self.config.snapshot_interval_secs, 1.0)),
            callback=self._on_snapshot_timer,
            fire_immediately=True,
        )
        self._schedule_task(self._publish_runtime_state(reason="controller_started"), "snapshot-start")

    def on_stop(self) -> None:
        if self._timer_name in self.clock.timer_names:
            self.clock.cancel_timer(self._timer_name)
        for task in list(self._tasks):
            task.cancel()
        for future in list(self._threadsafe_futures):
            future.cancel()
        if self._liquidation_topic:
            with suppress(Exception):
                self.msgbus.unsubscribe(
                    topic=self._liquidation_topic,
                    handler=self._handle_liquidation_message,
                )
        for topic in list(self._order_event_topics):
            with suppress(Exception):
                self.msgbus.unsubscribe(
                    topic=topic,
                    handler=self._handle_order_event_message,
                )
        self._order_event_topics.clear()
        stopped_snapshot = self._build_terminal_snapshot(
            reason="liquidation_complete" if self._shutdown_requested else "controller_stopped",
            status="stopped",
        )
        self._publish_runtime_snapshot_sync(stopped_snapshot)

    def _on_snapshot_timer(self, _event: Any) -> None:
        self._schedule_task(self._publish_runtime_state(reason="timer"), "snapshot-timer")

    def _handle_liquidation_message(self, payload: Any) -> None:
        self._schedule_task(self._handle_liquidation_command(payload), "liquidation")

    def _handle_order_event_message(self, event: Any) -> None:
        if not isinstance(event, OrderEvent):
            logger.warning(
                "live_state_invalid_order_event",
                deployment_id=self.config.deployment_id,
                payload_type=type(event).__name__,
            )
            return
        self._schedule_task(self._handle_order_event(event), "order-event")

    async def _handle_order_event(self, event: OrderEvent) -> None:
        member = self._member_metadata(_identifier_value(getattr(event, "strategy_id", None)))
        try:
            payload = _order_event_payload(
                event,
                deployment_id=self.config.deployment_id,
                strategy_db_id=str(member["strategy_id"]),
                strategy_id_full=(
                    str(member["strategy_id_full"]) if member.get("strategy_id_full") else None
                ),
                strategy_code_hash=str(member["strategy_code_hash"]),
                paper_trading=self.config.paper_trading,
            )
        except ValueError as exc:
            logger.warning(
                "live_state_order_event_payload_failed",
                deployment_id=self.config.deployment_id,
                event_type=type(event).__name__,
                error=str(exc),
            )
            return
        try:
            await self._persist_order_event(payload)
        except Exception as exc:
            logger.warning(
                "live_state_order_event_persist_failed",
                deployment_id=self.config.deployment_id,
                event_type=payload["event_type"],
                error=str(exc),
            )
        try:
            await publish_live_update(
                _order_event_update_type(payload["event_type"]),
                payload,
                scope=self.config.deployment_id,
            )
        except Exception as exc:
            logger.warning(
                "live_state_order_event_publish_failed",
                deployment_id=self.config.deployment_id,
                event_type=payload["event_type"],
                error=str(exc),
            )

        if isinstance(event, OrderFilled):
            trade_payload = self._fill_payload(event)
            try:
                await self._persist_trade_fill(trade_payload)
            except Exception as exc:
                logger.warning(
                    "live_state_trade_fill_persist_failed",
                    deployment_id=self.config.deployment_id,
                    broker_trade_id=trade_payload["broker_trade_id"],
                    error=str(exc),
                )
            try:
                await publish_live_update("trade.filled", trade_payload, scope=self.config.deployment_id)
            except Exception as exc:
                logger.warning(
                    "live_state_trade_fill_publish_failed",
                    deployment_id=self.config.deployment_id,
                    broker_trade_id=trade_payload["broker_trade_id"],
                    error=str(exc),
                )

        await self._publish_runtime_state(reason=f"order_event:{payload['event_type']}")

    async def _handle_liquidation_command(self, raw_payload: Any) -> None:
        try:
            command = _decode_liquidation_command(raw_payload)
        except ValueError as exc:
            logger.warning(
                "live_state_invalid_liquidation_command",
                deployment_id=self.config.deployment_id,
                error=str(exc),
            )
            return

        self._liquidation_requested_at = self._liquidation_requested_at or datetime.now(UTC)
        self._liquidation_reason = str(command["reason"])
        self._shutdown_after_liquidation = bool(command["shutdown_after_flat"]) or self._shutdown_after_liquidation
        self._account_refresh_grace_until = None

        self._ensure_order_event_subscription()
        strategy_id = self._resolve_live_strategy_id()
        if strategy_id is None:
            self._publish_runtime_snapshot_sync(
                self._build_runtime_snapshot(
                    reason="liquidation_failed_missing_strategy",
                    status="error",
                )
            )
            return

        try:
            self.market_exit_strategy_from_id(strategy_id)
        except Exception as exc:
            logger.warning(
                "live_state_market_exit_failed",
                deployment_id=self.config.deployment_id,
                error=str(exc),
            )
            self._publish_runtime_snapshot_sync(
                self._build_runtime_snapshot(
                    reason=f"liquidation_failed: {exc}",
                    status="error",
                )
            )
            return

        await self._publish_runtime_state(reason=self._liquidation_reason or "liquidation_requested")

    async def _publish_runtime_state(self, *, reason: str) -> None:
        self._ensure_order_event_subscription()
        async with self._snapshot_lock:
            snapshot = self._build_runtime_snapshot(
                reason=reason,
                status=self._runtime_status(),
            )
            scope = self.config.deployment_id
            await publish_live_snapshot("positions", snapshot["positions"], scope=scope)
            await publish_live_snapshot("orders", snapshot["orders"], scope=scope)
            await publish_live_snapshot("trades", snapshot["trades"], scope=scope)
            await publish_live_snapshot("risk", snapshot["risk"], scope=scope)
            await publish_live_snapshot("status", snapshot["status"], scope=scope)
        await self._advance_liquidation(snapshot)

    async def _persist_order_event(self, payload: dict[str, Any]) -> None:
        event_id = str(payload["event_id"])
        async with async_session_factory() as session:
            existing = (
                await session.execute(
                    select(LiveOrderEvent).where(
                        LiveOrderEvent.deployment_id == self.config.deployment_id,
                        LiveOrderEvent.event_id == event_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return

            session.add(
                LiveOrderEvent(
                    deployment_id=self.config.deployment_id,
                    strategy_id=str(payload["strategy_id"]),
                    strategy_id_full=str(payload["strategy_id_full"]) if payload.get("strategy_id_full") else None,
                    strategy_code_hash=str(payload["strategy_code_hash"]),
                    paper_trading=self.config.paper_trading,
                    event_id=event_id,
                    event_type=str(payload["event_type"]),
                    instrument=str(payload["instrument"]) if payload.get("instrument") else None,
                    client_order_id=(
                        str(payload["client_order_id"]) if payload.get("client_order_id") else None
                    ),
                    venue_order_id=str(payload["venue_order_id"]) if payload.get("venue_order_id") else None,
                    broker_account_id=str(payload["account_id"]) if payload.get("account_id") else None,
                    reason=str(payload["reason"]) if payload.get("reason") else None,
                    payload=_json_safe(payload["payload"]),
                    ts_event=datetime.fromisoformat(str(payload["executed_at"])),
                )
            )
            await session.commit()

    async def _persist_trade_fill(self, payload: dict[str, Any]) -> None:
        broker_trade_id = str(payload["broker_trade_id"])
        async with async_session_factory() as session:
            existing = (
                await session.execute(
                    select(Trade).where(
                        Trade.deployment_id == self.config.deployment_id,
                        Trade.broker_trade_id == broker_trade_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return

            session.add(
                Trade(
                    deployment_id=self.config.deployment_id,
                    strategy_id=str(payload["strategy_id"]),
                    strategy_id_full=str(payload["strategy_id_full"]) if payload.get("strategy_id_full") else None,
                    strategy_code_hash=str(payload["strategy_code_hash"]),
                    instrument=str(payload["instrument"]),
                    side=str(payload["side"]),
                    quantity=float(payload["quantity"]),
                    price=float(payload["price"]),
                    commission=float(payload["commission"]) if payload["commission"] is not None else None,
                    pnl=float(payload["pnl"]) if payload["pnl"] is not None else None,
                    is_live=True,
                    executed_at=datetime.fromisoformat(str(payload["executed_at"])),
                    broker_trade_id=broker_trade_id,
                    client_order_id=str(payload["client_order_id"]) if payload["client_order_id"] else None,
                    venue_order_id=str(payload["venue_order_id"]) if payload["venue_order_id"] else None,
                    position_id=str(payload["position_id"]) if payload["position_id"] else None,
                    broker_account_id=str(payload["account_id"]) if payload["account_id"] else None,
                )
            )
            await session.commit()

    def _configured_members(self) -> list[dict[str, Any]]:
        if self.config.strategy_members:
            return [dict(member) for member in self.config.strategy_members]
        return [
            {
                "strategy_id": self.config.strategy_db_id,
                "strategy_name": self.config.strategy_name,
                "strategy_code_hash": self.config.strategy_code_hash,
                "strategy_id_full": self.config.strategy_id_full,
                "instrument_ids": list(self.config.instrument_ids),
                "order_index": 0,
            }
        ]

    def _member_metadata(self, strategy_id_full: str | None = None) -> dict[str, Any]:
        members = self._configured_members()
        if strategy_id_full:
            for member in members:
                if str(member.get("strategy_id_full")) == strategy_id_full:
                    return member
        if len(members) == 1:
            return members[0]
        return {
            "strategy_id": self.config.strategy_db_id,
            "strategy_name": self.config.strategy_name,
            "strategy_code_hash": self.config.strategy_code_hash,
            "strategy_id_full": strategy_id_full or self.config.strategy_id_full,
            "instrument_ids": list(self.config.instrument_ids),
            "order_index": 0,
        }

    def _status_members(self) -> list[dict[str, Any]]:
        members = self._configured_members()
        if not self.config.portfolio_revision_id and len(members) <= 1:
            return []
        return [
            {
                "strategy_id": str(member.get("strategy_id")),
                "strategy_name": str(member.get("strategy_name")),
                "strategy_id_full": str(member.get("strategy_id_full")) if member.get("strategy_id_full") else None,
                "order_index": int(member.get("order_index", 0)),
                "instrument_ids": list(member.get("instrument_ids") or []),
            }
            for member in members
        ]

    def _position_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for position in self.cache.positions_open():
            base = position.to_dict()
            member = self._member_metadata(_identifier_value(base.get("strategy_id")))
            instrument_id = position.instrument_id
            account_id = position.account_id
            position_id = str(base["position_id"])
            if position_id in seen:
                continue
            seen.add(position_id)

            price = self.cache.price(instrument_id, PriceType.LAST)
            unrealized = self.portfolio.unrealized_pnl(instrument_id, account_id=account_id)
            exposure = self.portfolio.net_exposure(instrument_id, account_id=account_id)

            rows.append(
                {
                    "deployment_id": self.config.deployment_id,
                    "strategy_id": str(member["strategy_id"]),
                    "strategy_name": str(member["strategy_name"]),
                    "strategy_id_full": (
                        str(member["strategy_id_full"]) if member.get("strategy_id_full") else None
                    ),
                    "paper_trading": self.config.paper_trading,
                    "position_id": position_id,
                    "instrument": str(base["instrument_id"]),
                    "side": str(base["side"]),
                    "quantity": _to_float(base["quantity"]),
                    "avg_price": _to_float(base["avg_px_open"]),
                    "current_price": float(price) if price is not None else None,
                    "unrealized_pnl": float(unrealized) if unrealized is not None else 0.0,
                    "realized_pnl": _to_float(base["realized_pnl"]),
                    "market_value": float(exposure) if exposure is not None else 0.0,
                    "opened_at": _nanos_to_iso(base["ts_opened"]),
                    "updated_at": _nanos_to_iso(base["ts_last"]),
                }
            )

        rows.sort(key=lambda row: str(row["instrument"]))
        return rows

    def _order_rows(self) -> list[dict[str, Any]]:
        orders_df = self._trader.generate_orders_report()
        if orders_df.empty:
            return []

        rows = _frame_records(orders_df)
        for row in rows:
            member = self._member_metadata(_identifier_value(row.get("strategy_id")))
            row["deployment_id"] = self.config.deployment_id
            row["strategy_id"] = str(member["strategy_id"])
            row["strategy_name"] = str(member["strategy_name"])
            row["strategy_id_full"] = (
                str(member["strategy_id_full"]) if member.get("strategy_id_full") else None
            )
            row["paper_trading"] = self.config.paper_trading
            row["instrument"] = row.get("instrument_id")
        return rows

    def _trade_rows(self) -> list[dict[str, Any]]:
        fills_df = self._trader.generate_fills_report()
        if fills_df.empty:
            return []

        rows = _frame_records(fills_df)
        payload = []
        for row in rows:
            member = self._member_metadata(_identifier_value(row.get("strategy_id")))
            payload.append(
                {
                    "deployment_id": self.config.deployment_id,
                    "strategy_id": str(member["strategy_id"]),
                    "strategy_name": str(member["strategy_name"]),
                    "strategy_id_full": (
                        str(member["strategy_id_full"]) if member.get("strategy_id_full") else None
                    ),
                    "paper_trading": self.config.paper_trading,
                    "id": row.get("event_id") or row.get("trade_id"),
                    "executed_at": row.get("ts_event"),
                    "instrument": row.get("instrument_id"),
                    "side": row.get("order_side"),
                    "quantity": _to_float(row.get("last_qty")),
                    "price": _to_float(row.get("last_px")),
                    "commission": _to_float(row.get("commission")),
                    "pnl": 0.0,
                    "broker_trade_id": row.get("trade_id"),
                    "client_order_id": row.get("client_order_id"),
                    "venue_order_id": row.get("venue_order_id"),
                    "position_id": row.get("position_id"),
                    "account_id": row.get("account_id"),
                    "reconciliation": bool(row.get("reconciliation", False)),
                }
            )
        payload.sort(key=lambda row: str(row.get("executed_at", "")), reverse=True)
        return payload

    def _risk_payload(self, positions: list[dict[str, Any]]) -> dict[str, Any]:
        current_pnl = sum(float(row.get("unrealized_pnl") or 0.0) for row in positions)
        notional_exposure = sum(abs(float(row.get("market_value") or 0.0)) for row in positions)

        portfolio_value = 0.0
        margin_used = 0.0
        currencies: set[str] = set()
        for account in self.cache.accounts():
            events = getattr(account, "events", [])
            if not events:
                continue
            state = events[-1]
            for balance in getattr(state, "balances", []):
                portfolio_value += float(balance.total)
                currencies.add(balance.currency.code)
            for margin in getattr(state, "margins", []):
                margin_used += float(margin.maintenance)
                currencies.add(margin.currency.code)

        return {
            "deployment_id": self.config.deployment_id,
            "strategy_id": self.config.strategy_db_id,
            "paper_trading": self.config.paper_trading,
            "current_pnl": round(current_pnl, 8),
            "notional_exposure": round(notional_exposure, 8),
            "portfolio_value": round(portfolio_value, 8),
            "margin_used": round(margin_used, 8),
            "position_count": len(positions),
            "currencies": sorted(currencies),
            "updated_at": datetime.now(UTC).isoformat(),
            "portfolio_revision_id": self.config.portfolio_revision_id,
            "account_id": self.config.account_id,
        }

    def _build_runtime_snapshot(
        self,
        *,
        reason: str,
        status: str,
    ) -> dict[str, Any]:
        positions = self._position_rows()
        orders = self._order_rows()
        trades = self._trade_rows()
        risk = self._risk_payload(positions)
        live_strategy_id = self._resolve_live_strategy_id()
        return {
            "positions": positions,
            "orders": orders,
            "trades": trades,
            "risk": risk,
            "status": {
                "deployment_id": self.config.deployment_id,
                "strategy_id": self.config.strategy_db_id,
                "paper_trading": self.config.paper_trading,
                "status": status,
                "daily_pnl": risk["current_pnl"],
                "notional_exposure": risk["notional_exposure"],
                "margin_used": risk["margin_used"],
                "portfolio_value": risk["portfolio_value"],
                "open_positions": len(positions),
                "open_orders": len([row for row in orders if not row.get("is_closed", False)]),
                "updated_at": datetime.now(UTC).isoformat(),
                "reason": reason,
                "nautilus_strategy_id": str(live_strategy_id) if live_strategy_id is not None else None,
                "liquidation_requested_at": (
                    self._liquidation_requested_at.isoformat() if self._liquidation_requested_at is not None else None
                ),
                "portfolio_revision_id": self.config.portfolio_revision_id,
                "account_id": self.config.account_id,
                "members": self._status_members(),
            },
        }

    def _build_terminal_snapshot(
        self,
        *,
        reason: str,
        status: str,
    ) -> dict[str, Any]:
        trades = self._trade_rows()
        risk = self._risk_payload([])
        live_strategy_id = self._resolve_live_strategy_id()
        return {
            "positions": [],
            "orders": [],
            "trades": trades,
            "risk": risk,
            "status": {
                "deployment_id": self.config.deployment_id,
                "strategy_id": self.config.strategy_db_id,
                "paper_trading": self.config.paper_trading,
                "status": status,
                "daily_pnl": risk["current_pnl"],
                "notional_exposure": risk["notional_exposure"],
                "margin_used": risk["margin_used"],
                "portfolio_value": risk["portfolio_value"],
                "open_positions": 0,
                "open_orders": 0,
                "updated_at": datetime.now(UTC).isoformat(),
                "reason": reason,
                "nautilus_strategy_id": str(live_strategy_id) if live_strategy_id is not None else None,
                "liquidation_requested_at": (
                    self._liquidation_requested_at.isoformat() if self._liquidation_requested_at is not None else None
                ),
                "portfolio_revision_id": self.config.portfolio_revision_id,
                "account_id": self.config.account_id,
                "members": self._status_members(),
            },
        }

    def _publish_runtime_snapshot_sync(self, snapshot: dict[str, Any]) -> None:
        scope = self.config.deployment_id
        publish_live_snapshot_sync("positions", snapshot["positions"], scope=scope)
        publish_live_snapshot_sync("orders", snapshot["orders"], scope=scope)
        publish_live_snapshot_sync("trades", snapshot["trades"], scope=scope)
        publish_live_snapshot_sync("risk", snapshot["risk"], scope=scope)
        publish_live_snapshot_sync("status", snapshot["status"], scope=scope)
        publish_live_update_sync(
            f"deployment.{snapshot['status']['status']}",
            snapshot["status"],
            scope=scope,
        )

    def _fill_payload(self, event: OrderFilled) -> dict[str, Any]:
        executed_at = unix_nanos_to_dt(event.ts_event).replace(tzinfo=UTC).isoformat()
        member = self._member_metadata(_identifier_value(getattr(event, "strategy_id", None)))
        return {
            "deployment_id": self.config.deployment_id,
            "strategy_id": str(member["strategy_id"]),
            "strategy_name": str(member["strategy_name"]),
            "strategy_id_full": (
                str(member["strategy_id_full"]) if member.get("strategy_id_full") else None
            ),
            "strategy_code_hash": str(member["strategy_code_hash"]),
            "paper_trading": self.config.paper_trading,
            "id": event.id.value,
            "executed_at": executed_at,
            "instrument": event.instrument_id.value,
            "side": str(event.order_side).split(".")[-1],
            "quantity": float(event.last_qty),
            "price": float(event.last_px),
            "commission": float(event.commission),
            "pnl": 0.0,
            "broker_trade_id": event.trade_id.value,
            "client_order_id": event.client_order_id.value,
            "venue_order_id": event.venue_order_id.value if event.venue_order_id else None,
            "position_id": event.position_id.value if event.position_id else None,
            "account_id": event.account_id.value if event.account_id else None,
            "reconciliation": bool(event.reconciliation),
        }

    def _schedule_task(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            self._event_loop = loop
        except RuntimeError:
            loop = self._event_loop

        if loop is None:
            logger.warning("live_state_no_running_loop", deployment_id=self.config.deployment_id, task=name)
            coro.close()
            return

        if loop.is_running():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                task = loop.create_task(coro, name=f"{self.config.deployment_id}:{name}")
                self._tasks.add(task)
                task.add_done_callback(self._finalize_task)
                return

        future = asyncio.run_coroutine_threadsafe(coro, loop)
        self._threadsafe_futures.add(future)
        future.add_done_callback(self._finalize_threadsafe_future)

    def _finalize_task(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.warning(
                "live_state_task_failed",
                deployment_id=self.config.deployment_id,
                error=str(exc),
            )

    def _finalize_threadsafe_future(self, future: ConcurrentFuture[None]) -> None:
        self._threadsafe_futures.discard(future)
        try:
            exc = future.exception()
        except ConcurrentCancelledError:
            return
        if exc is not None:
            logger.warning(
                "live_state_task_failed",
                deployment_id=self.config.deployment_id,
                error=str(exc),
            )

    def _runtime_status(self) -> str:
        if self._liquidation_requested_at is not None:
            return "liquidating"
        return "running"

    def _resolve_live_strategy_ids(self) -> tuple[StrategyId, ...]:
        strategy_ids = tuple(self._trader.strategy_ids())
        if strategy_ids and self._nautilus_strategy_id is None:
            self._nautilus_strategy_id = strategy_ids[0]
        return strategy_ids

    def _resolve_live_strategy_id(self) -> StrategyId | None:
        if self._nautilus_strategy_id is not None:
            return self._nautilus_strategy_id
        strategy_ids = self._resolve_live_strategy_ids()
        if not strategy_ids:
            return None
        return self._nautilus_strategy_id

    def _ensure_order_event_subscription(self) -> None:
        desired_topics = {f"events.order.{strategy_id}" for strategy_id in self._resolve_live_strategy_ids()}
        for topic in sorted(self._order_event_topics - desired_topics):
            with suppress(Exception):
                self.msgbus.unsubscribe(
                    topic=topic,
                    handler=self._handle_order_event_message,
                )
            self._order_event_topics.discard(topic)
        for topic in sorted(desired_topics - self._order_event_topics):
            self.msgbus.subscribe(
                topic=topic,
                handler=self._handle_order_event_message,
            )
            self._order_event_topics.add(topic)

    async def _advance_liquidation(self, snapshot: dict[str, Any]) -> None:
        if self._liquidation_requested_at is None or self._shutdown_requested:
            return
        if not _snapshot_is_flat(snapshot):
            return

        now = datetime.now(UTC)
        if self._shutdown_after_liquidation:
            if self._account_refresh_grace_until is None and self._request_account_refresh():
                self._account_refresh_grace_until = now + timedelta(
                    seconds=max(self.config.snapshot_interval_secs, 1.0)
                )
                await self._publish_runtime_state(reason="liquidation_refreshing_account")
                return
            if self._account_refresh_grace_until is not None and now < self._account_refresh_grace_until:
                return

            self._shutdown_requested = True
            self.shutdown_system(
                reason=self._liquidation_reason or "liquidation completed; shutting down deployment",
            )

    def _request_account_refresh(self) -> bool:
        if not self.config.account_id:
            return False

        strategy_id = self._resolve_live_strategy_id()
        if strategy_id is None:
            return False

        strategies = getattr(self._trader, "_strategies", {})
        strategy = strategies.get(strategy_id)
        if strategy is None:
            return False

        try:
            strategy.query_account(AccountId(self.config.account_id))
        except Exception as exc:
            logger.warning(
                "live_state_account_refresh_failed",
                deployment_id=self.config.deployment_id,
                error=str(exc),
            )
            return False
        return True


def _frame_records(frame: Any) -> list[dict[str, Any]]:
    return json.loads(frame.reset_index().to_json(orient="records", date_format="iso"))


def _decode_liquidation_command(raw_payload: Any) -> dict[str, Any]:
    if isinstance(raw_payload, dict):
        payload = dict(raw_payload)
    elif isinstance(raw_payload, str):
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise ValueError("Liquidation payload is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Liquidation payload must decode to an object")
    else:
        raise ValueError(f"Liquidation payload must be a dict or JSON string, got {type(raw_payload).__name__}")

    action = str(payload.get("action") or "").strip().lower()
    if action != "liquidate":
        raise ValueError(f"Unsupported liquidation action: {action or 'missing'}")

    return {
        "action": action,
        "reason": str(payload.get("reason") or "liquidation requested"),
        "shutdown_after_flat": bool(payload.get("shutdown_after_flat", True)),
    }


def _snapshot_is_flat(snapshot: dict[str, Any]) -> bool:
    positions = snapshot.get("positions")
    orders = snapshot.get("orders")
    if not isinstance(positions, list) or not isinstance(orders, list):
        return False
    open_orders = len([row for row in orders if isinstance(row, dict) and not row.get("is_closed", False)])
    return len(positions) == 0 and open_orders == 0


def _order_event_payload(
    event: OrderEvent,
    *,
    deployment_id: str,
    strategy_db_id: str,
    strategy_id_full: str | None = None,
    strategy_code_hash: str,
    paper_trading: bool,
) -> dict[str, Any]:
    raw_payload = _json_safe(_raw_order_event_payload(event))
    executed_at = _nanos_to_iso(getattr(event, "ts_event", None))
    if executed_at is None:
        executed_at = datetime.now(UTC).isoformat()

    event_id = str(raw_payload.get("event_id") or _identifier_value(getattr(event, "id", None)) or "")
    if not event_id:
        raise ValueError(f"Order event {type(event).__name__} is missing an event_id")

    return {
        "deployment_id": deployment_id,
        "strategy_id": strategy_db_id,
        "strategy_id_full": strategy_id_full,
        "strategy_code_hash": strategy_code_hash,
        "paper_trading": paper_trading,
        "event_id": event_id,
        "event_type": type(event).__name__,
        "executed_at": executed_at,
        "instrument": raw_payload.get("instrument_id") or _identifier_value(getattr(event, "instrument_id", None)),
        "client_order_id": raw_payload.get("client_order_id")
        or _identifier_value(getattr(event, "client_order_id", None)),
        "venue_order_id": raw_payload.get("venue_order_id")
        or _identifier_value(getattr(event, "venue_order_id", None)),
        "account_id": raw_payload.get("account_id") or _identifier_value(getattr(event, "account_id", None)),
        "reason": raw_payload.get("reason"),
        "payload": raw_payload,
    }


def _raw_order_event_payload(event: OrderEvent) -> dict[str, Any]:
    serializer = getattr(type(event), "to_dict", None)
    if callable(serializer):
        payload = serializer(event)
        if isinstance(payload, dict):
            return dict(payload)
    return {}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC).isoformat()
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]

    nested = getattr(value, "value", None)
    if nested is not None and nested is not value:
        return _json_safe(nested)

    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _order_event_update_type(event_type: str) -> str:
    trimmed = event_type.removeprefix("Order")
    return f"order.{_camel_to_snake(trimmed)}"


def _identifier_value(value: object | None) -> str | None:
    if value is None:
        return None
    nested = getattr(value, "value", None)
    if nested is not None:
        return str(nested)
    return str(value)


def _camel_to_snake(value: str) -> str:
    if not value:
        return ""

    parts: list[str] = []
    for index, char in enumerate(value):
        if char.isupper() and index > 0:
            parts.append("_")
        parts.append(char.lower())
    return "".join(parts)


def _nanos_to_iso(value: object | None) -> str | None:
    if value in (None, 0):
        return None
    return unix_nanos_to_dt(int(value)).replace(tzinfo=UTC).isoformat()


def _to_float(value: object | None) -> float | None:
    if value in (None, ""):
        return None
    nested = getattr(value, "value", None)
    if nested is not None and nested is not value:
        return _to_float(nested)
    try:
        return float(value)
    except (TypeError, ValueError):
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        match = re.match(r"^([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", text)
        if match is None:
            return None
        return float(match.group(1))
