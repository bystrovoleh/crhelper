from data.mexc_client import MEXCClient
from data.indicators import compute_indicators
from agent.llm import LLMClient
from agent.rag import retrieve_similar_examples, format_examples_for_prompt
from agent.prompts import (
    SYSTEM_TREND, build_trend_prompt,
    SYSTEM_VOLATILITY, build_volatility_prompt,
    SYSTEM_PATTERN, build_pattern_prompt,
    SYSTEM_ENTRY, build_entry_prompt,
    SYSTEM_RISK, build_risk_prompt,
    add_backtest_context,
)


class TradingAgent:
    """
    Multi-agent trading pipeline:
      1. TrendAgent  — higher timeframe structure & bias
      2. EntryAgent  — precise entry, SL, TP with RR >= 1.5
      3. RiskAgent   — sentiment validation + RAG + final decision
    """

    def __init__(self, rag_source: str = None):
        from config.settings import RAG_SOURCE
        self.mexc = MEXCClient()
        self.llm = LLMClient()
        self.rag_source = rag_source if rag_source is not None else RAG_SOURCE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        symbol: str,
        liquidity_levels: list[float] = None,
        use_rag: bool = True,
    ) -> dict:
        print(f"  Fetching market data for {symbol}...")
        snapshot = self.mexc.get_market_snapshot(symbol)
        indicators = compute_indicators(snapshot)
        return self._run_pipeline(
            symbol=symbol,
            indicators=indicators,
            liquidity_levels=liquidity_levels,
            use_rag=use_rag,
        )

    def analyze_with_snapshot(
        self,
        symbol: str,
        snapshot: dict,
        liquidity_levels: list[float] = None,
        date_label: str = None,
        use_rag: bool = True,
    ) -> dict:
        indicators = compute_indicators(snapshot)
        return self._run_pipeline(
            symbol=symbol,
            indicators=indicators,
            liquidity_levels=liquidity_levels,
            use_rag=use_rag,
            date_label=date_label,
        )

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def _run_pipeline(
        self,
        symbol: str,
        indicators: dict,
        liquidity_levels: list[float] = None,
        use_rag: bool = True,
        date_label: str = None,
    ) -> dict:

        # --- Agent 1: Trend ---
        print(f"  [1] Trend analysis...")
        trend_prompt = build_trend_prompt(symbol, indicators)
        if date_label:
            trend_prompt = add_backtest_context(trend_prompt, symbol, date_label)
        trend_result = self.llm.complete_json(SYSTEM_TREND, trend_prompt)

        # --- Agent 2: Volatility ---
        print(f"  [2] Volatility regime...")
        volatility_prompt = build_volatility_prompt(symbol, indicators)
        if date_label:
            volatility_prompt = add_backtest_context(volatility_prompt, symbol, date_label)
        volatility_result = self.llm.complete_json(SYSTEM_VOLATILITY, volatility_prompt)

        # --- RAG: find similar examples ---
        if use_rag:
            similar = retrieve_similar_examples(indicators, asset=symbol, current_bias=trend_result.get("bias"), rag_source=self.rag_source)
            examples_text = format_examples_for_prompt(similar)
        else:
            similar = []
            examples_text = "RAG disabled."

        # --- Pattern Agent: extract patterns from examples ---
        pattern_result = None
        if use_rag and similar:
            print(f"  [3] Pattern analysis ({len(similar)} examples)...")
            pattern_prompt = build_pattern_prompt(examples_text, trend_result)
            if date_label:
                pattern_prompt = add_backtest_context(pattern_prompt, symbol, date_label)
            pattern_result = self.llm.complete_json(SYSTEM_PATTERN, pattern_prompt)

        # --- Agent 4: Entry ---
        step = "4" if (use_rag and similar) else "3"
        print(f"  [{step}] Entry search...")
        entry_prompt = build_entry_prompt(symbol, indicators, trend_result, examples_text, pattern_result, volatility_result)
        if date_label:
            entry_prompt = add_backtest_context(entry_prompt, symbol, date_label)
        entry_result = self.llm.complete_json(SYSTEM_ENTRY, entry_prompt)

        # If entry agent found no valid entry — skip risk agent
        if not entry_result.get("has_entry"):
            return self._no_setup(symbol, trend_result, entry_result, use_rag)

        # --- Agent 5: Risk ---
        step = "5" if (use_rag and similar) else "4"
        print(f"  [{step}] Risk validation...")
        risk_prompt = build_risk_prompt(
            symbol=symbol,
            indicators=indicators,
            trend_analysis=trend_result,
            entry_analysis=entry_result,
            liquidity_levels=liquidity_levels,
        )
        if date_label:
            risk_prompt = add_backtest_context(risk_prompt, symbol, date_label)
        risk_result = self.llm.complete_json(SYSTEM_RISK, risk_prompt)

        risk_result["indicators"] = indicators
        risk_result["similar_examples_count"] = len(similar)
        risk_result["rag_enabled"] = use_rag
        risk_result["trend_analysis"] = trend_result
        risk_result["volatility_analysis"] = volatility_result
        risk_result["entry_analysis"] = entry_result
        return risk_result

    def _no_setup(
        self,
        symbol: str,
        trend_result: dict,
        entry_result: dict,
        use_rag: bool,
    ) -> dict:
        return {
            "symbol": symbol,
            "has_setup": False,
            "direction": None,
            "confidence": "low",
            "entry1": None,
            "entry2": None,
            "sl": None,
            "tp1": None,
            "tp2": None,
            "risk_reward": None,
            "reasoning": entry_result.get(
                "entry_reasoning",
                f"No valid entry found. Bias: {trend_result.get('bias', 'N/A')}",
            ),
            "key_levels_used": [],
            "risks": "",
            "rag_enabled": use_rag,
            "similar_examples_count": 0,
            "trend_analysis": trend_result,
            "entry_analysis": entry_result,
        }
