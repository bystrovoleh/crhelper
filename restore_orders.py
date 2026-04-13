"""
Restore original orders that were replaced by the rebalancer.
Cancels current orders and places the original ones back.
"""

from trading.mexc_trader import MEXCTrader

ORDERS = [
    # SOLUSDT short
    {"symbol": "SOLUSDT", "side": "short", "price": 87.06,   "vol": 23,   "sl": 88.81,  "tp": 80.18},
    {"symbol": "SOLUSDT", "side": "short", "price": 87.5,    "vol": 10,   "sl": 88.81,  "tp": 80.18},
    # LDOUSDT short
    {"symbol": "LDOUSDT", "side": "short", "price": 0.331,   "vol": 530,  "sl": 0.349,  "tp": 0.3028},
    {"symbol": "LDOUSDT", "side": "short", "price": 0.334,   "vol": 225,  "sl": 0.349,  "tp": 0.3028},
    # ARBUSDT long
    {"symbol": "ARBUSDT", "side": "long",  "price": 0.105,   "vol": 1417, "sl": 0.0844, "tp": 0.1245},
    {"symbol": "ARBUSDT", "side": "long",  "price": 0.0994,  "vol": 641,  "sl": 0.0844, "tp": 0.1245},
    # BNBUSDT long
    {"symbol": "BNBUSDT", "side": "long",  "price": 576.7,   "vol": 21,   "sl": 565.55, "tp": 610.0},
    {"symbol": "BNBUSDT", "side": "long",  "price": 571.7,   "vol": 9,    "sl": 565.55, "tp": 610.0},
    # HYPEUSDT long
    {"symbol": "HYPEUSDT","side": "long",  "price": 36.7,    "vol": 29,   "sl": 33.93,  "tp": 43.74},
    {"symbol": "HYPEUSDT","side": "long",  "price": 36.2,    "vol": 12,   "sl": 33.93,  "tp": 43.74},
    # DOGEUSDT short
    {"symbol": "DOGEUSDT","side": "short", "price": 0.0933,  "vol": 9,    "sl": 0.09935,"tp": 0.0891},
    {"symbol": "DOGEUSDT","side": "short", "price": 0.0945,  "vol": 4,    "sl": 0.09935,"tp": 0.0891},
]

LEVERAGE = 10

def main():
    trader = MEXCTrader()

    # Cancel all current orders per symbol (deduplicated)
    symbols = list(dict.fromkeys(o["symbol"] for o in ORDERS))
    print("=== Cancelling current orders ===")
    for sym in symbols:
        cancelled = trader.cancel_all_orders(sym)
        print(f"  {sym}: cancelled {cancelled} order(s)")

    print()
    print("=== Restoring original orders ===")
    for o in ORDERS:
        trader.set_leverage(o["symbol"], LEVERAGE)
        result = trader.place_limit_order(
            symbol=o["symbol"],
            side=o["side"],
            price=o["price"],
            vol=o["vol"],
            sl=o["sl"],
            tp=o["tp"],
            leverage=LEVERAGE,
        )
        if result:
            print(f"  ✅ {o['symbol']} {o['side']} @ {o['price']}  vol={o['vol']}")
        else:
            print(f"  ❌ FAILED: {o['symbol']} {o['side']} @ {o['price']}")

    print()
    print("=== Done ===")

if __name__ == "__main__":
    main()
