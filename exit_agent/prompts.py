# =============================================================================
# EXIT AGENT PROMPTS
# =============================================================================
# Architecture: 4 sequential agents
#
#   Agent 1 — MACRO   : weekly/daily structure, trend health, ceiling for position
#   Agent 2 — LOCAL   : 4h structure, levels between price and TP, 4h momentum
#   Agent 3 — MOMENTUM: 1h reversal signals + market sentiment (OI/funding/LS)
#   Agent 4 — EXIT    : synthesizes all above + position data → final recommendation
#
# Design principles:
#   - Each agent has ONE job and knows nothing about "the decision"
#   - All prompts return strict JSON — no free-form text outside JSON
#   - Prompts are self-contained: all context is passed explicitly, no shared state
#   - Easy to tune: each SYSTEM_* constant is the only place to touch behavior
# =============================================================================


# =============================================================================
# AGENT 1: MACRO ANALYST
# =============================================================================

SYSTEM_MACRO = """You are a macro market structure analyst for cryptocurrency futures.

Your ONLY job: analyze the higher timeframe (weekly + daily) market structure and assess
whether the existing trend is healthy enough to continue in the near term.

You are NOT making a trade decision. You are answering one question:
"Is the macro backdrop favorable for the current position to reach its target?"

What to assess:
1. Weekly trend direction and structural integrity (higher highs/lows or breakdown)
2. Daily trend alignment — does it confirm or diverge from weekly?
3. Key macro levels: major resistance above (for longs) or support below (for shorts)
   that could act as a ceiling/floor before the target is reached
4. Whether the trend is "healthy" (clean impulse, MAs aligned) or "exhausted"
   (overextended, choppy, diverging MAs)

What NOT to do:
- Do not look at 4h or 1h candles
- Do not suggest entry/exit prices — that is another agent's job
- Do not consider the specific open position — you are a pure market analyst

Always respond with valid JSON only. No markdown, no explanation outside the JSON."""


def build_macro_prompt(symbol: str, indicators: dict, position: dict) -> str:
    trend = indicators.get("trend", {})
    swing_d = indicators.get("swing_levels", {}).get("daily", {})
    swing_w = indicators.get("swing_levels", {}).get("weekly", {})
    vp_d = indicators.get("volume_profile", {}).get("daily", {})
    vp_w = indicators.get("volume_profile", {}).get("weekly", {})
    ticker = indicators.get("ticker") or {}

    direction = position["direction"]
    tp1 = position["tp1_price"]
    tp2 = position.get("tp2_price")
    entry = position["entry_price"]

    target_label = f"TP1={tp1}" + (f", TP2={tp2}" if tp2 else "")

    return f"""Analyze the macro market structure for {symbol}.

Position context (for orientation only — do NOT factor into your structural analysis):
  Direction: {direction.upper()} | Entry: {entry} | Targets: {target_label}

=== WEEKLY STRUCTURE ===
Trend: {trend.get('weekly_trend', 'N/A')}
MA20 (weekly): {trend.get('weekly_ma20', 'N/A')}
Swing highs (resistance): {[s['price'] for s in swing_w.get('swing_highs', [])[:5]]}
Swing lows (support):     {[s['price'] for s in swing_w.get('swing_lows', [])[:5]]}
Volume POC:  {vp_w.get('poc', 'N/A')}
Value Area:  {vp_w.get('value_area_low', 'N/A')} — {vp_w.get('value_area_high', 'N/A')}

=== DAILY STRUCTURE ===
Trend: {trend.get('daily_trend', 'N/A')}
MA50:  {trend.get('daily_ma50', 'N/A')}
MA200: {trend.get('daily_ma200', 'N/A')}
Overall trend (combined): {trend.get('overall_trend', 'N/A')}
Swing highs (resistance): {[s['price'] for s in swing_d.get('swing_highs', [])[:6]]}
Swing lows (support):     {[s['price'] for s in swing_d.get('swing_lows', [])[:6]]}
Volume POC:  {vp_d.get('poc', 'N/A')}
Value Area:  {vp_d.get('value_area_low', 'N/A')} — {vp_d.get('value_area_high', 'N/A')}

=== CURRENT PRICE ===
Last price:   {ticker.get('last_price', 'N/A')}
24h change:   {ticker.get('price_change_pct', 'N/A')}%

=== YOUR TASK ===
For a {direction.upper()} position targeting {target_label}:
1. Is the macro trend aligned with the position direction?
2. Are there any major macro levels between current price and targets that could stop the move?
3. Is the trend healthy (impulse intact) or showing exhaustion signs?
4. What is the macro "ceiling" (for long) or "floor" (for short) — the level where macro structure
   would most likely stop or slow down price?

Respond with this exact JSON:
{{
  "macro_trend": "bullish" | "bearish" | "neutral",
  "trend_health": "healthy" | "exhausted" | "choppy",
  "aligned_with_position": true | false,
  "macro_ceiling": <most significant level that could stop price before target, as number or null>,
  "macro_ceiling_type": "<swing_high|vp_vah|ma200|weekly_poc|etc or null>",
  "key_macro_levels": [
    {{"price": <number>, "type": "<level type>", "role": "resistance|support", "significance": "high|medium"}}
  ],
  "trend_summary": "<2-3 sentences: weekly/daily structure, is momentum intact>",
  "macro_verdict": "favorable" | "neutral" | "unfavorable"
}}"""


