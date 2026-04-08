"""
Full Pipeline Backtest
======================
Simulates real intraday trading using both agents in sequence:

  Phase 1 — Entry search
    TradingAgent runs at date T on historical candles.
    Produces a signal: entry1, SL, TP1, TP2, direction.

  Phase 2 — Entry activation
    Walk forward on 1h candles from T.
    Position is "opened" when price touches entry1 (limit order filled).
    If price never touches entry1 within MAX_ENTRY_WAIT_DAYS → "missed entry".

  Phase 3 — Position management
    ExitAgent runs every EXIT_CHECK_INTERVAL_HOURS (default 8h).
    Each check produces: hold / adjust_tp / partial_exit / exit_now.
    Position closes when:
      A) ExitAgent says exit_now
      B) Price hits current TP (original or adjusted)
      C) Price hits SL
      D) MAX_POSITION_DAYS reached

  Phase 4 — Trade accounting
    Final PnL is calculated with leverage.
    Baseline PnL (hold to original TP) is calculated for comparison.
    Every ExitAgent recommendation is scored postfact.

Output: printed to terminal using rich.
"""

import time as _time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

from data.mexc_client import MEXCClient
from agent.analyzer import TradingAgent
from agent.llm import LLMClient
from data.indicators import (
    compute_indicators,
    find_swing_highs_lows,
    build_volume_profile,
    compute_atr,
)
from exit_agent.prompts import (
    SYSTEM_MACRO, build_macro_prompt,
    SYSTEM_LOCAL, build_local_prompt,
    SYSTEM_MOMENTUM, build_momentum_prompt,
    SYSTEM_EXIT, build_exit_prompt,
)

# ---------------------------------------------------------------------------
# Constants — easy to tune
# ---------------------------------------------------------------------------

MAX_ENTRY_WAIT_DAYS = 3          # days to wait for entry1 to be touched
MAX_POSITION_DAYS = 7            # max days to hold a position
EXIT_CHECK_INTERVAL_HOURS = 8    # how often ExitAgent runs (3x per day)
DEFAULT_LEVERAGE = 10
DEFAULT_SIZE_USD = 100           # virtual position size for PnL calculation


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ExitCheck:
    """Single ExitAgent check during position lifetime."""
    timestamp: str
    action: str                    # hold / adjust_tp / partial_exit / exit_now
    confidence: str
    reasoning: str
    suggested_tp1: Optional[float]
    move_sl_to_breakeven: bool
    partial_exit_pct: Optional[float]
    # Postfact scoring (filled after position closes)
    was_correct: Optional[bool] = None
    correctness_reason: str = ""


@dataclass
class TradeResult:
    """Full result of one simulated trade."""
    signal_date: str
    symbol: str
    direction: str
    entry_price: float
    sl_price: float
    original_tp1: float
    original_tp2: Optional[float]

    # Activation
    activated: bool = False
    activation_date: Optional[str] = None
    activation_price: Optional[float] = None
    missed_entry: bool = False

    # Close
    close_reason: str = ""         # tp_hit / sl_hit / exit_agent / partial+tp / timeout
    close_price: Optional[float] = None
    close_date: Optional[str] = None

    # Final TP after any adjustments
    final_tp1: Optional[float] = None

    # PnL
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None

    # Baseline: what would have happened holding to original TP
    baseline_close_reason: str = ""
    baseline_close_price: Optional[float] = None
    baseline_pnl_usd: Optional[float] = None
    baseline_pnl_pct: Optional[float] = None

    # Exit agent history
    exit_checks: list[ExitCheck] = field(default_factory=list)

    # Partial exit tracking
    partial_closed_pct: float = 0.0
    partial_pnl_usd: float = 0.0


# ---------------------------------------------------------------------------
# Full Pipeline Engine
# ---------------------------------------------------------------------------

