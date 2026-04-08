import numpy as np
from typing import Optional


def find_swing_highs_lows(candles: list[dict], lookback: int = 5) -> dict:
    """
    Find swing highs and lows from candle data.
    A swing high: high is higher than `lookback` candles on each side.
    A swing low: low is lower than `lookback` candles on each side.
    Returns recent levels sorted by significance (proximity to current price).
    """
    if len(candles) < lookback * 2 + 1:
        return {"swing_highs": [], "swing_lows": []}

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    times = [c["datetime"] for c in candles]

    swing_highs = []
    swing_lows = []

    for i in range(lookback, len(candles) - lookback):
        # swing high
        if highs[i] == max(highs[i - lookback: i + lookback + 1]):
            swing_highs.append({
                "price": highs[i],
                "datetime": times[i],
                "index": i,
            })
        # swing low
        if lows[i] == min(lows[i - lookback: i + lookback + 1]):
            swing_lows.append({
                "price": lows[i],
                "datetime": times[i],
                "index": i,
            })

    current_price = candles[-1]["close"]

    # Highs above current price (resistance), lows below (support)
    # Keep most recent ones — sorted by index descending, top 8
    swing_highs_above = sorted(
        [s for s in swing_highs if s["price"] > current_price],
        key=lambda x: x["index"], reverse=True
    )[:8]
    swing_lows_below = sorted(
        [s for s in swing_lows if s["price"] < current_price],
        key=lambda x: x["index"], reverse=True
    )[:8]

    # Also keep nearest highs below and lows above as context (inverted structure)
    swing_highs_below = sorted(
        [s for s in swing_highs if s["price"] <= current_price],
        key=lambda x: x["index"], reverse=True
    )[:3]
    swing_lows_above = sorted(
        [s for s in swing_lows if s["price"] >= current_price],
        key=lambda x: x["index"], reverse=True
    )[:3]

    return {
        "swing_highs": swing_highs_above + swing_highs_below,
        "swing_lows": swing_lows_below + swing_lows_above,
        "current_price": current_price,
    }


def build_volume_profile(candles: list[dict], bins: int = 30) -> dict:
    """
    Build a simplified volume profile (price histogram weighted by volume).
    Returns price levels sorted by volume — Point of Control (POC) and value area.
    """
    if not candles:
        return {"poc": None, "value_area_high": None, "value_area_low": None, "levels": []}

    all_highs = [c["high"] for c in candles]
    all_lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    price_min = min(all_lows)
    price_max = max(all_highs)
    if price_max == price_min:
        return {"poc": price_min, "value_area_high": price_max, "value_area_low": price_min, "levels": []}

    bin_size = (price_max - price_min) / bins
    profile = [0.0] * bins

    for i, candle in enumerate(candles):
        candle_range = candle["high"] - candle["low"]
        if candle_range == 0:
            continue
        # distribute volume proportionally across bins this candle touches
        for b in range(bins):
            bin_low = price_min + b * bin_size
            bin_high = bin_low + bin_size
            overlap = min(candle["high"], bin_high) - max(candle["low"], bin_low)
            if overlap > 0:
                profile[b] += volumes[i] * (overlap / candle_range)

    poc_bin = int(np.argmax(profile))
    poc_price = price_min + (poc_bin + 0.5) * bin_size

    # value area: 70% of total volume around POC
    total_volume = sum(profile)
    target = total_volume * 0.70
    accumulated = profile[poc_bin]
    low_bin = poc_bin
    high_bin = poc_bin

    while accumulated < target and (low_bin > 0 or high_bin < bins - 1):
        add_low = profile[low_bin - 1] if low_bin > 0 else 0
        add_high = profile[high_bin + 1] if high_bin < bins - 1 else 0
        if add_high >= add_low and high_bin < bins - 1:
            high_bin += 1
            accumulated += profile[high_bin]
        elif low_bin > 0:
            low_bin -= 1
            accumulated += profile[low_bin]
        else:
            break

    levels = []
    for b in range(bins):
        levels.append({
            "price": round(price_min + (b + 0.5) * bin_size, 4),
            "volume": round(profile[b], 2),
        })
    levels = sorted(levels, key=lambda x: x["volume"], reverse=True)

    return {
        "poc": round(poc_price, 4),
        "value_area_high": round(price_min + (high_bin + 1) * bin_size, 4),
        "value_area_low": round(price_min + low_bin * bin_size, 4),
        "top_levels": levels[:10],  # top 10 high-volume levels
    }


