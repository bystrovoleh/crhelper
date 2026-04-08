"""
Intraday-specific technical indicators.
Operates on H4/H1/M15/M5 candles.
Does NOT modify data/indicators.py — fully isolated.
"""

from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

def compute_vwap(candles_h1: list[dict]) -> dict:
    """
    Compute VWAP from the start of the current UTC day using H1 candles.
    Returns VWAP value and deviation bands (+1/-1 std dev).
    """
    if not candles_h1:
        return {"vwap": None, "upper_band": None, "lower_band": None, "price_vs_vwap": None}

    now_utc = datetime.now(tz=timezone.utc)
    day_start_ts = int(datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc).timestamp())

    today_candles = [c for c in candles_h1 if c["timestamp"] >= day_start_ts]
    if not today_candles:
        today_candles = candles_h1[-8:]  # fallback: last 8 candles

    cum_pv = 0.0
    cum_vol = 0.0
    cum_pv2 = 0.0

    for c in today_candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        vol = c["volume"]
        cum_pv += typical * vol
        cum_vol += vol
        cum_pv2 += typical ** 2 * vol

    if cum_vol == 0:
        return {"vwap": None, "upper_band": None, "lower_band": None, "price_vs_vwap": None}

    vwap = cum_pv / cum_vol
    variance = (cum_pv2 / cum_vol) - vwap ** 2
    std_dev = variance ** 0.5 if variance > 0 else 0

    current_price = today_candles[-1]["close"]
    price_vs_vwap = round((current_price - vwap) / vwap * 100, 3)

    return {
        "vwap": round(vwap, 6),
        "upper_band": round(vwap + std_dev, 6),
        "lower_band": round(vwap - std_dev, 6),
        "std_dev": round(std_dev, 6),
        "price_vs_vwap_pct": price_vs_vwap,   # positive = above VWAP
        "bias": "above" if price_vs_vwap > 0 else "below",
    }


# ---------------------------------------------------------------------------
# Session Levels
# ---------------------------------------------------------------------------

SESSIONS = {
    "asia":    {"open_h": 0,  "close_h": 8},   # UTC
    "europe":  {"open_h": 7,  "close_h": 16},
    "us":      {"open_h": 13, "close_h": 22},
}


def get_session_levels(candles_h1: list[dict], snapshot_ts: int = None) -> dict:
    """
    Extract High/Low for Asian, European, and US sessions from H1 candles.
    Also identifies current active session and Asian range size.
    snapshot_ts: unix timestamp of the snapshot (used in backtest to get correct session).
                 Defaults to now if not provided.
    """
    if not candles_h1:
        return {}

    if snapshot_ts:
        ref_dt = datetime.fromtimestamp(snapshot_ts, tz=timezone.utc)
    else:
        ref_dt = datetime.now(tz=timezone.utc)
    current_hour = ref_dt.hour

    # Determine current session — overlap checked first, then individual sessions
    in_asia   = SESSIONS["asia"]["open_h"]   <= current_hour < SESSIONS["asia"]["close_h"]
    in_europe = SESSIONS["europe"]["open_h"] <= current_hour < SESSIONS["europe"]["close_h"]
    in_us     = SESSIONS["us"]["open_h"]     <= current_hour < SESSIONS["us"]["close_h"]

    if in_europe and in_us:
        current_session = "overlap_eu_us"
    elif in_asia:
        current_session = "asia"
    elif in_europe:
        current_session = "europe"
    elif in_us:
        current_session = "us"
    else:
        current_session = "off"

    result = {"current_session": current_session}

    # Look back 48h to find session levels
    for sess_name, sess in SESSIONS.items():
        sess_candles = []
        for c in candles_h1:
            dt = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc)
            if sess["open_h"] <= dt.hour < sess["close_h"]:
                sess_candles.append(c)

        # Use last full session (most recent)
        # Group by date and take the last complete one
        by_date: dict = {}
        for c in sess_candles:
            dt = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc)
            day_key = dt.date()
            if day_key not in by_date:
                by_date[day_key] = []
            by_date[day_key].append(c)

        if not by_date:
            result[f"{sess_name}_high"] = None
            result[f"{sess_name}_low"] = None
            continue

        # Pick latest date with enough candles
        sorted_dates = sorted(by_date.keys(), reverse=True)
        target_candles = None
        for d in sorted_dates:
            if len(by_date[d]) >= 3:
                target_candles = by_date[d]
                break
        if target_candles is None:
            target_candles = by_date[sorted_dates[0]]

        sess_high = max(c["high"] for c in target_candles)
        sess_low = min(c["low"] for c in target_candles)
        result[f"{sess_name}_high"] = round(sess_high, 6)
        result[f"{sess_name}_low"] = round(sess_low, 6)

    # Asian range size (key for EU/US breakout trades)
    asia_h = result.get("asia_high")
    asia_l = result.get("asia_low")
    if asia_h and asia_l and asia_l > 0:
        result["asian_range_pct"] = round((asia_h - asia_l) / asia_l * 100, 3)
    else:
        result["asian_range_pct"] = None

    return result


