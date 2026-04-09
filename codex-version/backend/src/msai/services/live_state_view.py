from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_ACTIVE_RUNTIME_STATUSES = {"running", "starting", "liquidating", "blocked"}
_TERMINAL_STATUSES = {"error", "stopped", "unmanaged", "stale", "reconcile_required", "orphaned_exposure"}


def build_status_payload(
    rows: list[dict[str, Any]],
    strategy_name_by_id: Mapping[str, str],
    status_snapshots: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    runtime_by_id = {
        str(snapshot.get("scope")): snapshot.get("data")
        for snapshot in status_snapshots
        if snapshot.get("scope") and isinstance(snapshot.get("data"), Mapping)
    }
    payload: list[dict[str, Any]] = []
    for row in rows:
        deployment_id = str(row["id"])
        runtime = runtime_by_id.get(deployment_id, {})
        runtime_status = str(runtime.get("status")) if runtime.get("status") else None
        runtime_fresh = bool(row.get("runtime_fresh", False))
        status_value = str(row["status"])
        if runtime_fresh and runtime_status and status_value not in _TERMINAL_STATUSES:
            status_value = runtime_status
        runtime_authoritative = runtime_fresh and status_value in _ACTIVE_RUNTIME_STATUSES
        payload.append(
            {
                "id": deployment_id,
                "strategy": strategy_name_by_id.get(str(row["strategy_id"]), row["strategy_id"]),
                "status": status_value,
                "started_at": row["started_at"],
                "daily_pnl": _float_or_zero(runtime.get("daily_pnl")) if runtime_fresh else 0.0,
                "process_alive": row["process_alive"],
                "control_mode": row.get("control_mode"),
                "runtime_fresh": runtime_fresh,
                "paper_trading": row.get("paper_trading"),
                "open_positions": (
                    int(runtime.get("open_positions", 0) or 0)
                    if runtime_authoritative
                    else int(row.get("broker_open_positions", 0) or 0)
                ),
                "open_orders": (
                    int(runtime.get("open_orders", 0) or 0)
                    if runtime_authoritative
                    else int(row.get("broker_open_orders", 0) or 0)
                ),
                "updated_at": runtime.get("updated_at") if runtime_fresh else row.get("broker_updated_at"),
                "reason": runtime.get("reason") if runtime_fresh else row.get("reason"),
                "broker_connected": row.get("broker_connected"),
                "broker_mock_mode": row.get("broker_mock_mode"),
                "broker_updated_at": row.get("broker_updated_at"),
                "broker_open_positions": int(row.get("broker_open_positions", 0) or 0),
                "broker_open_orders": int(row.get("broker_open_orders", 0) or 0),
                "broker_exposure_detected": bool(row.get("broker_exposure_detected", False)),
            }
        )
    return payload


def build_positions_payload(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    active_deployments: set[str] | None = None,
    paper_trading: bool | None = None,
) -> list[dict[str, Any]]:
    rows = _flatten_snapshot_rows(
        snapshots,
        active_deployments=active_deployments,
        paper_trading=paper_trading,
    )
    rows.sort(key=lambda row: (str(row.get("instrument", "")), str(row.get("deployment_id", ""))))
    return rows


def build_orders_payload(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    active_deployments: set[str] | None = None,
    paper_trading: bool | None = None,
) -> list[dict[str, Any]]:
    rows = _flatten_snapshot_rows(
        snapshots,
        active_deployments=active_deployments,
        paper_trading=paper_trading,
    )
    rows.sort(key=lambda row: str(row.get("ts_last", "")), reverse=True)
    return rows


def build_trades_payload(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    active_deployments: set[str] | None = None,
    paper_trading: bool | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = _flatten_snapshot_rows(
        snapshots,
        active_deployments=active_deployments,
        paper_trading=paper_trading,
    )
    rows.sort(key=lambda row: str(row.get("executed_at", "")), reverse=True)
    return rows[:limit]


def build_risk_payload(
    halt_state: Mapping[str, Any],
    snapshots: Iterable[Mapping[str, Any]],
    *,
    active_deployments: set[str] | None = None,
) -> dict[str, Any]:
    metrics = {
        "current_pnl": 0.0,
        "notional_exposure": 0.0,
        "portfolio_value": 0.0,
        "margin_used": 0.0,
        "position_count": 0,
    }
    updated_at = halt_state.get("updated_at")

    for snapshot in snapshots:
        scope = snapshot.get("scope")
        if active_deployments is not None and scope not in active_deployments:
            continue
        data = snapshot.get("data")
        if not isinstance(data, Mapping):
            continue
        metrics["current_pnl"] += _float_or_zero(data.get("current_pnl"))
        metrics["notional_exposure"] += _float_or_zero(data.get("notional_exposure"))
        metrics["portfolio_value"] += _float_or_zero(data.get("portfolio_value"))
        metrics["margin_used"] += _float_or_zero(data.get("margin_used"))
        metrics["position_count"] += int(data.get("position_count", 0) or 0)
        generated_at = snapshot.get("generated_at")
        if isinstance(generated_at, str) and (updated_at is None or generated_at > str(updated_at)):
            updated_at = generated_at

    return {
        "halted": bool(halt_state.get("halted", False)),
        "reason": halt_state.get("reason"),
        "updated_at": updated_at,
        **metrics,
    }


def _flatten_snapshot_rows(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    active_deployments: set[str] | None,
    paper_trading: bool | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        scope = snapshot.get("scope")
        if active_deployments is not None and scope not in active_deployments:
            continue
        data = snapshot.get("data")
        if not isinstance(data, list):
            continue
        for row in data:
            if not isinstance(row, Mapping):
                continue
            if paper_trading is not None and row.get("paper_trading") is not paper_trading:
                continue
            rows.append(dict(row))
    return rows


def _float_or_zero(value: object | None) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        text = str(value).strip().replace(",", "")
        if not text:
            return 0.0
        match = re.match(r"^([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", text)
        if match is None:
            return 0.0
        return float(match.group(1))