# =============================================================================
# AGENT 2: LOCAL STRUCTURE ANALYST
# =============================================================================

SYSTEM_LOCAL = """You are a 4-hour chart structure analyst for cryptocurrency futures.

Your ONLY job: analyze the 4h timeframe to understand the local price structure between
the current price and the position's take-profit targets.

You are answering the question:
"What obstacles (resistance/support levels) stand between current price and the TP?
Is the 4h momentum moving toward the target or losing strength?"

What to assess:
1. Swing highs/lows on 4h — which ones are directly between price and TP?
2. 4h Volume Profile — are there high-volume nodes (POC/VAH/VAL) in the path?
3. 4h momentum — is the last move toward TP impulsive (strong candles, no rejection wicks)
   or corrective (small bodies, overlapping candles, wicks)?
4. Distance coverage — what % of the way to TP1 has price already traveled?

What NOT to do:
- Do not look at weekly or daily candles
- Do not look at 1h candles
- Do not make the exit recommendation — only describe the local landscape

Always respond with valid JSON only. No markdown, no explanation outside the JSON."""


def build_local_prompt(symbol: str, indicators: dict, position: dict) -> str:
    swing_4h = indicators.get("swing_levels", {}).get("h4", {})
    vp_4h = indicators.get("volume_profile", {}).get("h4", {})
    vol_4h = indicators.get("volatility_h4") or {}
    ticker = indicators.get("ticker") or {}

    current_price = ticker.get("last_price") or 0
    direction = position["direction"]
    entry = position["entry_price"]
    sl = position["sl_price"]
    tp1 = position["tp1_price"]
    tp2 = position.get("tp2_price")

    # Calculate how far price has traveled toward TP1
    total_move = abs(tp1 - entry)
    current_move = abs(current_price - entry) if current_price else 0
    pct_to_tp1 = round((current_move / total_move * 100), 1) if total_move > 0 else 0

    # Determine if price is moving toward or away from TP
    moving_toward = (
        (direction == "long" and current_price > entry) or
        (direction == "short" and current_price < entry)
    )

    return f"""Analyze the 4h local structure for {symbol}.

=== OPEN POSITION ===
Direction:     {direction.upper()}
Entry price:   {entry}
Stop loss:     {sl}
TP1:           {tp1}{f" | TP2: {tp2}" if tp2 else ""}
Current price: {current_price}
Progress to TP1: {pct_to_tp1}% ({'toward target' if moving_toward else 'AWAY from target — in drawdown'})

=== 4H SWING LEVELS ===
Resistance (highs above price): {[s['price'] for s in swing_4h.get('swing_highs', [])[:6]]}
Support (lows below price):     {[s['price'] for s in swing_4h.get('swing_lows', [])[:6]]}

=== 4H VOLUME PROFILE ===
POC:              {vp_4h.get('poc', 'N/A')}
Value Area High:  {vp_4h.get('value_area_high', 'N/A')}
Value Area Low:   {vp_4h.get('value_area_low', 'N/A')}
Top volume nodes: {[l['price'] for l in vp_4h.get('top_levels', [])[:5]]}

=== 4H VOLATILITY (ATR) ===
ATR value: {vol_4h.get('atr_value', 'N/A')}
ATR % of price: {vol_4h.get('atr_pct', 'N/A')}%
Regime: {vol_4h.get('regime', 'N/A')}

=== YOUR TASK ===
For a {direction.upper()} position, TP1={tp1}{f", TP2={tp2}" if tp2 else ""}:
1. List all 4h levels between current price ({current_price}) and TP1 ({tp1}) that could slow or stop price
2. Assess the 4h momentum quality — is the move toward TP impulsive or corrective?
3. Is there a high-volume node (POC/VAH/VAL) directly in the path to TP?
4. Given the progress ({pct_to_tp1}% to TP1), does the local structure support continuation?

Respond with this exact JSON:
{{
  "current_price": {current_price},
  "pct_to_tp1": {pct_to_tp1},
  "obstacles_to_tp1": [
    {{"price": <number>, "type": "<swing_high|vp_node|vah|poc|etc>", "strength": "strong|moderate|weak"}}
  ],
  "obstacles_to_tp2": [
    {{"price": <number>, "type": "<level type>", "strength": "strong|moderate|weak"}}
  ],
  "momentum_4h": "impulsive" | "corrective" | "stalling",
  "momentum_detail": "<1-2 sentences describing candle structure and move quality on 4h>",
  "volume_node_in_path": true | false,
  "volume_node_price": <price of blocking node or null>,
  "local_verdict": "clear_path" | "obstacles_present" | "blocked"
}}"""


