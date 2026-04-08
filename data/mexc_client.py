import requests
import time
import urllib3
from datetime import datetime, timezone
from typing import Optional
from config.settings import MEXC_BASE_URL

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _to_mexc_symbol(symbol: str) -> str:
    """Convert BTCUSDT → BTC_USDT for MEXC Futures API."""
    symbol = symbol.upper()
    if "_" in symbol:
        return symbol
    if symbol.endswith("USDT"):
        return symbol[:-4] + "_USDT"
    return symbol


class MEXCClient:
    """MEXC Futures public API client."""

    def __init__(self):
        self.base_url = MEXC_BASE_URL
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({"Content-Type": "application/json"})

    def _get(self, endpoint: str, params: dict = None) -> dict:
        url = f"{self.base_url}{endpoint}"
        response = self.session.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data.get("success", True) and "code" in data:
            raise ValueError(f"MEXC API error {data.get('code')}: {data.get('message')}")
        return data

    def get_candles(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> list[dict]:
        """
        Get klines/candles.
        interval: Min1, Min5, Min15, Min30, Min60, Hour4, Hour8, Day1, Week1, Month1
        Returns list of dicts: open, high, low, close, volume, timestamp
        """
        sym = _to_mexc_symbol(symbol)
        params = {"interval": interval, "limit": limit}
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        data = self._get(f"/api/v1/contract/kline/{sym}", params=params)
        rows = data.get("data", {})

        candles = []
        times = rows.get("time", [])
        opens = rows.get("open", [])
        highs = rows.get("high", [])
        lows = rows.get("low", [])
        closes = rows.get("close", [])
        volumes = rows.get("vol", [])

        for i in range(len(times)):
            candles.append({
                "timestamp": int(times[i]),
                "datetime": datetime.fromtimestamp(int(times[i]), tz=timezone.utc).isoformat(),
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
                "volume": float(volumes[i]),
            })

        return candles

    def get_ticker(self, symbol: str) -> dict:
        """Get current ticker — also contains open interest (holdVol) and funding rate."""
        sym = _to_mexc_symbol(symbol)
        data = self._get("/api/v1/contract/ticker", params={"symbol": sym})
        item = data.get("data", {})
        return {
            "symbol": symbol,
            "last_price": float(item.get("lastPrice", 0)),
            "price_change_pct": float(item.get("riseFallRate", 0)) * 100,
            "volume_24h": float(item.get("volume24", 0)),
            "high_24h": float(item.get("high24Price", 0)),
            "low_24h": float(item.get("lower24Price", 0)),
            "hold_vol": float(item.get("holdVol", 0)),       # open interest
            "funding_rate": float(item.get("fundingRate", 0)),
            "index_price": float(item.get("indexPrice", 0)),
            "fair_price": float(item.get("fairPrice", 0)),
            "timestamp": int(time.time()),
        }

    def get_open_interest(self, symbol: str) -> dict:
        """Get open interest from ticker (holdVol)."""
        ticker = self.get_ticker(symbol)
        return {
            "symbol": symbol,
            "open_interest": ticker["hold_vol"],
            "timestamp": ticker["timestamp"],
        }

    def get_funding_rate(self, symbol: str) -> dict:
        """Get current funding rate."""
        sym = _to_mexc_symbol(symbol)
        data = self._get(f"/api/v1/contract/funding_rate/{sym}")
        item = data.get("data", {})
        return {
            "symbol": symbol,
            "funding_rate": float(item.get("fundingRate", 0)),
            "next_settle_time": item.get("nextSettleTime"),
            "timestamp": int(time.time()),
        }

    def get_long_short_ratio(self, symbol: str, period: str = "1h") -> dict:
        """
        Get long/short ratio.
        period: 5m, 15m, 30m, 1h, 4h, 1d
        """
        sym = _to_mexc_symbol(symbol)
        try:
            data = self._get(
                "/api/v1/contract/long_short",
                params={"symbol": sym, "period": period},
            )
            item = data.get("data", {})
            if isinstance(item, list) and item:
                item = item[-1]
            return {
                "symbol": symbol,
                "long_ratio": float(item.get("longRatio", 0.5)),
                "short_ratio": float(item.get("shortRatio", 0.5)),
                "timestamp": int(time.time()),
            }
        except Exception:
            # Fallback: estimate from ticker
            ticker = self.get_ticker(symbol)
            fr = ticker.get("funding_rate", 0)
            long_ratio = 0.5 + min(abs(fr) * 50, 0.2) * (1 if fr > 0 else -1)
            return {
                "symbol": symbol,
                "long_ratio": round(long_ratio, 3),
                "short_ratio": round(1 - long_ratio, 3),
                "timestamp": int(time.time()),
                "estimated": True,
            }

    def get_market_snapshot(self, symbol: str) -> dict:
        """
        Collect all relevant market data for a symbol in one call.
        Used for saving context alongside examples.
        """
        snapshot = {
            "symbol": symbol,
            "timestamp": int(time.time()),
            "candles": {},
            "open_interest": None,
            "funding_rate": None,
            "long_short_ratio": None,
            "ticker": None,
        }

        from config.settings import TIMEFRAMES, CANDLE_LIMITS
        for name, interval in TIMEFRAMES.items():
            limit = CANDLE_LIMITS.get(name, 200)
            try:
                snapshot["candles"][name] = self.get_candles(symbol, interval, limit=limit)
            except Exception as e:
                snapshot["candles"][name] = []
                print(f"  [warn] candles {name}: {e}")

        # Ticker first — it also gives OI and funding rate
        try:
            snapshot["ticker"] = self.get_ticker(symbol)
            snapshot["open_interest"] = {
                "symbol": symbol,
                "open_interest": snapshot["ticker"]["hold_vol"],
                "timestamp": snapshot["ticker"]["timestamp"],
            }
            snapshot["funding_rate"] = {
                "symbol": symbol,
                "funding_rate": snapshot["ticker"]["funding_rate"],
                "timestamp": snapshot["ticker"]["timestamp"],
            }
        except Exception as e:
            print(f"  [warn] ticker: {e}")

        try:
            snapshot["long_short_ratio"] = self.get_long_short_ratio(symbol)
        except Exception as e:
            print(f"  [warn] long_short_ratio: {e}")

        return snapshot

    def get_exit_snapshot(self, symbol: str) -> dict:
        """
        Collect market data for exit analysis.
        Includes 1h candles in addition to the standard timeframes.
        """
        from config.settings import EXIT_TIMEFRAMES, EXIT_CANDLE_LIMITS

        snapshot = {
            "symbol": symbol,
            "timestamp": int(time.time()),
            "candles": {},
            "open_interest": None,
            "funding_rate": None,
            "long_short_ratio": None,
            "ticker": None,
        }

        for name, interval in EXIT_TIMEFRAMES.items():
            limit = EXIT_CANDLE_LIMITS.get(name, 100)
            try:
                snapshot["candles"][name] = self.get_candles(symbol, interval, limit=limit)
            except Exception as e:
                snapshot["candles"][name] = []
                print(f"  [warn] exit candles {name}: {e}")

        try:
            snapshot["ticker"] = self.get_ticker(symbol)
            snapshot["open_interest"] = {
                "symbol": symbol,
                "open_interest": snapshot["ticker"]["hold_vol"],
                "timestamp": snapshot["ticker"]["timestamp"],
            }
            snapshot["funding_rate"] = {
                "symbol": symbol,
                "funding_rate": snapshot["ticker"]["funding_rate"],
                "timestamp": snapshot["ticker"]["timestamp"],
            }
        except Exception as e:
            print(f"  [warn] exit ticker: {e}")

        try:
            snapshot["long_short_ratio"] = self.get_long_short_ratio(symbol)
        except Exception as e:
            print(f"  [warn] exit long_short_ratio: {e}")

        return snapshot

    def get_orderbook(self, symbol: str, depth: int = 20) -> dict:
        """
        Get order book snapshot.
        depth: 5, 10, 20 — number of bid/ask levels
        Returns bid/ask walls, imbalance ratio, top-of-book spread.
        """
        sym = _to_mexc_symbol(symbol)
        data = self._get(f"/api/v1/contract/depth/{sym}", params={"limit": depth})
        item = data.get("data", {})

        bids = item.get("bids", [])  # [[price, qty], ...]
        asks = item.get("asks", [])

        def parse_levels(levels):
            return [{"price": float(l[0]), "qty": float(l[1])} for l in levels]

        bids_parsed = parse_levels(bids)
        asks_parsed = parse_levels(asks)

        bid_vol = sum(l["qty"] for l in bids_parsed)
        ask_vol = sum(l["qty"] for l in asks_parsed)
        total_vol = bid_vol + ask_vol

        imbalance = round(bid_vol / total_vol, 3) if total_vol > 0 else 0.5
        # imbalance > 0.6 = bid pressure (bullish), < 0.4 = ask pressure (bearish)

        # Find walls: levels with qty > 3x average
        def find_walls(levels, n=5):
            if not levels:
                return []
            avg_qty = sum(l["qty"] for l in levels) / len(levels)
            return [l for l in levels if l["qty"] > avg_qty * 3][:n]

        bid_walls = find_walls(bids_parsed)
        ask_walls = find_walls(asks_parsed)

        best_bid = bids_parsed[0]["price"] if bids_parsed else 0
        best_ask = asks_parsed[0]["price"] if asks_parsed else 0
        spread_pct = round((best_ask - best_bid) / best_bid * 100, 4) if best_bid > 0 and best_ask > 0 else 0

        return {
            "symbol": symbol,
            "timestamp": int(time.time()),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_pct": spread_pct,
            "bid_volume": round(bid_vol, 2),
            "ask_volume": round(ask_vol, 2),
            "imbalance": imbalance,          # 0.0–1.0, >0.6 bullish pressure
            "bid_walls": bid_walls,          # large bid levels
            "ask_walls": ask_walls,          # large ask levels
            "bids": bids_parsed[:depth],
            "asks": asks_parsed[:depth],
        }

    def get_recent_trades(self, symbol: str, limit: int = 100) -> dict:
        """
        Get recent trades and aggregate into CVD + buy/sell pressure stats.
        limit: number of recent trades (max 100 per MEXC)
        Returns aggregated structure ready for LLM prompt.
        """
        sym = _to_mexc_symbol(symbol)
        data = self._get(f"/api/v1/contract/deals/{sym}", params={"limit": limit})
        raw = data.get("data", [])
        # MEXC returns data as a list directly, or as dict with resultList
        if isinstance(raw, dict):
            trades = raw.get("resultList", [])
        elif isinstance(raw, list):
            trades = raw
        else:
            trades = []

        buy_vol = 0.0
        sell_vol = 0.0
        buy_count = 0
        sell_count = 0
        large_buys = []   # trades > avg size * 3
        large_sells = []

        parsed = []
        for t in trades:
            # MEXC Futures deals: short fields p=price, v=vol, T=takerSide, t=timestamp
            # T: 1=buy, 2=sell
            side = t.get("T", t.get("takerSide", t.get("side", 1)))
            try:
                is_buy = int(side) == 1
            except (ValueError, TypeError):
                is_buy = str(side).lower() in ("1", "buy", "bid")

            qty = float(t.get("v", t.get("vol", t.get("quantity", 0))))
            price = float(t.get("p", t.get("price", 0)))
            ts = int(t.get("t", t.get("time", t.get("timestamp", 0))))
            parsed.append({"price": price, "qty": qty, "is_buy": is_buy, "timestamp": ts})
            if is_buy:
                buy_vol += qty
                buy_count += 1
            else:
                sell_vol += qty
                sell_count += 1

        total_vol = buy_vol + sell_vol
        avg_size = total_vol / len(parsed) if parsed else 0

        for t in parsed:
            if t["qty"] > avg_size * 3:
                if t["is_buy"]:
                    large_buys.append({"price": t["price"], "qty": t["qty"]})
                else:
                    large_sells.append({"price": t["price"], "qty": t["qty"]})

        cvd = round(buy_vol - sell_vol, 2)  # positive = buy pressure
        buy_pct = round(buy_vol / total_vol * 100, 1) if total_vol > 0 else 50.0

        # Price range of recent trades
        prices = [t["price"] for t in parsed if t["price"] > 0]
        price_high = max(prices) if prices else 0
        price_low = min(prices) if prices else 0

        return {
            "symbol": symbol,
            "timestamp": int(time.time()),
            "trade_count": len(parsed),
            "buy_volume": round(buy_vol, 2),
            "sell_volume": round(sell_vol, 2),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "cvd": cvd,                      # cumulative volume delta
            "buy_pct": buy_pct,              # % of volume that was buys
            "large_buys": large_buys[:5],    # notable aggressive buys
            "large_sells": large_sells[:5],  # notable aggressive sells
            "price_high": price_high,
            "price_low": price_low,
        }

    def get_intraday_snapshot(self, symbol: str) -> dict:
        """
        Collect all market data needed for intraday analysis in one call.
        Timeframes: H4 (context), H1 (structure), M15 (entry), M5 (precision).
        Also includes orderbook and recent trades aggregation.
        """
        from config.settings import INTRADAY_TIMEFRAMES, INTRADAY_CANDLE_LIMITS

        snapshot = {
            "symbol": symbol,
            "timestamp": int(time.time()),
            "candles": {},
            "ticker": None,
            "open_interest": None,
            "funding_rate": None,
            "long_short_ratio": None,
            "orderbook": None,
            "recent_trades": None,
        }

        for name, interval in INTRADAY_TIMEFRAMES.items():
            limit = INTRADAY_CANDLE_LIMITS.get(name, 100)
            try:
                snapshot["candles"][name] = self.get_candles(symbol, interval, limit=limit)
            except Exception as e:
                snapshot["candles"][name] = []
                print(f"  [warn] intraday candles {name}: {e}")

        try:
            snapshot["ticker"] = self.get_ticker(symbol)
            snapshot["open_interest"] = {
                "symbol": symbol,
                "open_interest": snapshot["ticker"]["hold_vol"],
                "timestamp": snapshot["ticker"]["timestamp"],
            }
            snapshot["funding_rate"] = {
                "symbol": symbol,
                "funding_rate": snapshot["ticker"]["funding_rate"],
                "timestamp": snapshot["ticker"]["timestamp"],
            }
        except Exception as e:
            print(f"  [warn] intraday ticker: {e}")

        try:
            snapshot["long_short_ratio"] = self.get_long_short_ratio(symbol, period="15m")
        except Exception as e:
            print(f"  [warn] intraday long_short_ratio: {e}")

        try:
            snapshot["orderbook"] = self.get_orderbook(symbol, depth=20)
        except Exception as e:
            print(f"  [warn] intraday orderbook: {e}")

        try:
            snapshot["recent_trades"] = self.get_recent_trades(symbol, limit=100)
        except Exception as e:
            print(f"  [warn] intraday recent_trades: {e}")

        return snapshot

    def get_historical_candles(
        self,
        symbol: str,
        interval: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[dict]:
        """
        Fetch historical candles between two dates (handles pagination).
        Used for backtest data loading.
        """
        start_ts = int(date_from.timestamp())
        end_ts = int(date_to.timestamp())
        all_candles = []
        current_start = start_ts

        while current_start < end_ts:
            batch = self.get_candles(
                symbol, interval, limit=500, start=current_start, end=end_ts
            )
            if not batch:
                break
            all_candles.extend(batch)
            last_ts = batch[-1]["timestamp"]
            # If batch has fewer than 500 candles — we got everything
            if len(batch) < 500 or last_ts >= end_ts:
                break
            if last_ts <= current_start:
                break
            current_start = last_ts + 1
            time.sleep(0.2)

        seen = set()
        unique = []
        for c in all_candles:
            if c["timestamp"] not in seen:
                seen.add(c["timestamp"])
                unique.append(c)

        return sorted(unique, key=lambda x: x["timestamp"])
