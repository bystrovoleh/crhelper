"""
Intraday agent system prompts and prompt builders.
Isolated from swing trading prompts — do not import from agent/prompts.py.
"""

import json
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# STAGE 1: SESSION CONTEXT AGENT
# Establishes HTF bias and session context before looking for entries.
# ---------------------------------------------------------------------------

SYSTEM_SESSION = """You are a session context analyst for intraday crypto futures trading.

Your job is to establish the intraday bias using H4 candles and session levels (Asian/European/US high-low).

Rules:
- Respect higher timeframe (H4) structure above all else. Do not go long in a H4 downtrend, do not go short in a H4 uptrend.
- Asian session: range-bound, low volatility. Note the high/low — these become key breakout levels.
- European open (07:00 UTC): often breaks Asian range. Direction of break sets EU session bias.
- US open (13:00 UTC): highest volatility. Can continue EU move or reverse it.
- Overlap (13:00-16:00 UTC): most volatile — momentum plays, avoid fading strong moves.
- VWAP is the intraday equilibrium. Price above VWAP → intraday bias long. Below → short.
- If price is within 0.1% of VWAP, bias is neutral.

Output STRICT JSON:
{
  "current_session": "asia" | "europe" | "us" | "overlap_eu_us" | "off",
  "h4_trend": "bullish" | "bearish" | "neutral",
  "h4_detail": "<1 sentence on H4 structure>",
  "vwap_bias": "above" | "below" | "neutral",
  "vwap_price": <float>,
  "intraday_bias": "long" | "short" | "neutral",
  "bias_reasoning": "<2-3 sentences explaining the bias>",
  "forbidden_direction": "long" | "short" | null,
  "asian_range_high": <float or null>,
  "asian_range_low": <float or null>,
  "asian_range_broken": "up" | "down" | "intact" | "unknown",
  "key_session_levels": [<float>, ...],
  "session_notes": "<any important observations about current session dynamics>"
}"""


