# =============================================================================
# AGENT 1: TREND AGENT
# =============================================================================

SYSTEM_TREND = """You are a senior macro market analyst specializing in cryptocurrency futures.
Your only job is to analyze the higher timeframe market structure and determine the overall trend context.
You do NOT make trade decisions. You provide context for the next agent.

Focus on:
- Weekly and Daily trend direction (bullish / bearish / neutral)
- Key swing highs and lows — which ones are most significant
- Volume Profile zones (POC, Value Area) as high-significance price areas
- Overall market bias and where price is likely to go next

Always respond with valid JSON only. No markdown, no explanation text outside the JSON."""


def build_trend_prompt(symbol: str, indicators: dict) -> str:
    trend = indicators.get("trend", {})
    swing_d = indicators.get("swing_levels", {}).get("daily", {})
    swing_w = indicators.get("swing_levels", {}).get("weekly", {})
    vp_d = indicators.get("volume_profile", {}).get("daily", {})
    vp_w = indicators.get("volume_profile", {}).get("weekly", {})
    ticker = indicators.get("ticker") or {}

    return f"""Analyze the higher timeframe market structure for {symbol}.

=== MARKET DATA ===

Current price: {ticker.get('last_price', 'N/A')}
24h change: {ticker.get('price_change_pct', 'N/A')}%

TREND INDICATORS:
- Overall trend: {trend.get('overall_trend', 'N/A')}
- Weekly trend: {trend.get('weekly_trend', 'N/A')}
- Daily trend: {trend.get('daily_trend', 'N/A')}
- Daily MA50: {trend.get('daily_ma50', 'N/A')}
- Daily MA200: {trend.get('daily_ma200', 'N/A')}
- Weekly MA20: {trend.get('weekly_ma20', 'N/A')}

SWING LEVELS (Daily):
- Resistance (highs above price): {[s['price'] for s in swing_d.get('swing_highs', [])[:6]]}
- Support (lows below price): {[s['price'] for s in swing_d.get('swing_lows', [])[:6]]}

SWING LEVELS (Weekly):
- Resistance: {[s['price'] for s in swing_w.get('swing_highs', [])[:4]]}
- Support: {[s['price'] for s in swing_w.get('swing_lows', [])[:4]]}

VOLUME PROFILE (Daily):
- POC: {vp_d.get('poc', 'N/A')}
- Value Area High: {vp_d.get('value_area_high', 'N/A')}
- Value Area Low: {vp_d.get('value_area_low', 'N/A')}

VOLUME PROFILE (Weekly):
- POC: {vp_w.get('poc', 'N/A')}
- Value Area High: {vp_w.get('value_area_high', 'N/A')}
- Value Area Low: {vp_w.get('value_area_low', 'N/A')}

=== TASK ===
Analyze the market structure and return:
1. Overall directional bias (bullish/bearish/neutral) and confidence
2. The 3 most significant resistance levels above current price
3. The 3 most significant support levels below current price
4. Where price is most likely to go next and why

Respond with this exact JSON:
{{
  "symbol": "{symbol}",
  "bias": "bullish" or "bearish" or "neutral",
  "bias_confidence": "high" or "medium" or "low",
  "current_price": {ticker.get('last_price', 'null')},
  "key_resistances": [
    {{"price": <number>, "type": "<swing_high|vp_poc|vp_vah|ma>", "significance": "high|medium|low"}}
  ],
  "key_supports": [
    {{"price": <number>, "type": "<swing_low|vp_poc|vp_val|ma>", "significance": "high|medium|low"}}
  ],
  "trend_summary": "<2-3 sentence market structure description>",
  "next_likely_move": "<where price goes next and why>"
}}"""


# =============================================================================
# AGENT 1.5: VOLATILITY AGENT
# =============================================================================