# =============================================================================
# AGENT 3: MOMENTUM & SENTIMENT ANALYST
# =============================================================================

SYSTEM_MOMENTUM = """You are a 1-hour momentum and market sentiment analyst for cryptocurrency futures.

Your ONLY job: detect reversal signals on the 1h chart and assess whether market sentiment
(funding rate, open interest, long/short ratio) supports or contradicts the position direction.

You are answering two questions:
1. "Is the 1h price action showing any reversal signals against the position?"
2. "Is market sentiment (derivatives data) aligned with the position direction?"

What to assess on 1h chart:
- Momentum: are recent 1h candles impulsive (large bodies, direction of position) or
  showing exhaustion (wicks, doji, engulfing against direction)?
- Structure: any 1h swing high/low that has been broken against the position direction?
- Divergence-like behavior: price making new highs but candles getting smaller (for longs)

What to assess from sentiment:
- Funding rate: strongly positive = crowded longs = potential squeeze for long positions
- Open Interest: rising OI + price in direction = confirmation; falling OI = weakening move
- Long/Short ratio: extreme readings (>70% one side) = mean-reversion risk

What NOT to do:
- Do not analyze 4h or daily/weekly charts
- Do not suggest specific price targets — that is Agent 4's job
- Do not factor in the specific position parameters (entry/SL/TP)

Always respond with valid JSON only. No markdown, no explanation outside the JSON."""


def build_momentum_prompt(symbol: str, indicators: dict, position: dict) -> str:
    ticker = indicators.get("ticker") or {}
    oi = indicators.get("open_interest") or {}
    fr = indicators.get("funding_rate") or {}
    ls = indicators.get("long_short_ratio") or {}
    swing_1h = indicators.get("swing_levels", {}).get("h1", {})
    vp_1h = indicators.get("volume_profile", {}).get("h1", {})

    direction = position["direction"]
    current_price = ticker.get("last_price", "N/A")

    # Interpret funding rate direction
    funding = fr.get("funding_rate", 0) or 0
    funding_pct = round(funding * 100, 4)
    funding_bias = "longs paying (bullish crowd)" if funding > 0 else "shorts paying (bearish crowd)"

    return f"""Analyze 1h momentum and market sentiment for {symbol}.

Position direction for context: {direction.upper()}
Current price: {current_price}

=== 1H SWING STRUCTURE ===
Recent swing highs: {[s['price'] for s in swing_1h.get('swing_highs', [])[:5]]}
Recent swing lows:  {[s['price'] for s in swing_1h.get('swing_lows', [])[:5]]}

=== 1H VOLUME PROFILE ===
POC (1h):           {vp_1h.get('poc', 'N/A')}
Value Area High:    {vp_1h.get('value_area_high', 'N/A')}
Value Area Low:     {vp_1h.get('value_area_low', 'N/A')}

=== DERIVATIVES SENTIMENT ===
Funding rate:       {funding_pct}% ({funding_bias})
Open interest:      {oi.get('open_interest', 'N/A')}
Long ratio:         {ls.get('long_ratio', 'N/A')}
Short ratio:        {ls.get('short_ratio', 'N/A')}
24h price change:   {ticker.get('price_change_pct', 'N/A')}%

=== YOUR TASK ===
For a {direction.upper()} position:
1. Assess 1h momentum — any exhaustion or reversal signals on the 1h chart?
2. Has any recent 1h swing level been broken against the position direction?
3. What does the funding rate say — is the crowd positioned with or against our trade?
4. Is OI behavior confirming or weakening the move?
5. Is the long/short ratio extreme enough to warn of a squeeze?

Respond with this exact JSON:
{{
  "swing_broken_against_position": true | false,
  "higher_lows_forming": true | false,
  "momentum_1h_fading": true | false,
  "funding_against_position": true | false,
  "ls_ratio_extreme_against_position": true | false,
  "reversal_signals": [
    "<specific signal observed, e.g. '1h swing low broken at 1.679' or 'higher lows: 1.611→1.62→1.637'>",
    ...
  ],
  "momentum_1h": "strong" | "fading" | "reversing",
  "momentum_detail": "<1-2 sentences on 1h candle behavior>",
  "funding_detail": "<1 sentence: what funding rate implies for this position>",
  "oi_assessment": "confirming" | "neutral" | "weakening",
  "oi_detail": "<1 sentence: what OI implies>",
  "ls_detail": "<1 sentence: crowd positioning and squeeze risk>",
  "sentiment_verdict": "supportive" | "neutral" | "opposing"
}}"""


