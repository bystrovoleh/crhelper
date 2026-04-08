"""
MEXC Futures private API client for order management.
Handles authentication, order placement, position checks, and cancellation.

MEXC Futures API docs: https://mexcdevelop.github.io/apidocs/contract_v1_en/
"""

import hashlib
import hmac
import json
import time
import requests
import urllib3
from datetime import datetime, timezone
from config.settings import MEXC_API_KEY, MEXC_API_SECRET, MEXC_BASE_URL

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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
        self.session = requests.Session()
        self.session.verify = False  # workaround for TLSV1_ALERT_INTERNAL_ERROR on some systems
        self.session.headers.update({
            "Content-Type": "application/json",
            "ApiKey": self.api_key,
        })

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

    def _get(self, endpoint: str, params: dict = None) -> dict:
        params = params or {}
        ts = int(time.time() * 1000)
        # For GET: params_str is sorted query string
        params_str = "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
        headers = self._signed_headers(ts, params_str)

        url = f"{self.base_url}{endpoint}"
        resp = self.session.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # MEXC sometimes returns a list directly instead of {"success": true, "data": [...]}
        if isinstance(data, list):
            return {"success": True, "data": data}
        if not data.get("success", True):
            raise ValueError(f"MEXC API error: {data.get('code')} — {data.get('message')}")
        return data

    def _post(self, endpoint: str, body: dict = None) -> dict:
        body = body or {}
        ts = int(time.time() * 1000)
        # MEXC POST: sign sorted JSON string, send as raw string body
        body_str = json.dumps(body, sort_keys=True, separators=(",", ":")) if body else ""
        headers = self._signed_headers(ts, body_str)

        url = f"{self.base_url}{endpoint}"
        resp = self.session.post(url, data=body_str, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success", True):
            raise ValueError(f"MEXC API error: {data.get('code')} — {data.get('message')}")
        return data

    def _delete(self, endpoint: str, params: dict = None) -> dict:
        params = params or {}
        ts = int(time.time() * 1000)
        params_str = "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
        headers = self._signed_headers(ts, params_str)

        url = f"{self.base_url}{endpoint}"
        resp = self.session.delete(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
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
                # availableOpen = what can actually be used for new positions
                val = float(asset.get("availableOpen", asset.get("availableBalance", 0)))
                return val
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
            if p.get("symbol") == sym and float(p.get("vol", 0)) > 0:
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

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel a single order by ID."""
        try:
            sym = _to_mexc_symbol(symbol)
            self._delete(f"/api/v1/private/order/cancel/{order_id}")
            print(f"  [trader] Cancelled order {order_id} for {sym}")
            return True
        except Exception as e:
            print(f"  [trader] Failed to cancel order {order_id}: {e}")
            return False

    def cancel_all_orders(self, symbol: str) -> int:
        """Cancel all open orders for a symbol. Returns count cancelled."""
        orders = self.get_open_orders(symbol)
        cancelled = 0
        for order in orders:
            order_id = order.get("orderId") or order.get("id")
            if order_id and self.cancel_order(str(order_id), symbol):
                cancelled += 1
        return cancelled

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
            data = self._post("/api/v1/private/order/submit", body)
            order_id = data.get("data")
            print(f"  [trader] ✓ Placed {side} limit @ {price}  vol={vol}  sl={sl}  tp={tp}  id={order_id}")
            return {"order_id": order_id, "symbol": sym, "side": side, "price": price, "vol": vol}
        except Exception as e:
            print(f"  [trader] ✗ Failed to place order: {e}")
            return None

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

    def calc_vol(self, symbol: str, price: float, usdt_amount: float) -> int:
        """
        Calculate contract volume (integer) from USDT amount.
        MEXC: vol = floor(usdt_amount / (price * contractSize))
        Minimum is minVol (usually 1).
        Returns 0 if below minimum.
        """
        if price <= 0:
            return 0
        contract_size, min_vol = self.get_contract_size(symbol)
        usdt_per_contract = price * contract_size
        vol = int(usdt_amount / usdt_per_contract)
        if vol < min_vol:
            return 0
        return vol