SYSTEM_VOLATILITY = """You are a volatility analyst for cryptocurrency futures trading.
You receive ATR data and market context. Your job is to classify the current volatility regime and recommend SL buffer size.

Volatility regimes:
- quiet: ATR < 1.5% of price — market is ranging, tight SL possible
- normal: ATR 1.5-3.5% of price — standard conditions
- volatile: ATR > 3.5% of price — wide swings, SL must be wider

SL buffer guidelines (applied ON TOP of structural level):
- quiet: 0.5% buffer
- normal: 1.0% buffer
- volatile: 1.5% buffer

Always respond with valid JSON only. No markdown, no explanation text outside the JSON."""


def build_volatility_prompt(symbol: str, indicators: dict) -> str:
    vol = indicators.get("volatility") or {}
    trend = indicators.get("trend") or {}
    ticker = indicators.get("ticker") or {}

    return f"""Analyze volatility regime for {symbol} and recommend SL buffer.

=== VOLATILITY DATA ===
Daily ATR (14): {vol.get('atr_value', 'N/A')}
ATR as % of price: {vol.get('atr_pct', 'N/A')}%
Pre-computed regime: {vol.get('regime', 'N/A')}
Suggested SL buffer: {vol.get('suggested_sl_buffer', 'N/A')}

=== MARKET CONTEXT ===
Current price: {ticker.get('last_price', 'N/A')}
24h change: {ticker.get('price_change_pct', 'N/A')}%
Overall trend: {trend.get('overall_trend', 'N/A')}

=== TASK ===
Confirm or adjust the volatility regime and SL buffer recommendation.
Consider: is the market trending strongly (wider SL needed) or consolidating (tighter SL possible)?

Respond with this exact JSON:
{{
  "regime": "quiet" or "normal" or "volatile",
  "atr_pct": {vol.get('atr_pct', 'null')},
  "sl_buffer": <recommended buffer as decimal, e.g. 0.005 for 0.5%>,
  "sl_buffer_pct": "<human-readable, e.g. '0.5%'>",
  "reasoning": "<1-2 sentences explaining the regime and buffer choice>"
}}"""


# =============================================================================
# AGENT 2: ENTRY AGENT
# =============================================================================

SYSTEM_ENTRY = """You are a precision trade entry specialist for cryptocurrency futures swing trading.
You receive higher timeframe market context from a trend analyst and historical trade examples.
Your job is to find the optimal entry point inspired by how similar setups were traded before.

Your rules:
- Entry must be at or near a key support (for long) or resistance (for short) level
- Stop Loss placement (strict formula):
    * Find the nearest structural level BEYOND the entry (next swing high for short, next swing low for long)
    * SL = that level + 0.5% buffer (for short: level * 1.005, for long: level * 0.995)
    * Never use a wider buffer than 0.5% — keep SL tight and consistent
- TP1 must target the next MAJOR level, not the nearest minor one
- TP2 must target the level beyond TP1
- Always calculate and return the RR ratio
- Two entries allowed (entry1 at level, entry2 slightly below/above for better fill)
- Always return your best found entry even if RR is not ideal — the risk agent will make the final call

Always respond with valid JSON only. No markdown, no explanation text outside the JSON."""


