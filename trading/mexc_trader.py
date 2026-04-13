"""
MEXC Futures private API client for order management.
Handles authentication, order placement, position checks, and cancellation.

MEXC Futures API docs: https://mexcdevelop.github.io/apidocs/contract_v1_en/
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from config.settings import MEXC_API_KEY, MEXC_API_SECRET, MEXC_BASE_URL

from curl_cffi.requests import Session as CurlSession


def _to_mexc_symbol(symbol: str) -> str:
    """Convert BTCUSDT → BTC_USDT for MEXC Futures API."""
    symbol = symbol.upper()
    if "_" in symbol:
        return symbol
    if symbol.endswith("USDT"):
        return symbol[:-4] + "_USDT"
    return symbol


class MEXCTrader:
    """
    MEXC Futures private API client.
    Handles signed requests for order management and account queries.
    """

    def __init__(self):
        self.base_url = MEXC_BASE_URL
        self.api_key = MEXC_API_KEY
        self.api_secret = MEXC_API_SECRET
        # curl_cffi impersonates Chrome TLS fingerprint — bypasses Cloudflare WAF
        self.session = CurlSession(impersonate="chrome110")

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _sign(self, timestamp: int, params_str: str = "") -> str:
        """
        MEXC Futures signature format:
        sign = HMAC-SHA256(api_key + timestamp + params_str, secret)
        """
        message = self.api_key + str(timestamp) + params_str
        return hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _signed_headers(self, timestamp: int, params_str: str = "") -> dict:
        return {
            "ApiKey": self.api_key,
            "Request-Time": str(timestamp),
            "Signature": self._sign(timestamp, params_str),
            "Content-Type": "application/json",
        }

    def _get(self, endpoint: str, params: dict = None, _retry: int = 0) -> dict:
        params = params or {}
        ts = int(time.time() * 1000)
        params_str = "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
        headers = self._signed_headers(ts, params_str)

        url = f"{self.base_url}{endpoint}"
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            if _retry < 2:
                time.sleep(1)
                return self._get(endpoint, params, _retry + 1)
            raise
        if isinstance(data, list):
            return {"success": True, "data": data}
        if not data.get("success", True):
            raise ValueError(f"MEXC API error: {data.get('code')} — {data.get('message')}")
        return data

    def _post(self, endpoint: str, body: dict = None, _retry: int = 0) -> dict:
        body = body or {}
        ts = int(time.time() * 1000)
        body_str = json.dumps(body, sort_keys=True, separators=(",", ":")) if body else ""
        headers = self._signed_headers(ts, body_str)

        url = f"{self.base_url}{endpoint}"
        try:
            resp = self.session.post(url, data=body_str, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            if _retry < 2:
                time.sleep(1)
                return self._post(endpoint, body, _retry + 1)
            raise
        if not data.get("success", True):
            raise ValueError(f"MEXC API error: {data.get('code')} — {data.get('message')}")
        return data

    def _delete(self, endpoint: str, params: dict = None, _retry: int = 0) -> dict:
        params = params or {}
        ts = int(time.time() * 1000)
        params_str = "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
        headers = self._signed_headers(ts, params_str)

        url = f"{self.base_url}{endpoint}"
        try:
            resp = self.session.delete(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            if _retry < 2:
                time.sleep(1)
                return self._delete(endpoint, params, _retry + 1)
            raise
        if not data.get("success", True):
            raise ValueError(f"MEXC API error: {data.get('code')} — {data.get('message')}")
        return data

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_balance(self) -> float:
        """Return available USDT balance for opening positions."""
        data = self._get("/api/v1/private/account/assets")
        assets = data.get("data", [])
        for asset in assets:
            if asset.get("currency") == "USDT":
                val = float(asset.get("availableOpen", asset.get("availableBalance", 0)))
                return val
        return 0.0

    def get_equity(self) -> float:
        """Return total USDT equity (available + frozen margin in orders/positions)."""
        data = self._get("/api/v1/private/account/assets")
        assets = data.get("data", [])
        for asset in assets:
            if asset.get("currency") == "USDT":
                return float(asset.get("equity", asset.get("cashBalance", 0)))
        return 0.0

    # ------------------------------------------------------------------
    # Position queries
    # ------------------------------------------------------------------

    def get_open_positions(self, symbol: str = None) -> list[dict]:
        """
        Return list of open positions.
        If symbol given, filter to that symbol only.
        """
        params = {}
        if symbol:
            params["symbol"] = _to_mexc_symbol(symbol)
        data = self._get("/api/v1/private/position/open_positions", params)
        positions = data.get("data", []) or []
        return positions

    def has_open_position(self, symbol: str) -> bool:
        """True if there is an open position for this symbol."""
        positions = self.get_open_positions(symbol)
        sym = _to_mexc_symbol(symbol)
        for p in positions:
            if p.get("symbol") == sym and float(p.get("holdVol") or p.get("vol") or 0) > 0:
                return True
        return False

    # ------------------------------------------------------------------
    # Order queries
    # ------------------------------------------------------------------

    def get_open_orders(self, symbol: str) -> list[dict]:
        """Return list of open (pending) orders for a symbol."""
        sym = _to_mexc_symbol(symbol)
        data = self._get("/api/v1/private/order/list/open_orders/" + sym)
        raw = data.get("data", [])
        # MEXC returns either a list directly or {"resultList": [...]}
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return raw.get("resultList", []) or []
        return []

    def get_order_age_hours(self, order: dict) -> float:
        """Return how many hours ago the order was created."""
        create_time = order.get("createTime")
        if not create_time:
            return 0.0
        now_ms = int(time.time() * 1000)
        return (now_ms - int(create_time)) / 3_600_000

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def cancel_all_orders(self, symbol: str) -> int:
        """Cancel all open orders for a symbol. Returns count cancelled."""
        orders = self.get_open_orders(symbol)
        if not orders:
            return 0
        sym = _to_mexc_symbol(symbol)
        try:
            self._post("/api/v1/private/order/cancel_all", {"symbol": sym})
            print(f"  [trader] Cancelled all orders for {sym}")
            return len(orders)
        except Exception as e:
            print(f"  [trader] Failed to cancel_all for {sym}: {e}")
            return 0

    def set_leverage(self, symbol: str, leverage: int):
        """Set leverage for a symbol (both long and short sides)."""
        sym = _to_mexc_symbol(symbol)
        try:
            self._post("/api/v1/private/position/change_leverage", {
                "symbol": sym,
                "leverage": leverage,
                "openType": 1,  # 1 = isolated
                "positionType": 1,  # 1 = long side
            })
            self._post("/api/v1/private/position/change_leverage", {
                "symbol": sym,
                "leverage": leverage,
                "openType": 1,
                "positionType": 2,  # 2 = short side
            })
        except Exception as e:
            print(f"  [trader] Set leverage warning: {e}")

    def place_limit_order(
        self,
        symbol: str,
        side: str,          # "long" | "short"
        price: float,
        vol: float,         # contract quantity
        sl: float,
        tp: float,
        leverage: int = 10,
    ) -> dict | None:
        """
        Place a limit order with attached SL and TP.
        side: "long" → openType buy, "short" → openType sell
        vol: number of contracts (calculated from USDT amount / price)

        MEXC order sides:
          1 = Open Long
          2 = Close Short
          3 = Open Short
          4 = Close Long

        For limit entry:
          long  → side=1 (Open Long)
          short → side=3 (Open Short)
        """
        sym = _to_mexc_symbol(symbol)
        order_side = 1 if side == "long" else 3

        body = {
            "symbol": sym,
            "price": str(price),
            "vol": str(vol),
            "side": order_side,
            "type": 1,          # 1 = limit order
            "openType": 1,      # 1 = isolated margin
            "leverage": leverage,
        }

        # Attach stop loss
        if sl:
            if side == "long":
                body["stopLossPrice"] = str(sl)
            else:
                body["stopLossPrice"] = str(sl)

        # Attach take profit
        if tp:
            body["takeProfitPrice"] = str(tp)

        try:
            data = self._post("/api/v1/private/order/create", body)
            order_id = data.get("data")
            print(f"  [trader] ✓ Placed {side} limit @ {price}  vol={vol}  sl={sl}  tp={tp}  id={order_id}")
            return {"order_id": order_id, "symbol": sym, "side": side, "price": price, "vol": vol}
        except Exception as e:
            print(f"  [trader] ✗ Failed to place order: {e}")
            return None

    def close_position_limit(self, symbol: str, direction: str) -> dict | None:
        """
        Close an open position with a limit order at current best price.

        direction: direction of the EXISTING position ("long" | "short")
          - closing a long  → side=4 (Close Long)
          - closing a short → side=2 (Close Short)

        Fetches current bid/ask from ticker and places a limit order
        slightly inside the spread to get fast fill:
          - closing long  → sell at current bid
          - closing short → buy  at current ask
        """
        sym = _to_mexc_symbol(symbol)

        # Get current position volume
        positions = self.get_open_positions(symbol)
        vol = 0
        for p in positions:
            if p.get("symbol") == sym and float(p.get("holdVol") or p.get("vol") or 0) > 0:
                vol = int(float(p.get("holdVol") or p.get("vol") or 0))
                break

        if vol <= 0:
            print(f"  [trader] close_position_limit: no open position found for {sym}")
            return None

        # Get current ticker price
        try:
            resp = self.session.get(
                f"{self.base_url}/api/v1/contract/ticker",
                params={"symbol": sym},
                timeout=10,
            )
            resp.raise_for_status()
            ticker = resp.json().get("data", {})
            # Use lastPrice as limit price — gets filled immediately if market moves to it
            price = float(ticker.get("lastPrice", 0))
        except Exception as e:
            print(f"  [trader] Failed to get ticker for {sym}: {e}")
            return None

        if price <= 0:
            print(f"  [trader] Invalid price {price} for {sym}")
            return None

        # side=4 closes long, side=2 closes short
        order_side = 4 if direction == "long" else 2

        body = {
            "symbol": sym,
            "price": str(price),
            "vol": str(vol),
            "side": order_side,
            "type": 1,       # 1 = limit order
            "openType": 1,   # 1 = isolated margin
        }

        try:
            data = self._post("/api/v1/private/order/create", body)
            order_id = data.get("data")
            print(f"  [trader] ✓ Close {direction} limit @ {price}  vol={vol}  id={order_id}")
            return {"order_id": order_id, "symbol": sym, "direction": direction, "price": price, "vol": vol}
        except Exception as e:
            print(f"  [trader] ✗ Failed to close position: {e}")
            return None

    def get_stop_orders(self, symbol: str) -> list[dict]:
        """Return open stop orders (SL/TP) for a symbol."""
        sym = _to_mexc_symbol(symbol)
        try:
            data = self._get("/api/v1/private/stoporder/list/orders", {"symbol": sym, "isFinished": 0})
            raw = data.get("data", [])
            if isinstance(raw, list):
                return raw
            if isinstance(raw, dict):
                return raw.get("resultList", []) or []
        except Exception as e:
            print(f"  [trader] get_stop_orders warning: {e}")
        return []

    def change_position_sl(self, symbol: str, position_type: int, new_sl: float) -> bool:
        """
        Change stop loss on an open position.
        position_type: 1 = long, 2 = short
        Finds the matching stop order by positionType and updates its SL.
        Returns True on success.
        """
        sym = _to_mexc_symbol(symbol)
        stop_orders = self.get_stop_orders(symbol)

        # Find the stop order matching this position side
        order_id = None
        for o in stop_orders:
            if int(o.get("positionType", 0)) == position_type:
                order_id = o.get("id") or o.get("orderId")
                break

        if not order_id:
            # No existing stop order — create a new one via trigger order
            # side: closing long = 4, closing short = 2
            close_side = 4 if position_type == 1 else 2
            try:
                self._post("/api/v1/private/stoporder/create", {
                    "symbol": sym,
                    "side": close_side,
                    "stopLossPrice": str(new_sl),
                    "openType": 1,
                    "vol": 0,           # 0 = close entire position
                    "positionType": position_type,
                })
                print(f"  [trader] ✓ Created new SL @ {new_sl} for {sym} pos_type={position_type}")
                return True
            except Exception as e:
                print(f"  [trader] ✗ Failed to create SL: {e}")
                return False

        # Update existing stop order
        try:
            self._post("/api/v1/private/stoporder/change_price", {
                "orderId": str(order_id),
                "stopLossPrice": str(new_sl),
            })
            print(f"  [trader] ✓ Updated SL → {new_sl} for {sym} order {order_id}")
            return True
        except Exception as e:
            print(f"  [trader] ✗ Failed to change SL: {e}")
            return False

    def get_contract_size(self, symbol: str) -> tuple[float, int]:
        """
        Return (contractSize, minVol) for a symbol.
        contractSize = how much base asset per 1 contract.
        minVol = minimum number of contracts per order.
        """
        sym = _to_mexc_symbol(symbol)
        try:
            resp = self.session.get(
                f"{self.base_url}/api/v1/contract/detail",
                params={"symbol": sym},
                timeout=10,
            )
            resp.raise_for_status()
            d = resp.json().get("data", {})
            contract_size = float(d.get("contractSize", 1))
            min_vol = int(d.get("minVol", 1))
            return contract_size, min_vol
        except Exception as e:
            print(f"  [trader] get_contract_size warning: {e}")
            return 1.0, 1

    def calc_vol(self, symbol: str, price: float, usdt_amount: float, leverage: int = 10) -> int:
        """
        Calculate contract volume (integer) from USDT margin amount.
        MEXC: vol = floor(margin * leverage / (price * contractSize))
        usdt_amount is treated as margin (collateral), not notional.
        Minimum is minVol (usually 1). Returns 0 if below minimum.
        """
        if price <= 0:
            return 0
        contract_size, min_vol = self.get_contract_size(symbol)
        usdt_per_contract = price * contract_size
        vol = int((usdt_amount * leverage) / usdt_per_contract)
        if vol < min_vol:
            return 0
        return vol