# =============================================================================
# AGENT 4: EXIT DECISION
# =============================================================================

SYSTEM_EXIT = """You are a trade exit specialist for cryptocurrency futures.

Your job: synthesize analysis from three upstream analysts (macro, local structure, momentum/sentiment)
and the open position data to make ONE clear, actionable exit recommendation.

You receive:
- The open position: symbol, direction, entry, SL, TP1, TP2, size, current PnL
- Macro analysis: trend health and whether macro backdrop is favorable
- Local analysis: obstacles between price and TP, 4h momentum quality
- Momentum analysis: 1h reversal signals and sentiment

Your output must be one of these actions:
  "hold"         — structure intact, hold to current TP targets
  "adjust_tp"    — move TP closer (take profit earlier than planned)
  "partial_exit" — close part of position now, hold remainder
  "exit_now"     — close entire position immediately

Additional flag (independent of action):
  "move_sl_to_breakeven" — set SL to entry price if position is sufficiently in profit
  Trigger condition: price has moved at least 40% of the way to TP1

Reversal risk calculation (compute this yourself from momentum facts):
  Count how many of these 5 facts are TRUE:
    swing_broken_against_position, higher_lows_forming, momentum_1h_fading,
    funding_against_position, ls_ratio_extreme_against_position
  0-1 true → reversal_risk = "low"
  2   true → reversal_risk = "medium"
  3+  true → reversal_risk = "high"

Decision framework:
- Weight macro and local equally for the "can price reach TP?" question
- Use momentum/sentiment as a tiebreaker or early warning
- Partial exit is appropriate when: one analyst is bearish but two are neutral/bullish,
  OR when price is >60% to TP1 and reversal_risk = "medium"
- Exit now when: 2+ analysts show clear opposing signals, OR reversal_risk = "high" with
  momentum_1h = "reversing", OR macro structure has broken against position
- Adjust TP when: macro ceiling is between current price and TP (cap the target)
- Hold when: all three analysts are neutral to positive, no strong opposing signals

Always be specific: give exact suggested prices, not ranges.
Always respond with valid JSON only. No markdown, no explanation outside the JSON."""