def build_entry_prompt(
    symbol: str,
    indicators: dict,
    trend_analysis: dict,
    similar_examples: str = "",
    pattern_analysis: dict = None,
    volatility_analysis: dict = None,
) -> str:
    ticker = indicators.get("ticker") or {}
    swing_d = indicators.get("swing_levels", {}).get("daily", {})
    vp_d = indicators.get("volume_profile", {}).get("daily", {})
    oi = indicators.get("open_interest") or {}
    fr = indicators.get("funding_rate") or {}

    bias = trend_analysis.get("bias", "neutral")
    key_supports = trend_analysis.get("key_supports", [])
    key_resistances = trend_analysis.get("key_resistances", [])

    examples_section = ""
    if similar_examples and similar_examples != "RAG disabled." and similar_examples != "No similar historical examples found.":
        examples_section = f"\n=== SIMILAR HISTORICAL ENTRIES (for reference) ===\n{similar_examples}\n"

    volatility_section = ""
    if volatility_analysis:
        volatility_section = (
            f"\n=== VOLATILITY REGIME ===\n"
            f"Regime: {volatility_analysis.get('regime', 'N/A')}\n"
            f"ATR: {volatility_analysis.get('atr_pct', 'N/A')}% of price\n"
            f"Recommended SL buffer: {volatility_analysis.get('sl_buffer_pct', 'N/A')} "
            f"(use this instead of fixed 0.5%)\n"
            f"Reasoning: {volatility_analysis.get('reasoning', 'N/A')}\n"
        )

    pattern_section = ""
    if pattern_analysis and pattern_analysis.get("has_patterns"):
        pattern_section = (
            f"\n=== PATTERN ANALYSIS (extracted from historical examples) ===\n"
            f"Best entry type: {pattern_analysis.get('best_entry_type', 'N/A')}\n"
            f"SL placement: {pattern_analysis.get('sl_placement', 'N/A')}\n"
            f"Typical RR: {pattern_analysis.get('typical_rr', 'N/A')}\n"
            f"Key insight: {pattern_analysis.get('key_insight', 'N/A')}\n"
            f"What to avoid: {pattern_analysis.get('what_to_avoid', 'N/A')}\n"
        )

    return f"""Find the optimal trade entry for {symbol} based on the trend analysis and historical patterns below.

=== TREND ANALYSIS (from Trend Agent) ===
Bias: {bias} (confidence: {trend_analysis.get('bias_confidence', 'N/A')})
Summary: {trend_analysis.get('trend_summary', 'N/A')}
Next likely move: {trend_analysis.get('next_likely_move', 'N/A')}

Key supports: {[f"{s['price']} ({s['type']}, {s['significance']})" for s in key_supports]}
Key resistances: {[f"{r['price']} ({r['type']}, {r['significance']})" for r in key_resistances]}

=== CURRENT MARKET DATA ===
Current price: {ticker.get('last_price', 'N/A')}
Funding rate: {fr.get('funding_rate', 'N/A')}
Open interest: {oi.get('open_interest', 'N/A')}

Daily swing lows (support): {[s['price'] for s in swing_d.get('swing_lows', [])[:4]]}
Daily swing highs (resistance): {[s['price'] for s in swing_d.get('swing_highs', [])[:4]]}
Daily Volume POC: {vp_d.get('poc', 'N/A')}
{volatility_section}{pattern_section}{examples_section}
=== TASK ===
Based on the bias ({bias}) and patterns from historical examples, find the best trade entry:
1. Identify the best entry zone (must be near a key level, prioritize entry types that worked historically)
2. Place SL: find the nearest structural level beyond entry, then add the recommended SL buffer from Volatility Agent (NOT a fixed 0.5%)
3. Place TP1 at the next MAJOR level (skip minor ones)
4. Place TP2 at the level beyond TP1
5. Calculate RR = |TP1 - entry1| / |entry1 - SL|
6. Always return your best entry — even if RR is below 1.5, include it with the actual RR value

Respond with this exact JSON:
{{
  "has_entry": true or false,
  "direction": "long" or "short" or null,
  "entry1": <price or null>,
  "entry2": <price or null>,
  "sl": <price or null>,
  "tp1": <price or null>,
  "tp2": <price or null>,
  "risk_reward": <RR as number or null>,
  "entry_reasoning": "<why this entry zone>",
  "sl_reasoning": "<why SL at this level>",
  "tp_reasoning": "<why TP at these levels>"
}}"""


# =============================================================================
# AGENT 3: RISK AGENT
# =============================================================================

