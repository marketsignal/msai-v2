"""Probe IB market data entitlements via ib_async.

Connects to ib-gateway and tests subscription state across asset classes
by calling reqMktData and watching for ticks vs error 354/162.

Usage (from backend/ container):
    docker exec -e PYTHONPATH=/app/src msai-claude-backend \
        uv run python /app/scripts/probe_market_data.py

Or locally:
    cd backend && uv run python ../scripts/probe_market_data.py
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

from ib_async import IB, Stock, Future, Option, Forex, Contract


HOST = os.environ.get("IB_GATEWAY_HOST", "localhost")
PORT = int(os.environ.get("IB_GATEWAY_PORT_LIVE", "4003"))
CLIENT_ID = 999  # avoid colliding with workers


@dataclass
class ProbeResult:
    name: str
    venue: str
    contract: Contract
    errors: list[tuple[int, str]] = field(default_factory=list)
    ticks_seen: int = 0
    last_price: float | None = None


def build_contracts() -> list[tuple[str, str, Contract]]:
    return [
        ("AAPL", "NASDAQ", Stock("AAPL", "SMART", "USD", primaryExchange="NASDAQ")),
        ("MSFT", "NASDAQ", Stock("MSFT", "SMART", "USD", primaryExchange="NASDAQ")),
        ("SPY", "ARCA",   Stock("SPY",  "SMART", "USD", primaryExchange="ARCA")),
        ("ES jun26", "CME", Future("ES", "202606", "CME")),
        ("NQ jun26", "CME", Future("NQ", "202606", "CME")),
        ("EUR/USD", "IDEALPRO", Forex("EURUSD")),
    ]


async def probe() -> None:
    ib = IB()
    print(f"Connecting to {HOST}:{PORT} client_id={CLIENT_ID} ...")
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    print(f"Connected. Server version: {ib.client.serverVersion()}")
    print(f"Accounts: {ib.managedAccounts()}\n")

    # Map by id(ticker) since ib_async Ticker has no reqId attribute exposed
    results: dict[int, ProbeResult] = {}
    contract_to_result: dict[int, ProbeResult] = {}

    def on_error(reqId, errorCode, errorString, contract):
        # Match error to result by contract conId if possible
        if contract is not None and id(contract) in contract_to_result:
            contract_to_result[id(contract)].errors.append((errorCode, errorString))
        elif contract is not None:
            for r in results.values():
                if r.contract.conId and contract.conId == r.contract.conId:
                    r.errors.append((errorCode, errorString))
                    break
        print(f"  [reqId={reqId}] ERROR {errorCode}: {errorString}")

    ib.errorEvent += on_error

    contracts = build_contracts()
    qualified: list[tuple[str, str, Contract]] = []
    for name, venue, c in contracts:
        try:
            qcs = await ib.qualifyContractsAsync(c)
            if qcs:
                qualified.append((name, venue, qcs[0]))
                print(f"Qualified: {name:<12} -> {qcs[0].localSymbol or qcs[0].symbol} @ {qcs[0].exchange}")
            else:
                print(f"Qualify failed: {name}")
        except Exception as e:
            print(f"Qualify error {name}: {e}")

    print("\nRequesting market data ...")
    tickers = []
    for name, venue, c in qualified:
        ticker = ib.reqMktData(c, "", False, False)
        result = ProbeResult(name=name, venue=venue, contract=c)
        results[id(ticker)] = result
        contract_to_result[id(c)] = result
        tickers.append((name, venue, ticker))
        print(f"  Subscribed conId={c.conId} -> {name}")

    # Wait for data to arrive (or errors)
    print("\nWaiting 10s for ticks/errors ...")
    for _ in range(10):
        await asyncio.sleep(1)
        for name, venue, t in tickers:
            r = results.get(id(t))
            if r is None:
                continue
            if t.bid > 0 or t.ask > 0 or t.last > 0:
                if r.ticks_seen == 0:
                    r.last_price = t.last or t.bid or t.ask
                    print(f"  TICK {name}: bid={t.bid} ask={t.ask} last={t.last}")
                r.ticks_seen += 1

    # Cancel all
    for name, venue, t in tickers:
        ib.cancelMktData(t.contract)

    # Print summary
    print("\n" + "=" * 70)
    print(f"{'Symbol':<14} {'Venue':<10} {'Status':<28} {'Ticks':>6}")
    print("-" * 70)
    for name, venue, t in tickers:
        r = results[id(t)]
        if r.errors:
            err_codes = ",".join(str(c) for c, _ in r.errors)
            status = f"REJECTED({err_codes})"
        elif r.ticks_seen > 0:
            status = "SUBSCRIBED"
        else:
            status = "SILENT (no tick, no error)"
        print(f"{name:<14} {venue:<10} {status:<28} {r.ticks_seen:>6}")

    ib.disconnect()


if __name__ == "__main__":
    asyncio.run(probe())
