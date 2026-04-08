"""
IntradayAgent — 5-stage intraday trading pipeline.
Isolated from TradingAgent. Uses M15/H1/H4 timeframes.

Pipeline:
  1. SessionAgent   — H4 trend + session context + VWAP bias
  2. StructureAgent — H1 key levels, POC, zones of interest
  3. FlowAgent      — Orderbook + CVD + OI confirmation/rejection
  4. EntryAgent     — M15 precise entry, SL, TP with RR >= 1.5
  5. RiskAgent      — Final validation and go/no-go
"""

from data.mexc_client import MEXCClient
from agent.llm import LLMClient
from intraday_agent.indicators import compute_intraday_indicators
from intraday_agent.prompts import (
    SYSTEM_SESSION, build_session_prompt,
    SYSTEM_STRUCTURE, build_structure_prompt,
    SYSTEM_FLOW, build_flow_prompt,
    SYSTEM_ENTRY, build_entry_prompt,
    SYSTEM_RISK, build_risk_prompt,
    add_backtest_context,
)


class IntradayAgent:
    """
    Multi-agent intraday trading pipeline.
    Analyses M15/H1/H4 structure with orderbook, CVD, session context.
    """

    def __init__(self):
        self.mexc = MEXCClient()
        self.llm = LLMClient()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, symbol: str, debug: bool = False) -> dict:
        """Live analysis — fetches fresh intraday snapshot."""
        print(f"  Fetching intraday data for {symbol}...")
        snapshot = self.mexc.get_intraday_snapshot(symbol)
        indicators = compute_intraday_indicators(snapshot)
        return self._run_pipeline(symbol=symbol, indicators=indicators, debug=debug)

    def analyze_with_snapshot(
        self,
        symbol: str,
        snapshot: dict,
        date_label: str = None,
        debug: bool = False,
    ) -> dict:
        """Backtest-mode analysis — uses pre-built historical snapshot."""
        indicators = compute_intraday_indicators(snapshot)
        return self._run_pipeline(
            symbol=symbol,
            indicators=indicators,
            date_label=date_label,
            debug=debug,
        )

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    @staticmethod
    def debug_indicators(indicators: dict):
        """Print a compact data-quality summary before the pipeline runs."""
        candles = (indicators.get("swings") or {})  # swings are computed from m15/h1
        price   = indicators.get("current_price", 0)
        atr     = indicators.get("atr") or {}
        rvol    = indicators.get("rvol") or {}
        vwap    = indicators.get("vwap") or {}
        vp      = indicators.get("volume_profile") or {}
        sess    = indicators.get("sessions") or {}
        ob      = indicators.get("orderbook") or {}
        trades  = indicators.get("trades") or {}
        swings  = indicators.get("swings") or {}

        ok   = "✓"
        warn = "!"
        miss = "✗"

        def chk(val, label):
            if val is None or val == 0 or val == "unknown":
                return f"  {miss} {label}: MISSING"
            return f"  {ok} {label}: {val}"

        lines = [
            f"\n  ── DATA CHECK ──────────────────────────",
            chk(price, "current_price"),
            chk(atr.get("atr_pct"), f"ATR%  ({atr.get('regime', '?')} regime, SL buffer {atr.get('sl_buffer_pct')}%)"),
            chk(rvol.get("rvol"), f"RVOL  ({rvol.get('rvol_label', '?')})"),
            chk(vwap.get("vwap"), f"VWAP  (price {vwap.get('price_vs_vwap_pct')}% {vwap.get('bias', '?')})"),
            chk(vp.get("poc"),    f"VOL PROFILE  POC={vp.get('poc')} VAH={vp.get('vah')} VAL={vp.get('val')}"),
            chk(sess.get("asia_high"), f"SESSION LEVELS  asia={sess.get('asia_low')}-{sess.get('asia_high')}"),
            f"  {ok if swings.get('swing_highs') else miss} SWINGS  highs={len(swings.get('swing_highs',[]))}  lows={len(swings.get('swing_lows',[]))}",
            f"  {ok if ob.get('imbalance') is not None else warn} ORDERBOOK  {ob.get('pressure', 'unavailable')}",
            f"  {ok if trades.get('trade_count') else warn} TRADES/CVD  {trades.get('aggression', 'unavailable')}",
            f"  ────────────────────────────────────────",
        ]
        print("\n".join(lines))

    def _run_pipeline(
        self,
        symbol: str,
        indicators: dict,
        date_label: str = None,
        debug: bool = False,
    ) -> dict:

        def inject_date(prompt: str) -> str:
            if date_label:
                from datetime import datetime
                try:
                    dt = datetime.fromisoformat(date_label)
                    return add_backtest_context(prompt, dt)
                except Exception:
                    pass
            return prompt

        if debug:
            self.debug_indicators(indicators)

        # --- Stage 1: Session Context ---
        print(f"  [1] Session context...")
        session_prompt = build_session_prompt(indicators)
        session_result = self.llm.complete_json(SYSTEM_SESSION, inject_date(session_prompt))

        # --- Stage 2: Structure ---
        print(f"  [2] H1 structure...")
        structure_prompt = build_structure_prompt(indicators, session_result)
        structure_result = self.llm.complete_json(SYSTEM_STRUCTURE, inject_date(structure_prompt))

        # --- Stage 3: Flow ---
        print(f"  [3] Order flow...")
        flow_prompt = build_flow_prompt(indicators, session_result)
        flow_result = self.llm.complete_json(SYSTEM_FLOW, inject_date(flow_prompt))

        # Note: we don't bail out early on flow — EntryAgent and RiskAgent make the final call.

        # --- Stage 4: Entry ---
        print(f"  [4] Entry search...")
        entry_prompt = build_entry_prompt(indicators, session_result, structure_result, flow_result)
        entry_result = self.llm.complete_json(SYSTEM_ENTRY, inject_date(entry_prompt))

        # If has_setup but RR < 1.5, retry once with explicit minimum TP floor
        if entry_result.get("has_setup"):
            _e = entry_result
            _entry = _e.get("entry1") or 0
            _sl = _e.get("sl") or 0
            _tp1 = _e.get("tp1") or 0
            _risk = abs(_entry - _sl)
            _reward = abs(_tp1 - _entry)
            _rr_real = round(_reward / _risk, 2) if _risk else 0

            if _rr_real < 1.5 and _risk > 0:
                _direction = _e.get("direction")
                _min_tp = round(_entry + 1.5 * _risk, 2) if _direction == "long" else round(_entry - 1.5 * _risk, 2)
                print(f"  [entry] RR {_rr_real} < 1.5, retrying with min_tp={_min_tp}")
                retry_prompt = build_entry_prompt(
                    indicators, session_result, structure_result, flow_result,
                    tp_floor_hint=f"The previous attempt placed TP1 at {_tp1} giving RR={_rr_real} which is below 1.5. "
                                  f"Entry={_entry}, SL={_sl}. For RR >= 1.5, TP1 must be at least {_min_tp} "
                                  f"(for {_direction}). Use the next level beyond {_min_tp} or output has_setup=false.",
                )
                entry_result = self.llm.complete_json(SYSTEM_ENTRY, inject_date(retry_prompt))
                _e = entry_result
                _entry2 = _e.get("entry1") or _entry
                _sl2 = _e.get("sl") or _sl
                _tp2v = _e.get("tp1") or 0
                _risk2 = abs(_entry2 - _sl2)
                _reward2 = abs(_tp2v - _entry2)
                _rr_real = round(_reward2 / _risk2, 2) if _risk2 else 0

        if entry_result.get("has_setup"):
            _e = entry_result
            _risk = abs((_e.get("entry1") or 0) - (_e.get("sl") or 0))
            _reward = abs((_e.get("tp1") or 0) - (_e.get("entry1") or 0))
            _rr_real = round(_reward / _risk, 2) if _risk else 0
            print(f"  [entry] {_e.get('direction')} entry={_e.get('entry1')} "
                  f"sl={_e.get('sl')} tp1={_e.get('tp1')} "
                  f"llm_rr={_e.get('risk_reward')} real_rr={_rr_real}")

        if not entry_result.get("has_setup"):
            return self._no_setup(
                symbol, session_result,
                entry_result.get("no_setup_reason", "No valid M15 entry found."),
                entry_result=entry_result,
                structure_result=structure_result,
                flow_result=flow_result,
                watch_level=entry_result.get("watch_level"),
                watch_condition=entry_result.get("watch_condition"),
            )

        # --- Stage 5: Risk ---
        print(f"  [5] Risk validation...")
        risk_prompt = build_risk_prompt(indicators, session_result, structure_result, flow_result, entry_result)
        risk_result = self.llm.complete_json(SYSTEM_RISK, inject_date(risk_prompt))

        # Hard validate: recalculate RR from actual prices, reject if wrong
        risk_result = self._validate_signal(risk_result)

        # Enrich output
        risk_result["symbol"] = symbol
        risk_result["indicators"] = indicators
        risk_result["session_analysis"] = session_result
        risk_result["structure_analysis"] = structure_result
        risk_result["flow_analysis"] = flow_result
        risk_result["entry_analysis"] = entry_result

        return risk_result

    # ------------------------------------------------------------------
    # No-setup response
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_signal(signal: dict) -> dict:
        """
        Hard-validate SL/TP direction and recalculate RR from actual prices.
        Rejects signal if SL is on the wrong side or RR < 1.5.
        """
        if not signal.get("has_setup"):
            return signal

        direction = signal.get("direction")
        entry = signal.get("entry1")
        sl = signal.get("sl")
        tp1 = signal.get("tp1")

        if not all([direction, entry, sl, tp1]):
            return signal

        # Check SL/TP are on correct sides
        if direction == "long":
            sl_ok = sl < entry
            tp_ok = tp1 > entry
        else:  # short
            sl_ok = sl > entry
            tp_ok = tp1 < entry

        if not sl_ok or not tp_ok:
            print(f"  [validate] ✗ rejected — SL/TP wrong side for {direction}: "
                  f"entry={entry} sl={sl} tp1={tp1}")
            signal["has_setup"] = False
            signal["rejection_reason"] = (
                f"SL or TP on wrong side for {direction}: entry={entry} sl={sl} tp1={tp1}"
            )
            return signal

        # Recalculate RR
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        if risk == 0:
            signal["has_setup"] = False
            signal["rejection_reason"] = "Risk is zero (entry == sl)"
            return signal

        actual_rr = round(reward / risk, 2)
        llm_rr = signal.get("risk_reward")
        if llm_rr and abs(actual_rr - float(llm_rr)) > 0.05:
            print(f"  [validate] RR corrected: {llm_rr} → {actual_rr}  "
                  f"(entry={entry} sl={sl} tp1={tp1} "
                  f"risk={round(risk,1)} reward={round(reward,1)})")
        signal["risk_reward"] = actual_rr

        if actual_rr < 1.5:
            print(f"  [validate] ✗ rejected — RR {actual_rr} < 1.5")
            signal["has_setup"] = False
            signal["rejection_reason"] = f"Actual RR {actual_rr} < 1.5 minimum"
            return signal

        return signal

    def _no_setup(
        self,
        symbol: str,
        session_result: dict,
        reason: str,
        entry_result: dict = None,
        structure_result: dict = None,
        flow_result: dict = None,
        watch_level: float = None,
        watch_condition: str = None,
    ) -> dict:
        return {
            "symbol": symbol,
            "has_setup": False,
            "direction": None,
            "confidence": None,
            "entry1": None,
            "entry2": None,
            "sl": None,
            "tp1": None,
            "tp2": None,
            "risk_reward": None,
            "reasoning": reason,
            "risks": "",
            "rejection_reason": reason,
            "watch_level": watch_level,
            "watch_condition": watch_condition,
            "session_analysis": session_result,
            "structure_analysis": structure_result,
            "flow_analysis": flow_result,
            "entry_analysis": entry_result,
            "indicators": None,
        }