class FullPipelineBacktest:
    """
    Runs the combined TradingAgent + ExitAgent backtest.

    Usage:
        engine = FullPipelineBacktest()
        results = engine.run("BTCUSDT", "2024-01-01", "2024-03-01", step_days=7)
    """

    def __init__(self, leverage: int = DEFAULT_LEVERAGE, size_usd: float = DEFAULT_SIZE_USD, rag_source: str = None):
        self.mexc = MEXCClient()
        self.trading_agent = TradingAgent(rag_source=rag_source)
        self.llm = LLMClient()
        self.leverage = leverage
        self.size_usd = size_usd

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        symbol: str,
        date_from: str,
        date_to: str,
        step_days: int = 7,
        use_rag: bool = True,
    ) -> list[TradeResult]:
        dt_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dt_to = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        results = []
        current_dt = dt_from

        print(f"\n{'='*60}")
        print(f"  FULL PIPELINE BACKTEST: {symbol}")
        print(f"  {date_from} → {date_to} | step={step_days}d")
        print(f"  Size: ${self.size_usd} × {self.leverage}x | Exit check: every {EXIT_CHECK_INTERVAL_HOURS}h")
        print(f"{'='*60}\n")

        while current_dt <= dt_to:
            date_label = current_dt.strftime("%Y-%m-%d")
            print(f"\n{'─'*50}")
            print(f"  [SIGNAL] {date_label}")
            print(f"{'─'*50}")

            try:
                result = self._run_one_cycle(symbol, current_dt, use_rag)
                results.append(result)
            except Exception as e:
                print(f"  [ERROR] {e}")

            current_dt += timedelta(days=step_days)

        self._print_summary(results, symbol, date_from, date_to)
        return results

    # ------------------------------------------------------------------
    # One signal cycle
    # ------------------------------------------------------------------

    def _run_one_cycle(self, symbol: str, signal_dt: datetime, use_rag: bool) -> TradeResult:
        # --- Phase 1: get signal from TradingAgent ---
        snapshot = self._build_snapshot_at(symbol, signal_dt)
        signal = self.trading_agent.analyze_with_snapshot(
            symbol=symbol,
            snapshot=snapshot,
            date_label=signal_dt.strftime("%Y-%m-%d"),
            use_rag=use_rag,
        )

        if not signal.get("has_setup"):
            bias = signal.get('trend_analysis', {}).get('bias', 'N/A')
            rejection = signal.get('rejection_reason') or signal.get('reasoning', '')
            rr = signal.get('entry_analysis', {}).get('risk_reward', 'N/A')
            watch = signal.get('watch_level')
            print(f"  → No setup found | bias={bias} | RR={rr}")
            print(f"     reason: {rejection[:120] if rejection else '—'}")
            if watch:
                print(f"     watch level: {watch}")
            result = TradeResult(
                signal_date=signal_dt.strftime("%Y-%m-%d"),
                symbol=symbol,
                direction=signal.get("direction") or "none",
                entry_price=0, sl_price=0, original_tp1=0, original_tp2=None,
            )
            result.missed_entry = True
            result.close_reason = "no_setup"
            return result

        entry = signal["entry1"]
        sl = signal["sl"]
        tp1 = signal["tp1"]
        tp2 = signal.get("tp2")
        direction = signal["direction"]

        print(f"  → Signal: {direction.upper()} | Entry={entry} | SL={sl} | TP1={tp1}" + (f" | TP2={tp2}" if tp2 else ""))

        trade = TradeResult(
            signal_date=signal_dt.strftime("%Y-%m-%d"),
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            original_tp1=tp1,
            original_tp2=tp2,
            final_tp1=tp1,
        )

        # --- Phase 2: wait for entry activation ---
        h1_candles = self._get_h1_candles(
            symbol, signal_dt,
            signal_dt + timedelta(days=MAX_ENTRY_WAIT_DAYS)
        )

        activated_candle = self._find_entry_activation(h1_candles, entry, sl, direction)
        if not activated_candle:
            print(f"  → Missed entry: price never touched {entry} within {MAX_ENTRY_WAIT_DAYS} days")
            trade.missed_entry = True
            trade.close_reason = "missed_entry"
            return trade

        trade.activated = True
        trade.activation_date = activated_candle["datetime"]
        trade.activation_price = entry
        print(f"  → Entry activated at {entry} on {activated_candle['datetime'][:16]}")

        # --- Phase 3: position management ---
        self._manage_position(trade, symbol, activated_candle["timestamp"])

        # --- Phase 4: baseline (hold to original TP) ---
        self._compute_baseline(trade, symbol, activated_candle["timestamp"])

        # --- Score exit agent recommendations ---
        self._score_exit_checks(trade)

        return trade

    # ------------------------------------------------------------------
    # Phase 2: Entry activation
    # ------------------------------------------------------------------

    def _find_entry_activation(
        self,
        candles: list[dict],
        entry: float,
        sl: float,
        direction: str,
    ) -> Optional[dict]:
        """
        Walk through 1h candles looking for price to touch entry1.
        Also check if SL is hit before entry (invalid signal).
        """
        for candle in candles:
            h = candle["high"]
            l = candle["low"]

            # Check SL hit before entry — don't open
            if direction == "long" and l <= sl:
                return None
            if direction == "short" and h >= sl:
                return None

            # Entry touched
            if direction == "long" and l <= entry <= h:
                return candle
            if direction == "short" and l <= entry <= h:
                return candle

        return None

    # ------------------------------------------------------------------
    # Phase 3: Position management
    # ------------------------------------------------------------------

    def _manage_position(self, trade: TradeResult, symbol: str, activation_ts: int):
        """
        Walk forward from activation in EXIT_CHECK_INTERVAL_HOURS steps.
        At each step: check if TP/SL hit, then run ExitAgent if not.
        """
        activation_dt = datetime.fromtimestamp(activation_ts, tz=timezone.utc)
        max_close_dt = activation_dt + timedelta(days=MAX_POSITION_DAYS)

        # First check starts after one full interval — give position time to breathe
        current_dt = activation_dt + timedelta(hours=EXIT_CHECK_INTERVAL_HOURS)
        remaining_pct = 1.0  # fraction of position still open

        print(f"  → Managing position | max close: {max_close_dt.strftime('%Y-%m-%d')}")

        # Check if TP/SL was hit during the first interval (before first ExitAgent check)
        first_interval_candles = self._get_h1_candles(symbol, activation_dt, current_dt)
        if first_interval_candles:
            hit = self._check_tp_sl_hit(first_interval_candles, trade.sl_price, trade.final_tp1, trade.direction)
            if hit == "sl":
                trade.close_reason = "sl_hit"
                trade.close_price = trade.sl_price
                trade.close_date = first_interval_candles[0]["datetime"][:16]
                print(f"  → SL hit in first interval at {trade.sl_price}")
                self._compute_baseline(trade, symbol, activation_ts)
                self._score_exit_checks(trade)
                trade.pnl_usd = round(self._calc_pnl(trade.entry_price, trade.sl_price, trade.direction, 1.0), 2)
                exposure = self.size_usd * self.leverage
                trade.pnl_pct = round(trade.pnl_usd / exposure * 100, 2)
                return
            if hit == "tp":
                trade.close_reason = "tp_hit"
                trade.close_price = trade.final_tp1
                trade.close_date = first_interval_candles[0]["datetime"][:16]
                print(f"  → TP hit in first interval at {trade.final_tp1}")
                self._compute_baseline(trade, symbol, activation_ts)
                self._score_exit_checks(trade)
                trade.pnl_usd = round(self._calc_pnl(trade.entry_price, trade.final_tp1, trade.direction, 1.0), 2)
                exposure = self.size_usd * self.leverage
                trade.pnl_pct = round(trade.pnl_usd / exposure * 100, 2)
                return

        while current_dt < max_close_dt:
            next_dt = current_dt + timedelta(hours=EXIT_CHECK_INTERVAL_HOURS)
            if next_dt > max_close_dt:
                next_dt = max_close_dt

            # Fetch 1h candles for this interval
            interval_candles = self._get_h1_candles(symbol, current_dt, next_dt)
            if not interval_candles:
                current_dt = next_dt
                continue

            # Check if TP or SL was hit during this interval
            hit = self._check_tp_sl_hit(
                interval_candles, trade.sl_price, trade.final_tp1, trade.direction
            )

            if hit == "sl":
                trade.close_reason = "sl_hit"
                trade.close_price = trade.sl_price
                trade.close_date = interval_candles[0]["datetime"][:16]
                print(f"  → SL hit at {trade.sl_price} ({trade.close_date})")
                break

            if hit == "tp":
                if remaining_pct < 1.0:
                    trade.close_reason = "partial+tp_hit"
                else:
                    trade.close_reason = "tp_hit"
                trade.close_price = trade.final_tp1
                trade.close_date = interval_candles[0]["datetime"][:16]
                print(f"  → TP hit at {trade.final_tp1} ({trade.close_date})")
                break

            # No hit — run ExitAgent at the end of this interval
            current_price = interval_candles[-1]["close"]
            check = self._run_exit_check(trade, symbol, current_dt, current_price, remaining_pct)
            trade.exit_checks.append(check)

            print(f"    [{current_dt.strftime('%m-%d %H:%M')}] ExitAgent → {check.action.upper()} "
                  f"(confidence: {check.confidence}) | price={current_price}")

            if check.action == "exit_now":
                trade.close_reason = "exit_agent"
                trade.close_price = current_price
                trade.close_date = current_dt.strftime("%Y-%m-%dT%H:%M")
                print(f"  → ExitAgent: EXIT NOW at {current_price}")
                break

            if check.action == "adjust_tp" and check.suggested_tp1:
                trade.final_tp1 = check.suggested_tp1
                print(f"    → TP adjusted to {trade.final_tp1}")

            if check.action == "partial_exit" and check.partial_exit_pct:
                pct_to_close = check.partial_exit_pct / 100
                if remaining_pct > 0:
                    closed_now = min(pct_to_close, remaining_pct)
                    partial_pnl = self._calc_pnl(trade.entry_price, current_price, trade.direction, closed_now)
                    trade.partial_closed_pct += closed_now
                    trade.partial_pnl_usd += partial_pnl
                    remaining_pct -= closed_now
                    print(f"    → Partial exit {closed_now*100:.0f}% at {current_price} | PnL: {partial_pnl:+.2f} USD")
                    if remaining_pct <= 0:
                        trade.close_reason = "partial_exit_full"
                        trade.close_price = current_price
                        trade.close_date = current_dt.strftime("%Y-%m-%dT%H:%M")
                        break

            current_dt = next_dt
        else:
            # Timeout
            if not trade.close_price:
                # Get last known price
                last_candles = self._get_h1_candles(
                    symbol,
                    max_close_dt - timedelta(hours=1),
                    max_close_dt,
                )
                last_price = last_candles[-1]["close"] if last_candles else trade.entry_price
                trade.close_reason = "timeout"
                trade.close_price = last_price
                trade.close_date = max_close_dt.strftime("%Y-%m-%dT%H:%M")
                print(f"  → Timeout: closing at {last_price} after {MAX_POSITION_DAYS} days")

        # Compute final PnL (remaining open fraction)
        if trade.close_price and remaining_pct > 0:
            final_pnl = self._calc_pnl(trade.entry_price, trade.close_price, trade.direction, remaining_pct)
            trade.pnl_usd = round(trade.partial_pnl_usd + final_pnl, 2)
        else:
            trade.pnl_usd = round(trade.partial_pnl_usd, 2)

        exposure = self.size_usd * self.leverage
        trade.pnl_pct = round(trade.pnl_usd / exposure * 100, 2) if exposure else None

    # ------------------------------------------------------------------
    # Phase 4: Baseline (hold to original TP)
    # ------------------------------------------------------------------

    def _compute_baseline(self, trade: TradeResult, symbol: str, activation_ts: int):
        """
        What would have happened if we just held to the original TP without ExitAgent.
        """
        activation_dt = datetime.fromtimestamp(activation_ts, tz=timezone.utc)
        max_dt = activation_dt + timedelta(days=MAX_POSITION_DAYS)

        try:
            candles = self._get_h1_candles(symbol, activation_dt, max_dt)
        except Exception:
            return

        for candle in candles:
            h, l = candle["high"], candle["low"]
            d = trade.direction

            if d == "long":
                if l <= trade.sl_price:
                    trade.baseline_close_reason = "sl_hit"
                    trade.baseline_close_price = trade.sl_price
                    break
                if h >= trade.original_tp1:
                    trade.baseline_close_reason = "tp_hit"
                    trade.baseline_close_price = trade.original_tp1
                    break
            elif d == "short":
                if h >= trade.sl_price:
                    trade.baseline_close_reason = "sl_hit"
                    trade.baseline_close_price = trade.sl_price
                    break
                if l <= trade.original_tp1:
                    trade.baseline_close_reason = "tp_hit"
                    trade.baseline_close_price = trade.original_tp1
                    break
        else:
            trade.baseline_close_reason = "timeout"
            trade.baseline_close_price = candles[-1]["close"] if candles else trade.entry_price

        if trade.baseline_close_price:
            trade.baseline_pnl_usd = round(
                self._calc_pnl(trade.entry_price, trade.baseline_close_price, trade.direction, 1.0), 2
            )
            exposure = self.size_usd * self.leverage
            trade.baseline_pnl_pct = round(trade.baseline_pnl_usd / exposure * 100, 2) if exposure else None

    # ------------------------------------------------------------------
    # Exit Agent (historical mode)
    # ------------------------------------------------------------------

    def _run_exit_check(
        self,
        trade: TradeResult,
        symbol: str,
        check_dt: datetime,
        current_price: float,
        remaining_pct: float,
    ) -> ExitCheck:
        """Build historical snapshot at check_dt and run ExitAgent pipeline."""
        try:
            snapshot = self._build_exit_snapshot_at(symbol, check_dt)
            indicators = self._compute_exit_indicators(snapshot)

            # Build a synthetic position dict for the exit agents
            position = {
                "id": 0,
                "symbol": symbol,
                "direction": trade.direction,
                "entry_price": trade.entry_price,
                "sl_price": trade.sl_price,
                "tp1_price": trade.final_tp1,
                "tp2_price": trade.original_tp2,
                "size_usd": self.size_usd * remaining_pct,
                "leverage": self.leverage,
                "status": "open",
            }

            macro = self.llm.complete_json(SYSTEM_MACRO, build_macro_prompt(symbol, indicators, position))
            local = self.llm.complete_json(SYSTEM_LOCAL, build_local_prompt(symbol, indicators, position))

            if not local.get("current_price"):
                local["current_price"] = current_price

            momentum = self.llm.complete_json(SYSTEM_MOMENTUM, build_momentum_prompt(symbol, indicators, position))
            exit_res = self.llm.complete_json(SYSTEM_EXIT, build_exit_prompt(position, macro, local, momentum))

            return ExitCheck(
                timestamp=check_dt.strftime("%Y-%m-%dT%H:%M"),
                action=exit_res.get("action", "hold"),
                confidence=exit_res.get("confidence", "low"),
                reasoning=exit_res.get("reasoning", ""),
                suggested_tp1=exit_res.get("suggested_tp1"),
                move_sl_to_breakeven=exit_res.get("move_sl_to_breakeven", False),
                partial_exit_pct=exit_res.get("partial_exit_pct"),
            )

        except Exception as e:
            return ExitCheck(
                timestamp=check_dt.strftime("%Y-%m-%dT%H:%M"),
                action="hold",
                confidence="low",
                reasoning=f"[error] {e}",
                suggested_tp1=None,
                move_sl_to_breakeven=False,
                partial_exit_pct=None,
            )

    # ------------------------------------------------------------------
    # Postfact scoring of ExitAgent recommendations
    # ------------------------------------------------------------------

    def _score_exit_checks(self, trade: TradeResult):
        """
        Score each ExitAgent recommendation postfact.

        hold/adjust_tp → correct if baseline hit TP (not SL).
          We use BASELINE here, not the managed outcome, because the managed
          outcome is already influenced by ExitAgent itself.

        exit_now/partial_exit → correct if baseline hit SL or timed out,
          OR if the managed PnL after exit was better than the baseline PnL.
          This covers the case where ExitAgent closed early and price reversed
          against the position (e.g. ExitAgent closed short, price went up → correct).
        """
        if not trade.exit_checks:
            return

        baseline_win = trade.baseline_close_reason == "tp_hit"
        baseline_loss = trade.baseline_close_reason in ("sl_hit", "timeout")
        agent_better = (
            (trade.pnl_usd or 0) > (trade.baseline_pnl_usd or 0)
        )

        for check in trade.exit_checks:
            if check.action in ("hold", "adjust_tp"):
                check.was_correct = baseline_win
                check.correctness_reason = "held → TP hit" if baseline_win else "held → SL/timeout"
            elif check.action in ("exit_now", "partial_exit"):
                check.was_correct = baseline_loss or agent_better
                check.correctness_reason = (
                    "exited before SL/reversal (saved PnL)" if check.was_correct
                    else "exited before TP (left money on table)"
                )

    # ------------------------------------------------------------------
    # PnL calculation
    # ------------------------------------------------------------------

    def _calc_pnl(self, entry: float, close: float, direction: str, fraction: float) -> float:
        exposure = self.size_usd * self.leverage * fraction
        move = (close - entry) / entry
        if direction == "short":
            move = -move
        return exposure * move

    def _check_tp_sl_hit(
        self,
        candles: list[dict],
        sl: float,
        tp: float,
        direction: str,
    ) -> Optional[str]:
        """Check if SL or TP was touched in a list of candles. Returns 'sl', 'tp', or None."""
        for candle in candles:
            h, l = candle["high"], candle["low"]
            if direction == "long":
                if l <= sl:
                    return "sl"
                if h >= tp:
                    return "tp"
            elif direction == "short":
                if h >= sl:
                    return "sl"
                if l <= tp:
                    return "tp"
        return None

    # ------------------------------------------------------------------
    # Data fetching helpers
    # ------------------------------------------------------------------

    def _get_h1_candles(self, symbol: str, dt_from: datetime, dt_to: datetime) -> list[dict]:
        candles = self.mexc.get_historical_candles(symbol, "Min60", dt_from, dt_to)
        _time.sleep(0.1)
        return candles

    def _build_snapshot_at(self, symbol: str, dt: datetime) -> dict:
        """Historical snapshot for TradingAgent (weekly/daily/4h)."""
        lookbacks = {
            "weekly": (timedelta(weeks=52), "Week1"),
            "daily": (timedelta(days=100), "Day1"),
            "h4": (timedelta(hours=400), "Hour4"),
        }
        snapshot = {
            "symbol": symbol,
            "timestamp": int(dt.timestamp()),
            "candles": {},
            "open_interest": None,
            "funding_rate": None,
            "long_short_ratio": None,
            "ticker": None,
        }
        for name, (lookback, interval) in lookbacks.items():
            try:
                snapshot["candles"][name] = self.mexc.get_historical_candles(
                    symbol, interval, date_from=dt - lookback, date_to=dt
                )
                _time.sleep(0.15)
            except Exception as e:
                snapshot["candles"][name] = []
                print(f"    [warn] candles {name}: {e}")

        daily = snapshot["candles"].get("daily", [])
        if daily:
            last = daily[-1]
            snapshot["ticker"] = {
                "symbol": symbol,
                "last_price": last["close"],
                "price_change_pct": round((last["close"] - last["open"]) / last["open"] * 100, 2),
                "volume_24h": last["volume"],
                "high_24h": last["high"],
                "low_24h": last["low"],
            }
        return snapshot

    def _build_exit_snapshot_at(self, symbol: str, dt: datetime) -> dict:
        """Historical snapshot for ExitAgent (weekly/daily/4h/1h)."""
        lookbacks = {
            "weekly": (timedelta(weeks=12), "Week1"),
            "daily":  (timedelta(days=60),  "Day1"),
            "h4":     (timedelta(hours=200), "Hour4"),
            "h1":     (timedelta(hours=96),  "Min60"),
        }
        snapshot = {
            "symbol": symbol,
            "timestamp": int(dt.timestamp()),
            "candles": {},
            "open_interest": None,
            "funding_rate": None,
            "long_short_ratio": None,
            "ticker": None,
        }
        for name, (lookback, interval) in lookbacks.items():
            try:
                snapshot["candles"][name] = self.mexc.get_historical_candles(
                    symbol, interval, date_from=dt - lookback, date_to=dt
                )
                _time.sleep(0.1)
            except Exception as e:
                snapshot["candles"][name] = []

        h1 = snapshot["candles"].get("h1", [])
        if h1:
            last = h1[-1]
            snapshot["ticker"] = {
                "symbol": symbol,
                "last_price": last["close"],
                "price_change_pct": round((last["close"] - last["open"]) / last["open"] * 100, 2),
                "volume_24h": last["volume"],
                "high_24h": last["high"],
                "low_24h": last["low"],
            }
        return snapshot

    def _compute_exit_indicators(self, snapshot: dict) -> dict:
        candles = snapshot.get("candles", {})
        indicators = compute_indicators(snapshot)
        for tf, lookback in [("h4", 3), ("h1", 3)]:
            c = candles.get(tf, [])
            if c:
                indicators["swing_levels"][tf] = find_swing_highs_lows(c, lookback=lookback)
                indicators["volume_profile"][tf] = build_volume_profile(c)
                indicators[f"volatility_{tf}"] = compute_atr(c, period=14)
        return indicators

    # ------------------------------------------------------------------
    # Terminal output
    # ------------------------------------------------------------------

    def _print_summary(self, results: list[TradeResult], symbol: str, date_from: str, date_to: str):
        try:
            from rich.console import Console
            from rich.table import Table
            from rich.panel import Panel
            from rich import box
            console = Console()
        except ImportError:
            self._print_summary_plain(results, symbol, date_from, date_to)
            return

        # Filter trades that had an actual entry
        active = [r for r in results if r.activated]
        no_setup = [r for r in results if r.close_reason == "no_setup"]
        missed = [r for r in results if r.missed_entry and r.close_reason != "no_setup"]

        if not active:
            console.print(Panel(
                f"[yellow]No activated trades in {date_from} → {date_to}[/yellow]\n"
                f"No setups: {len(no_setup)} | Missed entries: {len(missed)}",
                title=f"Full Pipeline Backtest — {symbol}",
                border_style="yellow",
            ))
            return

        # Metrics
        wins_agent = sum(1 for r in active if r.pnl_usd and r.pnl_usd > 0)
        losses_agent = sum(1 for r in active if r.pnl_usd and r.pnl_usd <= 0)
        wins_base = sum(1 for r in active if r.baseline_pnl_usd and r.baseline_pnl_usd > 0)
        losses_base = sum(1 for r in active if r.baseline_pnl_usd and r.baseline_pnl_usd <= 0)

        total_pnl_agent = sum(r.pnl_usd for r in active if r.pnl_usd is not None)
        total_pnl_base = sum(r.baseline_pnl_usd for r in active if r.baseline_pnl_usd is not None)

        wr_agent = round(wins_agent / len(active) * 100, 1) if active else 0
        wr_base = round(wins_base / len(active) * 100, 1) if active else 0

        all_checks = [c for r in active for c in r.exit_checks]
        correct_checks = [c for c in all_checks if c.was_correct]
        check_accuracy = round(len(correct_checks) / len(all_checks) * 100, 1) if all_checks else 0

        # Summary panel
        delta_pnl = total_pnl_agent - total_pnl_base
        delta_color = "green" if delta_pnl >= 0 else "red"
        console.print(Panel(
            f"Symbol: [bold]{symbol}[/bold]  |  {date_from} → {date_to}\n\n"
            f"Total signals:        {len(results)}\n"
            f"No setup:             {len(no_setup)}\n"
            f"Missed entry:         {len(missed)}\n"
            f"Activated trades:     [bold]{len(active)}[/bold]\n\n"
            f"[bold]━━━ WITH EXIT AGENT ━━━━━━━━━━━━━━━[/bold]\n"
            f"Win rate:             [bold {'green' if wr_agent >= 50 else 'red'}]{wr_agent}%[/bold {'green' if wr_agent >= 50 else 'red'}]\n"
            f"Wins / Losses:        {wins_agent} / {losses_agent}\n"
            f"Total PnL:            [bold]{'+' if total_pnl_agent >= 0 else ''}{total_pnl_agent:.2f} USD[/bold]\n\n"
            f"[bold]━━━ BASELINE (hold to TP) ━━━━━━━━━[/bold]\n"
            f"Win rate:             {wr_base}%\n"
            f"Wins / Losses:        {wins_base} / {losses_base}\n"
            f"Total PnL:            {'+' if total_pnl_base >= 0 else ''}{total_pnl_base:.2f} USD\n\n"
            f"[bold]━━━ EXIT AGENT QUALITY ━━━━━━━━━━━━[/bold]\n"
            f"Total checks:         {len(all_checks)}\n"
            f"Correct recommendations: [bold]{len(correct_checks)} ({check_accuracy}%)[/bold]\n"
            f"PnL delta (agent vs baseline): [{delta_color}]{'+' if delta_pnl >= 0 else ''}{delta_pnl:.2f} USD[/{delta_color}]",
            title="[bold]Full Pipeline Backtest Results[/bold]",
            border_style="blue",
        ))

        # Per-trade table
        table = Table(title="Trade Detail", box=box.SIMPLE)
        table.add_column("Date", style="dim")
        table.add_column("Dir")
        table.add_column("Entry")
        table.add_column("Close")
        table.add_column("Close reason")
        table.add_column("PnL (agent)", justify="right")
        table.add_column("PnL (base)", justify="right")
        table.add_column("Checks")
        table.add_column("Correct")

        for r in active:
            dir_color = "green" if r.direction == "long" else "red"
            pnl_color = "green" if (r.pnl_usd or 0) >= 0 else "red"
            base_color = "green" if (r.baseline_pnl_usd or 0) >= 0 else "red"
            checks_total = len(r.exit_checks)
            checks_correct = sum(1 for c in r.exit_checks if c.was_correct)
            table.add_row(
                r.signal_date,
                f"[{dir_color}]{r.direction.upper()}[/{dir_color}]",
                str(r.entry_price),
                str(r.close_price or "—"),
                r.close_reason,
                f"[{pnl_color}]{'+' if (r.pnl_usd or 0) >= 0 else ''}{r.pnl_usd:.2f}[/{pnl_color}]" if r.pnl_usd is not None else "—",
                f"[{base_color}]{'+' if (r.baseline_pnl_usd or 0) >= 0 else ''}{r.baseline_pnl_usd:.2f}[/{base_color}]" if r.baseline_pnl_usd is not None else "—",
                str(checks_total),
                f"{checks_correct}/{checks_total}",
            )
        console.print(table)

        # Exit agent recommendations breakdown
        if all_checks:
            action_counts: dict[str, int] = {}
            for c in all_checks:
                action_counts[c.action] = action_counts.get(c.action, 0) + 1

            console.print("\n[bold]Exit Agent recommendations breakdown:[/bold]")
            for action, count in sorted(action_counts.items()):
                correct = sum(1 for c in all_checks if c.action == action and c.was_correct)
                acc = round(correct / count * 100, 1)
                console.print(f"  {action:15s} → {count:3d} times | accuracy: {acc}%")

    def _print_summary_plain(self, results, symbol, date_from, date_to):
        active = [r for r in results if r.activated]
        print(f"\n{'='*60}")
        print(f"  RESULTS: {symbol} | {date_from} → {date_to}")
        print(f"  Activated trades: {len(active)}")
        for r in active:
            print(f"  {r.signal_date} {r.direction.upper()} | close: {r.close_reason} "
                  f"| PnL: {r.pnl_usd:+.2f} USD | base: {r.baseline_pnl_usd:+.2f} USD")
        print(f"{'='*60}\n")