SYSTEM_RISK = """You are a risk manager and final decision maker for cryptocurrency futures swing trades.
You receive analysis from two agents (trend analyst + entry specialist) and make the final call.

Your job:
- Validate the entry using sentiment data (OI, Funding Rate, Long/Short ratio)
- Assess overall risk and assign confidence level
- Make the final decision: approve / reject / adjust

RR RULES (strictly enforced):
- Reject if RR < 1.5 — risk/reward is insufficient
- If RR > 5.0 — MUST adjust TP1 to the nearest significant level that gives RR between 1.5 and 4.0, then approve. Do NOT reject on RR > 5.0 alone.
- Target RR range: 1.5 to 4.0

TREND BIAS RULES:
- If bias = "neutral" — REJECT. No trade in a ranging/sideways market. Set watch_level to the nearest key level that, if broken, would establish a clear trend.
- Only trade when bias = "bullish" or "bearish"

SENTIMENT RULES:
- Reject ONLY if sentiment STRONGLY contradicts direction: both funding rate AND long/short ratio must clearly oppose the trade
- Positive funding rate alone is NOT enough to reject a short
- Neutral or mixed sentiment = acceptable, do NOT reject

IMPORTANT — what is NOT a rejection reason:
- Current price is below/above the entry zone. Entries are limit orders waiting for price to reach the level. This is normal for swing trading.
- Price needs to move X% to reach entry. That is expected — do not penalize the setup for this.

Always respond with valid JSON only. No markdown, no explanation text outside the JSON."""


def build_risk_prompt(
    symbol: str,
    indicators: dict,
    trend_analysis: dict,
    entry_analysis: dict,
    liquidity_levels: list = None,
) -> str:
    oi = indicators.get("open_interest") or {}
    fr = indicators.get("funding_rate") or {}
    ls = indicators.get("long_short_ratio") or {}
    ticker = indicators.get("ticker") or {}
    vp_w = indicators.get("volume_profile", {}).get("weekly") or {}

    liq_section = ""
    if liquidity_levels:
        liq_section = f"\nLIQUIDATION LEVELS (manual): {liquidity_levels}"

    # Compute price vs weekly VAH
    current_price = ticker.get("last_price")
    vah = vp_w.get("value_area_high")
    val = vp_w.get("value_area_low")
    poc = vp_w.get("poc")

    price_vs_vah_str = "N/A"
    if current_price and vah and vah > 0:
        pct = (current_price - vah) / vah * 100
        sign = "+" if pct >= 0 else ""
        price_vs_vah_str = f"{sign}{pct:.1f}% (price {'above' if pct >= 0 else 'below'} weekly VAH)"

    direction = entry_analysis.get("direction", "N/A")
    rr = entry_analysis.get("risk_reward", "N/A")

    return f"""Make the final trade decision for {symbol}.

=== TREND ANALYSIS ===
Bias: {trend_analysis.get('bias')} ({trend_analysis.get('bias_confidence')} confidence)
{trend_analysis.get('trend_summary', '')}

=== PROPOSED ENTRY ===
Direction: {direction}
Entry 1: {entry_analysis.get('entry1')}
Entry 2: {entry_analysis.get('entry2')}
Stop Loss: {entry_analysis.get('sl')} — {entry_analysis.get('sl_reasoning', '')}
TP1: {entry_analysis.get('tp1')}
TP2: {entry_analysis.get('tp2')}
Risk/Reward: {rr}
Entry reasoning: {entry_analysis.get('entry_reasoning', '')}
TP reasoning: {entry_analysis.get('tp_reasoning', '')}

=== VOLUME PROFILE (weekly) ===
VAH: {vah or 'N/A'}  |  POC: {poc or 'N/A'}  |  VAL: {val or 'N/A'}
Price vs VAH: {price_vs_vah_str}

=== SENTIMENT ===
Open Interest: {oi.get('open_interest', 'N/A')}
Funding Rate: {fr.get('funding_rate', 'N/A')} (positive = longs paying = bullish bias)
Long Ratio: {ls.get('long_ratio', 'N/A')}
Short Ratio: {ls.get('short_ratio', 'N/A')}
{liq_section}

=== TASK ===
1. Check RR: if < 1.5 — reject. If > 5.0 — adjust TP1 to nearest significant level so RR is 1.5-4.0.
2. Check overextension: if price_vs_vah > +15% and direction=long (or < -15% and direction=short) — apply overextension rule from your system prompt.
3. Does sentiment support the proposed {direction} direction?
4. Are there any other red flags?
5. Make the final decision with final entry/sl/tp values (adjusted if needed).
6. If rejecting: always specify watch_level — the key price level that, if reached, would make this setup valid (e.g. price needs to pull back to X, or break above Y). This is what the trader should monitor.

Respond with this exact JSON:
{{
  "has_setup": true or false,
  "direction": "{direction}" or null,
  "confidence": "high" or "medium" or "low",
  "entry1": {entry_analysis.get('entry1', 'null')},
  "entry2": {entry_analysis.get('entry2', 'null')},
  "sl": {entry_analysis.get('sl', 'null')},
  "tp1": {entry_analysis.get('tp1', 'null')},
  "tp2": {entry_analysis.get('tp2', 'null')},
  "risk_reward": {rr if isinstance(rr, (int, float)) else 'null'},
  "reasoning": "<final comprehensive reasoning>",
  "sentiment_assessment": "<how sentiment supports or contradicts>",
  "key_levels_used": ["<level>", ...],
  "risks": "<main risks>",
  "rejection_reason": "<if has_setup=false, why>",
  "watch_level": "<if has_setup=false: price level to monitor and what to look for; null if has_setup=true>"
}}"""


