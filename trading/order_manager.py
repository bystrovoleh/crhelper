"""
Order manager — applies trading rules before placing orders.

Rules:
  1. Open position exists for symbol → skip, do nothing
  2. Open order exists > 3h → cancel it, place new order from signal
  3. No position, no order → place new order from signal
"""

from trading.mexc_trader import MEXCTrader

# Trading config (can be moved to settings.py if needed)
TRADE_SIZE_PCT = 0.10        # 10% of balance per trade
ENTRY1_WEIGHT  = 0.70        # 70% of trade size on entry1
ENTRY2_WEIGHT  = 0.30        # 30% of trade size on entry2
LEVERAGE       = 10
MAX_ORDER_AGE_HOURS = 3.0    # cancel and replace orders older than this


def _log(msg: str):
    print(f"  [order_mgr] {msg}")


def process_signal(signal: dict, dry_run: bool = False) -> dict:
    """
    Apply trading rules and place orders if appropriate.

    Args:
        signal:  dict from IntradayAgent with has_setup, direction, entry1, entry2, sl, tp1, tp2
        dry_run: if True, log what would happen but don't actually place orders

    Returns:
        dict with keys: action, orders_placed, orders_cancelled, reason
    """
    result = {
        "action": "none",
        "orders_placed": [],
        "orders_cancelled": 0,
        "reason": "",
    }

    if not signal.get("has_setup"):
        result["reason"] = "no_setup"
        return result

    symbol   = signal["symbol"]
    direction = signal["direction"]
    entry1   = signal.get("entry1")
    entry2   = signal.get("entry2")
    sl       = signal.get("sl")
    tp1      = signal.get("tp1")

    if not all([symbol, direction, entry1, sl, tp1]):
        result["reason"] = "missing_fields"
        _log(f"Missing required fields for {symbol}")
        return result

    trader = MEXCTrader()

    # ── Rule 1: open position exists ──────────────────────────────────
    if trader.has_open_position(symbol):
        result["action"] = "skip_position"
        result["reason"] = "open_position_exists"
        _log(f"{symbol}: open position exists — skipping")
        return result

    # ── Rule 2/3: check open orders ───────────────────────────────────
    open_orders = trader.get_open_orders(symbol)

    if open_orders:
        # Check age of oldest order
        oldest = max(open_orders, key=lambda o: trader.get_order_age_hours(o))
        age_h = trader.get_order_age_hours(oldest)

        if age_h < MAX_ORDER_AGE_HOURS:
            result["action"] = "skip_fresh_order"
            result["reason"] = f"open_order_is_fresh ({age_h:.1f}h < {MAX_ORDER_AGE_HOURS}h)"
            _log(f"{symbol}: order is only {age_h:.1f}h old — keeping it")
            return result

        # Cancel stale orders
        _log(f"{symbol}: orders are {age_h:.1f}h old — cancelling and replacing")
        if not dry_run:
            cancelled = trader.cancel_all_orders(symbol)
            result["orders_cancelled"] = cancelled

    # ── Place new orders ──────────────────────────────────────────────
    if not dry_run:
        balance = trader.get_balance()
    else:
        balance = 10000.0  # dummy for dry run

    if balance < 5.0:
        result["reason"] = f"insufficient_balance ({balance:.4f} USDT < 5 USDT minimum)"
        _log(f"{symbol}: balance {balance:.4f} USDT is too low to trade")
        return result

    trade_usdt = balance * TRADE_SIZE_PCT
    _log(f"{symbol}: balance={balance:.2f} USDT, trade size={trade_usdt:.2f} USDT ({TRADE_SIZE_PCT*100:.0f}%)")

    # Set leverage before placing orders
    if not dry_run:
        trader.set_leverage(symbol, LEVERAGE)

    orders_placed = []

    MIN_VOL = 1  # minimum contract volume (MEXC uses integer contracts)

    # Entry 1 (70%)
    usdt1 = trade_usdt * ENTRY1_WEIGHT
    vol1  = trader.calc_vol(symbol, entry1, usdt1)
    if vol1 >= MIN_VOL:
        _log(f"  Entry1: {direction} @ {entry1}  vol={vol1}  usdt={usdt1:.2f}  sl={sl}  tp={tp1}")
        if not dry_run:
            order = trader.place_limit_order(symbol, direction, entry1, vol1, sl, tp1, LEVERAGE)
            if order:
                orders_placed.append(order)
        else:
            orders_placed.append({"dry_run": True, "side": direction, "price": entry1, "vol": vol1})
    else:
        _log(f"  Entry1 skipped: vol={vol1} < MIN_VOL={MIN_VOL} (trade too small)")

    # Entry 2 (30%) — only if provided
    if entry2 and entry2 != entry1:
        usdt2 = trade_usdt * ENTRY2_WEIGHT
        vol2  = trader.calc_vol(symbol, entry2, usdt2)
        if vol2 >= MIN_VOL:
            _log(f"  Entry2: {direction} @ {entry2}  vol={vol2}  usdt={usdt2:.2f}  sl={sl}  tp={tp1}")
            if not dry_run:
                order = trader.place_limit_order(symbol, direction, entry2, vol2, sl, tp1, LEVERAGE)
                if order:
                    orders_placed.append(order)
            else:
                orders_placed.append({"dry_run": True, "side": direction, "price": entry2, "vol": vol2})
        else:
            _log(f"  Entry2 skipped: vol={vol2} < MIN_VOL={MIN_VOL}")

    result["action"] = "orders_placed"
    result["orders_placed"] = orders_placed
    result["reason"] = f"placed {len(orders_placed)} order(s)"
    return result
