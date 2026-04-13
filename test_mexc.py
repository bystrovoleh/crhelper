"""
MEXC connection test script.
Tests: balance, place limit order, check position/order, cancel order.

Usage:
    python test_mexc.py

The test places a limit order FAR from current price so it never fills.
It is cancelled at the end automatically.
"""

import sys
import time
import urllib3
urllib3.disable_warnings()
from trading.mexc_trader import MEXCTrader, _to_mexc_symbol

SYMBOL = "BTCUSDT"
TEST_DIRECTION = "long"   # we'll place a buy limit far below market


def separator(title: str):
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print('─' * 50)


def main():
    trader = MEXCTrader()

    # ── 1. Balance ────────────────────────────────────────────────────────
    separator("1. Balance")
    try:
        balance = trader.get_balance()
        print(f"  Available USDT: {balance:.4f}")
        if balance < 1.0:
            print("  ⚠️  Balance is very low — order placement test may fail.")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        sys.exit(1)

    # ── 2. Current ticker price ───────────────────────────────────────────
    separator("2. Current ticker price")
    sym_mexc = _to_mexc_symbol(SYMBOL)
    try:
        resp = trader.session.get(
            f"{trader.base_url}/api/v1/contract/ticker",
            params={"symbol": sym_mexc},
            timeout=10,
        )
        resp.raise_for_status()
        ticker = resp.json().get("data", {})
        last_price = float(ticker.get("lastPrice", 0))
        print(f"  {SYMBOL} last price: {last_price}")
    except Exception as e:
        print(f"  ❌ FAILED to get ticker: {e}")
        sys.exit(1)

    if last_price <= 0:
        print("  ❌ Invalid price received.")
        sys.exit(1)

    # ── 3. Place a test limit order far below market ──────────────────────
    separator("3. Place test limit order (far below market, won't fill)")
    test_price = round(last_price * 0.50, 1)   # 50% below — will never fill
    test_sl    = round(test_price * 0.90, 1)
    test_tp    = round(test_price * 1.10, 1)

    print(f"  Order params: {TEST_DIRECTION} @ {test_price}  SL={test_sl}  TP={test_tp}")

    # Set leverage first
    try:
        trader.set_leverage(SYMBOL, 10)
        print("  Leverage set to 10x ✓")
    except Exception as e:
        print(f"  ⚠️  set_leverage warning: {e}")

    # Calculate minimum volume
    vol = trader.calc_vol(SYMBOL, test_price, balance * 0.05)  # use 5% of balance
    if vol < 1:
        vol = 1  # force minimum 1 contract for the test
    print(f"  Calculated vol: {vol} contract(s)")

    # Raw request directly — bypass place_limit_order's exception handler
    import json as _json
    body = {
        "symbol": _to_mexc_symbol(SYMBOL),
        "price": str(test_price),
        "vol": str(vol),
        "side": 1,
        "type": 1,
        "openType": 1,
        "leverage": 10,
    }
    ts = int(time.time() * 1000)
    body_str = _json.dumps(body, sort_keys=True, separators=(",", ":"))
    headers = trader._signed_headers(ts, body_str)
    order_result = None
    try:
        order_result = trader.place_limit_order(
            symbol=SYMBOL,
            side=TEST_DIRECTION,
            price=test_price,
            vol=vol,
            sl=test_sl,
            tp=test_tp,
            leverage=10,
        )
        if order_result:
            print(f"  ✅ Order placed: id={order_result.get('order_id')}")
        else:
            print("  ❌ place_limit_order returned None")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")

    # ── 4. Check open orders ──────────────────────────────────────────────
    separator("4. Check open orders")
    time.sleep(1)  # brief pause for exchange to register the order
    try:
        open_orders = trader.get_open_orders(SYMBOL)
        print(f"  Open orders for {SYMBOL}: {len(open_orders)}")
        for o in open_orders:
            oid = o.get("orderId") or o.get("id")
            price = o.get("price")
            side = o.get("side")
            age_h = trader.get_order_age_hours(o)
            print(f"    id={oid}  price={price}  side={side}  age={age_h:.3f}h")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")

    # ── 5. Check open positions ───────────────────────────────────────────
    separator("5. Check open positions")
    try:
        has_pos = trader.has_open_position(SYMBOL)
        positions = trader.get_open_positions(SYMBOL)
        print(f"  has_open_position({SYMBOL}): {has_pos}")
        print(f"  Open positions: {len(positions)}")
        for p in positions:
            print(f"    {p.get('symbol')}  vol={p.get('vol')}  avgPrice={p.get('holdAvgPrice')}")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")

    # ── 6. Cancel the test order ──────────────────────────────────────────
    separator("6. Cancel test order")
    try:
        cancelled = trader.cancel_all_orders(SYMBOL)
        print(f"  ✅ Cancelled {cancelled} order(s)")
    except Exception as e:
        print(f"  ❌ FAILED to cancel: {e}")

    # ── 7. Verify no open orders remain ──────────────────────────────────
    separator("7. Verify orders cleared")
    time.sleep(1)
    try:
        open_orders = trader.get_open_orders(SYMBOL)
        print(f"  Open orders after cancel: {len(open_orders)}")
        if len(open_orders) == 0:
            print("  ✅ Clean — no hanging orders")
        else:
            print("  ⚠️  Some orders still open!")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")

    separator("Done")
    print()


if __name__ == "__main__":
    main()
