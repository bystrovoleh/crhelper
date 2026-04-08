from data.mexc_client import MEXCClient
from data.indicators import (
    compute_indicators,
    find_swing_highs_lows,
    build_volume_profile,
    compute_atr,
)
from agent.llm import LLMClient
from exit_agent.prompts import (
    SYSTEM_MACRO, build_macro_prompt,
    SYSTEM_LOCAL, build_local_prompt,
    SYSTEM_MOMENTUM, build_momentum_prompt,
    SYSTEM_EXIT, build_exit_prompt,
)
from positions.db import get_open_positions, get_position_by_id


class ExitAgent:
    """
    Isolated exit analysis pipeline — 4 sequential agents.

    Completely independent from TradingAgent (no shared state, no RAG).

    Pipeline:
      1. MacroAgent    — weekly/daily structure, trend health
      2. LocalAgent    — 4h obstacles between price and TP
      3. MomentumAgent — 1h reversal signals + OI/funding/LS sentiment
      4. ExitAgent     — synthesizes all → hold/adjust_tp/partial_exit/exit_now
    """

    def __init__(self):
        self.mexc = MEXCClient()
        self.llm = LLMClient()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_position(self, position_id: int) -> dict:
        """Analyze a single open position by ID."""
        position = get_position_by_id(position_id)
        if not position:
            return {"error": f"Position #{position_id} not found"}
        if position["status"] == "closed":
            return {"error": f"Position #{position_id} is already closed"}
        return self._analyze(position)

    def check_all_open(self) -> list[dict]:
        """Analyze all open positions from the database."""
        positions = get_open_positions()
        if not positions:
            return []
        results = []
        for pos in positions:
            print(f"\n--- Checking position #{pos['id']} {pos['symbol']} {pos['direction'].upper()} ---")
            result = self._analyze(pos)
            result["position_id"] = pos["id"]
            result["symbol"] = pos["symbol"]
            result["direction"] = pos["direction"]
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Core pipeline
    # ------------------------------------------------------------------

    def _analyze(self, position: dict) -> dict:
        symbol = position["symbol"]
        print(f"  Fetching exit snapshot for {symbol}...")
        snapshot = self.mexc.get_exit_snapshot(symbol)
        indicators = self._compute_exit_indicators(snapshot)

        # --- Agent 1: Macro ---
        print(f"  [1] Macro structure (weekly/daily)...")
        macro_prompt = build_macro_prompt(symbol, indicators, position)
        macro_result = self.llm.complete_json(SYSTEM_MACRO, macro_prompt)

        # --- Agent 2: Local structure ---
        print(f"  [2] Local structure (4h)...")
        local_prompt = build_local_prompt(symbol, indicators, position)
        local_result = self.llm.complete_json(SYSTEM_LOCAL, local_prompt)

        # Inject current price into local_result if agent returned it
        if not local_result.get("current_price"):
            ticker = indicators.get("ticker") or {}
            local_result["current_price"] = ticker.get("last_price")

        # --- Agent 3: Momentum & Sentiment ---
        print(f"  [3] Momentum & sentiment (1h + derivatives)...")
        momentum_prompt = build_momentum_prompt(symbol, indicators, position)
        momentum_result = self.llm.complete_json(SYSTEM_MOMENTUM, momentum_prompt)

        # --- Agent 4: Exit Decision ---
        print(f"  [4] Exit decision...")
        exit_prompt = build_exit_prompt(
            position=position,
            macro_analysis=macro_result,
            local_analysis=local_result,
            momentum_analysis=momentum_result,
        )
        exit_result = self.llm.complete_json(SYSTEM_EXIT, exit_prompt)

        # Attach sub-analyses for transparency / debugging
        exit_result["_macro"] = macro_result
        exit_result["_local"] = local_result
        exit_result["_momentum"] = momentum_result
        exit_result["_position"] = position

        return exit_result

    # ------------------------------------------------------------------
    # Indicators for exit analysis
    # ------------------------------------------------------------------

    def _compute_exit_indicators(self, snapshot: dict) -> dict:
        """
        Extends the standard compute_indicators with 4h and 1h specific data.
        Standard compute_indicators handles weekly/daily; we add h4 and h1 layers.
        """
        candles = snapshot.get("candles", {})
        candles_daily = candles.get("daily", [])
        candles_weekly = candles.get("weekly", [])
        candles_h4 = candles.get("h4", [])
        candles_h1 = candles.get("h1", [])

        # Reuse standard indicators for weekly/daily layer
        indicators = compute_indicators(snapshot)

        # Add 4h-specific swing levels, volume profile, ATR
        if candles_h4:
            indicators["swing_levels"]["h4"] = find_swing_highs_lows(candles_h4, lookback=3)
            indicators["volume_profile"]["h4"] = build_volume_profile(candles_h4)
            indicators["volatility_h4"] = compute_atr(candles_h4, period=14)

        # Add 1h-specific swing levels, volume profile
        if candles_h1:
            indicators["swing_levels"]["h1"] = find_swing_highs_lows(candles_h1, lookback=3)
            indicators["volume_profile"]["h1"] = build_volume_profile(candles_h1)
            indicators["volatility_h1"] = compute_atr(candles_h1, period=14)

        return indicators