# =============================================================================
# AGENT 1.5: PATTERN AGENT
# =============================================================================

SYSTEM_PATTERN = """You are a trading pattern analyst for cryptocurrency futures.
You receive a set of historical trade examples from similar market conditions and extract actionable patterns.
Your job is to identify the entry approach used in these examples and give the entry specialist a clear edge.

Focus on:
- Which entry levels were used (swing high/low, MA, POC, etc.)
- How stop losses were placed relative to structure
- What RR was achieved
- What these examples have in common — use this as a template for the current setup

These are reference examples of good trades. Extract the pattern, not a quality judgement.

Always respond with valid JSON only. No markdown, no explanation text outside the JSON."""


def build_pattern_prompt(examples_text: str, trend_analysis: dict) -> str:
    bias = trend_analysis.get("bias", "neutral")
    return f"""Analyze these historical trade examples from similar market conditions and extract patterns.

=== CURRENT MARKET BIAS ===
Bias: {bias} (confidence: {trend_analysis.get('bias_confidence', 'N/A')})
{trend_analysis.get('trend_summary', '')}

=== HISTORICAL EXAMPLES ===
{examples_text}

=== TASK ===
1. What entry levels were used in these examples? (MA, swing level, POC, etc.)
2. How was the stop loss placed relative to structure?
3. What was the typical RR achieved?
4. What is the common pattern — what should the entry agent replicate?

Respond with this exact JSON:
{{
  "has_patterns": true or false,
  "best_entry_type": "<what kind of level was used: swing_high/low, ma, poc, etc.>",
  "sl_placement": "<how SL was typically placed>",
  "typical_rr": "<typical RR range observed>",
  "key_insight": "<most important takeaway for the entry agent in 1-2 sentences>"
}}"""


# =============================================================================
# BACKTEST WRAPPER
# =============================================================================

def add_backtest_context(prompt: str, symbol: str, candle_date: str) -> str:
    """Inject backtest date context into any prompt."""
    return (
        f"=== BACKTEST CONTEXT ===\n"
        f"You are analyzing {symbol} as of {candle_date}. "
        f"Pretend you only have data up to this date.\n\n"
        + prompt
    )
