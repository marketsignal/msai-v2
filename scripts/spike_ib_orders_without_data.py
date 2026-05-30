#!/usr/bin/env python
"""S1-S4 verification spike (PR 1 multi-account-broker-fleet).

Verifies the council's load-bearing assumption: IB Gateway accepts a paper
market order on a sub-account WITHOUT an IB market-data subscription on the
underlying symbol. This is the gating step for Shape A (Databento live data
+ IB exec-only) — if it fails, the split topology is invalid.

DUP733213 is known to have NO real-time market data entitlement per
``reference_ib_entitlements.md`` — exactly the no-data sub-account we want
to verify can still submit orders.

Steps:
1. Use the existing ``DataClient`` of the running ``live-supervisor`` (we
   talk directly to IB Gateway via the ``ib_async`` connection so the
   spike doesn't depend on the supervisor's higher layers).
2. Connect to ``ib-gateway:4004`` (socat-proxied paper port) with
   ``clientId`` derived from a unique slug so we never collide with a
   running TradingNode.
3. Submit a MARKET order BUY 1 AAPL on DUP733213.
4. Observe whether the order is ACK'd by IB (orderStatus event) without
   touching market data.

Pass criteria: order receives ``Submitted`` or ``Filled`` orderStatus from
IB Gateway within 30 seconds, with no IB error code in the
1100-1200 series (`market data required` family) or 354
(`requested market data is not subscribed`).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone

import ib_async


SPIKE_ACCOUNT = os.environ.get("SPIKE_ACCOUNT", "DUP733213")
IB_HOST = os.environ.get("SPIKE_IB_HOST", "localhost")
IB_PORT = int(os.environ.get("SPIKE_IB_PORT", "4004"))
CLIENT_ID = int(os.environ.get("SPIKE_CLIENT_ID", "9911"))


async def main() -> int:
    ib = ib_async.IB()
    print(f"[{datetime.now(timezone.utc).isoformat()}] connecting to "
          f"{IB_HOST}:{IB_PORT} as clientId={CLIENT_ID}")
    try:
        await ib.connectAsync(IB_HOST, IB_PORT, clientId=CLIENT_ID, timeout=15.0)
    except Exception as exc:
        print(f"FAIL: connect: {exc!r}")
        return 1

    print(f"[{datetime.now(timezone.utc).isoformat()}] connected; "
          f"managedAccounts={ib.managedAccounts()}")
    if SPIKE_ACCOUNT not in ib.managedAccounts():
        print(f"FAIL: spike account {SPIKE_ACCOUNT!r} not in managedAccounts: "
              f"{ib.managedAccounts()!r}")
        ib.disconnect()
        return 1

    # Qualify the AAPL stock contract (SMART routing, USD).
    contract = ib_async.Stock("AAPL", "SMART", "USD")
    try:
        qualified = await asyncio.wait_for(ib.qualifyContractsAsync(contract), timeout=15.0)
    except Exception as exc:
        print(f"FAIL: qualify: {exc!r}")
        ib.disconnect()
        return 1
    if not qualified or qualified[0].conId == 0:
        print(f"FAIL: qualify returned empty: {qualified!r}")
        ib.disconnect()
        return 1
    contract = qualified[0]
    print(f"[{datetime.now(timezone.utc).isoformat()}] qualified AAPL conId={contract.conId}")

    # Submit MARKET BUY 1 AAPL on the spike account.
    order = ib_async.MarketOrder("BUY", 1, account=SPIKE_ACCOUNT)
    order.tif = "DAY"
    print(f"[{datetime.now(timezone.utc).isoformat()}] placing MKT BUY 1 AAPL "
          f"on {SPIKE_ACCOUNT}")
    trade = ib.placeOrder(contract, order)

    # Watch order status for 30 seconds.
    deadline = time.time() + 30.0
    errors: list[tuple[int, int, str]] = []

    def _on_error(reqId: int, errorCode: int, errorString: str,  # type: ignore[no-untyped-def]
                  contract: object = None) -> None:
        errors.append((reqId, errorCode, errorString))
        print(f"[{datetime.now(timezone.utc).isoformat()}] IB error: "
              f"reqId={reqId} code={errorCode} msg={errorString!r}")

    ib.errorEvent += _on_error

    while time.time() < deadline:
        await asyncio.sleep(0.5)
        status = trade.orderStatus.status
        print(f"  status={status!r} filled={trade.orderStatus.filled} "
              f"remaining={trade.orderStatus.remaining}")
        if status in ("Submitted", "PreSubmitted", "Filled"):
            # SUCCESS — order accepted by IB Gateway.
            # Cancel any remaining qty so we don't leave a working order on
            # the no-data account.
            if status != "Filled":
                print(f"[{datetime.now(timezone.utc).isoformat()}] cancelling "
                      f"working order to leave the account clean")
                ib.cancelOrder(order)
                await asyncio.sleep(1.0)
            break
        if status in ("Cancelled", "ApiCancelled", "Inactive"):
            print(f"FAIL: order moved to {status!r} before acceptance")
            ib.disconnect()
            return 1

    # Check for market-data-required errors specifically.
    md_errors = [
        (rid, code, msg)
        for (rid, code, msg) in errors
        if code in (354,) or (1100 <= code < 1200)
    ]
    if md_errors:
        print(f"FAIL: market-data-required errors observed: {md_errors!r}")
        ib.disconnect()
        return 1

    final_status = trade.orderStatus.status
    if final_status not in ("Submitted", "PreSubmitted", "Filled", "Cancelled", "ApiCancelled"):
        print(f"FAIL: final status={final_status!r} after 30s — order never accepted")
        ib.disconnect()
        return 1

    print(f"\nPASS: IB Gateway accepted MARKET BUY 1 AAPL on {SPIKE_ACCOUNT} "
          f"(no-data sub-account); final orderStatus={final_status!r}")
    print(f"  errors observed (none market-data-related): "
          f"{[(c, m) for (_, c, m) in errors]!r}")
    ib.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
