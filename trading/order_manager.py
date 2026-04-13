"""
Order manager — applies trading rules before placing orders.

Rules:
  1. Open position exists for symbol → skip, do nothing
  2. Open order exists > 3h → cancel it, place new order from signal
  3. No position, no order → place new order from signal
"""

from trading.mexc_trader import MEXCTrader

# Trading config (can be moved to settings.py if needed)
TRADE_SIZE_PCT = 0.15        # 15% of balance per trade
ENTRY1_WEIGHT  = 0.70        # 70% of trade size on entry1
ENTRY2_WEIGHT  = 0.30        # 30% of trade size on entry2
LEVERAGE       = 10
MAX_ORDER_AGE_HOURS       = 3.0    # intraday: cancel and replace orders older than this
SWING_MAX_ORDER_AGE_HOURS = 72.0   # swing: 3 days


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


def swing_process_signal(signal: dict, dry_run: bool = False) -> dict:
    """
    Same as process_signal but for swing trading:
    - Stale order threshold is 5 days (120h) instead of 3h
    - If a fresh order already exists (< 5 days) → keep it, skip
    - If order is > 5 days old → cancel and replace with new signal levels
    - If no order and no position → place new orders

    Returns dict with keys: action, orders_placed, orders_cancelled, reason
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

    symbol    = signal["symbol"]
    direction = signal["direction"]
    entry1    = signal.get("entry1")
    entry2    = signal.get("entry2")
    sl        = signal.get("sl")
    tp1       = signal.get("tp1")

    if not all([symbol, direction, entry1, sl, tp1]):
        result["reason"] = "missing_fields"
        _log(f"[swing] Missing required fields for {symbol}")
        return result

    trader = MEXCTrader()

    # Rule 1: open position exists — skip
    if trader.has_open_position(symbol):
        result["action"] = "skip_position"
        result["reason"] = "open_position_exists"
        _log(f"[swing] {symbol}: open position exists — skipping")
        return result

    # Rule 2/3: check open orders, threshold = 5 days
    open_orders = trader.get_open_orders(symbol)

    if open_orders:
        oldest = max(open_orders, key=lambda o: trader.get_order_age_hours(o))
        age_h = trader.get_order_age_hours(oldest)

        if age_h < SWING_MAX_ORDER_AGE_HOURS:
            result["action"] = "skip_fresh_order"
            result["reason"] = f"swing_order_is_fresh ({age_h:.1f}h < {SWING_MAX_ORDER_AGE_HOURS:.0f}h)"
            _log(f"[swing] {symbol}: order is {age_h:.1f}h old — keeping it (< 5 days)")
            return result

        # Cancel stale orders (> 5 days)
        _log(f"[swing] {symbol}: orders are {age_h:.1f}h old — cancelling and replacing")
        if not dry_run:
            cancelled = trader.cancel_all_orders(symbol)
            result["orders_cancelled"] = cancelled

    # Place new orders
    if not dry_run:
        balance = trader.get_balance()
    else:
        balance = 10000.0

    if balance < 5.0:
        result["reason"] = f"insufficient_balance ({balance:.4f} USDT)"
        _log(f"[swing] {symbol}: balance too low ({balance:.4f} USDT)")
        return result

    trade_usdt = balance * TRADE_SIZE_PCT
    _log(f"[swing] {symbol}: balance={balance:.2f} USDT, trade size={trade_usdt:.2f} USDT")

    if not dry_run:
        trader.set_leverage(symbol, LEVERAGE)

    orders_placed = []
    MIN_VOL = 1

    # Entry 1 (70%)
    usdt1 = trade_usdt * ENTRY1_WEIGHT
    vol1  = trader.calc_vol(symbol, entry1, usdt1)
    if vol1 >= MIN_VOL:
        _log(f"[swing]   Entry1: {direction} @ {entry1}  vol={vol1}  sl={sl}  tp={tp1}")
        if not dry_run:
            order = trader.place_limit_order(symbol, direction, entry1, vol1, sl, tp1, LEVERAGE)
            if order:
                orders_placed.append(order)
        else:
            orders_placed.append({"dry_run": True, "side": direction, "price": entry1, "vol": vol1})
    else:
        _log(f"[swing]   Entry1 skipped: vol={vol1} < MIN_VOL={MIN_VOL}")

    # Entry 2 (30%) — only if provided
    if entry2 and entry2 != entry1:
        usdt2 = trade_usdt * ENTRY2_WEIGHT
        vol2  = trader.calc_vol(symbol, entry2, usdt2)
        if vol2 >= MIN_VOL:
            _log(f"[swing]   Entry2: {direction} @ {entry2}  vol={vol2}  sl={sl}  tp={tp1}")
            if not dry_run:
                order = trader.place_limit_order(symbol, direction, entry2, vol2, sl, tp1, LEVERAGE)
                if order:
                    orders_placed.append(order)
            else:
                orders_placed.append({"dry_run": True, "side": direction, "price": entry2, "vol": vol2})
        else:
            _log(f"[swing]   Entry2 skipped: vol={vol2} < MIN_VOL={MIN_VOL}")

    result["action"] = "orders_placed"
    result["orders_placed"] = orders_placed
    result["reason"] = f"placed {len(orders_placed)} swing order(s)"
    return result


# ---------------------------------------------------------------------------
# Trailing stop management
# ---------------------------------------------------------------------------

# (pnl_threshold, new_sl_pct) — thresholds in % of MARGIN (as shown on MEXC dashboard)
# new_sl_pct is % of price movement from entry for the new SL level
# Example at 10x leverage: +50% margin = +5% price move
# Ordered from highest to lowest so we apply the best applicable step
TRAILING_STEPS = [
    (0.15, 0.10),   # +15% margin pnl → SL at +10% from entry price
    (0.10, 0.05),   # +10% margin pnl → SL at +5%  from entry price
    (0.05, 0.00),   # +5%  margin pnl → SL at breakeven (entry)
]


def check_trailing_stops() -> list[dict]:
    """
    Check all open positions and return trailing stop suggestions.
    Never modifies anything — only returns what should be moved.

    Returns list of dicts:
      {symbol, direction, pnl_pct, entry, current_sl, new_sl, action}
      action: "move_sl" | "already_protected"
    """
    trader = MEXCTrader()
    positions = trader.get_open_positions()
    results = []

    for p in positions:
        symbol = p.get("symbol", "")
        vol = float(p.get("holdVol") or p.get("vol") or 0)
        if vol <= 0:
            continue

        pos_type = int(p.get("positionType", 1))
        direction = "long" if pos_type == 1 else "short"
        entry = float(p.get("holdAvgPrice", 0))
        current_sl = float(p.get("stopLossPrice", 0))

        # Get current price from ticker
        last_price = 0.0
        try:
            ticker_resp = trader.session.get(
                f"{trader.base_url}/api/v1/contract/ticker",
                params={"symbol": symbol},
                timeout=20,
            )
            last_price = float(ticker_resp.json().get("data", {}).get("lastPrice", 0))
        except Exception:
            pass
        if last_price <= 0 or entry <= 0:
            continue

        # PnL% from margin (same as MEXC dashboard)
        im = float(p.get("im") or p.get("oim") or 0)
        contract_size, _ = trader.get_contract_size(symbol.replace("_", ""))
        if direction == "long":
            pnl_usdt = (last_price - entry) * vol * contract_size
        else:
            pnl_usdt = (entry - last_price) * vol * contract_size
        pnl_pct = (pnl_usdt / im) if im > 0 else 0

        # Find best applicable trailing step
        new_sl_pct = None
        for threshold, sl_pct in TRAILING_STEPS:
            if pnl_pct >= threshold:
                new_sl_pct = sl_pct
                break

        if new_sl_pct is None:
            continue

        # Calculate suggested SL price
        if direction == "long":
            new_sl = round(entry * (1 + new_sl_pct), 8)
            if current_sl > 0 and new_sl <= current_sl:
                results.append({
                    "symbol": symbol, "direction": direction,
                    "pnl_pct": pnl_pct, "entry": entry,
                    "current_sl": current_sl, "new_sl": new_sl,
                    "action": "already_protected",
                })
                continue
        else:
            new_sl = round(entry * (1 - new_sl_pct), 8)
            if current_sl > 0 and new_sl >= current_sl:
                results.append({
                    "symbol": symbol, "direction": direction,
                    "pnl_pct": pnl_pct, "entry": entry,
                    "current_sl": current_sl, "new_sl": new_sl,
                    "action": "already_protected",
                })
                continue

        results.append({
            "symbol": symbol,
            "direction": direction,
            "pnl_pct": pnl_pct,
            "entry": entry,
            "current_sl": current_sl,
            "new_sl": new_sl,
            "action": "move_sl",
        })
        _log(f"[trail] {symbol} {direction}  pnl={pnl_pct*100:.1f}%  SL {current_sl} → {new_sl}")

    return results


# ---------------------------------------------------------------------------
# Order rebalancing
# ---------------------------------------------------------------------------

RESERVE_PCT = 0.30        # keep 30% of equity untouched
REBALANCE_PAUSE = 0.15    # seconds between cancel+resubmit to avoid rate limits
REBALANCE_THRESHOLD = 0.05  # skip rebalance if margin deviation < 5%


def rebalance_orders(assets: list[str], dry_run: bool = False) -> dict:
    """
    After scanning, redistribute margin equally across all symbols that have
    pending limit orders, preserving the 70/30 split between entry1/entry2.

    Logic:
      1. Collect open orders grouped by symbol
      2. equity × (1 - RESERVE_PCT) = pool available for orders
      3. target_margin = pool / number_of_symbols_with_orders
      4. Per symbol: cancel all orders, then resubmit with:
         - 1 order  → target_margin × 100%
         - 2 orders → target_margin × 70% (higher-price / entry1)
                      target_margin × 30% (lower-price  / entry2)
         - If any new_vol < 1 the symbol's allocation is cancelled entirely

    Returns dict: rebalanced, cancelled, skipped, target_margin, errors
    """
    import time

    result = {
        "rebalanced": [],
        "cancelled": [],
        "skipped": [],
        "target_margin": 0.0,
        "errors": [],
    }

    trader = MEXCTrader()

    # Step 1 — collect open orders grouped by symbol
    orders_by_symbol: dict[str, list[dict]] = {}
    for symbol in assets:
        try:
            orders = trader.get_open_orders(symbol)
            if orders:
                orders_by_symbol[symbol] = orders
        except Exception as e:
            result["errors"].append(f"{symbol}: {e}")

    if not orders_by_symbol:
        _log("[rebalance] No open orders found — nothing to rebalance")
        return result

    # Step 2 — get total equity and compute target margin per symbol
    try:
        equity = trader.get_equity()
    except Exception as e:
        result["errors"].append(f"get_equity: {e}")
        return result

    pool = equity * (1.0 - RESERVE_PCT)
    n = len(orders_by_symbol)
    target_margin = pool / n

    result["target_margin"] = round(target_margin, 4)
    _log(f"[rebalance] equity={equity:.2f}  pool={pool:.2f}  symbols={n}  target_margin={target_margin:.2f} USDT each")

    # Step 3 — build per-symbol rebalance plan, sorted so reductions come first
    symbol_info = []
    for symbol, orders in orders_by_symbol.items():
        try:
            contract_size, _ = trader.get_contract_size(symbol)
            current_total_margin = sum(
                (float(o.get("price", 0)) * float(o.get("vol", 0)) * contract_size) / LEVERAGE
                for o in orders
                if float(o.get("price", 0)) > 0
            )
            symbol_info.append({
                "symbol": symbol,
                "orders": orders,
                "contract_size": contract_size,
                "current_total_margin": current_total_margin,
                "excess": current_total_margin - target_margin,
            })
        except Exception as e:
            result["errors"].append(f"{symbol} margin calc: {e}")

    # Reduce over-allocated symbols first to free margin before topping up
    symbol_info.sort(key=lambda x: x["excess"], reverse=True)

    # Step 4 — cancel and resubmit per symbol
    for info in symbol_info:
        symbol = info["symbol"]
        orders = info["orders"]

        # Determine side from first order
        side_int = int(orders[0].get("side", 1))
        is_long = side_int == 1

        # For longs:  entry1 = higher price (closer to market from below) → 70%
        # For shorts: entry1 = lower  price (closer to market from above) → 70%
        orders_sorted = sorted(orders, key=lambda o: float(o.get("price", 0)), reverse=is_long)

        # Determine margin split based on number of orders for this symbol
        if len(orders_sorted) == 1:
            weights = [1.0]
        else:
            weights = [ENTRY1_WEIGHT, ENTRY2_WEIGHT]  # 70%, 30%

        # Build list of (order, allocated_margin)
        plan = list(zip(orders_sorted, [target_margin * w for w in weights]))

        # Validate vols before touching anything
        new_vols = []
        valid = True
        for o, alloc_margin in plan:
            price = float(o.get("price", 0))
            if price <= 0:
                valid = False
                break
            nv = trader.calc_vol(symbol, price, alloc_margin, leverage=LEVERAGE)
            if nv < 1:
                valid = False
                break
            new_vols.append(nv)

        if not valid:
            _log(f"[rebalance] {symbol}: new vol < 1 for some order — cancelling all orders for symbol")
            if not dry_run:
                trader.cancel_all_orders(symbol)
                time.sleep(REBALANCE_PAUSE)
            for o in orders_sorted:
                result["cancelled"].append({
                    "symbol": symbol,
                    "order_id": o.get("orderId") or o.get("id"),
                    "reason": "vol<1",
                })
            continue

        # Check if anything actually needs to change
        # Skip if margin deviation is within threshold OR vols are identical
        all_same = all(
            new_vols[i] == int(float(orders_sorted[i].get("vol", 0)))
            for i in range(len(orders_sorted))
        )
        margin_close_enough = (
            target_margin > 0 and
            abs(info["current_total_margin"] - target_margin) / target_margin < REBALANCE_THRESHOLD
        )
        if all_same or margin_close_enough:
            _log(f"[rebalance] {symbol}: within threshold — skipping")
            for o in orders_sorted:
                result["skipped"].append({
                    "symbol": symbol,
                    "order_id": o.get("orderId") or o.get("id"),
                    "vol": int(float(o.get("vol", 0))),
                })
            continue

        old_vols = [int(float(o.get("vol", 0))) for o in orders_sorted]
        _log(f"[rebalance] {symbol}: margin {info['current_total_margin']:.2f} → {target_margin:.2f}  "
             f"vols {old_vols} → {new_vols}")

        if not dry_run:
            # Cancel all orders for this symbol at once
            trader.cancel_all_orders(symbol)
            time.sleep(REBALANCE_PAUSE)

            # Resubmit each order with the new vol
            for i, (o, _) in enumerate(plan):
                price = float(o.get("price", 0))
                sl = o.get("stopLossPrice") or o.get("stopLoss")
                tp = o.get("takeProfitPrice") or o.get("takeProfit")
                side_int = int(o.get("side", 1))
                side_str = "long" if side_int == 1 else "short"

                new_order = trader.place_limit_order(
                    symbol=symbol,
                    side=side_str,
                    price=price,
                    vol=new_vols[i],
                    sl=float(sl) if sl else None,
                    tp=float(tp) if tp else None,
                    leverage=LEVERAGE,
                )
                time.sleep(REBALANCE_PAUSE)
                if new_order:
                    result["rebalanced"].append({
                        "symbol": symbol,
                        "old_vol": old_vols[i],
                        "new_vol": new_vols[i],
                        "price": price,
                    })
                else:
                    result["errors"].append(f"{symbol}: failed to resubmit order @ {price}")
        else:
            for i, (o, _) in enumerate(plan):
                price = float(o.get("price", 0))
                result["rebalanced"].append({
                    "symbol": symbol,
                    "old_vol": int(float(o.get("vol", 0))),
                    "new_vol": new_vols[i],
                    "price": price,
                    "dry_run": True,
                })

    return result