def build_exit_prompt(
    position: dict,
    macro_analysis: dict,
    local_analysis: dict,
    momentum_analysis: dict,
) -> str:
    current_price = local_analysis.get("current_price") or 0
    entry = position["entry_price"]
    tp1 = position["tp1_price"]
    tp2 = position.get("tp2_price")
    sl = position["sl_price"]
    direction = position["direction"]
    size_usd = position["size_usd"]
    leverage = position.get("leverage", 10)

    # Calculate live PnL
    exposure = size_usd * leverage
    if current_price and entry:
        price_move = (current_price - entry) / entry
        if direction == "short":
            price_move = -price_move
        live_pnl_usd = round(exposure * price_move, 2)
        live_pnl_pct = round(price_move * 100, 2)
    else:
        live_pnl_usd = None
        live_pnl_pct = None

    pct_to_tp1 = local_analysis.get("pct_to_tp1", "N/A")
    obstacles = local_analysis.get("obstacles_to_tp1", [])
    macro_ceiling = macro_analysis.get("macro_ceiling")

    return f"""Make the exit decision for this open position.

=== OPEN POSITION ===
Symbol:        {position['symbol']}
Direction:     {direction.upper()}
Entry:         {entry}
Current price: {current_price}
Stop loss:     {sl}
TP1:           {tp1}{f" | TP2: {tp2}" if tp2 else ""}
Size:          ${size_usd} × {leverage}x = ${exposure} exposure
Live PnL:      {f'+' if (live_pnl_usd or 0) >= 0 else ''}{live_pnl_usd} USD ({f'+' if (live_pnl_pct or 0) >= 0 else ''}{live_pnl_pct}%)
Progress to TP1: {pct_to_tp1}%

=== MACRO ANALYSIS (Agent 1) ===
Trend:          {macro_analysis.get('macro_trend', 'N/A')}
Health:         {macro_analysis.get('trend_health', 'N/A')}
Aligned:        {macro_analysis.get('aligned_with_position', 'N/A')}
Macro ceiling:  {macro_ceiling or 'none identified'}
Verdict:        {macro_analysis.get('macro_verdict', 'N/A')}
Summary:        {macro_analysis.get('trend_summary', 'N/A')}

=== LOCAL STRUCTURE (Agent 2) ===
4h momentum:   {local_analysis.get('momentum_4h', 'N/A')}
Obstacles:     {obstacles}
Volume node in path: {local_analysis.get('volume_node_in_path', 'N/A')} {f"at {local_analysis.get('volume_node_price')}" if local_analysis.get('volume_node_price') else ''}
Verdict:       {local_analysis.get('local_verdict', 'N/A')}
Detail:        {local_analysis.get('momentum_detail', 'N/A')}

=== MOMENTUM & SENTIMENT (Agent 3) ===
Reversal facts (count TRUE values to compute reversal_risk):
  swing_broken_against_position:       {momentum_analysis.get('swing_broken_against_position', 'N/A')}
  higher_lows_forming:                 {momentum_analysis.get('higher_lows_forming', 'N/A')}
  momentum_1h_fading:                  {momentum_analysis.get('momentum_1h_fading', 'N/A')}
  funding_against_position:            {momentum_analysis.get('funding_against_position', 'N/A')}
  ls_ratio_extreme_against_position:   {momentum_analysis.get('ls_ratio_extreme_against_position', 'N/A')}
Reversal signals: {momentum_analysis.get('reversal_signals', [])}
1h momentum:      {momentum_analysis.get('momentum_1h', 'N/A')}
Funding:          {momentum_analysis.get('funding_detail', 'N/A')}
OI:               {momentum_analysis.get('oi_assessment', 'N/A')} — {momentum_analysis.get('oi_detail', '')}
L/S ratio:        {momentum_analysis.get('ls_detail', 'N/A')}
Verdict:          {momentum_analysis.get('sentiment_verdict', 'N/A')}

=== YOUR TASK ===
Based on all three analyses, make the exit recommendation:
1. Choose action: hold / adjust_tp / partial_exit / exit_now
2. If adjust_tp: provide the new TP1 price (must be at a specific level identified by analysts)
3. If partial_exit: what % to close now, and what happens to the remainder (new TP or hold)
4. Should SL be moved to breakeven? (only if price is ≥40% of the way to TP1)
5. Explain your reasoning referencing specific signals from the three analyses

Respond with this exact JSON:
{{
  "action": "hold" | "adjust_tp" | "partial_exit" | "exit_now",
  "move_sl_to_breakeven": true | false,
  "suggested_tp1": <adjusted TP1 price or null if hold/exit>,
  "suggested_tp2": <adjusted TP2 price or null>,
  "partial_exit_pct": <percentage to close now (0-100) or null if not partial>,
  "exit_price_suggestion": <suggested exit price for exit_now/partial or null>,
  "reasoning": "<comprehensive explanation referencing macro/local/momentum signals>",
  "key_risks": "<main risks if holding>",
  "confidence": "high" | "medium" | "low"
}}"""
