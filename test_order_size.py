"""
Test order size logic:
  - 15% of balance as margin
  - x10 leverage
  - limit price far below market (won't fill)
  - SL -3% from entry, TP +6% from entry (RR 2.0)
  - Does NOT cancel the order — check it manually on MEXC
"""

from trading.mexc_trader import MEXCTrader, _to_mexc_symbol
from trading.order_manager import TRADE_SIZE_PCT, LEVERAGE

SYMBOL = "BTCUSDT"

def main():
    trader = MEXCTrader()

    # Balance
    balance = trader.get_balance()
    print(f"Balance:       {balance:.4f} USDT")

    margin = balance * TRADE_SIZE_PCT
    print(f"Margin (15%):  {margin:.4f} USDT")

    # Current price
    sym_mexc = _to_mexc_symbol(SYMBOL)
    resp = trader.session.get(
        f"{trader.base_url}/api/v1/contract/ticker",
        params={"symbol": sym_mexc}, timeout=10,
    )
    last_price = float(resp.json()["data"]["lastPrice"])
    print(f"Current price: {last_price}")

    # Order price — 50% below market, won't fill
    entry = round(last_price * 0.50, 1)
    sl    = round(entry * 0.97, 1)    # -3%
    tp    = round(entry * 1.06, 1)    # +6%

    print(f"\nOrder params:")
    print(f"  Entry (50% below market): {entry}")
    print(f"  SL (-3%):                 {sl}")
    print(f"  TP (+6%):                 {tp}")

    # Volume calculation
    vol = trader.calc_vol(SYMBOL, entry, margin, leverage=LEVERAGE)
    contract_size, _ = trader.get_contract_size(SYMBOL)
    notional = vol * entry * contract_size
    print(f"\nVolume calculation:")
    print(f"  contractSize:  {contract_size}")
    print(f"  vol:           {vol} contracts")
    print(f"  notional:      {notional:.2f} USDT  (vol * entry * contractSize)")
    print(f"  effective lev: {notional / margin:.1f}x  (notional / margin)")

    if vol < 1:
        print("\n❌ vol < 1 — balance too low to place order")
        return

    # Set leverage
    trader.set_leverage(SYMBOL, LEVERAGE)
    print(f"\nLeverage set to {LEVERAGE}x ✓")

    # Place order
    result = trader.place_limit_order(
        symbol=SYMBOL,
        side="long",
        price=entry,
        vol=vol,
        sl=sl,
        tp=tp,
        leverage=LEVERAGE,
    )

    if result:
        print(f"\n✅ Order placed successfully")
        print(f"  order_id: {result.get('order_id')}")
        print(f"  symbol:   {result.get('symbol')}")
        print(f"  side:     {result.get('side')}")
        print(f"  price:    {result.get('price')}")
        print(f"  vol:      {result.get('vol')}")
        print(f"\n⚠️  Order NOT cancelled — check and delete manually on MEXC")
    else:
        print("\n❌ Failed to place order")

if __name__ == "__main__":
    main()