def determine_trend(candles_daily: list[dict], candles_weekly: list[dict]) -> dict:
    """
    Determine overall market trend using daily and weekly candles.
    Uses simple moving averages and higher highs/lower lows structure.
    """
    result = {
        "daily_trend": "neutral",
        "weekly_trend": "neutral",
        "overall_trend": "neutral",
        "daily_ma50": None,
        "daily_ma200": None,
        "weekly_ma20": None,
    }

    def calc_ma(candles: list[dict], period: int) -> Optional[float]:
        closes = [c["close"] for c in candles]
        if len(closes) < period:
            return None
        return round(sum(closes[-period:]) / period, 4)

    def structure_trend(candles: list[dict], lookback: int = 5) -> str:
        if len(candles) < lookback * 3:
            return "neutral"
        recent = candles[-lookback * 3:]
        highs = [c["high"] for c in recent]
        lows = [c["low"] for c in recent]
        mid = len(highs) // 2
        if highs[-1] > highs[mid] > highs[0] and lows[-1] > lows[mid] > lows[0]:
            return "bullish"
        if highs[-1] < highs[mid] < highs[0] and lows[-1] < lows[mid] < lows[0]:
            return "bearish"
        return "neutral"

    if candles_daily:
        result["daily_ma50"] = calc_ma(candles_daily, 50)
        result["daily_ma200"] = calc_ma(candles_daily, 200)
        result["daily_trend"] = structure_trend(candles_daily)

        current = candles_daily[-1]["close"]
        if result["daily_ma50"] and result["daily_ma200"]:
            if current > result["daily_ma50"] > result["daily_ma200"]:
                result["daily_trend"] = "bullish"
            elif current < result["daily_ma50"] < result["daily_ma200"]:
                result["daily_trend"] = "bearish"

    if candles_weekly:
        result["weekly_ma20"] = calc_ma(candles_weekly, 20)
        result["weekly_trend"] = structure_trend(candles_weekly, lookback=3)

    # overall: weekly takes priority
    if result["weekly_trend"] != "neutral":
        result["overall_trend"] = result["weekly_trend"]
    else:
        result["overall_trend"] = result["daily_trend"]

    # Sideways filter: if price is between MA50 and MA200, mark as neutral
    # TODO: this cuts too many signals in strong bull markets (2020-10→2021-04: 10→2 signals)
    # TODO: find a smarter condition — e.g. require BOTH MAs to be flat/converging, not just price between them
    if candles_daily:
        current = candles_daily[-1]["close"]
        ma50 = result.get("daily_ma50")
        ma200 = result.get("daily_ma200")
        if ma50 and ma200:
            price_between_mas = min(ma50, ma200) < current < max(ma50, ma200)
            if price_between_mas:
                result["overall_trend"] = "neutral"

    return result


def compute_atr(candles: list[dict], period: int = 14) -> dict:
    """
    Compute Average True Range (ATR) and derive volatility regime.
    Returns atr_value, atr_pct (% of price), regime, suggested_sl_buffer.
    """
    if len(candles) < period + 1:
        return {"atr_value": None, "atr_pct": None, "regime": "normal", "suggested_sl_buffer": 0.01}

    true_ranges = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    atr = sum(true_ranges[-period:]) / period
    current_price = candles[-1]["close"]
    atr_pct = (atr / current_price) * 100 if current_price else 0

    # Regime thresholds based on ATR % of price
    if atr_pct < 1.5:
        regime = "quiet"
        suggested_sl_buffer = 0.005   # 0.5%
    elif atr_pct < 3.5:
        regime = "normal"
        suggested_sl_buffer = 0.010   # 1.0%
    else:
        regime = "volatile"
        suggested_sl_buffer = 0.015   # 1.5%

    return {
        "atr_value": round(atr, 4),
        "atr_pct": round(atr_pct, 3),
        "regime": regime,
        "suggested_sl_buffer": suggested_sl_buffer,
    }


def compute_indicators(snapshot: dict) -> dict:
    """
    Compute all indicators from a market snapshot.
    Returns a clean dict ready to be passed to the agent.
    """
    candles_daily = snapshot["candles"].get("daily", [])
    candles_weekly = snapshot["candles"].get("weekly", [])
    candles_h4 = snapshot["candles"].get("h4", [])

    result = {
        "symbol": snapshot["symbol"],
        "trend": determine_trend(candles_daily, candles_weekly),
        "swing_levels": {
            "daily": find_swing_highs_lows(candles_daily, lookback=5) if candles_daily else {},
            "weekly": find_swing_highs_lows(candles_weekly, lookback=3) if candles_weekly else {},
        },
        "volume_profile": {
            "daily": build_volume_profile(candles_daily) if candles_daily else {},
            "weekly": build_volume_profile(candles_weekly) if candles_weekly else {},
        },
        "volatility": compute_atr(candles_daily, period=14),
        "open_interest": snapshot.get("open_interest"),
        "funding_rate": snapshot.get("funding_rate"),
        "long_short_ratio": snapshot.get("long_short_ratio"),
        "ticker": snapshot.get("ticker"),
    }

    return result