# ---------------------------------------------------------------------------
# Relative Volume (RVOL)
# ---------------------------------------------------------------------------

def compute_rvol(candles_m15: list[dict], lookback_days: int = 5) -> dict:
    """
    Relative Volume: current candle volume vs average volume at same time-of-day
    over the past N days using M15 candles.
    RVOL > 1.5 = confirmed move, < 0.7 = low conviction / possible fake breakout.
    """
    if not candles_m15 or len(candles_m15) < 4:
        return {"rvol": None, "current_volume": None, "avg_volume": None}

    current = candles_m15[-1]
    current_vol = current["volume"]
    current_dt = datetime.fromtimestamp(current["timestamp"], tz=timezone.utc)
    current_slot = current_dt.hour * 4 + current_dt.minute // 15  # 0..95

    # Find candles at same time slot in prior days
    same_slot_vols = []
    for c in candles_m15[:-1]:
        dt = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc)
        slot = dt.hour * 4 + dt.minute // 15
        if slot == current_slot:
            same_slot_vols.append(c["volume"])

    same_slot_vols = same_slot_vols[-lookback_days:]

    if not same_slot_vols:
        return {"rvol": None, "current_volume": current_vol, "avg_volume": None}

    avg_vol = sum(same_slot_vols) / len(same_slot_vols)
    rvol = round(current_vol / avg_vol, 2) if avg_vol > 0 else None

    label = "normal"
    if rvol is not None:
        if rvol >= 2.0:
            label = "very_high"
        elif rvol >= 1.5:
            label = "high"
        elif rvol < 0.5:
            label = "very_low"
        elif rvol < 0.7:
            label = "low"

    return {
        "rvol": rvol,
        "rvol_label": label,
        "current_volume": round(current_vol, 2),
        "avg_volume": round(avg_vol, 2),
    }


# ---------------------------------------------------------------------------
# Intraday ATR (M15-based)
# ---------------------------------------------------------------------------