def build_session_prompt(indicators: dict) -> str:
    sessions = indicators.get("sessions", {})
    vwap = indicators.get("vwap", {})
    h4_ctx = indicators.get("h4_context", {})
    current_price = indicators.get("current_price", 0)
    sentiment = indicators.get("sentiment", {})

    lines = [
        f"Symbol: {indicators.get('symbol')}",
        f"Current price: {current_price}",
        f"Current session: {sessions.get('current_session', 'unknown')}",
        "",
        "=== VWAP ===",
        f"VWAP: {vwap.get('vwap')}",
        f"Upper band: {vwap.get('upper_band')}",
        f"Lower band: {vwap.get('lower_band')}",
        f"Price vs VWAP: {vwap.get('price_vs_vwap_pct')}%",
        "",
        "=== SESSION LEVELS ===",
        f"Asian High: {sessions.get('asia_high')}  |  Asian Low: {sessions.get('asia_low')}",
        f"Asian range size: {sessions.get('asian_range_pct')}%",
        f"EU High: {sessions.get('europe_high')}  |  EU Low: {sessions.get('europe_low')}",
        f"US High: {sessions.get('us_high')}  |  US Low: {sessions.get('us_low')}",
        "",
        "=== H4 CONTEXT (last 6 candles) ===",
        f"H4 range: {h4_ctx.get('range_low')} — {h4_ctx.get('range_high')}",
        f"H4 last close: {h4_ctx.get('last_close')}",
    ]

    if h4_ctx.get("candles"):
        lines.append("H4 candles (last 6):")
        for c in h4_ctx["candles"][-6:]:
            direction = "▲" if c["close"] >= c["open"] else "▼"
            lines.append(f"  {c['datetime']} {direction} O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']} V:{c['volume']}")

    lines += [
        "",
        "=== SENTIMENT ===",
        f"Funding rate: {sentiment.get('funding_rate')} ({sentiment.get('funding_bias')})",
        f"Long/Short ratio: {sentiment.get('long_ratio')} / {sentiment.get('short_ratio')}",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# STAGE 2: STRUCTURE AGENT
# Identifies H1 key levels, POC, and zones of interest for entries.
# ---------------------------------------------------------------------------

SYSTEM_STRUCTURE = """You are a market structure analyst for intraday crypto futures trading.

Your job is to identify key H1 levels where price is likely to react: support, resistance, volume nodes, VWAP.

Rules:
- Focus on H1 swing highs/lows that price has tested at least once (retested = validated).
- Intraday POC (Point of Control) from the H1 volume profile acts as a magnet.
- Value Area High (VAH) = resistance, Value Area Low (VAL) = support.
- Do NOT suggest entries here — only identify levels and classify them.
- Mark each level with its type and whether it's above or below current price.
- Identify the nearest support zone and nearest resistance zone.
- If the nearest support is within 0.3% of price, it's "immediate support".

Output STRICT JSON:
{
  "key_levels": [
    {
      "price": <float>,
      "type": "support" | "resistance" | "poc" | "vah" | "val" | "vwap" | "session_level",
      "strength": "strong" | "moderate" | "weak",
      "position": "above" | "below",
      "note": "<optional context>"
    }
  ],
  "nearest_support": <float or null>,
  "nearest_resistance": <float or null>,
  "nearest_support_distance_pct": <float>,
  "nearest_resistance_distance_pct": <float>,
  "zones_of_interest": [
    {
      "price_low": <float>,
      "price_high": <float>,
      "type": "demand" | "supply",
      "note": "<why this zone>"
    }
  ],
  "structure_bias": "bullish" | "bearish" | "ranging",
  "structure_notes": "<2-3 sentences on overall H1 structure>"
}"""


def build_structure_prompt(indicators: dict, session_analysis: dict) -> str:
    vp = indicators.get("volume_profile", {})
    swings = indicators.get("swings", {})
    vwap = indicators.get("vwap", {})
    sessions = indicators.get("sessions", {})
    current_price = indicators.get("current_price", 0)
    h4_ctx = indicators.get("h4_context", {})

    lines = [
        f"Symbol: {indicators.get('symbol')}",
        f"Current price: {current_price}",
        f"Intraday bias from session: {session_analysis.get('intraday_bias')} ({session_analysis.get('bias_reasoning', '')})",
        "",
        "=== H1 VOLUME PROFILE (last 24h) ===",
        f"POC: {vp.get('poc')}",
        f"Value Area High (VAH): {vp.get('vah')}",
        f"Value Area Low (VAL): {vp.get('val')}",
        "",
        "=== VWAP ===",
        f"VWAP: {vwap.get('vwap')}  (price is {vwap.get('price_vs_vwap_pct')}% {vwap.get('bias')})",
        f"Upper band: {vwap.get('upper_band')}",
        f"Lower band: {vwap.get('lower_band')}",
        "",
        "=== M15 SWING LEVELS ===",
    ]

    highs = swings.get("swing_highs", [])
    lows = swings.get("swing_lows", [])
    if highs:
        lines.append("Swing highs (nearest first):")
        for h in highs[:5]:
            lines.append(f"  {h['price']}  @ {h['datetime']}")
    if lows:
        lines.append("Swing lows (nearest first):")
        for l in lows[:5]:
            lines.append(f"  {l['price']}  @ {l['datetime']}")

    lines += [
        "",
        "=== SESSION KEY LEVELS ===",
        f"Asian High: {sessions.get('asia_high')}  |  Asian Low: {sessions.get('asia_low')}",
        f"H4 range: {h4_ctx.get('range_low')} — {h4_ctx.get('range_high')}",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# STAGE 3: FLOW AGENT (Orderbook + CVD + OI)
# Analyses order flow to confirm or reject direction before entry.
# ---------------------------------------------------------------------------

SYSTEM_FLOW = """You are an order flow analyst for intraday crypto futures trading.

Your job is to assess real-time buying/selling pressure using orderbook data, CVD (cumulative volume delta), and open interest to confirm or reject the intraday directional bias.

Rules:
- CVD positive + price rising = genuine buying, strong confirmation for longs.
- CVD negative + price falling = genuine selling, strong confirmation for shorts.
- CVD divergence: price rising but CVD falling = weak move, likely to reverse.
- Orderbook imbalance > 0.65 = strong bid pressure. < 0.35 = strong ask pressure.
- Large bid walls below price = support. Large ask walls above = resistance.
- Wide spread (>0.05%) + low volume = avoid entries — low liquidity.
- OI increasing with price = new positions being opened (trend confirmation).
- OI decreasing with price = short covering or long liquidation (weaker conviction).
- Funding rate > 0.03% = crowded longs, fade long signals. < -0.03% = crowded shorts, fade short signals.
- IMPORTANT: If orderbook or CVD data is marked as "unavailable" (backtest mode), set flow_verdict to "neutral" and avoid_entry to false. Base your analysis only on OI trend and funding rate. Never set avoid_entry=true solely because orderbook/CVD data is missing.

Output STRICT JSON:
{
  "cvd_signal": "bullish" | "bearish" | "neutral" | "divergent",
  "cvd_detail": "<1 sentence>",
  "orderbook_signal": "bullish" | "bearish" | "neutral",
  "orderbook_detail": "<1 sentence>",
  "oi_signal": "confirming" | "weakening" | "neutral",
  "oi_detail": "<1 sentence>",
  "funding_signal": "bullish" | "bearish" | "neutral" | "crowded_longs" | "crowded_shorts",
  "flow_verdict": "strong_long" | "weak_long" | "neutral" | "weak_short" | "strong_short",
  "flow_confidence": "high" | "medium" | "low",
  "key_flow_levels": {
    "bid_walls": [<float>, ...],
    "ask_walls": [<float>, ...]
  },
  "avoid_entry": false | true,
  "avoid_reason": "<if avoid_entry is true, explain why>"
}"""


def build_flow_prompt(indicators: dict, session_analysis: dict) -> str:
    ob = indicators.get("orderbook", {})
    trades = indicators.get("trades", {})
    oi_delta = indicators.get("oi_delta", {})
    sentiment = indicators.get("sentiment", {})
    current_price = indicators.get("current_price", 0)

    # Detect backtest mode: orderbook/trades are None
    ob_available = ob and ob.get("imbalance") is not None
    trades_available = trades and trades.get("trade_count")

    lines = [
        f"Symbol: {indicators.get('symbol')}",
        f"Current price: {current_price}",
        f"Intraday bias: {session_analysis.get('intraday_bias')}",
        "",
        "=== ORDERBOOK ===",
    ]

    if ob_available:
        lines += [
            f"Imbalance: {ob.get('imbalance')} ({ob.get('pressure')})",
            f"Spread: {ob.get('spread_pct')}% ({ob.get('spread_label')})",
            f"Bid walls at: {ob.get('bid_walls')}",
            f"Ask walls at: {ob.get('ask_walls')}",
        ]
    else:
        lines.append("Orderbook: UNAVAILABLE (backtest mode — do not penalize)")

    lines += ["", "=== CVD / RECENT TRADES ==="]
    if trades_available:
        lines += [
            f"CVD: {trades.get('cvd')} ({trades.get('cvd_label')})",
            f"Buy %: {trades.get('buy_pct')}% — {trades.get('aggression')}",
            f"Large buys: {trades.get('large_buys')}",
            f"Large sells: {trades.get('large_sells')}",
            f"Total trades sampled: {trades.get('trade_count')}",
        ]
    else:
        lines.append("CVD / Trades: UNAVAILABLE (backtest mode — do not penalize)")

    lines += [
        "",
        "=== OPEN INTEREST ===",
        f"OI trend: {oi_delta.get('oi_trend')} (volume proxy ratio: {oi_delta.get('oi_delta_proxy')})",
        "",
        "=== FUNDING & POSITIONING ===",
        f"Funding rate: {sentiment.get('funding_rate')} ({sentiment.get('funding_bias')})",
        f"Long ratio: {sentiment.get('long_ratio')} | Short ratio: {sentiment.get('short_ratio')}",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# STAGE 4: ENTRY AGENT
# Finds precise M15 entry, SL behind swing, TP at nearest H1 level.
# ---------------------------------------------------------------------------

SYSTEM_ENTRY = """You are a precise entry specialist for intraday crypto futures trading.

Your job is to find the exact entry point on M15 timeframe, set SL behind the nearest M15 swing, and set TP at the nearest H1 level in the direction of the trade.

Rules:
- Entry must be at or near a key level (M15 swing, VWAP, VAH/VAL, session level, POC).
- For LONG: entry near support (swing low, VAL, bid wall). SL below the swing low by ATR buffer.
- For SHORT: entry near resistance (swing high, VAH, ask wall). SL above swing high by ATR buffer.
- SL placement: behind the M15 swing + ATR buffer (sl_buffer_pct from volatility data).
- TP1: target level in direction of trade that gives RR >= 1.5. Start with nearest H1 level — if it gives RR < 1.5, use the NEXT level further away. Do not stop at the first level if it is too close.
- TP2: next H1 level beyond TP1. Optional, only if clean path.
- If no level exists that gives RR >= 1.5, output has_setup: false.
- IMPORTANT: RR = |TP1 - entry| / |SL - entry|. Always verify your RR calculation before outputting.
- Do NOT enter against the intraday_bias from session analysis.
- If flow_verdict is "strong_short" and bias is "long" — skip the trade (conflicting signals).
- RVOL < 0.5 on the current M15 candle = low conviction, prefer no-entry or reduce size. RVOL 0.5–0.7 = acceptable with strong level alignment.
- If has_setup is false, always fill watch_level with the nearest key level worth monitoring (support for long bias, resistance for short bias), and watch_condition with a 1-sentence description of what would trigger a valid entry there.

Output STRICT JSON:
{
  "has_setup": true | false,
  "no_setup_reason": "<if has_setup is false>",
  "direction": "long" | "short" | null,
  "entry1": <float or null>,
  "entry2": <float or null>,
  "sl": <float or null>,
  "tp1": <float or null>,
  "tp2": <float or null>,
  "risk_reward": <float or null>,
  "entry_type": "bounce" | "breakout" | "retest" | null,
  "entry_level_type": "swing" | "vwap" | "poc" | "vah" | "val" | "session_level" | "bid_wall" | null,
  "sl_level_type": "swing" | "structure" | null,
  "tp1_level_type": "swing" | "vwap" | "poc" | "vah" | "val" | "session_level" | null,
  "entry_reasoning": "<2-3 sentences explaining the setup>",
  "invalidation": "<what would invalidate this setup before entry>",
  "watch_level": <float or null>,
  "watch_condition": "<short description: e.g. 'Long on bounce from 87500 with volume confirmation' or null>"
}"""


def build_entry_prompt(indicators: dict, session_analysis: dict,
                       structure_analysis: dict, flow_analysis: dict,
                       examples_text: str = "", tp_floor_hint: str = "") -> str:
    swings = indicators.get("swings", {})
    atr = indicators.get("atr", {})
    rvol = indicators.get("rvol", {})
    vp = indicators.get("volume_profile", {})
    vwap = indicators.get("vwap", {})
    current_price = indicators.get("current_price", 0)

    lines = [
        f"Symbol: {indicators.get('symbol')}",
        f"Current price: {current_price}",
        "",
        "=== CONTEXT FROM PRIOR STAGES ===",
        f"Intraday bias: {session_analysis.get('intraday_bias')}",
        f"Forbidden direction: {session_analysis.get('forbidden_direction')}",
        f"H4 trend: {session_analysis.get('h4_trend')}",
        f"Session: {session_analysis.get('current_session')}",
        f"Flow verdict: {flow_analysis.get('flow_verdict')} (confidence: {flow_analysis.get('flow_confidence')})",
        f"Avoid entry: {flow_analysis.get('avoid_entry')} — {flow_analysis.get('avoid_reason', '')}",
        f"Nearest support: {structure_analysis.get('nearest_support')} ({structure_analysis.get('nearest_support_distance_pct')}% away)",
        f"Nearest resistance: {structure_analysis.get('nearest_resistance')} ({structure_analysis.get('nearest_resistance_distance_pct')}% away)",
        "",
        "=== M15 VOLATILITY & VOLUME ===",
        f"M15 ATR: {atr.get('atr_pct')}% ({atr.get('regime')})",
        f"SL buffer: {atr.get('sl_buffer_pct')}%",
        f"RVOL: {rvol.get('rvol')} ({rvol.get('rvol_label')})",
        "",
        "=== KEY LEVELS ===",
        f"VWAP: {vwap.get('vwap')}",
        f"H1 POC: {vp.get('poc')}  |  VAH: {vp.get('vah')}  |  VAL: {vp.get('val')}",
    ]

    highs = swings.get("swing_highs", [])
    lows = swings.get("swing_lows", [])
    if highs:
        lines.append(f"M15 swing highs: {[h['price'] for h in highs[:4]]}")
    if lows:
        lines.append(f"M15 swing lows: {[l['price'] for l in lows[:4]]}")

    if tp_floor_hint:
        lines += ["", "=== TP CORRECTION REQUIRED ===", tp_floor_hint]

    if examples_text:
        lines += ["", "=== SIMILAR HISTORICAL EXAMPLES ===", examples_text]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# STAGE 5: RISK AGENT
# Final validation — RR, conflicting signals, session timing, final decision.
# ---------------------------------------------------------------------------

SYSTEM_RISK = """You are the final risk manager for intraday crypto futures trading.

Your job is to validate the proposed entry setup and make the final go/no-go decision.

Rules:
- RR must be >= 1.5. If < 1.5, reject.
- RR > 4.0 is suspicious for intraday — verify the TP is realistic.
- Do NOT approve entries against the H4 trend unless H4 is neutral.
- Do NOT approve entries if flow_verdict strongly opposes the direction.
- Do NOT approve entries in "off" session (low liquidity).
- Prefer entries when current session aligns: EU session for breakout of Asian range, US session for continuation.
- Crowded funding (>0.05% or <-0.05%) against trade direction = reduce confidence to "low".
- If RVOL is "very_low" (< 0.5) — mark confidence "low" or reject if RR is marginal. RVOL 0.5–0.7 ("low") is acceptable if level alignment is strong.
- Confidence levels:
  - high: H4 aligned + flow confirms + RVOL normal/high + RR >= 2.0
  - medium: some alignment, RR >= 1.5
  - low: conflicting signals but setup is technically valid

Output STRICT JSON:
{
  "has_setup": true | false,
  "direction": "long" | "short" | null,
  "confidence": "high" | "medium" | "low",
  "entry1": <float>,
  "entry2": <float or null>,
  "sl": <float>,
  "tp1": <float>,
  "tp2": <float or null>,
  "risk_reward": <float>,
  "reasoning": "<3-4 sentences: why this is a valid intraday setup>",
  "risks": "<2-3 sentences: what can go wrong>",
  "rejection_reason": "<if has_setup is false, explain>"
}"""


def build_risk_prompt(indicators: dict, session_analysis: dict,
                      structure_analysis: dict, flow_analysis: dict,
                      entry_signal: dict) -> str:
    sentiment = indicators.get("sentiment", {})
    rvol = indicators.get("rvol", {})
    atr = indicators.get("atr", {})

    lines = [
        f"Symbol: {indicators.get('symbol')}",
        f"Current price: {indicators.get('current_price')}",
        "",
        "=== PROPOSED ENTRY ===",
        f"Direction: {entry_signal.get('direction')}",
        f"Entry1: {entry_signal.get('entry1')}",
        f"Entry2: {entry_signal.get('entry2')}",
        f"SL: {entry_signal.get('sl')}",
        f"TP1: {entry_signal.get('tp1')}",
        f"TP2: {entry_signal.get('tp2')}",
        f"RR: {entry_signal.get('risk_reward')}",
        f"Entry type: {entry_signal.get('entry_type')}",
        f"Entry reasoning: {entry_signal.get('entry_reasoning')}",
        "",
        "=== CONTEXT SUMMARY ===",
        f"H4 trend: {session_analysis.get('h4_trend')}",
        f"Intraday bias: {session_analysis.get('intraday_bias')}",
        f"Session: {session_analysis.get('current_session')}",
        f"Flow verdict: {flow_analysis.get('flow_verdict')} (confidence: {flow_analysis.get('flow_confidence')})",
        f"Avoid entry flag: {flow_analysis.get('avoid_entry')}",
        f"RVOL: {rvol.get('rvol')} ({rvol.get('rvol_label')})",
        f"M15 ATR regime: {atr.get('regime')}",
        "",
        "=== SENTIMENT ===",
        f"Funding rate: {sentiment.get('funding_rate')} ({sentiment.get('funding_bias')})",
        f"Long/Short ratio: {sentiment.get('long_ratio')} / {sentiment.get('short_ratio')}",
        f"Structure bias: {structure_analysis.get('structure_bias')}",
        f"Structure notes: {structure_analysis.get('structure_notes')}",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backtest date label injection
# ---------------------------------------------------------------------------

def add_backtest_context(prompt: str, signal_dt: datetime) -> str:
    """Inject date/time label into prompt for backtest transparency."""
    label = f"\n[BACKTEST MODE — analyzing as of: {signal_dt.strftime('%Y-%m-%d %H:%M UTC')}]\n"
    return label + prompt