def compute_intraday_atr(candles_m15: list[dict], period: int = 14) -> dict:
    """
    ATR on M15 candles. Used for precise SL sizing on intraday entries.
    Regime thresholds are tighter than daily ATR.
    """
    if len(candles_m15) < period + 1:
        return {"atr": None, "atr_pct": None, "regime": "unknown", "sl_buffer_pct": 0.2}

    trs = []
    for i in range(1, len(candles_m15)):
        high = candles_m15[i]["high"]
        low = candles_m15[i]["low"]
        prev_close = candles_m15[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    atr = sum(trs[-period:]) / period
    last_close = candles_m15[-1]["close"]
    atr_pct = round(atr / last_close * 100, 3) if last_close > 0 else 0

    # Regime thresholds for M15 ATR
    if atr_pct < 0.15:
        regime = "quiet"
        sl_buffer = 0.10
    elif atr_pct < 0.40:
        regime = "normal"
        sl_buffer = 0.20
    else:
        regime = "volatile"
        sl_buffer = 0.35

    return {
        "atr": round(atr, 6),
        "atr_pct": atr_pct,
        "regime": regime,
        "sl_buffer_pct": sl_buffer,
    }


# ---------------------------------------------------------------------------
# Swing Highs/Lows on M15
# ---------------------------------------------------------------------------

def find_intraday_swings(candles_m15: list[dict], lookback: int = 3) -> dict:
    """
    Find recent swing highs/lows on M15 timeframe.
    lookback: candles on each side to confirm a swing.
    """
    if len(candles_m15) < lookback * 2 + 1:
        return {"swing_highs": [], "swing_lows": []}

    swing_highs = []
    swing_lows = []

    for i in range(lookback, len(candles_m15) - lookback):
        c = candles_m15[i]
        is_high = all(c["high"] >= candles_m15[i - j]["high"] for j in range(1, lookback + 1)) and \
                  all(c["high"] >= candles_m15[i + j]["high"] for j in range(1, lookback + 1))
        is_low = all(c["low"] <= candles_m15[i - j]["low"] for j in range(1, lookback + 1)) and \
                 all(c["low"] <= candles_m15[i + j]["low"] for j in range(1, lookback + 1))

        if is_high:
            swing_highs.append({"price": round(c["high"], 6), "datetime": c["datetime"]})
        if is_low:
            swing_lows.append({"price": round(c["low"], 6), "datetime": c["datetime"]})

    current_price = candles_m15[-1]["close"]

    # Keep most recent 6 of each, sorted by proximity
    swing_highs = sorted(swing_highs, key=lambda x: abs(x["price"] - current_price))[:6]
    swing_lows = sorted(swing_lows, key=lambda x: abs(x["price"] - current_price))[:6]

    return {
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "current_price": current_price,
    }


# ---------------------------------------------------------------------------
# OI Delta (change rate)
# ---------------------------------------------------------------------------

def compute_oi_delta(ticker_now: dict, candles_h1: list[dict]) -> dict:
    """
    Estimate OI change direction from H1 candle volume trend.
    Real OI delta requires two OI snapshots; here we proxy from volume acceleration.
    """
    if not candles_h1 or len(candles_h1) < 6:
        return {"oi_delta": "unknown", "oi_trend": "unknown"}

    recent_vols = [c["volume"] for c in candles_h1[-3:]]
    prev_vols = [c["volume"] for c in candles_h1[-6:-3]]

    avg_recent = sum(recent_vols) / 3
    avg_prev = sum(prev_vols) / 3

    if avg_prev == 0:
        return {"oi_delta": "unknown", "oi_trend": "flat"}

    ratio = avg_recent / avg_prev

    if ratio > 1.3:
        trend = "increasing"
    elif ratio < 0.7:
        trend = "decreasing"
    else:
        trend = "flat"

    return {
        "oi_delta_proxy": round(ratio, 2),  # >1 = more activity, <1 = less
        "oi_trend": trend,
    }


# ---------------------------------------------------------------------------
# Volume Profile on H1 (intraday POC)
# ---------------------------------------------------------------------------

def build_intraday_volume_profile(candles_h1: list[dict], bins: int = 20) -> dict:
    """
    Build volume profile on last N H1 candles to find intraday POC and value area.
    """
    if not candles_h1:
        return {"poc": None, "vah": None, "val": None}

    # Use last 24 candles (1 day)
    candles = candles_h1[-24:]

    price_min = min(c["low"] for c in candles)
    price_max = max(c["high"] for c in candles)

    if price_max == price_min:
        return {"poc": round(price_min, 6), "vah": round(price_max, 6), "val": round(price_min, 6)}

    bin_size = (price_max - price_min) / bins
    profile = [0.0] * bins

    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        vol = c["volume"]
        # Distribute volume proportionally across bins touched by candle
        for b in range(bins):
            bin_low = price_min + b * bin_size
            bin_high = bin_low + bin_size
            overlap = max(0, min(c["high"], bin_high) - max(c["low"], bin_low))
            candle_range = c["high"] - c["low"] if c["high"] != c["low"] else bin_size
            profile[b] += vol * (overlap / candle_range)

    total_vol = sum(profile)
    poc_idx = profile.index(max(profile))
    poc = price_min + (poc_idx + 0.5) * bin_size

    # Value area: 70% of volume
    sorted_bins = sorted(range(bins), key=lambda i: profile[i], reverse=True)
    va_vol = 0.0
    va_bins = []
    for idx in sorted_bins:
        va_vol += profile[idx]
        va_bins.append(idx)
        if va_vol >= total_vol * 0.7:
            break

    vah = price_min + (max(va_bins) + 1) * bin_size
    val = price_min + min(va_bins) * bin_size

    return {
        "poc": round(poc, 6),
        "vah": round(vah, 6),
        "val": round(val, 6),
    }


# ---------------------------------------------------------------------------
# Orderbook Analysis (pre-processed for LLM)
# ---------------------------------------------------------------------------

def analyze_orderbook(orderbook: Optional[dict]) -> dict:
    """
    Translate raw orderbook data into LLM-friendly analysis summary.
    """
    if not orderbook:
        return {"summary": "no orderbook data"}

    imbalance = orderbook.get("imbalance", 0.5)
    bid_walls = orderbook.get("bid_walls", [])
    ask_walls = orderbook.get("ask_walls", [])
    spread_pct = orderbook.get("spread_pct", 0)

    if imbalance > 0.65:
        pressure = "strong bid pressure (buyers dominating)"
    elif imbalance > 0.55:
        pressure = "mild bid pressure"
    elif imbalance < 0.35:
        pressure = "strong ask pressure (sellers dominating)"
    elif imbalance < 0.45:
        pressure = "mild ask pressure"
    else:
        pressure = "balanced order flow"

    bid_wall_prices = [f"{w['price']}" for w in bid_walls[:3]]
    ask_wall_prices = [f"{w['price']}" for w in ask_walls[:3]]

    return {
        "imbalance": imbalance,
        "pressure": pressure,
        "bid_walls": bid_wall_prices,   # support walls
        "ask_walls": ask_wall_prices,   # resistance walls
        "spread_pct": spread_pct,
        "spread_label": "wide" if spread_pct > 0.05 else "tight",
    }


# ---------------------------------------------------------------------------
# Recent Trades Analysis (pre-processed for LLM)
# ---------------------------------------------------------------------------

def analyze_trades(recent_trades: Optional[dict]) -> dict:
    """
    Translate raw trade aggregation into LLM-friendly CVD + pressure summary.
    """
    if not recent_trades:
        return {"summary": "no trades data"}

    cvd = recent_trades.get("cvd", 0)
    buy_pct = recent_trades.get("buy_pct", 50)
    large_buys = recent_trades.get("large_buys", [])
    large_sells = recent_trades.get("large_sells", [])
    trade_count = recent_trades.get("trade_count", 0)

    if buy_pct >= 65:
        aggression = "aggressive buying"
    elif buy_pct >= 55:
        aggression = "mild buying pressure"
    elif buy_pct <= 35:
        aggression = "aggressive selling"
    elif buy_pct <= 45:
        aggression = "mild selling pressure"
    else:
        aggression = "balanced"

    cvd_label = "positive (net buying)" if cvd > 0 else ("negative (net selling)" if cvd < 0 else "neutral")

    return {
        "cvd": cvd,
        "cvd_label": cvd_label,
        "buy_pct": buy_pct,
        "aggression": aggression,
        "large_buy_count": len(large_buys),
        "large_sell_count": len(large_sells),
        "large_buys": [f"{b['price']} x{b['qty']}" for b in large_buys[:3]],
        "large_sells": [f"{s['price']} x{s['qty']}" for s in large_sells[:3]],
        "trade_count": trade_count,
    }


# ---------------------------------------------------------------------------
# Master: compute all intraday indicators
# ---------------------------------------------------------------------------

def compute_intraday_indicators(snapshot: dict) -> dict:
    """
    Orchestrates all intraday indicator calculations from a snapshot.
    Returns unified dict ready for agent prompts.
    """
    candles = snapshot.get("candles", {})
    h4 = candles.get("h4", [])
    h1 = candles.get("h1", [])
    m15 = candles.get("m15", [])

    ticker = snapshot.get("ticker", {}) or {}
    # Resolve current price: ticker → M15 → H1 → H4 (robust for backtest)
    current_price = ticker.get("last_price") or 0
    if not current_price:
        for tf_candles in (m15, h1, h4):
            if tf_candles:
                current_price = tf_candles[-1]["close"]
                break

    indicators = {
        "symbol": snapshot.get("symbol"),
        "current_price": current_price,
        "timestamp": snapshot.get("timestamp"),
    }

    # VWAP (H1-based, from start of UTC day)
    indicators["vwap"] = compute_vwap(h1)

    # Session levels — pass snapshot timestamp so backtest gets correct session
    indicators["sessions"] = get_session_levels(h1, snapshot_ts=snapshot.get("timestamp"))

    # Intraday swings on M15
    indicators["swings"] = find_intraday_swings(m15, lookback=3)

    # M15 ATR (SL sizing)
    indicators["atr"] = compute_intraday_atr(m15, period=14)

    # Relative volume on M15
    indicators["rvol"] = compute_rvol(m15, lookback_days=5)

    # Intraday volume profile (H1 last 24h)
    indicators["volume_profile"] = build_intraday_volume_profile(h1, bins=20)

    # OI delta proxy
    indicators["oi_delta"] = compute_oi_delta(ticker, h1)

    # Orderbook analysis
    indicators["orderbook"] = analyze_orderbook(snapshot.get("orderbook"))

    # Trades / CVD analysis
    indicators["trades"] = analyze_trades(snapshot.get("recent_trades"))

    # Market sentiment from ticker
    funding = snapshot.get("funding_rate", {}) or {}
    ls = snapshot.get("long_short_ratio", {}) or {}
    indicators["sentiment"] = {
        "funding_rate": funding.get("funding_rate", 0),
        "long_ratio": ls.get("long_ratio", 0.5),
        "short_ratio": ls.get("short_ratio", 0.5),
        "funding_bias": "longs paying" if (funding.get("funding_rate", 0) or 0) > 0 else "shorts paying",
    }

    # H4 context: last few candles for trend health check
    if h4:
        last_h4 = h4[-6:]
        h4_highs = [c["high"] for c in last_h4]
        h4_lows = [c["low"] for c in last_h4]
        h4_closes = [c["close"] for c in last_h4]
        indicators["h4_context"] = {
            "last_close": h4_closes[-1] if h4_closes else None,
            "range_high": max(h4_highs) if h4_highs else None,
            "range_low": min(h4_lows) if h4_lows else None,
            "candles": last_h4,
        }

    return indicators
